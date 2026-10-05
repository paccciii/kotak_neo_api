# Neo Desk canonical project handoff

This file is the canonical context for Codex and other coding agents working on this repository. Read it before changing code. Keep it updated whenever behavior, architecture, safety rules, setup, or the roadmap changes.

## Project purpose

Neo Desk is a local, single-user web application for viewing and manually trading Kotak Neo F&O products. It runs on the user's desktop, serves the UI at `http://localhost:8000`, and keeps broker credentials and account data on that computer.

The product separates two kinds of P&L:

- **Today's estimate** comes from current Kotak position and execution reports. It may include unrealized P&L and estimated fees.
- **Confirmed history** comes only from imported statements and contract-note/ledger reconciliation. Live estimates must never be added to confirmed history.

Net P&L means after trading costs and before personal income tax.

## Current implementation

- `server.py`: standard-library HTTP server, authentication/session boundary, API routing, response filtering, same-origin/loopback enforcement, and integration of portfolio, accounting, and trading modules.
- `portfolio.py`: Kotak response normalization, current positions/orders, F&O filtering, and authenticated index streaming for NIFTY 50 (`nse_cm|26000`) and SENSEX (`bse_cm|1`).
- `trading.py`: contract search, quote snapshots, margin review, immutable order reviews, explicit confirmation, placement/modification/cancellation, execution tracking, static-IP checks, position-exit preparation, and SQLite order-intent journalling.
- `accounting.py`: daily F&O fee estimates, brokerage settings, statement CSV validation/import, confirmed history, and full-history coverage attestation.
- `index.html`, `app.js`, `trading.js`: responsive local dashboard and manual order workflow.
- `tests/`: unit/integration-style tests plus isolated mocked browser checks.

Implemented user-facing behavior:

- Saved local login for consumer key, registered mobile number, client code/UCC, and MPIN; only TOTP is entered for each fresh login.
- NIFTY and SENSEX index cards use Kotak's authenticated SFeed index stream. They can remain unavailable until Kotak sends a stream update; do not substitute an ETF, future, option premium, or unrelated public quote.
- NSE NIFTY F&O and BSE SENSEX F&O contract discovery, with contract metadata sourced from Kotak's scrip master.
- Manual DAY Limit and Stop-limit orders for NRML/MIS.
- Review, explicit live-account confirmation, server-held immutable payload, idempotency protection, and broker execution tracking.
- Reviewed modification/cancellation and reviewed opposite-side position exits.
- A static public IPv4 gate for live submissions, with checks at login and submission.
- Estimated daily net P&L and fees for supported NSE F&O dates; statement-confirmed history stays separate.
- CSV statement import and account-history coverage attestation.

## Safety and correctness invariants

Preserve these rules in every change:

1. Never commit, log, render, or return credentials, MPIN, TOTP, access tokens, session tokens, GitHub tokens, or the contents of `.local/`.
2. Never save TOTP. Broker tokens remain server-side and in memory only.
3. Keep the service loopback-only. Reject untrusted Host/Origin requests and require an authenticated local session for private data and trading operations.
4. No order may be dispatched without a fresh server-side review and a separate explicit confirmation.
5. Treat broker acknowledgement as acknowledgement only. Show completion only after the broker reports actual fills.
6. Journal mutating requests before dispatch. Never automatically retry placement, modification, or cancellation after an ambiguous result.
7. Use server-sourced contract metadata and quantities. Do not trust browser-supplied symbols, lot sizes, tick sizes, freeze quantities, or reviewed payloads.
8. Do not combine today's estimate with confirmed historical P&L.
9. Fail closed when fee inputs, dates, broker fields, margin, IP readiness, or statement coverage are unknown.
10. Do not place a real broker order during development or tests. Existing tests use fakes/mocks and must remain non-trading.

## Credentials and local data

All private runtime data belongs under `.local/`, which is ignored by Git:

- `.local/credentials.json`: plaintext saved Kotak login values requested by the user. TOTP is never stored.
- `.local/trading.json`: configured static public IP and attestation state.
- `.local/accounts.sqlite3`: account-scoped settings, estimates, confirmed daily records, coverage, and order journal.
- `.local/browsers/`: optional Playwright browser installation.

Do not copy `.local/` into Git or a normal project archive. On a new computer, recreate credentials locally and transfer the database only through a private, encrypted backup if historical local records are needed.

If any secret is pasted into chat or a terminal command, treat it as exposed and advise revocation/rotation. Never place a Personal Access Token in a Git remote URL, source file, documentation, shell history, or Git configuration. Use a credential manager or GitHub CLI authentication on the target computer.

## Setup on a new computer

The established environment is Windows with the repository at `D:\\Stock_market`, run through WSL as `/mnt/d/Stock_market`.

```bash
git clone https://github.com/paccciii/kotak_neo_api.git
cd kotak_neo_api
python3 -m venv .venv-web
.venv-web/bin/python -m pip install -r requirements.txt
.venv-web/bin/python server.py
```

Open `http://localhost:8000`. Create `.local/credentials.json` through the application's login/setup flow or local setup procedure; never retrieve it from Git. Register and verify the new machine's provider-assigned static public IP in Kotak before enabling live submission.

For development checks:

```bash
.venv-web/bin/python -m pip install -r requirements-dev.txt
.venv-web/bin/python -m unittest discover -s tests -v
PLAYWRIGHT_BROWSERS_PATH=.local/browsers .venv-web/bin/python -m playwright install chromium
.venv-web/bin/python tests/browser_check.py
.venv-web/bin/python tests/browser_trading_check.py
```

## Kotak API constraints currently relied upon

- Installed/targeted Kotak Neo Python SDK version: 3.0.7.
- Current regular order types are `L`, `MKT`, `SL`, and `SL-M`.
- The current UI implements `L` and `SL`; it does not expose `MKT` or `SL-M` yet.
- Current SDK migration guidance removes legacy Cover Order and Bracket Order parameters, including native square-off and trailing-stop fields.
- A take-profit can be represented by an opposite-side Limit order.
- A stop-loss can be represented by an opposite-side `SL` or `SL-M` order.
- Linked take-profit/stop-loss (OCO) and trailing stop-loss require application-managed monitoring and order modification. They are not implemented.
- Fields accepted by a margin-calculation endpoint do not prove that the same fields are accepted for live order placement.

Primary API reference: <https://github.com/Kotak-Neo/kotak-neo-python>

## Known limitations and next work

The next likely feature is managed exits for F&O positions. Before implementation, design and test a persistent server-side state machine with these properties:

- Wait for verified entry fills and use actual filled quantity.
- Create and track profit-target and stop-loss exits without accidentally opening a reverse position.
- When one exit fills, cancel the sibling and verify cancellation/fill outcomes.
- Reconcile partial fills, app restarts, expired sessions, stale/missing quotes, market gaps, manual broker-terminal activity, rejected modifications, rate limits, network timeouts, and ambiguous broker responses.
- Persist strategy state and broker IDs before mutation, following the existing journal-first/no-blind-retry model.
- Make automation explicitly opt-in with visible armed/disarmed state and a reliable manual stop control.
- Add `SL-M` separately if desired; evaluate slippage risk and broker/exchange eligibility.
- Implement trailing stop-loss by monotonically adjusting a protective order through reviewed broker state. Never loosen the stop automatically.

Do not describe these managed exits as exchange-native OCO/bracket orders. The server must remain alive and authenticated for application-managed trailing behavior, and a crash or connectivity loss can interrupt management.

Other known limitations:

- Complete inception-to-date account history has not been automatically fetched. It requires all relevant Kotak statements/contract notes/ledger records to be imported and reconciled.
- Raw Kotak PDF/Excel statement formats are not parsed automatically.
- Fee estimates currently support a deliberately narrow verified NSE F&O schedule. BSE charges and unsupported dates fail closed and require statement confirmation.
- Kotak portfolio/report services can return temporary errors even when login succeeds.
- Index cards depend on an authenticated SFeed update and can be blank outside market hours or after a fresh server start.

## Change workflow

Before editing, inspect `git status`, this file, and the relevant tests. Make focused changes, add meaningful tests for trading/accounting/session invariants, run the unit suite, and run the relevant browser check for UI work. Update this file and `README.md` when behavior or setup changes.

Never infer authorization to send a live order from a request to build, test, preview, or explain a feature. Live trading requires the application's explicit review and confirmation flow.
