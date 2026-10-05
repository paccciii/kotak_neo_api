import http.client
import json
import threading
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from server import App, Handler, HOLDINGS, checked, rows


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store_patch = patch('accounting.STORE', Path(self.temp.name) / 'accounts.sqlite3')
        self.store_patch.start()
        self.credentials_patch = patch('server.saved_credentials', return_value={})
        self.credentials_patch.start()
        self.app = App(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.thread.join()
        self.store_patch.stop()
        self.credentials_patch.stop()
        self.temp.cleanup()

    def request(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.app.server_port)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        result = r.status, r.read()
        c.close()
        return result

    def test_private_data_requires_session(self):
        self.assertEqual(self.request('GET', '/api/dashboard')[0], 401)
        self.assertEqual(self.request('GET', '/api/indices')[0], 401)

    def test_trading_requires_authentication_and_same_origin(self):
        headers = {'Origin': f'http://127.0.0.1:{self.app.server_port}', 'X-Neo-Request': '1'}
        for endpoint in ('preview', 'confirm', 'configure', 'search', 'quote', 'resolve'):
            self.assertEqual(self.request('POST', '/api/trading/' + endpoint, headers, '{}')[0], 401)
            self.assertEqual(self.request('POST', '/api/trading/' + endpoint, {}, '{}')[0], 403)
        self.assertEqual(self.request('GET', '/api/trading/tracking')[0], 401)
        self.assertEqual(self.request('GET', '/api/trading/status')[0], 401)
        self.assertEqual(self.request('GET', '/trading.js')[0], 200)

    def test_wrong_host_and_cross_origin_rejected(self):
        self.assertEqual(self.request('GET', '/', {'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/login', {'Origin': 'https://evil.example'})[0], 403)

    def test_error_not_mistaken_for_empty_portfolio(self):
        with self.assertRaises(ValueError):
            rows({'Error': 'Session expired'}, HOLDINGS)
        self.assertEqual(rows({'data': []}, HOLDINGS), [])

    def test_mapping_does_not_expose_extra_account_fields(self):
        result = rows({'data': [{'displaySymbol': 'ABC', 'token': 'secret'}]}, HOLDINGS)
        self.assertEqual(result[0]['Symbol'], 'ABC')
        self.assertNotIn('secret', json.dumps(result))

    def test_partial_failure_preserves_other_section(self):
        self.app.session = 'test-session'
        self.app.expires = time.monotonic() + 100
        self.app.client = Mock()
        self.app.client.holdings.return_value = {'data': []}
        self.app.client.positions.return_value = {'Error': 'private broker detail'}
        status, body = self.request('GET', '/api/dashboard', {'Cookie': 'neo_session=test-session'})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data['holdings']['rows'], [])
        self.assertIn('error', data['positions'])
        self.assertNotIn(b'private broker detail', body)

    def test_expired_session_is_cleared(self):
        self.app.client = Mock()
        client = self.app.client
        self.app.session = 'expired'
        self.app.expires = 0
        self.assertEqual(self.request('GET', '/api/dashboard', {'Cookie': 'neo_session=expired'})[0], 401)
        self.assertIsNone(self.app.client)
        client.logout.assert_called_once()

    def test_login_and_logout_keep_tokens_server_side(self):
        client = Mock()
        client.totp_login.return_value = {'data': {'token': 'view-secret'}}
        client.totp_validate.return_value = {'data': {'token': 'trade-secret', 'sid': 'broker-session', 'status': 'success'}}
        module = Mock()
        module.NeoAPI.return_value = client
        headers = {'Origin': f'http://127.0.0.1:{self.app.server_port}', 'X-Neo-Request': '1'}
        body = json.dumps(dict(consumer_key='key', mobile='+919876543210', ucc='test', totp='123456', mpin='123456'))
        with patch.dict('sys.modules', {'neo_api_client': module}):
            status, payload = self.request('POST', '/api/login', headers, body)
        self.assertEqual(status, 200)
        self.assertNotIn(b'secret', payload)
        self.assertIs(self.app.client, client)
        headers['Cookie'] = 'neo_session=' + self.app.session
        self.assertEqual(self.request('POST', '/api/logout', headers, '{}')[0], 200)
        self.assertIsNone(self.app.session)
        client.logout.assert_called_once()

    def test_failed_login_does_not_create_session(self):
        module = Mock()
        module.NeoAPI.return_value.totp_login.return_value = {'error': 'sensitive detail'}
        headers = {'Origin': f'http://127.0.0.1:{self.app.server_port}', 'X-Neo-Request': '1'}
        body = json.dumps(dict(consumer_key='key', mobile='+919876543210', ucc='test', totp='123456', mpin='123456'))
        with patch.dict('sys.modules', {'neo_api_client': module}):
            status, payload = self.request('POST', '/api/login', headers, body)
        self.assertEqual(status, 401)
        self.assertNotIn(b'sensitive detail', payload)
        self.assertIsNone(self.app.session)

    def test_saved_login_never_returns_credentials(self):
        credentials = dict(consumer_key='secret-key', mobile='+919999999999', ucc='test', mpin='654321')
        with patch('server.saved_credentials', return_value=credentials):
            status, payload = self.request('GET', '/api/status')
            self.assertTrue(json.loads(payload)['saved_login'])
            self.assertNotIn(b'secret-key', payload)
            module = Mock()
            client = module.NeoAPI.return_value
            client.totp_login.return_value = {'data': {'token': 'view-secret'}}
            client.totp_validate.return_value = {'data': {'token': 'trade-secret', 'sid': 'session', 'status': 'success'}}
            headers = {'Origin': f'http://127.0.0.1:{self.app.server_port}', 'X-Neo-Request': '1'}
            with patch.dict('sys.modules', {'neo_api_client': module}):
                status, payload = self.request('POST', '/api/login', headers, json.dumps({'use_saved': True, 'totp': '123456'}))
            self.assertEqual(status, 200)
            module.NeoAPI.assert_called_once_with(consumer_key='secret-key', environment='prod')
            client.totp_validate.assert_called_once_with(mpin='654321')
            self.assertNotIn(b'secret', payload)
        self.assertEqual(self.request('GET', '/.local/credentials.json')[0], 404)

    def test_import_requires_session_and_confirmation(self):
        headers = {'Origin': f'http://127.0.0.1:{self.app.server_port}', 'X-Neo-Request': '1'}
        self.assertEqual(self.request('POST', '/api/import-preview', headers, '{}')[0], 401)
        self.assertEqual(self.request('GET', '/api/history')[0], 401)

    def test_statement_preview_is_read_only_and_confirm_is_idempotent(self):
        from test_accounting import statement
        self.app.session = 'test-session'
        self.app.expires = time.monotonic() + 100
        self.app.account = 'test-account'
        headers = {'Origin': f'http://127.0.0.1:{self.app.server_port}', 'X-Neo-Request': '1', 'Cookie': 'neo_session=test-session'}
        body = {'csv': statement()}
        self.assertEqual(self.request('POST', '/api/import-preview', headers, json.dumps(body))[0], 200)
        self.assertIsNone(json.loads(self.request('GET', '/api/history', headers)[1])['confirmed_net'])
        self.assertEqual(self.request('POST', '/api/import-statements', headers, json.dumps(body))[0], 400)
        body['verified'] = True
        for _ in range(2):
            status, response = self.request('POST', '/api/import-statements', headers, json.dumps(body))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(response)['history']['confirmed_net'], 77.25)


if __name__ == '__main__':
    unittest.main()
