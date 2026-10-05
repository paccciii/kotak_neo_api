"""Real local HTTP routes + mocked broker; all mutations stay inside Mock objects."""
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.local' / 'browsers')
from playwright.sync_api import sync_playwright
from server import App, Handler
from test_trading import TradingTests, ORDER
from browser_fixtures import SNAPSHOT


fixture = TradingTests()
fixture.setUp()
app = App(('127.0.0.1', 0), Handler)
app.session, app.account, app.client = 'browser-test-session', 'A', fixture.client
app.expires, app.login_ip = time.monotonic() + 1800, '49.37.170.35'
thread = threading.Thread(target=app.serve_forever, daemon=True)
thread.start()
url = f'http://127.0.0.1:{app.server_port}'
try:
    with patch('server.saved_credentials', return_value={}), patch('server.dashboard', return_value=SNAPSHOT.copy()), sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1920, 'height': 1080})
        context.add_cookies([{'name': 'neo_session', 'value': app.session, 'url': url}])
        # Never allow the browser to contact any external broker or website.
        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(url + '/') else route.abort())
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url)
        page.wait_for_function("() => document.getElementById('tradeReadiness').textContent.includes('Live submission configured')")
        page.locator('#tradeFind').click()
        page.wait_for_function("() => document.getElementById('tradeContract').options.length===2")
        page.locator('#tradeContract').select_option('nse_fo:123')
        page.wait_for_function("() => document.getElementById('tradeQuote').textContent.includes('150')")
        page.locator('#tradePrice').fill('200')
        page.locator('#tradeReview').click()
        page.locator('#tradeReviewBox').wait_for(state='visible')
        assert '65' in page.locator('#tradeReviewTable').inner_text()
        assert page.locator('#tradeConfirm').is_disabled()
        fixture.client.place_order.assert_not_called()
        # Editing invalidates a reviewed payload.
        page.locator('#tradePrice').fill('201')
        assert page.locator('#tradeReviewBox').is_hidden()
        page.locator('#tradeReview').click()
        page.locator('#tradeReviewBox').wait_for(state='visible')
        page.locator('#tradeConfirmCheck').check()
        page.evaluate("document.getElementById('tradeConfirm').click();document.getElementById('tradeConfirm').click()")
        page.wait_for_function("() => document.getElementById('tradeMessage').textContent.includes('acknowledged')")
        fixture.client.place_order.assert_called_once()
        assert fixture.client.place_order.call_args.kwargs['price'] == '201'
        assert 'execution is not confirmed' in page.locator('#tradeMessage').inner_text()
        # Partial fill and a rejected order remain distinguishable.
        fixture.client.order_report.return_value = {'data': [{**ORDER, 'fldQty': 20, 'prc': '201'}, {**ORDER, 'nOrdNo': 'rejected-2', 'ordSt': 'rejected', 'rejRsn': 'Insufficient margin'}]}
        page.locator('#tradeTrack').click()
        page.wait_for_function("() => document.getElementById('tradeOrders').textContent.includes('partially filled')")
        assert 'Insufficient margin' in page.locator('#tradeOrders').inner_text()
        # Cancellation must also be reviewed, and does not claim a confirmed cancellation on acknowledgement.
        page.locator('#tradeOrders').get_by_role('button', name='Cancel', exact=True).click()
        page.locator('#tradeReviewBox').wait_for(state='visible')
        fixture.client.cancel_order.assert_not_called()
        assert 'CANCEL' in page.locator('#tradeReviewTable').inner_text()
        page.locator('#tradeConfirmCheck').check()
        page.locator('#tradeConfirm').click()
        page.wait_for_function("() => document.getElementById('tradeMessage').textContent.includes('acknowledged')")
        fixture.client.cancel_order.assert_called_once()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(ROOT / '.local' / 'manual-orders-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(ROOT / '.local' / 'manual-orders-mobile.png'), full_page=True)
        assert not errors, errors
        browser.close()
        print('Manual-order browser checks passed: exact contract, review invalidation, single dispatch, acknowledgement vs fill, partial fills, rejection, reviewed cancellation, desktop/mobile layout.')
finally:
    app.shutdown()
    app.server_close()
    thread.join()
    fixture.doCleanups()
