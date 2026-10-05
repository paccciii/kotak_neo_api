"""Execution tests use mocked brokers only; never place real orders."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import accounting
import trading
from portfolio import IST


MASTER = dict(pSymbol=123, pExchSeg='nse_fo', pOptionType='CE', pExpiryDate='06Oct2026',
              pSymbolName='NIFTY', pTrdSymbol='NIFTY26O0622500CE', dTickSize=5,
              lLotSize=65, lFreezeQty=1800, **{'dStrikePrice;': 2250000})
ORDER = dict(nOrdNo='order-1', exSeg='nse_fo', tok='123', trdSym='NIFTY26O0622500CE',
             prod='NRML', trnsTp='B', qty=65, fldQty=0, prc='200', trgPrc='0',
             prcTp='L', avgPrc='0', ordSt='open', rejRsn='--')


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in [('accounting.STORE', Path(self.temp.name) / 'db.sqlite'),
                            ('trading.CONFIG', Path(self.temp.name) / 'trading.json')]:
            patcher = patch(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in [('trading.now', datetime(2026, 10, 1, 11, tzinfo=IST)), ('trading.public_ip', '49.37.170.35')]:
            patcher = patch(name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        trading.CONFIG.write_text(json.dumps({'ip': '49.37.170.35'}))
        self.client = Mock()
        self.client.search_scrip.return_value = [MASTER]
        self.client.quotes.return_value = [{'exchange_token': '123', 'exchange': 'nse_fo', 'ltp': '150', 'depth': {'buy': [{'price': '149.95'}], 'sell': [{'price': '150'}]}}]
        self.client.margin_required.return_value = {'data': {'stat': 'Ok', 'rmsVldtd': 'OK', 'insufFund': '0', 'ordMrgn': '13000', 'avlCash': '20000'}}
        self.client.place_order.return_value = {'stat': 'Ok', 'nOrdNo': 'order-1'}
        self.client.modify_order.return_value = {'stat': 'Ok', 'nOrdNo': 'order-1'}
        self.client.cancel_order.return_value = {'stat': 'Ok', 'nOrdNo': 'order-1'}
        self.client.order_report.return_value = {'data': []}
        self.client.positions.return_value = {'data': []}
        self.c = trading.contract(MASTER)
        self.body = dict(action='place', side='B', product='NRML', lots='1', type='L', price='200', trigger='')

    def preview(self, **overrides):
        return trading.preview(self.client, 'A', 'session', {**self.body, **overrides}, self.c)

    def confirm(self, r, **overrides):
        return trading.confirm(self.client, 'A', 'session', {'review_id': r['review_id'], 'confirmed': True, **overrides}, '49.37.170.35')

    def test_contract_metadata_and_preview_do_not_trade(self):
        self.assertEqual(self.c['lot'], 65)
        self.assertEqual(self.c['tick'], '0.05')
        self.assertEqual(self.c['strike'], 22500)
        r = self.preview()
        self.assertEqual(r['params']['quantity'], '65')
        self.assertEqual(r['fees']['breakdown']['brokerage'], 0)
        self.assertEqual(r['margin']['ordMrgn'], '13000')
        self.client.place_order.assert_not_called()

    def test_bad_lots_price_side_stop_and_freeze_fail_closed(self):
        for change in [dict(lots='0'), dict(lots='1.5'), dict(lots='28'), dict(price='NaN'), dict(price='200.01'), dict(side='X'), dict(type='MKT'), dict(type='SL', trigger='201'), dict(product='CNC')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.preview(**change)
        self.client.place_order.assert_not_called()

    def test_expired_and_non_fo_contracts_rejected(self):
        for values in [dict(pExpiryDate='01Jan2020'), dict(pExchSeg='nse_cm'), dict(lLotSize=0), dict(dTickSize=None)]:
            with self.assertRaises(ValueError):
                trading.contract({**MASTER, **values})

    def test_search_exact_underlying_and_server_metadata(self):
        self.client.search_scrip.return_value = [MASTER, {**MASTER, 'pSymbolName': 'NIFTYNXT50'}]
        rows = trading.search(self.client, dict(symbol='NIFTY', segment='nse_fo', kind='CE', expiry='2026-10-06'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.client.search_scrip.call_args.kwargs['expiry'], '06Oct2026')

    def test_insufficient_or_unknown_margin_blocks_preview(self):
        for d in [{'rmsVldtd': 'OK', 'insufFund': '1'}, {'rmsVldtd': 'OK'}, {'rmsVldtd': 'NO', 'insufFund': '0'}, {'Error': 'private'}]:
            self.client.margin_required.return_value = {'data': d}
            with self.assertRaises(ValueError):
                self.preview()

    def test_replay_and_tampered_confirm_submit_original_once(self):
        r = self.preview()
        result = self.confirm(r, price='1', quantity='999')
        self.assertEqual(result['state'], 'acknowledged')
        self.assertNotEqual(result['state'], 'complete')
        self.confirm(r)
        self.client.place_order.assert_called_once()
        self.assertEqual(self.client.place_order.call_args.kwargs['price'], '200')
        self.assertEqual(self.client.place_order.call_args.kwargs['quantity'], '65')

    def test_confirmation_session_expiry_and_ip_gates(self):
        r = self.preview()
        with self.assertRaises(ValueError):
            self.confirm(r, confirmed=False)
        with self.assertRaises(ValueError):
            trading.confirm(self.client, 'B', 'session', {'review_id': r['review_id'], 'confirmed': True}, '49.37.170.35')
        with self.assertRaises(ValueError):
            trading.confirm(self.client, 'A', 'other-session', {'review_id': r['review_id'], 'confirmed': True}, '49.37.170.35')
        with patch('trading.public_ip', return_value='49.37.170.36'), self.assertRaises(ValueError):
            self.confirm(r)
        with accounting.connect() as db:
            db.execute('UPDATE order_intents SET created=0')
        with self.assertRaises(ValueError):
            self.confirm(r)
        self.client.place_order.assert_not_called()

    def test_missing_ip_does_not_block_review_but_blocks_dispatch(self):
        trading.CONFIG.unlink()
        r = self.preview()
        with self.assertRaises(ValueError):
            self.confirm(r)
        self.client.place_order.assert_not_called()

    def test_ambiguous_timeout_is_persisted_and_never_retried(self):
        self.client.place_order.side_effect = TimeoutError()
        r = self.preview()
        self.assertEqual(self.confirm(r)['state'], 'unknown')
        self.assertEqual(self.confirm(r)['state'], 'unknown')
        with self.assertRaises(ValueError):
            self.confirm(self.preview(price='201'))
        self.client.place_order.assert_called_once()
        # Reconcile by our unique tag when the response was lost.
        self.client.order_report.return_value = {'data': [{**ORDER, 'GuiOrdId': 'ND' + r['review_id'][:18], 'fldQty': 65, 'ordSt': 'complete', 'avgPrc': '199.95'}]}
        data = trading.tracking(self.client, 'A')
        self.assertEqual(data['intents'][0]['state'], 'resolved')
        self.assertEqual(data['orders'][0]['average'], '199.95')

    def test_crash_after_persist_before_response_does_not_resend(self):
        r = self.preview()
        with accounting.connect() as db:
            db.execute("UPDATE order_intents SET state='submitting'")
        self.assertEqual(self.confirm(r)['state'], 'submitting')
        self.client.place_order.assert_not_called()

    def test_identical_pending_order_blocks_new_review_dispatch(self):
        self.confirm(self.preview())
        with self.assertRaises(ValueError):
            self.confirm(self.preview())
        self.client.place_order.assert_called_once()

    def test_partial_fill_tracking_is_not_complete(self):
        self.client.order_report.return_value = {'data': [{**ORDER, 'fldQty': 20}]}
        d = trading.tracking(self.client, 'A')
        self.assertEqual(d['orders'][0]['status'], 'partially filled')
        self.assertEqual(d['orders'][0]['filled'], 20)

    def test_order_changes_after_review_prevent_modify(self):
        self.client.order_report.return_value = {'data': [ORDER]}
        r = self.preview(action='modify', order_id='order-1', price='201')
        self.client.order_report.return_value = {'data': [{**ORDER, 'fldQty': 20}]}
        with self.assertRaises(ValueError):
            self.confirm(r)
        self.client.modify_order.assert_not_called()

    def test_modify_total_and_cancel_require_confirmation_and_track(self):
        self.client.order_report.return_value = {'data': [ORDER]}
        r = self.preview(action='modify', order_id='order-1', price='201')
        self.confirm(r)
        self.client.modify_order.assert_called_once_with(order_id='order-1', price='201', order_type='L', quantity='65', validity='DAY', trigger_price='0')
        self.client.order_report.return_value = {'data': [{**ORDER, 'prc': '201'}]}
        self.assertEqual(trading.tracking(self.client, 'A')['intents'][0]['state'], 'resolved')
        cancel = self.preview(action='cancel', order_id='order-1')
        self.client.cancel_order.assert_not_called()
        self.assertEqual(self.confirm(cancel)['state'], 'acknowledged')
        self.client.cancel_order.assert_called_once_with(order_id='order-1')

    def test_exit_revalidates_position_and_blocks_overlapping_orders(self):
        pos = dict(exSeg='nse_fo', tok='123', trdSym=ORDER['trdSym'], prod='NRML', netQty='65')
        self.client.positions.return_value = {'data': [pos]}
        r = self.preview(action='exit', side='S')
        self.client.positions.return_value = {'data': [{**pos, 'netQty': '0'}]}
        with self.assertRaises(ValueError):
            self.confirm(r)
        self.client.positions.return_value = {'data': [pos]}
        self.client.order_report.return_value = {'data': [ORDER]}
        with self.assertRaises(ValueError):
            self.preview(action='exit', side='S')
        self.client.place_order.assert_not_called()

    def test_resolve_uses_exact_broker_position_and_contract(self):
        self.client.order_report.return_value = {'data': [ORDER]}
        self.assertEqual(trading.resolve(self.client, dict(action='cancel', order_id='order-1'))['id'], self.c['id'])

    def test_api_brokerage_ids_are_account_scoped(self):
        self.confirm(self.preview())
        self.assertEqual(trading.api_order_ids('A'), {('nse_fo', 'order-1')})
        self.assertEqual(trading.api_order_ids('B'), set())


if __name__ == '__main__':
    unittest.main()
