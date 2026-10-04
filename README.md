# Neo Desk — personal F&O dashboard

Run in WSL:

```bash
cd /mnt/d/Stock_market
.venv-web/bin/python server.py
```

Open http://localhost:8000. Ctrl+C stops the server. For a fresh installation, create `.venv-web` with `python3 -m venv .venv-web` and install `requirements.txt` first.

## Saved login

Consumer key, mobile, UCC and MPIN are saved in `.local/credentials.json`, at the user's request. This file contains plaintext credentials, is excluded from Git, and has Windows file permissions restricted to the desktop user and the editing account. It is never served by the web server. Do not share or commit the `.local` folder. TOTP must be entered each login and is never persisted. The browser receives only a saved-login boolean, not the stored values. Sessions last 30 minutes. Disconnect clears the active broker session, not the saved login. Edit/delete the credentials file locally to update/remove saved credentials.

Loopback-only, single-user application; do not expose it to the internet. Manual trading endpoints require the authenticated session, same-origin requests, an immutable server-side review and explicit confirmation. No automated strategy runs.

## Manual F&O orders

Use **Manual F&O orders** to search NSE/BSE contracts by underlying, type, optional expiry and strike. Select the exact returned contract. Lot size, expiry, tick size and freeze quantity come from Kotak's scrip master; browser-supplied symbols and quantities are not trusted. This version supports regular DAY limit and stop-limit orders with NRML/MIS, subject to broker eligibility. No market orders, AMO, baskets, automatic splitting, bracket/target strategy or automatic retries.

1. Search and select the contract; fetch a quote snapshot with bid/ask and broker/fetch timestamps.
2. Enter side, product, lots, limit and optional trigger. Review calls Kotak margin validation without submitting an order.
3. Check the exact contract, units, prices, margin and estimated charges. Reviews expire after 90 seconds and belong to the current session. Editing invalidates the browser review.
4. Tick the live-account confirmation and submit. The server sends only the stored reviewed payload, once. An acknowledgement is explicitly not a completed trade.
5. Execution tracking polls the order book every 5 seconds while the connected browser page is visible. It shows actual filled units, average price, broker status and rejection reason. Polling stops when the tab is hidden; broker orders remain active independently of the browser/server/session.

Pending orders have reviewed Modify/Cancel actions. Modification quantity is the TOTAL, including already filled units. Position Exit prepares an opposite-side order and requires a fresh position check, no outstanding orders on that position, a price and confirmation. It is not an exchange-enforced reduce-only order: concurrent activity in another terminal can still race with an exit. Manage outstanding orders directly in Kotak if local access, session or tracking fails.

**Live submission is locked initially.** In Static IP setup, enter the provider-assigned static public IPv4 already whitelisted in Kotak and attest those prerequisites. The server checks its outbound public address via ipify, saves `.local/trading.json`, and requires reconnection with TOTP. Login checks the address before and after authentication; each submission checks it again. These checks establish an address match, not independent proof of an ISP static-IP assignment or Kotak whitelist membership; Kotak enforces its whitelist. Reading/searching/reviewing remains available without enabling submission.

Each confirmation is journalled in `.local/accounts.sqlite3` BEFORE calling Kotak. Replaying the same confirmation never re-dispatches. Uncertain responses and crashes remain `unknown`/`submitting`; order IDs and unique `GuiOrdId` tags reconcile against the broker book. No automatic retry of placement, modification or cancellation occurs. Identical pending placements and unresolved requests block conflicting submissions. If the broker never exposes a matching record, this conservative lock remains: check directly with Kotak; do not delete the journal or blindly create replacement orders. This does not guarantee deduplication of trades manually placed outside this app.

Ticket charge estimates cover one side at the entered price, assume ₹0 API brokerage on an eligible Trade Free plan, and use the dated NSE schedule below. BSE/unsupported dates show unavailable charges, not zero. Margin availability does not guarantee execution. No real order was sent during development or testing.

API references: https://github.com/Kotak-Neo/kotak-neo-python (installed SDK 3.0.7); static-IP requirement: https://www.kotakneo.com/platform/kotak-neo-trade-api/static-ip-details/

## Separate daily and historical results

- Today's panel is for the server's current IST date. It uses dated F&O positions and execution reports. Stale/missing position dates, missing fields or unsupported fee schedules prevent an unverified net total from being shown.
- F&O positions and orders exclude equity holdings. Equity holdings are not requested.
- Day gross uses the broker's carried reference amounts (daily MTM); it is not original-entry lifetime profit. Open exposure may include unrealized gains/losses.
- Fee estimates use actual executed fills. Brokerage is per unique executed order; repeated fills are deduplicated. The user confirmed ₹10 per executed order for app trades; this is saved per account and editable. Orders identified in this app's API journal use an estimated ₹0 brokerage; all other orders use the selected rate. Other API platforms and nominal charges require statement reconciliation. Reading an app order through the API does not remove its brokerage.
- Estimates are replaced per account/date on refresh, never accumulated across refreshes.
- Confirmed history is stored separately. Live estimates are NEVER added to confirmed history. Today's confirmed statement record, if imported, appears once in history and is also displayed for comparison with today's estimate.
- Net is after trading costs and before personal income tax.
- Order date/time filters affect the order table only; they do not affect daily or historical P&L.

## Charge estimate scope and sources

Only NSE equity F&O ordinary executed trades are supported. Verified schedule: 2026-04-01 through 2026-10-02; dates outside it fail closed pending rate verification. This is intentionally not a historical fee calculator. BSE, commodity, exercise/assignment, physical settlement, financing and penalties require actual statement charges. Final contract-note rounding and brokerage variations may differ.

- STT: options sell premium 0.15%; futures sell value 0.05%.
- Exchange charges per crore, each side: options premium ₹3552.99; futures ₹182.99.
- IPFT ₹0.01/crore each side; SEBI ₹10/crore each side.
- Buy-side stamp duty: options 0.003%, futures 0.002%.
- GST estimate: 18% of brokerage, exchange, SEBI and IPFT taxable charges.
- Component totals rounded to paise; final broker rounding is confirmed via statements.

Sources checked 2026-10-02:
- https://nsearchives.nseindia.com/content/circulars/FA73061.pdf (effective March 1, 2026)
- https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax
- https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies
- https://www.kotakneo.com/calculator/brokerage-calculator/
- https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/orders/trade_report.md

## Historical statement import

Complete historical statements have NOT been fetched or imported. Obtain Kotak F&O gain/loss reports, contract notes and ledger for every financial year since opening. The current Trade API integration is not an inception-to-date archive.

Use the dashboard's CSV template. Raw Kotak Excel/PDF formats are not automatically parsed yet; map their figures to the template after examining the actual report format.

Columns:
`date,gross_pnl,brokerage,stt,exchange,sebi,stamp,ipft,gst,other,contract_note,ledger_reference`

One consolidated F&O daily row per IST date. Amounts are rupees with at most two decimal places. Gross must be BEFORE the separately entered charges. Reconcile daily F&O settlements consistently; don't include deposits/withdrawals or personal income tax. Don't count futures settlement movements twice or subtract charges already included in a net P&L figure. Attach short source references in the final two columns. Preview validates every row before writing. Confirm only after checking contract notes and ledger. Conflicting dates require explicit replacement; identical reimports are idempotent and account-scoped.

Lifetime net remains unavailable until the user verifies full statement coverage from account opening through a selected date, including no-trade periods. This is user-attested completeness, not an independent automatic audit. Coverage must contain all imported dates. Added/corrected daily records invalidate the prior coverage attestation. Confirmed net across imported dates remains visible even before full coverage.

Storage: `.local/accounts.sqlite3`, excluded from Git. It stores per-account estimates, confirmed daily statement values, source references, brokerage settings and coverage. It does not store live broker tokens or TOTP. Back up this file securely; never share credentials with backups intended for others.

## Validation

```bash
.venv-web/bin/python -m pip install -r requirements-dev.txt
.venv-web/bin/python -m unittest discover -s tests -v
PLAYWRIGHT_BROWSERS_PATH=/mnt/d/Stock_market/.local/browsers .venv-web/bin/python -m playwright install chromium
.venv-web/bin/python tests/browser_check.py
.venv-web/bin/python tests/browser_trading_check.py
```

Browser checks use mocked data and an isolated browser; they never log into Kotak or read real account data. Live fee estimates still require an authenticated refresh and comparison with the contract note.
