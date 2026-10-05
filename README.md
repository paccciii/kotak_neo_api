
# Neo Desk — personal F&O dashboard

For coding agents and future development sessions, read [`AGENTS.md`](AGENTS.md) first. It is the canonical project handoff covering architecture, safety invariants, setup, current capabilities, limitations, and next work.

Run in WSL:

```bash
cd /mnt/d/Stock_trading
.venv-web/bin/python server.py
```

Open http://localhost:18765. Ctrl+C stops the server. For a fresh installation, create `.venv-web` with `python3 -m venv .venv-web` and install `requirements.txt` first.

The top market strip displays NIFTY 50 (`nse_cm|26000`) and SENSEX (`bse_cm|1`) through Kotak's authenticated SFeed `subscribeIndices` stream, including point/percentage change, broker update time and local fetch time. The browser reads the latest received snapshot every five seconds while visible. Kotak's REST quote endpoint rejects these index tokens, so it is not used. The app never substitutes an ETF, future or option premium. If Kotak sends no snapshot outside market hours, the cards stay unavailable and say they are waiting for an index-stream update.

## Saved login

Consumer key, mobile, UCC and MPIN are saved in `.local/credentials.json`, at the user's request. This file contains plaintext credentials, is excluded from Git, and has Windows file permissions restricted to the desktop user and the editing account. It is never served by the web server. Do not share or commit the `.local` folder. TOTP must be entered each login and is never persisted. The browser receives only a saved-login boolean, not the stored values. Sessions last 30 minutes. Disconnect clears the active broker session, not the saved login. Edit/delete the credentials file locally to update/remove saved credentials.

Loopback-only, single-user application; do not expose it to the internet. Trading endpoints require the authenticated session, same-origin requests, an immutable server-side review and explicit confirmation. Automatic management runs only after a separate explicit strategy-policy confirmation.

## Manual F&O orders

Use **Manual F&O orders** to search NSE/BSE contracts by underlying, type, optional expiry and strike. The ticket includes NIFTY (NSE F&O) and SENSEX (BSE F&O) presets, while still allowing other broker-supported underlyings. Select the exact returned contract. Lot size, expiry, tick size and freeze quantity come from Kotak's scrip master; browser-supplied symbols and quantities are not trusted. This version supports regular DAY limit and stop-limit orders with NRML/MIS, subject to broker eligibility. No market orders, SL-M, AMO, baskets, automatic splitting, native bracket/OCO or automatic mutation retries.

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

## Managed entry and exits

On a new order ticket, check **Manage this new entry**, enter one target/stop distance per unit (1:1), exit limit offset and optional trailing distance (0 disables trailing), then review. The entry trigger is separate and only activates a stop-limit entry. After Kotak reports terminal entry status, verified filled quantity and a valid average fill price, the app rounds that average to the nearest tradable tick as the common reference, then calculates the target and initial stop the same distance above and below that reference (reversed for sells); it does not guess from the limit or moving quote. If the actual average is between ticks, the rounded reference means actual distances can differ by up to one tick. The resulting average, reference and target/stop appear in managed-strategy status. If Kotak does not provide a valid average fill, management suspends without placing protection; handle the position directly in Kotak. Both the live-account and automatic-policy checkboxes must be checked before **Confirm and arm strategy**. Arming schedules the entry; it does not report it as filled.

`managed.py` owns the persistent state machine. `managed_strategies` and `managed_reviews` are stored in the existing ignored SQLite database. Every automatic placement, modification and cancellation uses the existing `order_intents` journal, with the concrete server-reviewed payload and strategy ID saved before dispatch. Arming explicitly authorizes only the immutable policy's bounded actions. Session tokens and broker tokens are never persisted.

Lifecycle:

1. Start flat with no outstanding orders for this contract/product, a known freeze quantity, verified fee schedule, valid margin, fresh timestamped bid/ask/LTP and the configured IP. Only one strategy can be armed per account.
2. Submit the reviewed L/SL entry once. A partial entry causes one cancellation of its remainder. Wait for terminal broker status and reconcile the **actual** filled quantity against the net position before creating protection. Fills can still arrive during cancellation; the position is unprotected during this interval.
3. Round the verified broker average entry fill to the nearest tradable tick as the common reference, then derive and persist equal-distance target/stop levels. Actual average-fill distances may differ by up to one tick when the average is between ticks. Require positive levels and a usable offset before dispatch. Place one opposite-side protective SL order. For a long, its limit is stop minus offset; for a short, stop plus offset. If a threshold is already crossed when protection is prepared, use one bounded L exit instead, priced from executable bid/ask and offset.
4. Monitor every 5 seconds on the server even if the browser closes. Trail from the best observed executable bid (long) or ask (short), strictly tightening the stop. At the target, convert that **same exit order** to L at fresh bid minus offset (sell) or ask plus offset (buy). The target is a trigger, not a promised fill price. No independent target sibling is placed. Broker rejection of conversion suspends management and is never blindly retried.
5. Once an exit is triggered, partially filled or converted to L, keep tracking it without chasing price or sending replacement exits. Mark complete only when all exit fills and a flat position agree. A rejected/cancelled exit with remaining quantity suspends management for manual handling.

This is application-managed behavior, not exchange-native OCO or a bracket order. It avoids two independently fillable exits, but cannot enforce reduce-only execution against concurrent external-terminal activity. Keep other trading on that contract out of this workflow. Manual changes in Neo Desk require disarming first.

**Disarm** stops subsequent automatic requests; it neither cancels broker orders nor closes positions. An in-flight request can still complete. Handle any remaining orders directly in Kotak or through the reviewed manual controls. Disarm is terminal for that strategy; a suspended strategy instead offers **Review resume**, followed by explicit confirmation. Resume reconciles broker state and preserves the tightened stop; it never replays an uncertain request.

Restart, logout, the existing 30-minute session expiry, missing/stale quotes (over 15 seconds), unknown broker fields, position mismatches, other orders, rate limits and ambiguous outcomes suspend management. Known pending outcomes are observed for up to 20 seconds, then require reconciliation. Missing broker evidence can block resumption indefinitely. Never delete the journal to bypass this block. No automatic re-arming occurs after restart.

Management is restricted to the same IST date, weekdays 09:15–15:25, and the contract expiry. This is not an exchange-holiday calendar. At the cutoff, it suspends; it does **not** flatten positions or cancel pending entries. DAY orders expire and overnight positions are not automatically protected next session. Limits can remain unfilled after gaps, and target/trailing management stops if the server, session or connection fails. Monitor in Kotak when suspended.

**Current live-entry limitation:** the existing verified fee schedule only covers supported NSE F&O dates through **2026-10-02**. Managed arming fails closed for later dates and BSE charges. Preview remains available. This feature does not extend that fee schedule or claim live-broker validation. All development tests use fakes; no real order is submitted by tests.

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
PLAYWRIGHT_BROWSERS_PATH=.local/browsers .venv-web/bin/python -m playwright install chromium
.venv-web/bin/python tests/browser_check.py
.venv-web/bin/python tests/browser_trading_check.py
.venv-web/bin/python tests/browser_managed_check.py
```

Browser checks use mocked data and an isolated browser; they never log into Kotak or read real account data. Live fee estimates still require an authenticated refresh and comparison with the contract note.

# kotak_neo_api
I have used the api from the kotak neo application for trading.
8b555c31f80938aff0e2c467b5b6934083a77026
