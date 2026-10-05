import unittest
from unittest.mock import Mock
from portfolio import PortfolioError, dashboard, holding_rows, position_row, position_rows, records, broker_time, order_rows, IndexFeed


def raw(**changes):
    r = dict(trdSym='SAMPLE', prod='NRML', exSeg='nse_fo', tok='42',
             cfBuyQty='0', flBuyQty='100', cfSellQty='0', flSellQty='0',
             buyAmt='1000', sellAmt='0', multiplier='1', genNum='1', genDen='1',
             prcNum='1', prcDen='1', ltp='12')
    r.update(changes)
    return r


class PortfolioTests(unittest.TestCase):
    def test_index_feed_snapshot_is_unavailable_until_broker_update(self):
        result = IndexFeed().snapshot()
        self.assertEqual([r['name'] for r in result['indices']], ['NIFTY 50', 'SENSEX'])
        self.assertTrue(all('error' in r for r in result['indices']))

    def test_index_feed_snapshot_exposes_received_values(self):
        feed = IndexFeed()
        feed._latest[('nse_cm', '26000')] = {'value': 22500.25, 'change': -50.5, 'percent': -.22,
                                                    'broker_updated': '2026-10-04T15:30:00+05:30', 'received_at': '2026-10-04T15:30:01+05:30'}
        result = feed.snapshot()
        self.assertEqual(result['indices'][0]['value'], 22500.25)
        self.assertIn('error', result['indices'][1])

    def test_order_dates_sort_and_keep_rejected_orders_distinct(self):
        response = {'data': [
            {'ordEntTm': '01-Oct-2026 12:20:03', 'trnsTp': 'B', 'fldQty': 65, 'qty': 65, 'avgPrc': '171.20', 'ordSt': 'complete'},
            {'ordEntTm': '01-Oct-2026 13:04:34', 'trnsTp': 'S', 'fldQty': 0, 'qty': 65, 'avgPrc': '0', 'ordSt': 'rejected'},
            {'ordEntTm': '30-Sep-2026 15:00:00', 'ordSt': 'complete'},
        ]}
        result = order_rows(response)
        self.assertEqual(result[0]['Order time (IST)'], '2026-10-01T13:04:34+05:30')
        self.assertEqual(result[0]['Filled units'], 0)
        self.assertEqual(result[0]['Status'], 'rejected')
        self.assertEqual(result[1]['Side'], 'BUY')
        self.assertEqual(result[2]['Order time (IST)'][:10], '2026-09-30')

    def test_unknown_dates_not_replaced_by_fetch_time(self):
        self.assertIsNone(broker_time('NA'))
        self.assertIsNone(order_rows({'data': [{'ordEntTm': 'bad'}]})[0]['Order time (IST)'])
        self.assertEqual(broker_time('2026-09-30T20:00:00Z'), '2026-10-01T01:30:00+05:30')
        self.assertEqual(broker_time('2026/10/01 14:00:00'), '2026-10-01T14:00:00+05:30')

    def test_closed_position_matches_screenshot_and_is_labeled(self):
        r = position_row(raw(flBuyQty='65', flSellQty='65', buyAmt='10777.00', sellAmt='10455.25', hsUpTm='2026/10/01 14:23:26'), None, [])
        self.assertEqual(r['P&L'], -321.75)
        self.assertEqual(r['State'], 'Closed')
        self.assertIsNone(r['Average / reference'])
        self.assertEqual(r['Broker updated (IST)'], '2026-10-01T14:23:26+05:30')

    def test_dashboard_fetch_timestamp_and_independent_orders(self):
        c = Mock()
        c.holdings.return_value = {'Error': 'secret'}
        c.positions.return_value = {'data': []}
        c.order_report.return_value = {'data': []}
        r = dashboard(c)
        self.assertTrue(r['fetched_at'].endswith('+05:30'))
        self.assertEqual(r['orders']['rows'], [])
        c.order_report.assert_called_once_with()

    def test_long_short_and_flat(self):
        long = position_row(raw(), None, [])
        self.assertEqual((long['Quantity (units)'], long['Average / reference'], long['P&L']), (100, 10, 200))
        short = position_row(raw(flBuyQty='0', flSellQty='100', buyAmt='0', sellAmt='1000'), None, [])
        self.assertEqual((short['Quantity (units)'], short['Average / reference'], short['P&L']), (-100, 10, -200))
        flat = position_row(raw(flSellQty='100', sellAmt='1100', ltp=None), None, [])
        self.assertEqual(flat['P&L'], 100)

    def test_carry_equity_requires_actual_cost_and_exchange_match(self):
        r = raw(exSeg='nse_cm', cfBuyQty='100', flBuyQty='0', buyAmt='0', cfBuyAmt='1200')
        self.assertIsNone(position_row(r, None, [])['P&L'])
        h = dict(exchangeIdentifier='42', exchangeSegment='nse_cm', averagePrice='8')
        self.assertEqual(position_row(r, None, [h])['P&L'], 400)
        h['exchangeSegment'] = 'bse_cm'
        self.assertIsNone(position_row(r, None, [h])['P&L'])

    def test_carried_derivative_labels_reference_basis(self):
        result = position_row(raw(cfSellQty='100', flBuyQty='0', buyAmt='0', cfBuyAmt='0', cfSellAmt='1300'), None, [])
        self.assertEqual(result['P&L'], 100)
        self.assertIn('not lifetime', result['Basis / availability'])

    def test_missing_amounts_and_zero_denominator_not_invented(self):
        self.assertIsNone(position_row(raw(buyAmt=None), None, [])['P&L'])
        self.assertIsNone(position_row(raw(genDen='0'), None, [])['P&L'])
        self.assertIsNone(position_row(raw(flBuyQty=None), None, [])['Quantity (units)'])

    def test_quote_join_by_exchange_and_token_not_order(self):
        client = Mock()
        client.quotes.return_value = [dict(exchange='nse_fo', exchange_token='43', ltp='50'), dict(exchange='nse_fo', exchange_token='42', ltp='11')]
        result = position_rows(client, {'data': [raw(ltp=None), raw(tok='43', ltp=None)]}, [])
        self.assertEqual([r['LTP'] for r in result], [11, 50])
        self.assertEqual(result[0]['P&L'], 100)

    def test_quote_failure_preserves_quantity_without_fabricating_pnl(self):
        client = Mock()
        client.quotes.side_effect = RuntimeError('secret')
        row = position_rows(client, {'data': [raw(ltp=None)]}, [])[0]
        self.assertEqual(row['Quantity (units)'], 100)
        self.assertIsNone(row['P&L'])
        self.assertNotIn('secret', str(row))

    def test_holdings_envelopes_and_zero_values(self):
        h = dict(displaySymbol='ABC', quantity=0, averagePrice=0, token='secret')
        for response in ([h], {'data': [h]}, {'data': {'holdings': [h]}}):
            self.assertEqual(holding_rows(response)[0]['Quantity'], 0)
            self.assertNotIn('secret', str(holding_rows(response)))

    def test_broker_failure_not_empty_and_sensitive_details_not_exposed(self):
        for response in ({'Error': 'secret', 'StatusCode': 503}, {'data': None}, {'stat': 'Not_Ok', 'emsg': 'secret'}):
            with self.assertRaises(PortfolioError) as caught:
                records(response)
            self.assertNotIn('secret', str(caught.exception))
        client = Mock()
        client.holdings.return_value = {'Error': 'secret', 'StatusCode': 503}
        client.positions.return_value = {'data': [raw()]}
        data = dashboard(client)
        self.assertEqual(data['holdings']['rows'], [])
        client.holdings.assert_not_called()
        self.assertEqual(data['positions']['rows'][0]['P&L'], 200)
