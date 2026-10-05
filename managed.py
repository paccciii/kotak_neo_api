"""Explicitly armed, single-exit DAY strategies. Broker mutations are journalled once.

All calls run on the HTTP server's single owner thread. The browser only configures
and observes; closing it does not stop an armed strategy. Never retry a mutation.
"""
import hashlib
import json
import secrets
import time
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP

import accounting
import trading
from portfolio import IST, broker_time, number, records

POLL_SECONDS = 5
MAX_QUOTE_AGE = 15
ACK_SECONDS = 20
NOTICE = ('Arming authorizes the reviewed entry, cancellation of its unfilled remainder, '
          'one opposite-side stop-limit, and bounded changes to that same exit for target/trailing. '
          'Target uses a fresh executable bid/ask with the reviewed limit offset; it is not a guaranteed price. '
          'Partial entry fills are unprotected until entry cancellation is verified and the exit is accepted. '
          'Gaps or illiquidity can leave a limit exit unfilled. Management requires this server, '
          'a valid session and fresh quotes. DAY orders expire today. Disarm stops management; '
          'it does not cancel broker orders or flatten positions. Manage those directly in Kotak.')


class ReadBroker:
    """Keep SDK exception text (which may contain private response data) out of UI/state."""
    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        if name not in ('quotes', 'margin_required', 'order_report', 'positions', 'search_scrip'):
            raise AttributeError(name)
        def call(*args, **kwargs):
            try:
                return getattr(self.client, name)(*args, **kwargs)
            except Exception:
                raise RuntimeError('Broker data unavailable.') from None
        return call


def init_db(db):
    trading.init_db(db)
    db.execute('''CREATE TABLE IF NOT EXISTS managed_strategies (
        id TEXT PRIMARY KEY, account TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS managed_reviews (
        id TEXT PRIMARY KEY, account TEXT NOT NULL, session TEXT NOT NULL,
        created REAL NOT NULL, strategy_id TEXT NOT NULL, version INTEGER NOT NULL)''')


def save(db, s):
    s['version'] += 1
    db.execute('INSERT OR REPLACE INTO managed_strategies VALUES (?,?,?)',
               (s['id'], s['account'], json.dumps(s)))


def normalize_strategy(s):
    """Keep already-running absolute-price strategies operable after policy upgrade."""
    policy = s.get('policy') or {}
    s.setdefault('target', policy.get('target'))
    s.setdefault('stop', policy.get('stop'))
    s.setdefault('entry_average', None)
    s.setdefault('exit_anchor', s.get('entry_average'))
    return s


def load(identity, account):
    with accounting.connect() as db:
        init_db(db)
        row = db.execute('SELECT payload FROM managed_strategies WHERE id=? AND account=?',
                         (identity, account)).fetchone()
    if not row:
        raise ValueError('Strategy unavailable for this account.')
    return normalize_strategy(json.loads(row[0]))


def strategies(account):
    with accounting.connect() as db:
        init_db(db)
        return [normalize_strategy(json.loads(r[0])) for r in db.execute(
            'SELECT payload FROM managed_strategies WHERE account=? ORDER BY rowid DESC', (account,))]


def view(s):
    return {k: s.get(k) for k in ('id', 'phase', 'armed', 'message', 'contract', 'params',
            'policy', 'entry_id', 'exit_id', 'entry_filled', 'exit_filled', 'entry_average', 'exit_anchor', 'target', 'stop', 'best',
            'pending', 'day', 'version', 'created', 'last_checked')}


def status(account):
    return {'strategies': [view(s) for s in strategies(account)], 'notice': NOTICE,
            'poll_seconds': POLL_SECONDS}


def same_contract(r, c, product):
    return r.get('exSeg') == c['segment'] and r.get('trdSym') == c['symbol'] and r.get('prod') == product


def quantity(value, label):
    n = number(value)
    if n is None or n < 0 or n != int(n):
        raise ValueError(label + ' is unknown or invalid; reconcile in Kotak.')
    return int(n)


def reports(client, s):
    c, p = s['contract'], s['params']
    safe = ReadBroker(client)
    orders = records(safe.order_report())
    matches = [r for r in records(safe.positions()) if same_contract(r, c, p['product'])]
    if len(matches) > 1:
        raise ValueError('Multiple position rows; reconcile in Kotak.')
    net = 0
    if matches:
        r = matches[0]
        n = number(r.get('netQty'))
        if str(r.get('tok')) != c['token'] or n is None or n != int(n):
            raise ValueError('Position quantity or contract identity is unknown.')
        net = int(n)
    return orders, net


def idle_contract(client, account, c, p, exclude=None):
    for other in strategies(account):
        if other['id'] != exclude and other['phase'] not in ('review', 'complete', 'disarmed') and other['contract']['id'] == c['id']:
            raise ValueError('An existing strategy owns this contract. Reconcile and disarm it first.')
        if other['id'] != exclude and other['armed']:
            raise ValueError('Only one strategy may be armed per account at a time in this version.')
    rows, net = reports(client, {'contract': c, 'params': p})
    with accounting.connect() as db:
        init_db(db)
        intents = db.execute("SELECT * FROM order_intents WHERE account=? AND state IN ('acknowledged','unknown','submitting')", (account,)).fetchall()
    for intent in intents:
        payload = json.loads(intent['payload'])
        if payload['contract']['id'] == c['id'] and not any(
                str(r.get('nOrdNo')) == intent['order_id'] or r.get('GuiOrdId') == 'ND' + intent['id'][:18] for r in rows):
            raise ValueError('An earlier request for this contract is missing from broker reports. Reconcile it before a new entry.')
    if net or any(same_contract(r, c, p['product']) and str(r.get('ordSt', '')).lower() not in trading.TERMINAL for r in rows):
        raise ValueError('Managed entry requires a flat position and no outstanding orders for this contract/product.')


def fresh_quote(client, c):
    q = trading.quote(ReadBroker(client), c)
    raw = q['broker_timestamp']
    n = number(raw)
    if n is not None:
        stamp = float(n / 1000 if n > 100000000000 else n)
    else:
        parsed = broker_time(raw)
        if parsed is None:
            raise ValueError('A broker quote timestamp is required for management.')
        stamp = datetime.fromisoformat(parsed).timestamp()
    age = trading.now().timestamp() - stamp
    if not -2 <= age <= MAX_QUOTE_AGE:
        raise ValueError('Broker quote is stale or in the future. Management suspended.')
    for key in ('ltp', 'bid', 'ask'):
        q[key] = number(q[key])
        if q[key] is None or q[key] <= 0:
            raise ValueError('Fresh LTP, bid and ask are required for management.')
        if q[key] % Decimal(c['tick']):
            raise ValueError('Quote prices do not match the contract tick size.')
    if q['bid'] > q['ask']:
        raise ValueError('Crossed quote; management suspended.')
    return q


def allowed_day(s):
    today = trading.now()
    if today.date().isoformat() != s['day'] or s['contract']['expiry'] < s['day']:
        raise ValueError('DAY strategy expired. Reconcile orders and positions directly in Kotak.')
    if today.weekday() > 4 or not (9, 15) <= (today.hour, today.minute) < (15, 25):
        raise ValueError('Management is limited to 09:15–15:25 IST on weekdays; verify exchange holidays separately.')


def preview(client, account, session, body, c):
    if body.get('action', 'place') != 'place':
        raise ValueError('Managed strategies start with a new entry.')
    if c['freeze'] <= 0:
        raise ValueError('A verified freeze quantity is required for managed entry.')
    entry = trading.preview(ReadBroker(client), account, session, body, c)
    p = entry['params']
    idle_contract(client, account, c, p)
    policy = {}
    for field in ('distance', 'offset'):
        policy[field] = trading.price_value(body.get(field), c['tick'], field.title())
    trail = body.get('trail', '0')
    policy['trail'] = '0' if number(trail) == 0 else trading.price_value(trail, c['tick'], 'Trailing distance')
    s = dict(id=entry['review_id'], account=account, session=trading.session_hash(session),
             created=time.time(), day=trading.now().date().isoformat(), version=0,
             phase='review', armed=False, contract=c, params=p, policy=policy,
             entry_average=None, exit_anchor=None, target=None, stop=None, best=None, entry_id=None, exit_id=None,
             entry_filled=0, exit_filled=0, pending=None, last_checked=None,
             message='Review ready; nothing has been submitted.')
    allowed_day(s)
    fresh_quote(client, c)
    with accounting.connect() as db:
        init_db(db)
        # A managed review cannot be confirmed through the manual endpoint.
        db.execute("UPDATE order_intents SET session=? WHERE id=?", ('managed-review', s['id']))
        save(db, s)
    return {**entry, 'managed': True, 'policy': policy, 'notice': NOTICE, 'strategy': view(s)}


def unresolved(db, account):
    return db.execute("SELECT id FROM order_intents WHERE account=? AND state IN ('unknown','submitting')", (account,)).fetchall()


def arm(client, account, session, body, login_ip, authorized):
    if body.get('confirmed') is not True or body.get('automation_confirmed') is not True:
        raise ValueError('Explicitly confirm the live entry and automatic exit policy.')
    s = load(str(body.get('review_id', '')), account)
    if s['session'] != trading.session_hash(session):
        raise ValueError('Review belongs to another session.')
    if 'distance' not in s['policy']:
        raise ValueError('This strategy review predates the equal-distance exit setting. Review the order again.')
    if s['phase'] != 'review':
        return view(s)  # Repeated clicks never re-arm or resend.
    if time.time() - s['created'] > 90:
        raise ValueError('Strategy review expired. Review again.')
    allowed_day(s)
    trading.assert_ready(login_ip)
    fresh_quote(client, s['contract'])
    idle_contract(client, account, s['contract'], s['params'], s['id'])
    trading.margin(ReadBroker(client), s['contract'], s['params'])
    with accounting.connect() as db:
        init_db(db)
        db.execute('BEGIN IMMEDIATE')
        if unresolved(db, account):
            raise ValueError('Resolve earlier uncertain requests before arming.')
        entry = db.execute('SELECT payload FROM order_intents WHERE id=?', (s['id'],)).fetchone()
        if json.loads(entry[0])['fees']['total'] is None:
            raise ValueError('Managed live entry is blocked until fees are verified for this exchange/date.')
        s.update(phase='entry', armed=True, message='Armed; entry awaiting dispatch.')
        save(db, s)
    authorized.add(s['id'])
    return view(s)


def suspend(s, message):
    s.update(armed=False, message=message)
    with accounting.connect() as db:
        save(db, s)


def disarm(account, identity, authorized):
    s = load(identity, account)
    authorized.discard(identity)
    s['phase_before_disarm'] = s['phase']
    s['phase'] = 'disarmed'
    suspend(s, 'Disarmed. Broker orders remain active; this does not close the position. Manage them in Kotak or through reviewed manual controls.')
    return view(s)


def pause_account(account, authorized, message):
    if account:
        for s in strategies(account):
            if s['armed']:
                suspend(s, message)
    authorized.clear()


def manual_guard(account, c):
    if any(s['contract']['id'] == c['id'] and s['phase'] not in ('review', 'complete', 'disarmed') for s in strategies(account)):
        raise ValueError('This contract belongs to a managed strategy. Disarm it before manual changes; reconcile any uncertain request first.')


def _order(rows, identity):
    matches = [r for r in rows if str(r.get('nOrdNo')) == identity]
    if len(matches) != 1:
        raise ValueError('Managed broker order missing or duplicated; reconcile in Kotak.')
    return matches[0]


def validate_order(r, s, p):
    c = s['contract']
    if not same_contract(r, c, p['product']) or str(r.get('tok')) != c['token'] or r.get('trnsTp') != p['transaction_type']:
        raise ValueError('Broker order identity changed; reconcile in Kotak.')
    qty, filled = quantity(r.get('qty'), 'Order quantity'), quantity(r.get('fldQty'), 'Filled quantity')
    status = str(r.get('ordSt', '')).lower()
    if qty != int(p['quantity']) or filled > qty or status not in trading.PENDING | trading.TERMINAL:
        raise ValueError('Broker quantity/status changed or is unknown; reconcile in Kotak.')
    if status in ('complete', 'completed', 'traded') and filled != qty:
        raise ValueError('Completion without matching fills; reconcile in Kotak.')
    return filled, status


def matches_params(r, p):
    return (number(r.get('prc')) == number(p['price']) and
            number(r.get('trgPrc')) == number(p['trigger_price']) and r.get('prcTp') == p['order_type'])


def reconcile_pending(s, rows):
    """Read only. Acknowledgement alone never completes a command."""
    if not s['pending']:
        return True
    with accounting.connect() as db:
        intent = db.execute('SELECT * FROM order_intents WHERE id=?', (s['pending'],)).fetchone()
    payload = json.loads(intent['payload'])
    p, kind = payload['params'], payload['kind']
    matches = [r for r in rows if (intent['order_id'] and str(r.get('nOrdNo')) == intent['order_id']) or
               (intent['action'] in ('place', 'exit') and r.get('GuiOrdId') == 'ND' + intent['id'][:18])]
    if len(matches) > 1:
        raise ValueError('Multiple broker matches for one managed request; reconcile in Kotak.')
    if not matches:
        return False
    r = matches[0]
    # Cancellation payload retains the full expected order for identity/fill checks.
    filled, state = validate_order(r, s, payload.get('expected', p))
    if kind == 'cancel_entry':
        if state not in trading.TERMINAL:
            return False
    elif state not in trading.TERMINAL and not matches_params(r, p):
        return False
    if kind == 'entry':
        s['entry_id'] = str(r['nOrdNo'])
    elif kind in ('protect', 'trail', 'target'):
        s['exit_id'] = str(r['nOrdNo'])
        s['exit_params'] = p
        if kind == 'trail':
            s['stop'] = p['trigger_price']
        s['phase'] = 'exiting' if kind == 'target' else 'protected'
    s['pending'] = None
    with accounting.connect() as db:
        db.execute("UPDATE order_intents SET state='resolved',order_id=?,message=? WHERE id=?",
                   (str(r['nOrdNo']), 'Managed request reconciled with broker; filled units: ' + str(filled), intent['id']))
        save(db, s)
    return True


def observe(client, s):
    rows, net = reports(client, s)
    if not reconcile_pending(s, rows):
        raise ValueError('Request outcome is not yet verified. No resend; reconcile in Kotak.')
    own = {s['entry_id'], s['exit_id']}
    for r in rows:
        if same_contract(r, s['contract'], s['params']['product']) and str(r.get('nOrdNo')) not in own and str(r.get('ordSt', '')).lower() not in trading.TERMINAL:
            raise ValueError('Another order is active on this position. Management suspended.')
    entry = _order(rows, s['entry_id']) if s['entry_id'] else None
    exit_order = _order(rows, s['exit_id']) if s['exit_id'] else None
    ef, es = validate_order(entry, s, s['params']) if entry else (0, None)
    xf, xs = validate_order(exit_order, s, s['exit_params']) if exit_order else (0, None)
    if ef < s['entry_filled'] or xf < s['exit_filled'] or xf > ef:
        raise ValueError('Fill quantities regressed or exceeded the entry. Management suspended.')
    if entry and es not in trading.TERMINAL and not matches_params(entry, s['params']):
        raise ValueError('Entry was modified outside this strategy.')
    if exit_order and xs not in trading.TERMINAL and not matches_params(exit_order, s['exit_params']):
        raise ValueError('Protective exit was modified outside this strategy.')
    expected = (ef - xf) * (1 if s['params']['transaction_type'] == 'B' else -1)
    if net != expected:
        raise ValueError('Position does not match verified strategy fills. Management suspended; reconcile manual activity or delayed reports.')
    s.update(entry_filled=ef, exit_filled=xf, last_checked=trading.now().isoformat(timespec='seconds'))
    return entry, es, exit_order, xs


def resume_preview(client, account, session, identity):
    s = load(identity, account)
    if s['armed'] or s['phase'] in ('review', 'complete', 'disarmed'):
        raise ValueError('Only a suspended strategy can be resumed.')
    allowed_day(s)
    observe(client, s)
    fresh_quote(client, s['contract'])
    with accounting.connect() as db:
        init_db(db)
        save(db, s)
        identity = secrets.token_hex(16)
        db.execute('INSERT INTO managed_reviews VALUES (?,?,?,?,?,?)',
                   (identity, account, trading.session_hash(session), time.time(), s['id'], s['version']))
    return {'review_id': identity, 'strategy': view(s), 'notice': NOTICE, 'expires_in': 90}


def resume(client, account, session, body, login_ip, authorized):
    if body.get('confirmed') is not True or body.get('automation_confirmed') is not True:
        raise ValueError('Review and explicitly confirm resuming automatic management.')
    with accounting.connect() as db:
        init_db(db)
        r = db.execute('SELECT * FROM managed_reviews WHERE id=? AND account=?', (body.get('review_id'), account)).fetchone()
    if not r or r['session'] != trading.session_hash(session) or time.time() - r['created'] > 90:
        raise ValueError('Resume review expired or belongs to another session.')
    s = load(r['strategy_id'], account)
    if s['version'] != r['version'] or s['armed'] or s['phase'] in ('complete', 'disarmed', 'review'):
        raise ValueError('Strategy changed. Review again.')
    allowed_day(s)
    if any(other['id'] != s['id'] and other['armed'] for other in strategies(account)):
        raise ValueError('Disarm the other strategy before resuming this one.')
    trading.assert_ready(login_ip)
    observe(client, s)
    fresh_quote(client, s['contract'])
    with accounting.connect() as db:
        if unresolved(db, account):
            raise ValueError('Uncertain request remains; reconcile before resuming.')
        s.update(armed=True, session=trading.session_hash(session), message='Management resumed after explicit review.')
        save(db, s)
        db.execute('DELETE FROM managed_reviews WHERE id=?', (r['id'],))
    authorized.add(s['id'])
    return view(s)


def dispatch(client, s, kind, params, login_ip, alive):
    allowed_day(s)
    trading.assert_ready(login_ip)
    if not alive():
        raise ValueError('Session expired before dispatch.')
    q = fresh_quote(client, s['contract'])
    buying = s['params']['transaction_type'] == 'S'
    executable = q['ask'] if buying else q['bid']
    if kind == 'target' and not (executable <= Decimal(s['target']) if buying else executable >= Decimal(s['target'])):
        raise ValueError('Target condition changed before dispatch; existing stop remains at broker.')
    if kind in ('protect', 'trail') and params['order_type'] == 'SL':
        trigger = Decimal(params['trigger_price'])
        if not (trigger > q['ltp'] if buying else trigger < q['ltp']):
            raise ValueError('Market crossed the proposed stop before dispatch; reconcile protection in Kotak.')
    if kind in ('protect', 'target') and params['order_type'] == 'L':
        offset = Decimal(s['policy']['offset'])
        params = {**params, 'price': str(rounded_tick(executable + offset if buying else executable - offset,
                                                     s['contract']['tick'], buying))}
        trading.price_value(params['price'], s['contract']['tick'], 'Exit limit')
    if kind != 'cancel_entry':
        trading.margin(ReadBroker(client), s['contract'], params)
    # Re-read fills, position and order identity immediately before journalling.
    entry, es, exit_order, xs = observe(client, s)
    if kind == 'entry' and entry is not None:
        raise ValueError('Entry already exists; no second entry is permitted.')
    if kind == 'protect' and (es not in trading.TERMINAL or exit_order is not None):
        raise ValueError('Entry is not terminal or an exit already exists.')
    if kind in ('trail', 'target') and (xs != 'trigger pending' or s['exit_filled']):
        raise ValueError('Exit triggered or filled before modification; reconcile broker state.')
    latest = fresh_quote(client, s['contract'])
    last_executable = latest['ask'] if buying else latest['bid']
    if kind == 'target' and not (last_executable <= Decimal(s['target']) if buying else last_executable >= Decimal(s['target'])):
        raise ValueError('Target condition changed before dispatch; existing stop remains at broker.')
    if kind in ('protect', 'trail') and params['order_type'] == 'SL' and not (
            Decimal(params['trigger_price']) > latest['ltp'] if buying else Decimal(params['trigger_price']) < latest['ltp']):
        raise ValueError('Quote crossed the protective trigger before dispatch.')
    if kind in ('protect', 'target') and params['order_type'] == 'L':
        limit, offset = Decimal(params['price']), Decimal(s['policy']['offset'])
        if (limit > last_executable + offset if buying else limit < last_executable - offset):
            raise ValueError('Quote moved beyond the reviewed limit offset before dispatch.')
    allowed_day(s)
    if not alive():
        raise ValueError('Session expired before dispatch.')
    identity = secrets.token_hex(16)
    action = {'entry': 'place', 'protect': 'exit', 'trail': 'modify', 'target': 'modify', 'cancel_entry': 'cancel'}[kind]
    payload = {'contract': s['contract'], 'params': params, 'action': action,
               'managed_id': s['id'], 'kind': kind, 'expected': s['params'] if kind == 'cancel_entry' else params}
    with accounting.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if unresolved(db, s['account']):
            raise ValueError('Another request has an uncertain outcome.')
        # A protect quantity cannot exceed the latest filled entry.
        if kind == 'protect' and int(params['quantity']) != s['entry_filled'] - s['exit_filled']:
            raise ValueError('Fill quantity changed before protection; review current broker state.')
        s.update(pending=identity, pending_since=time.time(), message='Request journalled; awaiting broker verification.')
        db.execute('INSERT INTO order_intents VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (identity, s['account'], 'managed', time.time(), action, json.dumps(payload),
                    hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest(),
                    'submitting', params.get('order_id'), 'Managed dispatch; execution not confirmed'))
        save(db, s)
    order_id, state = params.get('order_id'), 'unknown'
    try:
        if action in ('place', 'exit'):
            response = client.place_order(**params, tag='ND' + identity[:18])
        elif action == 'modify':
            response = client.modify_order(**{k: params[k] for k in ('order_id', 'price', 'order_type', 'quantity', 'validity', 'trigger_price')})
        else:
            response = client.cancel_order(order_id=params['order_id'])
        response = trading.obj(response)
        returned = str(response.get('nOrdNo') or '')
        if str(response.get('stat', '')).lower() == 'ok' and returned and (not order_id or returned == order_id):
            order_id, state = returned, 'acknowledged'
    except Exception:
        pass  # Deliberately never return raw SDK exceptions or retry a mutation.
    with accounting.connect() as db:
        db.execute('UPDATE order_intents SET state=?,order_id=?,message=? WHERE id=?',
                   (state, order_id, 'Managed request ' + state + '; awaiting broker reconciliation', identity))


def rounded_tick(value, tick, buying):
    t = Decimal(tick)
    return (value / t).to_integral_value(rounding=ROUND_CEILING if buying else ROUND_FLOOR) * t


def derive_exits(s, entry):
    """Freeze equal-distance target/stop levels from verified broker average fill."""
    avg = number(entry.get('avgPrc'))
    if avg is None or avg <= 0:
        raise ValueError('Verified entry average fill price is unavailable; manage the position in Kotak.')
    tick = Decimal(s['contract']['tick'])
    # Average fills can fall between tradable ticks when Kotak aggregates multiple fills.
    anchor = (avg / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    distance = Decimal(s['policy']['distance'])
    is_long = s['params']['transaction_type'] == 'B'
    target, stop = (anchor + distance, anchor - distance) if is_long else (anchor - distance, anchor + distance)
    if target <= 0 or stop <= 0:
        raise ValueError('The requested exit distance produces a non-positive target or stop; manage the position in Kotak.')
    offset = Decimal(s['policy']['offset'])
    if offset >= min(target, stop):
        raise ValueError('Exit limit offset must be smaller than the calculated target and stop.')
    s.update(entry_average=str(avg), exit_anchor=str(anchor), target=str(target), stop=str(stop))


def step(client, s, login_ip, alive):
    allowed_day(s)
    if s['pending']:
        rows, _ = reports(client, s)
        if not reconcile_pending(s, rows):
            if time.time() - s['pending_since'] > ACK_SECONDS:
                raise ValueError('Broker outcome unverified. Management suspended; no automatic resend. Reconcile in Kotak.')
            return
    entry, es, exit_order, xs = observe(client, s)
    if entry is None:
        dispatch(client, s, 'entry', s['params'], login_ip, alive)
        return
    if es not in trading.TERMINAL:
        if s['entry_filled']:
            dispatch(client, s, 'cancel_entry', {'order_id': s['entry_id']}, login_ip, alive)
        else:
            s['message'] = 'Entry pending at broker; no fills verified.'
        return
    if not s['entry_filled']:
        s.update(phase='complete', armed=False, message='Entry ended without a fill; no position opened.')
        return
    if s.get('target') is None or s.get('stop') is None:
        derive_exits(s, entry)
        with accounting.connect() as db:
            save(db, s)
    if exit_order and xs in trading.TERMINAL:
        if s['entry_filled'] == s['exit_filled']:
            s.update(phase='complete', armed=False, message='Exit fills and flat broker position verified.')
            return
        raise ValueError('Exit ended with remaining position. Manage it in Kotak; no replacement is sent automatically.')
    q = fresh_quote(client, s['contract'])
    buying = s['params']['transaction_type'] == 'S'
    executable = q['ask'] if buying else q['bid']
    best = number(s['best'])
    best = executable if best is None else (min(best, executable) if buying else max(best, executable))
    s['best'] = str(best)
    stop, target, offset, trail = (Decimal(s['stop']), Decimal(s['target']),
                                  Decimal(s['policy']['offset']), Decimal(s['policy']['trail']))
    hit_target = executable <= target if buying else executable >= target
    hit_stop = q['ltp'] >= stop if buying else q['ltp'] <= stop
    p = {**s['params'], 'transaction_type': 'B' if buying else 'S',
         'quantity': str(s['entry_filled']), 'order_type': 'SL', 'trigger_price': str(stop),
         'price': str(stop + offset if buying else stop - offset)}
    if exit_order is None:
        # Already through an exit threshold: use one bounded limit, not an invalid SL.
        if hit_target or hit_stop:
            p.update(order_type='L', trigger_price='0', price=str(rounded_tick(
                executable + offset if buying else executable - offset, s['contract']['tick'], buying)))
        trading.price_value(p['price'], s['contract']['tick'], 'Exit limit')
        dispatch(client, s, 'protect', p, login_ip, alive)
    elif s['exit_params']['order_type'] == 'L' or xs != 'trigger pending' or s['exit_filled']:
        s['phase'] = 'exiting'
        s['message'] = 'Exit working at broker; fills pending. A limit can remain unfilled after a gap. Monitor in Kotak.'
    elif hit_target:
        p.update(order_id=s['exit_id'], order_type='L', trigger_price='0', price=str(rounded_tick(
            executable + offset if buying else executable - offset, s['contract']['tick'], buying)))
        trading.price_value(p['price'], s['contract']['tick'], 'Target exit limit')
        dispatch(client, s, 'target', p, login_ip, alive)
    elif trail and not hit_stop:
        candidate = rounded_tick(best + trail if buying else best - trail, s['contract']['tick'], buying)
        candidate = min(stop, candidate) if buying else max(stop, candidate)
        # Stop must remain on the correct side of current LTP and be strictly tighter.
        if candidate != stop and (candidate > q['ltp'] if buying else candidate < q['ltp']):
            p.update(order_id=s['exit_id'], trigger_price=str(candidate),
                     price=str(candidate + offset if buying else candidate - offset))
            trading.price_value(p['price'], s['contract']['tick'], 'Trailing limit')
            dispatch(client, s, 'trail', p, login_ip, alive)
        else:
            s['message'] = 'Protective stop verified; monitoring target and trailing distance.'
    else:
        s['message'] = 'Protective stop verified; monitoring target.'


class Manager:
    def __init__(self):
        self.authorized = set()  # Process-local approval; never silently restored from disk.
        self.next_poll = 0

    def tick(self, app):
        if time.monotonic() < self.next_poll or not app.client or not app.account:
            return
        self.next_poll = time.monotonic() + POLL_SECONDS
        alive = lambda: bool(app.client and app.session and time.monotonic() < app.expires)
        for s in strategies(app.account):
            if not s['armed']:
                continue
            if s['id'] not in self.authorized or s['session'] != trading.session_hash(app.session or '') or not alive():
                suspend(s, 'Management suspended after restart/session change. Review and resume; broker orders may remain active.')
                continue
            try:
                step(app.client, s, app.login_ip, alive)
            except ValueError as error:
                suspend(s, str(error))
                self.authorized.discard(s['id'])
            except Exception:
                suspend(s, 'Broker data unavailable. Management suspended; check active orders and protection in Kotak.')
                self.authorized.discard(s['id'])
            else:
                with accounting.connect() as db:
                    save(db, s)
