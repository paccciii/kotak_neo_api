"""Adapters for raw Neo 3.0.7 portfolio responses; no account data is logged."""
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone, timedelta
import asyncio
import threading
import time

IST = timezone(timedelta(hours=5, minutes=30))


def broker_time(value):
    """Neo's timezone-less broker timestamps are interpreted in exchange time (IST)."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        parsed = None
        for fmt in ('%d-%b-%Y %H:%M:%S', '%d/%m/%Y %H:%M:%S', '%Y/%m/%d %H:%M:%S', '%d %b %Y %H:%M:%S'):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    return (parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)).isoformat(timespec='seconds')


def first_time(row, *keys):
    return next((parsed for key in keys if (parsed := broker_time(row.get(key)))), None)


def order_rows(response):
    result = []
    for r in records(response):
        result.append({
            'Order time (IST)': first_time(r, 'ordEntTm', 'ordDtTm'),
            'Updated (IST)': first_time(r, 'hsUpTm', 'exCfmTm', 'ordDtTm'),
            'Symbol': pick(r, 'trdSym', 'sym'), 'Product': r.get('prod'),
            'Side': {'B': 'BUY', 'S': 'SELL'}.get(r.get('trnsTp'), r.get('trnsTp')),
            'Filled units': r.get('fldQty'), 'Order units': r.get('qty'),
            'Average fill': r.get('avgPrc'), 'Status': pick(r, 'ordSt', 'stat'),
        })
    return sorted(result, key=lambda r: r['Order time (IST)'] or '', reverse=True)


class PortfolioError(ValueError):
    pass


def records(response):
    if isinstance(response, list):
        if not all(isinstance(item, dict) for item in response):
            raise PortfolioError('Unexpected portfolio entry format.')
        return response
    if not isinstance(response, dict):
        raise PortfolioError('Kotak returned an unexpected portfolio response.')
    status = str(response.get('stat', response.get('status', ''))).lower()
    code = str(response.get('StatusCode', response.get('stCode', '200')))
    if code in ('401', '403') or response.get('Error Message'):
        raise PortfolioError('Kotak session/access rejected. Disconnect and sign in again.')
    if code == '429':
        raise PortfolioError('Kotak rate limit reached. Wait before refreshing.')
    if code.isdigit() and int(code) >= 500:
        raise PortfolioError('Kotak portfolio service is unavailable (HTTP ' + code + '). Try later.')
    if any(response.get(k) for k in ('error', 'Error')) or status in ('not_ok', 'not ok', 'failed', 'error') or code not in ('200', '0'):
        raise PortfolioError('Kotak rejected this portfolio request. Check account access in Neo; this is not a zero balance.')
    # Support direct arrays and the common data/holdings envelope variants.
    for key in ('data', 'holdings'):
        if key in response and isinstance(response[key], (list, dict)):
            return records(response[key])
    raise PortfolioError('Kotak returned an unrecognized portfolio format. No balances have been assumed.')


def number(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def pick(row, *keys):
    for key in keys:
        if row.get(key) is not None and row[key] != '':
            return row[key]
    return None


def _quote_time(value):
    try:
        stamp = float(value)
        if stamp > 100000000000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, timezone.utc).astimezone(IST).isoformat(timespec='seconds')
    except (TypeError, ValueError, OverflowError):
        return broker_time(value)
class IndexFeed:
    """Background Kotak SFeed subscription for cash-index messages."""
    TOKENS = (('NIFTY 50', 'nse_cm', '26000'), ('SENSEX', 'bse_cm', '1'))

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._latest = {}
        self._error = None

    def start(self, client):
        self.stop()
        with self._lock:
            self._latest = {}
            self._error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(client,), daemon=True,
                                        name='neo-index-feed')
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)

    def _run(self, client):
        try:
            asyncio.run(self._listen(client))
        except Exception:
            with self._lock:
                self._error = 'Kotak index stream is unavailable. Reconnect or try during market hours.'

    async def _listen(self, client):
        from neo_api_client.websocket.feed import WsToken, SFeedIndex
        tokens = [WsToken(segment, token) for _, segment, token in self.TOKENS]
        async with client.create_websocket(max_reconnect_attempts=5,
                                           max_connect_retries=3) as websocket:
            await websocket.subscribe_index(tokens)
            iterator = websocket.__aiter__()
            while not self._stop.is_set():
                try:
                    message = await asyncio.wait_for(iterator.__anext__(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                except StopAsyncIteration:
                    break
                if not isinstance(message, SFeedIndex):
                    continue
                key = (message.exchange_segment, str(message.instrument_token))
                with self._lock:
                    self._latest[key] = {
                        'value': float(message.last_traded_price),
                        'change': float(message.change),
                        'percent': float(message.net_change_percent),
                        'broker_updated': _quote_time(message.last_trade_time),
                        'received_at': datetime.now(IST).isoformat(timespec='seconds'),
                    }
                    self._error = None

    def snapshot(self):
        with self._lock:
            latest, error = dict(self._latest), self._error
        output = []
        for name, segment, token in self.TOKENS:
            item = latest.get((segment, token))
            if item:
                output.append({'name': name, **item})
            else:
                output.append({'name': name, 'error': error or
                               'Waiting for a Kotak index-stream update; the market may be closed.'})
        return {'indices': output, 'fetched_at': datetime.now(IST).isoformat(timespec='seconds'),
                'source': 'Kotak SFeed index stream'}


def holding_rows(response):
    return [{
        'Symbol': pick(r, 'displaySymbol', 'symbol', 'trdSym'),
        'Quantity': pick(r, 'quantity'),
        'Average': pick(r, 'averagePrice'),
        'Market value': pick(r, 'mktValue'),
        'Closing price': pick(r, 'closingPrice'),
    } for r in records(response)]


def quote_key(row):
    return str(row.get('exSeg', '')), str(row.get('tok', ''))


def position_rows(client, response, holdings):
    positions = records(response)
    prices = {}
    needed = sorted({quote_key(r) for r in positions if number(r.get('ltp')) is None and all(quote_key(r))})
    for start in range(0, len(needed), 50):
        if start:
            time.sleep(.05)
        batch = needed[start:start + 50]
        try:
            quotes = records(client.quotes(instrument_tokens=[{'exchange_segment': s, 'instrument_token': t} for s, t in batch], quote_type='all'))
            for quote in quotes:
                key = str(quote.get('exchange', '')), str(quote.get('exchange_token', ''))
                prices[key] = number(quote.get('ltp'))
        except Exception:
            pass  # Keep quantities and cost information when market data fails.
    return [position_row(r, prices.get(quote_key(r)), holdings) for r in positions]


def position_row(raw, quote, holdings):
    row = {'Symbol': pick(raw, 'trdSym', 'sym'), 'Product': raw.get('prod'),
           'State': 'Unknown', 'Broker updated (IST)': first_time(raw, 'hsUpTm'),
           'Quantity (units)': None, 'Average / reference': None,
           'LTP': None, 'P&L': None, 'Basis / availability': None}
    notes = []
    ltp = number(raw.get('ltp'))
    if ltp is None:
        ltp = quote
    row['LTP'] = ltp
    quantities = [number(raw.get(k)) for k in ('cfBuyQty', 'flBuyQty', 'cfSellQty', 'flSellQty')]
    if any(v is None for v in quantities):
        row['Quantity (units)'] = number(raw.get('netQty'))
        notes.append('Quantity components unavailable; average and P&L not calculated.')
    else:
        cb, fb, cs, fs = quantities
        buyqty, sellqty = cb + fb, cs + fs
        qty = buyqty - sellqty
        row['Quantity (units)'] = qty
        row['State'] = 'Closed' if qty == 0 else 'Open'
        factors = [number(raw.get(k)) for k in ('multiplier', 'genNum', 'genDen', 'prcNum', 'prcDen')]
        if any(v is None or v <= 0 for v in factors):
            notes.append('Price scaling unavailable; average and P&L not calculated.')
        else:
            mult, gn, gd, pn, pd = factors
            scale = mult * gn / gd * pn / pd
            buy, sell = number(raw.get('buyAmt')), number(raw.get('sellAmt'))
            carrybuy = carrysell = Decimal(0)
            if cb or cs:
                if raw.get('exSeg') in ('nse_cm', 'bse_cm'):
                    match = next((h for h in holdings if str(h.get('exchangeIdentifier')) == str(raw.get('tok')) and h.get('exchangeSegment') == raw.get('exSeg')), None)
                    cost = number(match.get('averagePrice')) if match else None
                    if cost is None:
                        carrybuy = carrysell = None
                        notes.append('Carry-forward purchase cost unavailable from holdings.')
                    else:
                        carrybuy, carrysell = cb * cost * scale, cs * cost * scale
                else:
                    carrybuy, carrysell = number(raw.get('cfBuyAmt')), number(raw.get('cfSellAmt'))
                    notes.append('Carry-forward uses broker reference amounts; P&L is not lifetime profit.')
            else:
                notes.append('Reported session fills; P&L includes realized and unrealized amounts, before charges.')
            if all(v is not None for v in (buy, sell, carrybuy, carrysell)):
                buy += carrybuy
                sell += carrysell
                row['Average / reference'] = buy / (buyqty * scale) if qty > 0 else sell / (sellqty * scale) if qty < 0 else None
                if qty == 0:
                    row['P&L'] = sell - buy
                elif ltp is not None:
                    row['P&L'] = sell - buy + qty * ltp * scale
            else:
                notes.append('Required cost amounts unavailable; average and P&L not calculated.')
    if ltp is None:
        notes.append('Closed position; no price needed for realized P&L.' if row['State'] == 'Closed' else 'Last traded price unavailable from Kotak.')
    row['Basis / availability'] = ' '.join(notes)
    return {key: float(value) if isinstance(value, Decimal) else value for key, value in row.items()}


def dashboard(client):
    result = {'requested_at': datetime.now(IST).isoformat(timespec='seconds')}
    result['holdings'] = {'rows': []}  # F&O workspace does not fetch equity holdings.
    raw_positions = None
    try:
        raw_positions = {'data': [r for r in records(client.positions()) if r.get('exSeg') in ('nse_fo', 'bse_fo')]}
        result['positions'] = {'rows': position_rows(client, raw_positions, [])}
    except PortfolioError as error:
        result['positions'] = {'error': str(error)}
    except Exception:
        result['positions'] = {'error': 'Unable to reach or interpret Kotak positions. Try refreshing later.'}
    try:
        result['orders'] = {'rows': order_rows({'data': [r for r in records(client.order_report()) if r.get('exSeg') in ('nse_fo', 'bse_fo')]})}
    except PortfolioError as error:
        result['orders'] = {'error': str(error)}
    except Exception:
        result['orders'] = {'error': 'Unable to fetch Kotak order book. Try refreshing later.'}
    try:
        result['_trades'] = client.trade_report()
        result['_positions'] = raw_positions
    except Exception:
        result['_trades'] = None
        result['_positions'] = raw_positions
    result['fetched_at'] = datetime.now(IST).isoformat(timespec='seconds')
    return result
