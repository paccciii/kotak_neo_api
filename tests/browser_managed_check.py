"""Managed strategy UI with a fake broker and isolated SQLite; never live trades."""
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.local' / 'browsers')
from playwright.sync_api import sync_playwright, expect
from server import App, Handler
from test_managed import ManagedTests
from browser_fixtures import SNAPSHOT

fixture = ManagedTests()
fixture.setUp()
app = App(('127.0.0.1', 0), Handler)
app.session, app.account, app.client = 'session', 'A', fixture.client
app.expires, app.login_ip = time.monotonic() + 1800, '49.37.170.35'
thread = threading.Thread(target=app.serve_forever, daemon=True)
thread.start()
url = f'http://127.0.0.1:{app.server_port}'
try:
    with patch('server.saved_credentials', return_value={}), patch('server.dashboard', return_value=SNAPSHOT.copy()), patch('managed.POLL_SECONDS', .1), sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_cookies([{'name': 'neo_session', 'value': app.session, 'url': url}])
        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(url + '/') else route.abort())
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(url)
        expect(page.locator('#tradeReadiness')).to_contain_text('Live submission configured')
        page.locator('#tradeFind').click()
        expect(page.locator('#tradeContract option')).to_have_count(2)
        page.locator('#tradeContract').select_option('nse_fo:123')
        page.locator('#tradePrice').fill('150')
        page.locator('#managedEnabled').check()
        for field, value in [('Distance', '10'), ('Offset', '1'), ('Trail', '0')]:
            page.locator('#managed' + field).fill(value)
        page.locator('#tradeReview').click()
        page.locator('#tradeReviewBox').wait_for(state='visible')
        fixture.client.place_order.assert_not_called()
        page.locator('#tradeConfirmCheck').check()
        assert page.locator('#tradeConfirm').is_disabled()
        page.locator('#managedConsent').check()
        assert page.locator('#tradeConfirm').is_enabled()
        page.locator('#managedDistance').fill('11')
        assert page.locator('#tradeReviewBox').is_hidden()
        assert not page.locator('#managedConsent').is_checked()
        page.locator('#managedDistance').fill('10')
        page.locator('#tradeReview').click()
        page.locator('#tradeReviewBox').wait_for(state='visible')
        page.locator('#tradeConfirmCheck').check()
        page.locator('#managedConsent').check()
        page.evaluate("() => {document.getElementById('tradeConfirm').click();document.getElementById('tradeConfirm').click();}")
        expect(page.locator('#managedStrategies')).to_contain_text('ARMED', timeout=15000)
        deadline = time.monotonic()+15
        while len(fixture.rows) < 1 and time.monotonic()<deadline:
            page.wait_for_timeout(100)
        fixture.client.place_order.assert_called_once()
        fixture.rows[0].update(fldQty=65, ordSt='complete', avgPrc='150')
        deadline = time.monotonic()+15
        while len(fixture.rows) < 2 and time.monotonic()<deadline:
            page.wait_for_timeout(100)
        assert len(fixture.rows)==2
        assert fixture.rows[1]['prcTp']=='SL'
        fixture.quote(171)
        expect(page.locator('#managedStrategies')).to_contain_text('exiting', timeout=15000)
        assert len(fixture.rows)==2
        assert fixture.rows[1]['prcTp']=='L'
        fixture.rows[1].update(fldQty=65, ordSt='complete')
        expect(page.locator('#managedStrategies')).to_contain_text('COMPLETE', timeout=15000)
        assert 'flat broker position verified' in page.locator('#managedStrategies').inner_text()
        # Arm a second entry and disarm using the visible stop control.
        fixture.quote(150)
        page.locator('#tradeReview').click()
        page.locator('#tradeReviewBox').wait_for(state='visible')
        page.locator('#tradeConfirmCheck').check()
        page.locator('#managedConsent').check()
        page.locator('#tradeConfirm').click()
        page.locator('#managedStrategies').get_by_role('button', name='Disarm', exact=True).wait_for()
        page.locator('#managedStrategies').get_by_role('button', name='Disarm', exact=True).click()
        expect(page.locator('#managedStrategies')).to_contain_text('DISARMED', timeout=15000)
        fixture.client.cancel_order.assert_not_called()
        for width,height,name in [(1440,1000,'desktop'),(390,844,'mobile')]:
            page.set_viewport_size({'width':width,'height':height})
            assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(ROOT/'.local'/('managed-'+name+'.png')), full_page=True)
        assert not errors, errors
        browser.close()
        print('Managed browser checks passed: separate consent, invalidation, one entry, one exit, target conversion, verified completion, disarm, desktop/mobile.')
finally:
    app.shutdown()
    app.server_close()
    thread.join()
    fixture.doCleanups()
