"""Manual order review and at-most-once dispatch. Never retries a mutation."""
import hashlib
import ipaddress
import json
import re
import secrets
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.request import urlopen

import accounting
from portfolio import IST, number, records, position_row

CONFIG = Path(__file__).parent / '.local' / 'trading.json'
PENDING = {'open', 'pending', 'trigger pending', 'partially filled', 'partially executed'}
TERMINAL = {'complete', 'completed', 'traded', 'cancelled', 'canceled', 'rejected'}


def now():
    return datetime.now(IST)


def public_ip():
    with urlopen('https://api.ipify.org', timeout=5) as response:
        value = response.read(64).decode().strip()
    parsed = ipaddress.ip_address(value)
    if parsed.version != 4 or not parsed.is_global:
        raise ValueError('A public IPv4 address is required.')
    return value


def config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def configure(body):
    if body.get('attested') is not True:
        raise ValueError('Confirm that your provider assigned this static public IPv4 and that it is whitelisted in Kotak.')
    value = str(body.get('ip', '')).strip()
    if value != public_ip():
        raise ValueError('The configured IP does not match this server’s current public IPv4.')
    CONFIG.parent.mkdir(exist_ok=True)
    CONFIG.write_text(json.dumps({'ip': value}), encoding='utf-8')
    return {'message': 'IP saved. Reconnect with a fresh TOTP to create a session from this IP.'}


def readiness(login_ip):
    value = config().get('ip')
    return {'configured_ip': value, 'login_ip': login_ip,
            'ready': bool(value and login_ip == value),
            'message': 'Live orders require your assigned static public IPv4 to be whitelisted in Kotak and a fresh login from that IP.'}


def assert_ready(login_ip):
    value = config().get('ip')
    if not value or value != login_ip or public_ip() != value:
        raise ValueError('Live submission blocked: configure your static IP and reconnect; the current IP must match the login IP.')


def obj(response):
    if not isinstance(response, dict):
        raise ValueError('Unrecognized broker response.')
    if any(k.lower().startswith('error') for k in response) or str(response.get('stat', '')).lower() in ('not_ok', 'not ok', 'failed', 'error') or str(response.get('stCode', '200')) not in ('200', '0'):
        raise ValueError('Broker request failed. Check your account and API access in Kotak.')
    if isinstance(response.get('data'), dict):
        return obj(response['data'])
    return response


def integer(value, label):
    n = number(value)
    if n is None or n <= 0 or n != int(n):
        raise ValueError(label + ' must be a positive whole number.')
    return int(n)


def contract(row):
    seg = row.get('pExchSeg')
    kind = str(row.get('pOptionType', '')).upper()
    if seg not in ('nse_fo', 'bse_fo') or kind not in ('CE', 'PE', 'XX'):
        raise ValueError('Only equity F&O contracts are supported.')
    expiry = datetime.strptime(row['pExpiryDate'], '%d%b%Y').date()
    if expiry < now().date():
        raise ValueError('Contract has expired.')
    # Equity F&O scrip-master prices are in paise; order/quote prices are rupees.
    tick = number(row.get('dTickSize'))
    if tick is None or tick <= 0:
        raise ValueError('Contract tick size unavailable.')
    lot = integer(row.get('lLotSize'), 'Lot size')
    token = str(integer(row.get('pSymbol'), 'Instrument token'))
    symbol = str(row.get('pTrdSymbol', '')).strip()
    if not symbol:
        raise ValueError('Trading symbol unavailable.')
    return {'id': seg + ':' + token, 'segment': seg, 'token': token, 'symbol': symbol,
            'underlying': row.get('pSymbolName'), 'expiry': expiry.isoformat(),
            'kind': 'FUT' if kind == 'XX' else kind,
            'strike': float(number(row.get('dStrikePrice;')) / 100) if kind != 'XX' and number(row.get('dStrikePrice;')) is not None else None,
            'lot': lot, 'tick': str(tick / 100),
            'freeze': int(number(row.get('lFreezeQty')) or 0)}


def search(client, body):
    symbol = str(body.get('symbol', '')).strip().upper()
    segment, kind = body.get('segment'), body.get('kind')
    if not re.fullmatch(r'[A-Z0-9&-]{2,30}', symbol) or segment not in ('nse_fo', 'bse_fo') or kind not in ('CE', 'PE', 'FUT'):
        raise ValueError('Choose an F&O segment, underlying and CE, PE or FUT.')
    expiry = body.get('expiry')
    expiry = datetime.strptime(expiry, '%Y-%m-%d').strftime('%d%b%Y') if expiry else None
    strike = str(body.get('strike') or '')
    if strike and (number(strike) is None or number(strike) <= 0):
        raise ValueError('Enter a positive strike price.')
    rows = records(client.search_scrip(exchange_segment=segment, symbol=re.escape(symbol), expiry=expiry,
                                      option_type=kind, strike_price=strike or None, ignore_50multiple=False))
    result = []
    for row in rows:
        if str(row.get('pSymbolName', '')).strip().upper() != symbol:
            continue
        try:
            result.append(contract(row))
        except (ValueError, KeyError, TypeError):
            continue
    result.sort(key=lambda r: (r['expiry'], r['strike'] or 0, r['symbol']))
    return result[:300]


def quote(client, c):
    rows = records(client.quotes(instrument_tokens=[{'exchange_segment': c['segment'], 'instrument_token': c['token']}], quote_type='all'))
    q = next((r for r in rows if str(r.get('exchange_token')) == c['token'] and r.get('exchange') == c['segment']), None)
    if q is None:
        raise ValueError('Quote unavailable for this contract.')
    depth = q.get('depth') or {}
    def best(side):
        levels = depth.get(side) or []
        return str(levels[0].get('price')) if levels else None
    return {'ltp': q.get('ltp'), 'bid': best('buy'), 'ask': best('sell'),
            'broker_timestamp': q.get('lstup_time'), 'fetched_at': now().isoformat(timespec='seconds')}


def book(client):
    return [r for r in records(client.order_report()) if r.get('exSeg') in ('nse_fo', 'bse_fo')]


def order_view(r):
    status = str(r.get('ordSt') or r.get('stat') or 'Unknown').lower()
    filled, qty = number(r.get('fldQty')), number(r.get('qty'))
    if status not in TERMINAL and filled is not None and qty is not None and 0 < filled < qty:
        status = 'partially filled'
    return {'id': str(r.get('nOrdNo', '')), 'symbol': r.get('trdSym'), 'segment': r.get('exSeg'),
            'token': str(r.get('tok', '')), 'product': r.get('prod'), 'side': r.get('trnsTp'),
            'quantity': int(qty) if qty is not None else None, 'filled': int(filled) if filled is not None else None,
            'price': r.get('prc'), 'trigger': r.get('trgPrc'), 'type': r.get('prcTp'),
            'average': r.get('avgPrc'), 'status': status, 'reason': str(r.get('rejRsn') or '')[:500],
            'updated': r.get('exCfmTm') or r.get('ordDtTm'), 'manageable': status in PENDING}


def position_views(client):
    result = []
    for r in records(client.positions()):
        if r.get('exSeg') not in ('nse_fo', 'bse_fo'):
            continue
        view = position_row(r, None, [])
        qty = number(view.get('Quantity (units)'))
        if qty is not None and qty != 0 and qty == int(qty):
            result.append({'symbol': r.get('trdSym'), 'segment': r['exSeg'], 'token': str(r.get('tok')),
                           'product': r.get('prod'), 'quantity': int(qty)})
    return result


def resolve(client, body):
    if body.get('action') in ('modify', 'cancel'):
        row = order_view(pending_order(client, str(body.get('order_id', ''))))
    elif body.get('action') == 'exit':
        row = next((r for r in position_views(client) if r['symbol'] == body.get('symbol') and r['segment'] == body.get('segment') and r['product'] == body.get('product')), None)
        if row is None:
            raise ValueError('Position unavailable. Refresh positions.')
    else:
        raise ValueError('Unsupported action.')
    # Do not infer an underlying from encoded symbols (some start with digits).
    rows = records(client.search_scrip(exchange_segment=row['segment'], symbol='', ignore_50multiple=False))
    exact = next((r for r in rows if r.get('pTrdSymbol') == row['symbol'] and str(r.get('pSymbol')) == row['token']), None)
    if exact is None:
        raise ValueError('Active contract metadata unavailable. Manage this order in Kotak.')
    return contract(exact)


def init_db(db):
    db.execute('''CREATE TABLE IF NOT EXISTS order_intents (
        id TEXT PRIMARY KEY, account TEXT NOT NULL, session TEXT NOT NULL,
        created REAL NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL,
        fingerprint TEXT NOT NULL, state TEXT NOT NULL, order_id TEXT, message TEXT)''')


def api_order_ids(account):
    with accounting.connect() as db:
        init_db(db)
        rows = db.execute("SELECT payload,order_id FROM order_intents WHERE account=? AND action IN ('place','exit') AND order_id IS NOT NULL", (account,)).fetchall()
    return {(json.loads(r[0])['contract']['segment'], r[1]) for r in rows}


def session_hash(session):
    return hashlib.sha256(session.encode()).hexdigest()


def price_value(value, tick, label):
    n = number(value)
    if n is None or n <= 0 or n % Decimal(tick) != 0:
        raise ValueError(f'{label} must be positive and a multiple of tick size ₹{tick}.')
    return str(n)


def pending_order(client, order_id):
    row = next((r for r in book(client) if str(r.get('nOrdNo')) == order_id), None)
    if row is None or not order_view(row)['manageable']:
        raise ValueError('Order is no longer pending or is unavailable. Refresh the order book.')
    return row


def check_exit(client, payload):
    c, p = payload['contract'], payload['params']
    positions = position_views(client)
    match = next((r for r in positions if r['segment'] == c['segment'] and r['symbol'] == c['symbol'] and r['product'] == p['product']), None)
    if not match or (p['transaction_type'] == 'S') != (match['quantity'] > 0) or int(p['quantity']) > abs(match['quantity']):
        raise ValueError('Position changed. Refresh it and review the exit again.')
    if any(r.get('trdSym') == c['symbol'] and r.get('prod') == p['product'] and r.get('exSeg') == c['segment'] and order_view(r)['status'] not in TERMINAL for r in book(client)):
        raise ValueError('This position has an outstanding order. Resolve it before reviewing an exit to avoid overlapping orders.')


def margin(client, c, p):
    d = obj(client.margin_required(exchange_segment=c['segment'], instrument_token=c['token'],
        **{k: p[k] for k in ('price', 'order_type', 'product', 'quantity', 'transaction_type', 'trigger_price')}))
    if str(d.get('rmsVldtd', '')).upper() != 'OK' or number(d.get('insufFund')) is None or number(d['insufFund']) > 0:
        raise ValueError('Kotak margin validation did not pass. Check funds and product eligibility.')
    return {key: str(d[key]) if key in d else None for key in ('avlCash', 'ordMrgn', 'reqdMrgn', 'avlMrgn', 'insufFund')}


def preview(client, account, session, body, c):
    action = body.get('action', 'place')
    if action not in ('place', 'exit', 'modify', 'cancel'):
        raise ValueError('Unsupported order action.')
    order_id = str(body.get('order_id', ''))
    existing = pending_order(client, order_id) if action in ('modify', 'cancel') else None
    if existing and (existing.get('trdSym') != c['symbol'] or existing.get('exSeg') != c['segment']):
        raise ValueError('Contract does not match the selected order.')
    if datetime.fromisoformat(c['expiry']).date() < now().date():
        raise ValueError('Contract expired. Search again.')
    side = existing.get('trnsTp') if existing else body.get('side')
    product = existing.get('prod') if existing else body.get('product')
    if side not in ('B', 'S') or product not in ('NRML', 'MIS'):
        raise ValueError('Choose Buy/Sell and NRML/MIS.')
    if action == 'cancel':
        params = {'order_id': order_id}
        fees, m, q = None, None, None
    else:
        lots = integer(body.get('lots'), 'Lots')
        qty = lots * c['lot']
        if c['freeze'] > 0 and qty > c['freeze']:
            raise ValueError('Quantity exceeds the contract freeze limit; automatic order splitting is not enabled.')
        if existing and (number(existing.get('fldQty')) is None or qty < number(existing['fldQty'])):
            raise ValueError('Total modified quantity cannot be below already filled units.')
        order_type = body.get('type')
        if order_type not in ('L', 'SL'):
            raise ValueError('Only limit and stop-limit DAY orders are supported.')
        price = price_value(body.get('price'), c['tick'], 'Limit price')
        trigger = price_value(body.get('trigger'), c['tick'], 'Trigger price') if order_type == 'SL' else '0'
        if order_type == 'SL' and ((side == 'B' and Decimal(price) < Decimal(trigger)) or (side == 'S' and Decimal(price) > Decimal(trigger))):
            raise ValueError('Buy limit must be at or above trigger; sell limit must be at or below trigger.')
        params = dict(exchange_segment=c['segment'], trading_symbol=c['symbol'], product=product,
                      transaction_type=side, quantity=str(qty), price=price, order_type=order_type,
                      trigger_price=trigger, validity='DAY')
        if existing:
            params['order_id'] = order_id
        q = quote(client, c)
        m = margin(client, c, params)
        # Estimate this side only, at the entered limit, using the existing dated schedule.
        synthetic = dict(exSeg=c['segment'], optTp=c['kind'], trdSym=c['symbol'],
            fldQty=str(qty), avgPrc=price, trnsTp=side, flId='preview', nOrdNo='preview',
            exTm=now().isoformat(), multiplier='1', genNum='1', genDen='1', prcNum='1', prcDen='1')
        fees = accounting.estimate_fees([synthetic], now().date().isoformat(), '0')
    payload = {'contract': c, 'params': params, 'action': action,
               'existing': order_view(existing) if existing else None,
               'quote': q, 'margin': m, 'fees': fees}
    if action == 'exit':
        check_exit(client, payload)
    identity = secrets.token_hex(16)
    fingerprint = hashlib.sha256(json.dumps([action, params], sort_keys=True).encode()).hexdigest()
    with accounting.connect() as db:
        init_db(db)
        db.execute('INSERT INTO order_intents VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (identity, account, session_hash(session), time.time(), action, json.dumps(payload), fingerprint, 'review', order_id or None, 'Awaiting confirmation'))
    return {'review_id': identity, 'expires_in': 90, **payload}


def intent_view(row):
    return {'review_id': row[0], 'created': datetime.fromtimestamp(row[3], IST).isoformat(timespec='seconds'),
            'action': row[4], 'symbol': json.loads(row[5])['contract']['symbol'],
            'state': row[7], 'order_id': row[8], 'message': row[9]}


def confirm(client, account, session, body, login_ip):
    if body.get('confirmed') is not True:
        raise ValueError('Review the order and explicitly confirm it.')
    identity = str(body.get('review_id', ''))
    with accounting.connect() as db:
        init_db(db)
        row = db.execute('SELECT * FROM order_intents WHERE id=? AND account=?', (identity, account)).fetchone()
    if not row or row[2] != session_hash(session):
        raise ValueError('This review belongs to another session. Review again.')
    if row[7] != 'review':
        return intent_view(row)  # A replay never calls the broker again.
    if time.time() - row[3] > 90:
        raise ValueError('Review expired. Review current prices and margin again.')
    assert_ready(login_ip)
    payload = json.loads(row[5])
    p, c, action = payload['params'], payload['contract'], row[4]
    if datetime.fromisoformat(c['expiry']).date() < now().date():
        raise ValueError('Contract expired.')
    if action in ('modify', 'cancel'):
        current = order_view(pending_order(client, p['order_id']))
        previous = payload['existing']
        if any(current[k] != previous[k] for k in ('quantity', 'filled', 'price', 'trigger', 'type', 'status')):
            raise ValueError('Order changed since review. Refresh and review again.')
    if action == 'exit':
        check_exit(client, payload)
    if action != 'cancel':
        margin(client, c, p)
    with accounting.connect() as db:
        init_db(db)
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT * FROM order_intents WHERE id=?', (identity,)).fetchone()
        if current[7] != 'review':
            return intent_view(current)
        others = db.execute("SELECT * FROM order_intents WHERE account=? AND id<>? AND state IN ('submitting','unknown','acknowledged')", (account, identity)).fetchall()
        if any((r[7] in ('submitting', 'unknown') and (action != 'cancel' or not r[8] or row[8] == r[8]))
               or r[6] == row[6]
               or (row[8] and row[8] == r[8] and r[4] in ('modify', 'cancel')) for r in others):
            raise ValueError('An earlier request may still be active. Refresh tracking and reconcile it before another submission.')
        db.execute("UPDATE order_intents SET state='submitting', message='Dispatch started; execution not confirmed' WHERE id=?", (identity,))
    # Persist before network I/O. A crash or timeout stays uncertain; NEVER resend.
    state, order_id, message = 'unknown', row[8], 'Outcome unknown. Refresh tracking and check Kotak before creating another order. This request will not be resent.'
    try:
        if action in ('place', 'exit'):
            response = client.place_order(**p, tag='ND' + identity[:18])
        elif action == 'modify':
            response = client.modify_order(**{k: p[k] for k in ('order_id', 'price', 'order_type', 'quantity', 'validity', 'trigger_price')})
        else:
            response = client.cancel_order(order_id=p['order_id'])
        # Even a broker/transport error is treated as ambiguous until reconciled.
        response = obj(response)
        returned_id = str(response.get('nOrdNo') or '')
        if str(response.get('stat', '')).lower() == 'ok' and (returned_id or order_id):
            order_id = returned_id or order_id
            state, message = 'acknowledged', 'Request acknowledged by Kotak; execution is not confirmed. Refresh tracking.'
    except Exception:
        pass
    with accounting.connect() as db:
        db.execute('UPDATE order_intents SET state=?,order_id=?,message=? WHERE id=?', (state, order_id, message, identity))
        return intent_view(db.execute('SELECT * FROM order_intents WHERE id=?', (identity,)).fetchone())


def tracking(client, account):
    rows = book(client)
    with accounting.connect() as db:
        init_db(db)
        intents = db.execute("SELECT * FROM order_intents WHERE account=? AND state<>'review' ORDER BY created DESC", (account,)).fetchall()
        for intent in intents:
            matches = [r for r in rows if (intent[8] and str(r.get('nOrdNo')) == intent[8]) or (intent[4] in ('place', 'exit') and r.get('GuiOrdId') == 'ND' + intent[0][:18])]
            if len(matches) != 1:
                continue
            r = matches[0]
            view = order_view(r)
            state = None
            if intent[4] in ('place', 'exit'):
                state = 'resolved' if view['status'] in TERMINAL else 'acknowledged'
            elif view['status'] in TERMINAL:
                state = 'resolved'
            elif intent[4] == 'modify':
                p = json.loads(intent[5])['params']
                if number(view['price']) == number(p['price']) and number(view['quantity']) == number(p['quantity']) and view['type'] == p['order_type'] and number(view['trigger']) == number(p['trigger_price']):
                    state = 'resolved'
            if state:
                db.execute('UPDATE order_intents SET state=?,order_id=?,message=? WHERE id=?',
                    (state, view['id'], 'Broker order status: ' + view['status'] + '; filled units: ' + str(view['filled']), intent[0]))
        intents = db.execute("SELECT * FROM order_intents WHERE account=? AND state<>'review' ORDER BY created DESC LIMIT 50", (account,)).fetchall()
    try:
        positions, error = position_views(client), None
    except Exception:
        positions, error = [], 'Positions unavailable; exit controls disabled.'
    return {'orders': [order_view(r) for r in rows], 'intents': [intent_view(r) for r in intents],
            'positions': positions, 'positions_error': error, 'fetched_at': now().isoformat(timespec='seconds')}
