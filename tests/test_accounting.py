import csv
import io
import tempfile
import unittest
from pathlib import Path
from accounting import estimate_fees, daily_estimate, CSV_FIELDS, parse_statement_csv, import_statements, save_estimate, history, verify_coverage


def fill(**overrides):
    row = dict(exSeg='nse_fo', flId='fill-1', nOrdNo='order-1', flDt='01-Oct-2026', flTm='10:00:00', optTp='CE', trdSym='NIFTY26O0622500CE', fldQty='65', avgPrc='100', trnsTp='B', multiplier='1', genNum='1', genDen='1', prcNum='1', prcDen='1')
    row.update(overrides)
    return row


def statement(day='2026-10-01', gross='107.25', **changes):
    row = dict.fromkeys(CSV_FIELDS, '0')
    row.update(date=day, gross_pnl=gross, brokerage='20', stt='10', contract_note='CN-1', ledger_reference='LEDGER-1')
    row.update(changes)
    output = io.StringIO()
    writer = csv.DictWriter(output, CSV_FIELDS)
    writer.writeheader()
    writer.writerow(row)
    return output.getvalue()


class FeeTests(unittest.TestCase):
    def test_known_api_orders_use_zero_brokerage_without_changing_app_orders(self):
        r = estimate_fees([fill(), fill(flId='2', nOrdNo='api-order')], '2026-10-01', '10', {('nse_fo', 'api-order')})
        self.assertEqual(r['breakdown']['brokerage'], 10)
        self.assertEqual(r['executed_orders'], 2)

    def test_nse_options_round_trip(self):
        r = estimate_fees({'data': [fill(), fill(flId='2', nOrdNo='2', trnsTp='S', avgPrc='110')]}, '2026-10-01', '10')
        self.assertEqual(r['breakdown']['brokerage'], 20)
        self.assertEqual(r['breakdown']['stt'], 10.73)
        self.assertEqual(r['breakdown']['stamp'], .20)
        self.assertEqual(r['breakdown']['exchange'], 4.85)
        self.assertEqual(r['breakdown']['gst'], 4.48)
        self.assertEqual(r['total'], 40.27)

    def test_partial_fills_and_repeated_responses_not_double_counted(self):
        r = estimate_fees({'data': [fill(), fill(), fill(flId='2')]}, '2026-10-01', '10')
        self.assertEqual(r['fills'], 2)
        self.assertEqual(r['executed_orders'], 1)
        self.assertEqual(r['breakdown']['brokerage'], 10)

    def test_unknown_brokerage_unsupported_segment_or_date_blocks_net(self):
        for row, day, brokerage in [(fill(), '2026-10-01', None), (fill(exSeg='bse_fo'), '2026-10-01', 10), (fill(), '2026-10-03', 10), (fill(flDt='unknown'), '2026-10-01', 10)]:
            self.assertIsNone(estimate_fees({'data': [row]}, day, brokerage)['total'])

    def test_previous_day_fills_are_excluded(self):
        r = estimate_fees({'data': [fill(flDt='30-Sep-2026')]}, '2026-10-01', 10)
        self.assertEqual(r['total'], 0)
        self.assertEqual(r['fills'], 0)

    def test_old_position_date_does_not_become_today_profit(self):
        result = daily_estimate({'data': [{'hsUpTm': '2026/09/30 15:00:00'}]}, [{'P&L': 500}], {'data': []}, '2026-10-01', 10)
        self.assertIsNone(result['gross'])
        self.assertIsNone(result['net'])

    def test_futures_rates(self):
        r = estimate_fees({'data': [fill(optTp='XX', trdSym='NIFTY26OCTFUT', avgPrc='25000', trnsTp='S')]}, '2026-10-01', 10)
        self.assertEqual(r['breakdown']['stt'], 812.5)
        self.assertEqual(r['breakdown']['stamp'], 0)
        self.assertEqual(r['breakdown']['exchange'], 29.74)


class StatementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def test_day_estimate_never_added_to_confirmed_history(self):
        save_estimate('A', {'date': '2026-10-01', 'net': 999}, self.path)
        self.assertIsNone(history('A', self.path)['confirmed_net'])
        import_statements('A', statement(), path=self.path)
        import_statements('A', statement(), path=self.path)
        result = history('A', self.path)
        self.assertEqual(result['confirmed_net'], 77.25)
        self.assertEqual(len(result['confirmed']), 1)
        self.assertIsNone(result['lifetime_net'])
        self.assertIsNone(history('B', self.path)['confirmed_net'])

    def test_conflict_requires_explicit_replacement(self):
        import_statements('A', statement(), path=self.path)
        with self.assertRaises(ValueError):
            import_statements('A', statement(gross='200'), path=self.path)
        self.assertEqual(history('A', self.path)['confirmed_net'], 77.25)
        import_statements('A', statement(gross='200'), replace=True, path=self.path)
        self.assertEqual(history('A', self.path)['confirmed_net'], 170)

    def test_invalid_statements_do_not_write_partial_history(self):
        for csv_text in (statement(stt='NaN'), statement(stt='-1'), statement(contract_note=''), statement(gross='1.234'), 'bad,columns\n1,2\n'):
            with self.assertRaises(ValueError):
                import_statements('A', csv_text, path=self.path)
        self.assertIsNone(history('A', self.path)['confirmed_net'])

    def test_parser_net_is_after_costs_not_income_tax(self):
        self.assertEqual(parse_statement_csv(statement())[0]['net'], 77.25)

    def test_lifetime_requires_coverage_and_corrections_reset_it(self):
        import_statements('A', statement(), path=self.path)
        with self.assertRaises(ValueError):
            verify_coverage('A', '2026-09-01', '2026-10-01', False, self.path)
        verify_coverage('A', '2026-09-01', '2026-10-01', True, self.path)
        self.assertEqual(history('A', self.path)['lifetime_net'], 77.25)
        import_statements('A', statement(), path=self.path)
        self.assertEqual(history('A', self.path)['lifetime_net'], 77.25)
        import_statements('A', statement(gross='200'), replace=True, path=self.path)
        self.assertIsNone(history('A', self.path)['lifetime_net'])
