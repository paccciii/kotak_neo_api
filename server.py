"""Loopback-only, single-user Neo dashboard and reviewed manual trading."""
import json
import logging
import secrets
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from portfolio import dashboard
import accounting
import trading

ROOT = Path(__file__).parent
TTL = 1800


def saved_credentials():
    try:
        data = json.loads((ROOT / '.local' / 'credentials.json').read_text(encoding='utf-8'))
        return {key: data[key] for key in ('consumer_key', 'mobile', 'ucc', 'mpin') if isinstance(data.get(key), str)}
    except (OSError, ValueError, TypeError):
        return {}


def checked(response):
    if not isinstance(response, dict):
        raise ValueError('Unexpected broker response')
    if response.get('error') or response.get('Error') or str(response.get('stat', '')).lower() in ('not_ok', 'not ok', 'failed'):
        raise ValueError('Broker rejected request')
    if str(response.get('stCode', '200')) not in ('200', '0'):
        raise ValueError('Broker rejected request')
    return response


def rows(response, fields):
    data = checked(response).get('data')
    if not isinstance(data, list):
        raise ValueError('Unexpected broker data format')
    return [{label: row.get(key) for label, key in fields.items()} for row in data if isinstance(row, dict)]


HOLDINGS = dict(Symbol='displaySymbol', Quantity='quantity', Average='averagePrice', **{'Market value': 'mktValue', 'Closing price': 'closingPrice'})
POSITIONS = dict(Symbol='trdSym', Product='prod', Quantity='netQty', Average='averagePrice', LTP='ltp', **{'Position P&L': 'positionPnl', 'Calculation note': 'pnlCalculationError'})


class App(HTTPServer):
    client = None
    session = None
    expires = 0
    account = None
    login_ip = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.contracts = {}

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def disconnect(self):
        client, self.client = self.client, None
        self.session = None
        self.expires = 0
        self.account = None
        self.login_ip = None
        self.contracts.clear()
        if client:
            try:
                client.logout()
            except Exception:
                pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log request bodies, account data or SDK errors.

    def reply(self, status, payload, cookie=None, html=False):
        body = payload if html else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get('Host') in (f'localhost:{port}', f'127.0.0.1:{port}')

    def authenticated(self):
        if time.monotonic() > self.server.expires:
            self.server.disconnect()
            return False
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
            value = cookies['neo_session'].value
        except (KeyError, ValueError):
            return False
        return bool(self.server.session) and secrets.compare_digest(value, self.server.session)

    def do_GET(self):
        if not self.valid_host():
            return self.reply(403, {'error': 'Local access only.'})
        if self.path == '/':
            return self.reply(200, (ROOT / 'index.html').read_bytes(), html=True)
        if self.path in ('/app.js', '/trading.js'):
            body = (ROOT / self.path[1:]).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/javascript; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/api/status':
            return self.reply(200, {'connected': self.authenticated(), 'saved_login': len(saved_credentials()) == 4})
        if self.path in ('/api/trading/status', '/api/trading/tracking'):
            if not self.authenticated():
                return self.reply(401, {'error': 'Please connect your account again.'})
            try:
                result = trading.readiness(self.server.login_ip) if self.path.endswith('/status') else trading.tracking(self.server.client, self.server.account)
                return self.reply(200, result)
            except Exception:
                return self.reply(502, {'error': 'Trading report unavailable. Previously submitted orders may still be active; check Kotak.'})
        if self.path == '/api/history':
            if not self.authenticated():
                return self.reply(401, {'error': 'Please connect your account again.'})
            return self.reply(200, accounting.history(self.server.account))
        if self.path != '/api/dashboard':
            return self.reply(404, {'error': 'Not found.'})
        if not self.authenticated():
            return self.reply(401, {'error': 'Please connect your account again.'})
        result = dashboard(self.server.client)
        trades = result.pop('_trades', None)
        positions = result.pop('_positions', None)
        day = result['fetched_at'][:10]
        brokerage = accounting.setting(self.server.account)
        try:
            result['today'] = accounting.daily_estimate(positions, result['positions']['rows'], trades, day, brokerage, trading.api_order_ids(self.server.account))
        except Exception:
            result['today'] = {'date': day, 'gross': None, 'net': None, 'fees': {'total': None, 'breakdown': {}, 'issues': []}, 'issues': ['Today’s trade/position report is unavailable. No net P&L has been assumed.']}
        accounting.save_estimate(self.server.account, result['today'])
        result['history'] = accounting.history(self.server.account)
        result['brokerage_per_order'] = brokerage
        return self.reply(200, result)

    def do_POST(self):
        origin = self.headers.get('Origin')
        if not self.valid_host() or origin != 'http://' + self.headers.get('Host', '') or self.headers.get('X-Neo-Request') != '1':
            return self.reply(403, {'error': 'Request origin rejected.'})
        if self.path.startswith('/api/trading/'):
            if not self.authenticated():
                return self.reply(401, {'error': 'Please connect your account again.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length < 16384:
                    raise ValueError('Invalid request size.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Invalid request.')
                action = self.path.rsplit('/', 1)[-1]
                client = self.server.client
                if action == 'configure':
                    result = trading.configure(body)
                elif action == 'search':
                    result = {'contracts': trading.search(client, body)}
                    self.server.contracts.update({c['id']: c for c in result['contracts']})
                elif action == 'resolve':
                    c = trading.resolve(client, body)
                    self.server.contracts[c['id']] = c
                    result = {'contract': c}
                elif action in ('quote', 'preview'):
                    c = self.server.contracts.get(body.get('contract_id'))
                    if not c:
                        raise ValueError('Search and select a broker-verified contract first.')
                    result = trading.quote(client, c) if action == 'quote' else trading.preview(client, self.server.account, self.server.session, body, c)
                elif action == 'confirm':
                    result = trading.confirm(client, self.server.account, self.server.session, body, self.server.login_ip)
                else:
                    return self.reply(404, {'error': 'Not found.'})
                return self.reply(200, result)
            except ValueError as error:
                return self.reply(400, {'error': str(error)})
            except Exception:
                return self.reply(502, {'error': 'Broker request unavailable. If confirming an order, refresh tracking; do not assume it failed or submit a replacement.'})
        if self.path in ('/api/brokerage', '/api/import-preview', '/api/import-statements', '/api/coverage'):
            if not self.authenticated():
                return self.reply(401, {'error': 'Please connect your account again.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 2000000:
                    raise ValueError('Request must be between 1 byte and 2 MB.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Invalid request.')
                if self.path == '/api/brokerage':
                    return self.reply(200, {'brokerage': accounting.setting(self.server.account, body.get('amount'), save=True)})
                if self.path == '/api/coverage':
                    accounting.verify_coverage(self.server.account, body.get('start'), body.get('end'), body.get('attested'))
                    return self.reply(200, accounting.history(self.server.account))
                csv_text = body.get('csv')
                if not isinstance(csv_text, str):
                    raise ValueError('Select a CSV file first.')
                entries = accounting.parse_statement_csv(csv_text)
                if self.path == '/api/import-preview':
                    return self.reply(200, {'rows': entries, 'net': round(sum(r['net'] for r in entries), 2)})
                if body.get('verified') is not True:
                    raise ValueError('Verify the amounts against contract notes and ledger before confirming.')
                count = accounting.import_statements(self.server.account, csv_text, replace=body.get('replace') is True)
                return self.reply(200, {'imported': count, 'history': accounting.history(self.server.account)})
            except (ValueError, TypeError) as error:
                return self.reply(400, {'error': str(error) if isinstance(error, ValueError) else 'Invalid input.'})
        if self.path == '/api/logout':
            if not self.authenticated():
                return self.reply(401, {'error': 'Not connected.'})
            self.server.disconnect()
            return self.reply(200, {'connected': False}, 'neo_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        if self.path != '/api/login':
            return self.reply(404, {'error': 'Not found.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 8192:
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError()
            if data.get('use_saved') is True:
                data = {**saved_credentials(), 'totp': data.get('totp')}
            for name in ('consumer_key', 'mobile', 'ucc', 'totp', 'mpin'):
                if not isinstance(data.get(name), str) or not data[name].strip():
                    raise ValueError()
        except (ValueError, TypeError):
            return self.reply(400, {'error': 'Complete all login fields.'})
        try:
            from neo_api_client import NeoAPI
        except ImportError:
            return self.reply(503, {'error': 'Install requirements.txt in your Python environment, then restart the server.'})
        self.server.disconnect()
        client = None
        try:
            login_ip_before = None
            if trading.config().get('ip'):
                try:
                    login_ip_before = trading.public_ip()
                except Exception:
                    pass
            client = NeoAPI(consumer_key=data['consumer_key'].strip(), environment='prod')
            first = checked(client.totp_login(mobile_number=data['mobile'].strip(), ucc=data['ucc'].strip(), totp=data['totp'].strip()))
            if not isinstance(first.get('data'), dict) or not first['data'].get('token'):
                raise ValueError()
            final = checked(client.totp_validate(mpin=data['mpin'].strip()))
            auth = final.get('data', {})
            if not auth.get('token') or not auth.get('sid') or auth.get('status') != 'success':
                raise ValueError()
            self.server.client = client
            self.server.account = data['ucc'].strip()
            self.server.session = secrets.token_urlsafe(32)
            self.server.expires = time.monotonic() + TTL
            if trading.config().get('ip'):
                try:
                    login_ip_after = trading.public_ip()
                    self.server.login_ip = login_ip_after if login_ip_after == login_ip_before else None
                except Exception:
                    self.server.login_ip = None
            return self.reply(200, {'connected': True}, f'neo_session={self.server.session}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TTL}')
        except Exception:
            if client:
                try:
                    client.logout()
                except Exception:
                    pass
            return self.reply(401, {'error': 'Kotak login failed. Check your API token, mobile (+91), client code, fresh TOTP and MPIN. Also check API/IP settings in Neo.'})


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    app = App(('127.0.0.1', 8000), Handler)
    app.timeout = 1
    print('Neo Desk: http://localhost:8000 — Ctrl+C to stop', flush=True)
    try:
        while True:
            app.handle_request()
            if app.client and time.monotonic() > app.expires:
                app.disconnect()
    except KeyboardInterrupt:
        pass
    finally:
        app.disconnect()
        app.server_close()
