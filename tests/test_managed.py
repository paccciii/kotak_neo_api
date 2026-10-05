"""Deterministic managed-exit tests. All broker calls are Mock objects."""
import json
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import accounting
import managed
import trading
import test_trading
from test_trading import ORDER


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_trading.TradingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client, self.c = self.fixture.client, self.fixture.c
        self.rows = []
        self.client.order_report.side_effect = lambda: {'data': self.rows.copy()}
        self.client.place_order.side_effect = self.place
        self.client.modify_order.side_effect = self.modify
        self.client.cancel_order.side_effect = self.cancel
        self.client.positions.side_effect = self.positions
        self.body = {**self.fixture.body, 'price': '150', 'distance': '10', 'trail': '5', 'offset': '1'}
        self.auth = set()
        self.quote(150)

    def quote(self, value, age=0):
        self.client.quotes.return_value = [{'exchange_token': '123', 'exchange': 'nse_fo',
            'ltp': str(value), 'lstup_time': (trading.now() - timedelta(seconds=age)).timestamp(),
            'depth': {'buy': [{'price': str(value)}], 'sell': [{'price': str(value)}]}}]

    def positions(self):
        net = sum(int(r['fldQty']) * (1 if r['trnsTp'] == 'B' else -1) for r in self.rows)
        return {'data': [{**ORDER, 'netQty': str(net)}]}

    def place(self, **p):
        with accounting.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM order_intents WHERE state='submitting'").fetchone()[0], 1)
        r = {**ORDER, 'nOrdNo': str(len(self.rows) + 1), 'prc': p['price'], 'prcTp': p['order_type'],
             'trgPrc': p['trigger_price'], 'qty': p['quantity'], 'trnsTp': p['transaction_type'],
             'GuiOrdId': p['tag'], 'fldQty': 0, 'ordSt': 'trigger pending' if p['order_type'] == 'SL' else 'open'}
        self.rows.append(r)
        return {'stat': 'Ok', 'nOrdNo': r['nOrdNo']}

    def modify(self, **p):
        r = next(r for r in self.rows if r['nOrdNo'] == p['order_id'])
        r.update(prc=p['price'], prcTp=p['order_type'], trgPrc=p['trigger_price'], qty=p['quantity'],
                 ordSt='trigger pending' if p['order_type'] == 'SL' else 'open')
        return {'stat': 'Ok', 'nOrdNo': r['nOrdNo']}

    def cancel(self, **p):
        r = next(r for r in self.rows if r['nOrdNo'] == p['order_id'])
        r['ordSt'] = 'cancelled'
        return {'stat': 'Ok', 'nOrdNo': r['nOrdNo']}

    def preview(self, **kw):
        return managed.preview(self.client, 'A', 'session', {**self.body, **kw}, self.c)

    def arm(self, r=None, **kw):
        r = r or self.preview()
        self.id = r['review_id']
        return managed.arm(self.client, 'A', 'session', {'review_id': self.id, 'confirmed': True,
            'automation_confirmed': True, **kw}, '49.37.170.35', self.auth)

    def state(self):
        return managed.load(self.id, 'A')

    def step(self):
        s = self.state()
        managed.step(self.client, s, '49.37.170.35', lambda: True)
        with accounting.connect() as db:
            managed.save(db, s)
        return s

    def protect(self, **kw):
        self.arm(self.preview(**kw))
        self.step()
        self.rows[0].update(fldQty=65, ordSt='complete', avgPrc='150')
        self.step()
        self.step()

    def test_review_confirmation_and_replay(self):
        r = self.preview()
        self.client.place_order.assert_not_called()
        with self.assertRaises(ValueError):
            self.arm(r, automation_confirmed=False)
        with self.assertRaises(ValueError):
            trading.confirm(self.client, 'A', 'session', {'review_id': r['review_id'], 'confirmed': True}, '49.37.170.35')
        self.arm(r, params={'quantity': '9999'})
        self.arm(r)
        self.step()
        self.step()
        self.client.place_order.assert_called_once()
        self.assertEqual(self.rows[0]['qty'], '65')

    def test_bad_policy_freshness_and_unknown_fees(self):
        for kw in [dict(distance='0'), dict(offset='0'), dict(trail='-1'), dict(trail='NaN'), dict(distance='10.01')]:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.preview(**kw)
        self.quote(150, age=16)
        with self.assertRaises(ValueError):
            self.preview()
        self.quote(150)
        with patch('accounting.estimate_fees', return_value={'total': None}):
            r = self.preview()
        with self.assertRaisesRegex(ValueError, 'fees'):
            self.arm(r)
        self.client.place_order.assert_not_called()

    def test_acknowledgement_does_not_create_exit(self):
        self.arm()
        self.step()
        self.step()
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(self.state()['entry_filled'], 0)
        self.assertNotEqual(self.state()['phase'], 'complete')

    def test_partial_entry_cancellation_is_verified_before_protection(self):
        self.arm()
        self.step()
        self.rows[0]['fldQty'] = 20
        self.client.cancel_order.side_effect = None
        self.step()
        self.step()
        self.assertEqual(len(self.rows), 1)
        self.client.cancel_order.assert_called_once()
        # More entry fills arrive while cancellation is in flight.
        self.rows[0].update(fldQty=30, ordSt='cancelled')
        self.rows[0]['avgPrc'] = '150'
        self.step()
        self.assertEqual(self.rows[1]['qty'], '30')
        self.assertEqual(self.rows[1]['trnsTp'], 'S')

    def test_cancel_races_with_full_entry_fill(self):
        self.arm()
        self.step()
        self.rows[0]['fldQty'] = 20
        self.step()
        self.rows[0].update(fldQty=65, ordSt='complete')
        self.step()
        self.assertEqual(self.rows[1]['qty'], '65')

    def test_target_modifies_single_exit_and_waits_for_actual_fills(self):
        self.protect(trail='0')
        self.quote(171)
        self.step()
        self.assertEqual(len(self.rows), 2)
        self.assertEqual(self.rows[1]['prcTp'], 'L')
        self.assertEqual(self.rows[1]['prc'], '170')
        self.rows[1]['fldQty'] = 20
        self.step()
        self.assertNotEqual(self.state()['phase'], 'complete')
        self.rows[1].update(fldQty=65, ordSt='complete')
        self.assertEqual(self.step()['phase'], 'complete')
        self.client.cancel_order.assert_not_called()

    def test_trailing_never_loosens_for_long_or_short(self):
        self.protect()
        self.quote(160)
        self.step()
        self.step()
        self.assertEqual(self.state()['stop'], '155')
        before = self.client.modify_order.call_count
        self.quote(157)
        self.step()
        self.assertEqual(self.state()['stop'], '155')
        self.assertEqual(self.client.modify_order.call_count, before)

    def test_short_trailing_and_target(self):
        self.body.update(side='S', distance='20')
        self.protect(trail='0')
        self.quote(140)
        self.step()
        self.step()
        self.assertEqual(self.state()['stop'], '145')
        self.quote(144)
        self.step()
        self.assertEqual(self.state()['stop'], '145')
        self.quote(129)
        self.step()
        self.assertEqual(self.rows[1]['prcTp'], 'L')
        self.assertEqual(self.rows[1]['prc'], '130')

    def test_equal_distance_levels_derive_from_actual_average_fill_not_entry_limit(self):
        self.arm(self.preview(distance='5'))
        self.step()
        self.rows[0].update(fldQty=65, ordSt='complete', avgPrc='149.98')
        self.step()
        self.step()
        state = self.state()
        self.assertEqual(state['entry_average'], '149.98')
        self.assertEqual(state['exit_anchor'], '150.00')
        self.assertEqual(state['target'], '155.00')
        self.assertEqual(state['stop'], '145.00')
        self.assertEqual(self.rows[1]['trgPrc'], '145.00')

    def test_missing_or_invalid_average_fill_fails_closed_without_protection(self):
        self.arm(self.preview(distance='5'))
        self.step()
        self.rows[0].update(fldQty=65, ordSt='complete', avgPrc='0')
        with self.assertRaisesRegex(ValueError, 'average fill price'):
            self.step()
        self.client.place_order.assert_called_once()
        self.assertEqual(len(self.rows), 1)

    def test_exit_offset_too_large_for_calculated_levels_fails_closed(self):
        self.arm(self.preview(distance='10', offset='145'))
        self.step()
        self.rows[0].update(fldQty=65, ordSt='complete', avgPrc='150')
        with self.assertRaisesRegex(ValueError, 'offset must be smaller'):
            self.step()
        self.assertEqual(len(self.rows), 1)

    def test_short_levels_reverse_around_actual_average_fill(self):
        self.body.update(side='S')
        self.arm(self.preview(distance='5'))
        self.step()
        self.rows[0].update(fldQty=65, ordSt='complete', avgPrc='150.05')
        self.step()
        self.step()
        state = self.state()
        self.assertEqual(state['target'], '145.05')
        self.assertEqual(state['stop'], '155.05')

    def test_stop_fill_completes_without_target_order(self):
        self.protect(trail='0')
        self.rows[1].update(fldQty=65, ordSt='complete')
        self.assertEqual(self.step()['phase'], 'complete')
        self.assertEqual(len(self.rows), 2)

    def test_timeout_never_retries_and_can_reconcile_by_tag(self):
        def accepted_but_timeout(**p):
            self.place(**p)
            raise TimeoutError('private broker details')
        self.client.place_order.side_effect = accepted_but_timeout
        self.arm()
        self.step()
        self.step()
        self.client.place_order.assert_called_once()
        self.assertIsNone(self.state()['pending'])

    def test_lost_response_without_broker_evidence_suspends(self):
        self.client.place_order.side_effect = TimeoutError('private')
        self.arm()
        self.step()
        s = self.state()
        s['pending_since'] = time.time() - 30
        with accounting.connect() as db:
            managed.save(db, s)
        with self.assertRaisesRegex(ValueError, 'unverified'):
            self.step()
        with self.assertRaises(ValueError):
            managed.resume_preview(self.client, 'A', 'session', self.id)
        self.client.place_order.assert_called_once()

    def test_crash_after_journal_never_resends(self):
        self.client.place_order.side_effect = KeyboardInterrupt()
        self.arm()
        with self.assertRaises(KeyboardInterrupt):
            self.step()
        self.step()
        self.client.place_order.assert_called_once()
        with accounting.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM order_intents WHERE state='submitting'").fetchone()[0], 1)

    def test_rejected_modification_is_not_retried(self):
        self.protect(trail='0')
        self.quote(175)
        self.client.modify_order.side_effect = None
        self.client.modify_order.return_value = {'Error': 'rejected'}
        self.step()
        self.step()
        self.client.modify_order.assert_called_once()
        self.assertIsNotNone(self.state()['pending'])

    def test_manual_activity_and_unknown_position_block(self):
        self.protect(trail='0')
        for qty in ('64', 'NaN', None):
            self.client.positions.side_effect = None
            self.client.positions.return_value = {'data': [{**ORDER, 'netQty': qty}]}
            with self.assertRaises(ValueError):
                self.step()
        self.assertEqual(len(self.rows), 2)

    def test_other_pending_order_or_broker_amendment_blocks(self):
        self.protect(trail='0')
        self.rows.append({**ORDER, 'nOrdNo': 'external'})
        with self.assertRaisesRegex(ValueError, 'Another order'):
            self.step()
        self.rows.pop()
        self.rows[1]['prc'] = '135'
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.step()

    def test_stale_quote_keeps_existing_protection_and_disarm_no_mutation(self):
        self.protect(trail='0')
        self.quote(150, age=100)
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.step()
        result = managed.disarm('A', self.id, self.auth)
        self.assertFalse(result['armed'])
        self.client.cancel_order.assert_not_called()
        self.assertEqual(len(self.rows), 2)
        self.assertEqual(self.rows[1]['prcTp'], 'SL')

    def test_restart_requires_explicit_resume_and_scopes_accounts(self):
        self.protect(trail='0')
        app = SimpleNamespace(client=self.client, account='A', session='session', expires=time.monotonic()+1800, login_ip='49.37.170.35')
        manager = managed.Manager()
        manager.tick(app)
        self.assertFalse(self.state()['armed'])
        r = managed.resume_preview(self.client, 'A', 'new-session', self.id)
        body = dict(review_id=r['review_id'], confirmed=True, automation_confirmed=True)
        with self.assertRaises(ValueError):
            managed.resume(self.client, 'B', 'new-session', body, app.login_ip, manager.authorized)
        managed.resume(self.client, 'A', 'new-session', body, app.login_ip, manager.authorized)
        self.assertTrue(self.state()['armed'])
        with self.assertRaises(ValueError):
            managed.resume(self.client, 'A', 'new-session', body, app.login_ip, manager.authorized)
        self.assertEqual(managed.status('B')['strategies'], [])

    def test_session_expiry_and_ip_change_prevent_dispatch(self):
        self.arm()
        s = self.state()
        with self.assertRaisesRegex(ValueError, 'expired'):
            managed.step(self.client, s, '49.37.170.35', lambda: False)
        with patch('trading.public_ip', return_value='49.37.170.36'), self.assertRaises(ValueError):
            self.step()
        self.client.place_order.assert_not_called()

    def test_unknown_order_fields_and_inconsistent_completion(self):
        self.arm()
        self.step()
        self.client.positions.side_effect = None
        self.client.positions.return_value = {'data': []}
        for changes in ({'fldQty': None}, {'ordSt': 'complete', 'fldQty': 0}, {'qty': 66}):
            original = self.rows[0].copy()
            self.rows[0].update(changes)
            with self.assertRaises(ValueError):
                self.step()
            self.rows[0] = original

    def test_target_races_with_stop_fill_never_places_another_exit(self):
        self.protect(trail='0')
        def fill_on_modify(**p):
            self.rows[1].update(fldQty=65, ordSt='complete')
            return {'Error': 'already completed'}
        self.client.modify_order.side_effect = fill_on_modify
        self.quote(171)
        self.step()
        self.assertEqual(self.step()['phase'], 'complete')
        self.assertEqual(len(self.rows), 2)

    def test_expired_day_and_external_exit_cancellation_no_replacement(self):
        self.protect(trail='0')
        self.rows[1]['ordSt'] = 'cancelled'
        with self.assertRaisesRegex(ValueError, 'remaining position'):
            self.step()
        with patch('trading.now', return_value=trading.now()+timedelta(days=1)), self.assertRaisesRegex(ValueError, 'expired'):
            self.step()
        self.assertEqual(len(self.rows), 2)

    def test_manual_guard_and_explicit_disarm(self):
        self.arm()
        with self.assertRaisesRegex(ValueError, 'Disarm'):
            self.fixture.confirm(self.fixture.preview())
        managed.disarm('A', self.id, self.auth)
        managed.manual_guard('A', self.c)

    def test_disarm_cannot_hide_acknowledged_order_missing_from_book(self):
        self.arm()
        self.step()
        managed.disarm('A', self.id, self.auth)
        self.rows.clear()
        with self.assertRaisesRegex(ValueError, 'missing from broker'):
            self.preview()

    def test_rate_limit_suspends_with_no_mutation_or_private_error(self):
        self.arm()
        self.client.order_report.side_effect = ValueError('private broker credential')
        app = SimpleNamespace(client=self.client, account='A', session='session', expires=time.monotonic()+1800, login_ip='49.37.170.35')
        manager = managed.Manager()
        manager.authorized = self.auth
        manager.tick(app)
        self.assertFalse(self.state()['armed'])
        self.assertNotIn('private', self.state()['message'])
        self.client.place_order.assert_not_called()

    def test_quote_changes_between_target_decision_and_dispatch(self):
        self.protect(trail='0')
        self.quote(171)
        target_quote = self.client.quotes.return_value
        self.quote(150)
        safe_quote = self.client.quotes.return_value
        self.client.quotes.side_effect = [target_quote, safe_quote]
        with self.assertRaisesRegex(ValueError, 'Target condition changed'):
            self.step()
        self.client.modify_order.assert_not_called()

    def test_gap_on_entry_fill_uses_one_bounded_limit(self):
        self.arm(self.preview(trail='0'))
        self.step()
        self.rows[0].update(fldQty=65, ordSt='complete')
        self.quote(135)
        self.step()
        self.assertEqual(self.rows[1]['prcTp'], 'L')
        self.assertEqual(trading.number(self.rows[1]['prc']), 134)
        self.step()
        self.assertEqual(len(self.rows), 2)


if __name__ == '__main__':
    unittest.main()
