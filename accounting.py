"""F&O daily estimates and statement-confirmed history, deliberately separate."""
import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from portfolio import IST, records, number, first_time, broker_time

STORE = Path(__file__).parent / '.local' / 'accounts.sqlite3'
CHARGES = ('brokerage', 'stt', 'exchange', 'sebi', 'stamp', 'ipft', 'gst', 'other')
CSV_FIELDS = ['date', 'gross_pnl', *CHARGES, 'contract_note', 'ledger_reference']
RATE_START = date(2026, 4, 1)
# Verified 2026-10-02. Later dates require review of the applicable schedule.
RATE_END = date(2026, 10, 2)


def rounded(value):
    return float(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def estimate_fees(trades, day, brokerage, api_orders=None):
    values = {key: Decimal(0) for key in CHARGES}
    problems, used, seen, orders = [], 0, set(), set()
    if not RATE_START <= date.fromisoformat(day) <= RATE_END:
        return {'total': None, 'breakdown': {}, 'issues': ['Charge rates need verification for this date.'], 'fills': 0}
    for r in records(trades):
        segment = r.get('exSeg')
        if segment in ('nse_cm', 'bse_cm'):
            continue
        stamp = broker_time(str(r.get('flDt', '')) + ' ' + str(r.get('flTm', ''))) or first_time(r, 'exTm')
        if stamp is None:
            problems.append('A trade has no usable execution date.')
            continue
        if stamp[:10] != day:
            continue
        if segment != 'nse_fo':
            problems.append('Only NSE equity F&O rates are verified; another segment was returned.')
            continue
        identity = (segment, str(r.get('flId', '')), str(r.get('nOrdNo', '')))
        if not all(identity):
            problems.append('A fill is missing its trade or order identifier.')
            continue
        if identity in seen:
            continue
        seen.add(identity)
        kind = 'options' if str(r.get('optTp', '')).strip() in ('CE', 'PE') else 'futures' if str(r.get('trdSym', '')).endswith('FUT') else None
        qty, price = number(r.get('fldQty')), number(r.get('avgPrc'))
        factors = [number(r.get(k)) for k in ('multiplier', 'genNum', 'genDen', 'prcNum', 'prcDen')]
        side = r.get('trnsTp')
        if kind is None or qty is None or qty <= 0 or price is None or price < 0 or side not in ('B', 'S') or any(v is None or v <= 0 for v in factors):
            problems.append('A fill has incomplete price, quantity, type or scaling fields.')
            continue
        mult, gn, gd, pn, pd = factors
        turnover = qty * price * mult * gn / gd * pn / pd
        values['stt'] += turnover * (Decimal('.0015') if kind == 'options' else Decimal('.0005')) if side == 'S' else 0
        values['stamp'] += turnover * (Decimal('.00003') if kind == 'options' else Decimal('.00002')) if side == 'B' else 0
        values['exchange'] += turnover * (Decimal('3552.99') if kind == 'options' else Decimal('182.99')) / Decimal(10000000)
        values['sebi'] += turnover * Decimal('.000001')
        values['ipft'] += turnover * Decimal('.01') / Decimal(10000000)
        orders.add((segment, str(r['nOrdNo'])))
        used += 1
    rate = number(brokerage)
    if rate is None or rate < 0:
        problems.append('Select your brokerage per executed order to complete the estimate.')
    else:
        values['brokerage'] = sum((Decimal(0) if order in (api_orders or set()) else rate for order in orders), Decimal(0))
    values['gst'] = (values['brokerage'] + values['exchange'] + values['sebi'] + values['ipft']) * Decimal('.18')
    breakdown = {k: rounded(v) for k, v in values.items()}
    return {'breakdown': breakdown, 'total': None if problems else round(sum(breakdown.values()), 2),
            'issues': sorted(set(problems)), 'fills': used, 'executed_orders': len(orders),
            'basis': 'Estimated executed-trade charges only. Excludes exercise/assignment, physical settlement, penalties and financing; contract-note rounding may differ. GST includes taxable exchange/IPFT, SEBI and brokerage.'}


def daily_estimate(position_response, position_view, trades, day, brokerage, api_orders=None):
    issues = []
    raw = records(position_response)
    for r in raw:
        stamp = first_time(r, 'hsUpTm')
        if not stamp or stamp[:10] != day:
            issues.append('Position update date is missing or differs from today; today’s gross P&L is unverified.')
    gross = Decimal(0)
    for r in position_view:
        n = number(r.get('P&L'))
        if n is None:
            issues.append('A position has unavailable P&L.')
        else:
            gross += n
    fees = estimate_fees(trades, day, brokerage, api_orders)
    if not raw and fees['fills']:
        issues.append('Trades exist but no positions were returned; gross P&L is unverified.')
    gross_value = None if issues else rounded(gross)
    return {'date': day, 'gross': gross_value, 'fees': fees,
            'net': round(gross_value - fees['total'], 2) if gross_value is not None and fees['total'] is not None else None,
            'issues': sorted(set(issues)), 'status': 'estimated',
            'basis': 'Today’s F&O broker-reference/MTM result, before income tax. Not lifetime or original-entry P&L for carried positions.'}


@contextmanager
def connect(path=None):
    target = Path(path or STORE)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS daily (account TEXT, day TEXT, payload TEXT, PRIMARY KEY(account,day))')
    db.execute('CREATE TABLE IF NOT EXISTS confirmed (account TEXT, day TEXT, payload TEXT, PRIMARY KEY(account,day))')
    db.execute('CREATE TABLE IF NOT EXISTS settings (account TEXT PRIMARY KEY, brokerage TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS coverage (account TEXT PRIMARY KEY, start TEXT, end TEXT)')
    try:
        with db:
            yield db
    finally:
        db.close()


def setting(account, value=None, save=False, path=None):
    with connect(path) as db:
        if save:
            n = number(value)
            if n is None or n < 0 or n > 10000:
                raise ValueError('Enter a valid non-negative brokerage amount.')
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (account, str(n)))
        row = db.execute('SELECT brokerage FROM settings WHERE account=?', (account,)).fetchone()
        return row['brokerage'] if row else None


def save_estimate(account, snapshot, path=None):
    with connect(path) as db:
        db.execute('INSERT OR REPLACE INTO daily VALUES (?,?,?)', (account, snapshot['date'], json.dumps(snapshot)))


def parse_statement_csv(text):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if reader.fieldnames != CSV_FIELDS:
        raise ValueError('Use the supplied CSV template with its exact column order. Raw Kotak exports need mapping first.')
    entries, seen = [], set()
    for index, row in enumerate(reader, 2):
        if index > 20000:
            raise ValueError('Import at most 19,999 daily rows at a time.')
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'Row {index}: invalid CSV columns.')
        try:
            day = date.fromisoformat(row['date'])
        except ValueError:
            raise ValueError(f'Row {index}: date must be YYYY-MM-DD.') from None
        if day > datetime.now(IST).date() or row['date'] in seen:
            raise ValueError(f'Row {index}: future or duplicate date.')
        seen.add(row['date'])
        entry = {'date': row['date']}
        for field in ('gross_pnl', *CHARGES):
            value = number(row[field])
            if value is None or abs(value) > Decimal('100000000000') or (field != 'gross_pnl' and value < 0) or value != value.quantize(Decimal('.01')):
                raise ValueError(f'Row {index}: invalid {field}; use rupees with at most two decimals.')
            entry[field] = rounded(value)
        for field in ('contract_note', 'ledger_reference'):
            if not row[field].strip() or len(row[field]) > 200:
                raise ValueError(f'Row {index}: a short {field} reference is required.')
            entry[field] = row[field].strip()
        entry['net'] = round(entry['gross_pnl'] - sum(entry[k] for k in CHARGES), 2)
        entries.append(entry)
    if not entries:
        raise ValueError('CSV contains no daily records.')
    return entries


def import_statements(account, text, replace=False, path=None):
    entries = parse_statement_csv(text)
    with connect(path) as db:
        changed = False
        for entry in entries:
            previous = db.execute('SELECT payload FROM confirmed WHERE account=? AND day=?', (account, entry['date'])).fetchone()
            if previous and json.loads(previous['payload']) != entry and not replace:
                raise ValueError('A date already has different confirmed figures. Select replacement only for a verified correction.')
            changed |= not previous or json.loads(previous['payload']) != entry
        for entry in entries:
            db.execute('INSERT OR REPLACE INTO confirmed VALUES (?,?,?)', (account, entry['date'], json.dumps(entry)))
        if changed:
            db.execute('DELETE FROM coverage WHERE account=?', (account,))
    return len(entries)


def verify_coverage(account, start, end, attested, path=None):
    if attested is not True:
        raise ValueError('Confirm that all statement periods from account opening were checked, including no-trade days.')
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        raise ValueError('Enter account opening and verified-through dates.') from None
    if first > last or last > datetime.now(IST).date():
        raise ValueError('Coverage dates are out of order or in the future.')
    with connect(path) as db:
        bounds = db.execute('SELECT MIN(day),MAX(day) FROM confirmed WHERE account=?', (account,)).fetchone()
        if bounds[0] is None:
            raise ValueError('Import and reconcile statement records first.')
        if bounds[0] < start or bounds[1] > end:
            raise ValueError('Coverage must include every imported date.')
        db.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?)', (account, start, end))


def history(account, path=None):
    with connect(path) as db:
        confirmed = [json.loads(r['payload']) for r in db.execute('SELECT payload FROM confirmed WHERE account=? ORDER BY day DESC', (account,))]
        estimates = [json.loads(r['payload']) for r in db.execute('SELECT payload FROM daily WHERE account=? ORDER BY day DESC LIMIT 90', (account,))]
        coverage = db.execute('SELECT start,end FROM coverage WHERE account=?', (account,)).fetchone()
    total = round(sum(r['net'] for r in confirmed), 2) if confirmed else None
    return {'confirmed': confirmed, 'estimates': estimates,
            'confirmed_net': total, 'lifetime_net': total if coverage else None,
            'coverage_start': coverage['start'] if coverage else None, 'coverage_end': coverage['end'] if coverage else None,
            'coverage': f"User-verified statement coverage: {coverage['start']} through {coverage['end']}. Includes confirmed daily records only; estimates are never added." if coverage else 'Full account history has not been verified. This total covers imported statement dates only; live estimates are never added.'}
