"""Mocked browser regression; no broker requests or real credentials."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.local' / 'browsers')
EMPTY_HISTORY = {'confirmed': [], 'estimates': [], 'confirmed_net': None, 'lifetime_net': None, 'coverage': 'No full account history verified.'}
SNAPSHOT = {
    'fetched_at': '2026-10-01T20:00:00+05:30', 'brokerage_per_order': '10',
    'today': {'date': '2026-10-01', 'gross': 107.25, 'net': -42.75, 'fees': {'total': 150, 'breakdown': {'brokerage': 100, 'stt': 50}, 'issues': []}, 'issues': []},
    'positions': {'rows': [{'Symbol': 'SAMPLE-FUT', 'State': 'Closed', 'P&L': 107.25}]},
    'orders': {'rows': []}, 'history': EMPTY_HISTORY,
}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1920, 'height': 1080})
    errors, login_bodies = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def route(request):
        path = request.request.url.split('http://neo.test')[-1]
        if path == '/':
            request.fulfill(body=(ROOT / 'index.html').read_text(), content_type='text/html')
        elif path in ('/app.js', '/trading.js'):
            request.fulfill(body=(ROOT / path[1:]).read_text(), content_type='text/javascript')
        elif path == '/api/trading/status':
            request.fulfill(json={'ready': False})
        elif path == '/api/trading/tracking':
            request.fulfill(json={'orders': [], 'intents': [], 'positions': [], 'fetched_at': '2026-10-01T20:00:00+05:30'})
        elif path == '/api/status':
            request.fulfill(json={'connected': False, 'saved_login': True})
        elif path == '/api/login':
            login_bodies.append(request.request.post_data_json)
            request.fulfill(json={'connected': True})
        elif path == '/api/dashboard':
            request.fulfill(json=SNAPSHOT)
        else:
            request.fulfill(status=404, json={'error': 'Mock endpoint not provided'})

    page.route('http://neo.test/**', route)
    page.goto('http://neo.test/')
    page.wait_for_function("document.getElementById('loginHelp').textContent.includes('saved login')")
    assert page.locator('#manualLogin').is_hidden()
    page.locator('[name=totp]').fill('123456')
    page.locator('#connect').click()
    page.wait_for_function("document.getElementById('netPnl').textContent.includes('42.75')")
    assert login_bodies == [{'totp': '123456', 'use_saved': True}]
    assert '107.25' in page.locator('#grossPnl').inner_text()
    assert page.locator('#historyNet').inner_text() == '—'
    assert page.locator('#lifetimeNet').inner_text() == 'Not verified'
    page.locator('#orderDate').fill('2026-09-01')
    assert '42.75' in page.locator('#netPnl').inner_text()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=str(ROOT / '.local' / 'fno-dashboard-test.png'), full_page=True)
    page.locator('#accountToggle').click()
    assert page.locator('aside').is_hidden()
    assert not errors, errors
    browser.close()
    print('Browser checks passed: TOTP-only request, separate day/history, date filters, layout, no JS errors.')
