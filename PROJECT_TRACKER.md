# Project Tracker

## Current status

The original Bybit Demo connection and dashboard were reused. New interactive selection, manual-order, risk-display, and order-history features are implemented. Static review is complete; runtime and manual trading checks remain unverified. No order was submitted for this upgrade.

## Phase status

| Phase | Status | Notes |
|---|---|---|
| 0. Environment and architecture | Complete | Existing Python 3.13 `.venv` retained; stable NautilusTrader 1.231.0 and Windows wheel compatibility confirmed. |
| 1. Bybit Demo connection and market data | Implemented; runtime unverified | Official Bybit adapter is configured for Demo BTC options; market data subscribes to option quotes and index prices. |
| 2. Instrument selection | Implemented; runtime unverified | Original ATM selector retained; expiry/strike selection and Auto ATM use loaded BTC option instruments, with data subscribed only for the selected pair. |
| 3. Level 1 strategy | Implemented; runtime unverified | Automatic Demo IOC entries remain disabled by default; local manual Market/Limit IOC orders route through the existing NautilusTrader strategy. |
| 4. Combined risk management | Implemented; runtime unverified | Ask-side buyback, actual position quantities, multipliers, 1% SL / 2% TP, and a guard against overlapping manual/automatic exits. |
| 5. Restart reconciliation | Implemented; runtime unverified | Nautilus execution reconciliation enabled; exact matching pair can resume, ambiguous startup state fails closed. |
| 6. FastAPI backend | Implemented; runtime unverified | Read-only state routes plus loopback-only command routes enqueue work to the engine thread. |
| 7. Dashboard | Implemented; runtime unverified | Responsive HTML/CSS/JavaScript UI includes Live Market Watch, manual controls, open positions, risk, and order history. |
| 8. Documentation | Complete | README, limitations, and manual handoff included. |
| 9. Static review | Complete | Python AST parsing and safe imports, JavaScript syntax check, dependency consistency, secret-file ignore checks, and diff whitespace check passed; Pylance tooling was unavailable. No runtime or order was started. |

## Completed features

- Dependencies installed into the existing virtual environment.
- `.gitignore` and `.env.example` created; `.env` remains private and ignored.
- Bybit client configs explicitly use `BybitEnvironment.DEMO` and the option product type.
- Strategy entry defaults to disabled and requires explicit Demo credentials to enable.
- API responses do not include credentials.
- Order acknowledgement, rejection, cancellation, expiry, and fill callbacks update dashboard state.
- Exchange position events update displayed quantities; unexpected non-short positions fail closed.
- Dashboard trading commands are processed on the existing NautilusTrader strategy thread, not by a second exchange connection.
- Manual orders require local requests, Demo credentials, fresh BTC/option data, valid quantity/price increments, and no existing BTC option orders.
- `Sell Both` requires matching expiry/strike/currency contracts and attempts recovery if actual fills are unbalanced.
- Manual closes use actual remaining short position quantities and reduce-only buys.
- Explicit non-trading instrument status blocks manual orders; if no status event is available, a fresh quote and unexpired loaded instrument are still required.
- Startup with unresolved BTC option positions or open orders fails closed.
- Exit orders use actual outstanding quantity and are guarded against duplicate active orders.
- Risk state and execution history are published through a lock-protected runtime state object.
- Untracked BTC option positions observed after startup now halt trading for reconciliation instead of being silently ignored.
- Dashboard refresh uses one atomic snapshot request; engine activity is distinguished from fresh BTC reference data, and stale BTC prices are withheld.
- Order history is bounded to the most recent 500 records, matching the existing execution-history retention.
- Date-only and timestamp displays use slightly smaller, consistent typography.

## Verified technical decisions

- Existing interpreter: Python 3.13.3, 64-bit Windows.
- Stable NautilusTrader package installed: 1.231.0, CPython 3.13 Windows wheel.
- Installed adapter exports `BybitDataClientConfig`, `BybitExecClientConfig`, and live client factories.
- `BybitProductType.OPTION`, `BybitEnvironment.DEMO`, and `CryptoOption` are available in the installed package.
- The Bybit adapter supports option quote/index subscriptions and IOC limit order interfaces; the installed order factory supports market orders.
- NautilusTrader live execution configuration defaults to reconciliation enabled.
- Current `latest` NautilusTrader docs cover the 2.x API; implementation targets the installed stable 1.x API instead.
- Official Bybit Demo documentation states Demo supports market endpoints and place/cancel/open-order operations; Demo WebSocket trade is unavailable, so the adapter uses HTTP.

## Unresolved compatibility and runtime issues

- Windows 11 is not listed in NautilusTrader's official supported Windows OS table (Windows Server 2022+ is listed).
- Actual availability, permissions, and liquidity of BTC options in the user's Bybit Demo account have not been checked for this upgrade.
- The last observed Bybit Demo execution startup reported error 10010 (API key IP allowlist mismatch); verify execution connectivity after restarting with the updated Demo key/IP allowlist.
- No order or simulated trade has been performed for the incremental features.

## Important implementation decisions

- No live trading mode or automatic environment switching is provided.
- `STRATEGY_ENABLED=false` is the default and blocks new entries; software risk management remains active for reconciled open positions.
- Automatic entries remain disabled by default. Manual controls use IOC Market or Limit orders, and limit prices must match contract ticks.
- Combined risk uses confirmed remaining short quantities, average actual entry fills, quote currency, and contract multipliers.
- Stop loss triggers at combined buyback >= remaining entry basis x 1.01; take profit triggers at <= remaining entry basis x 0.98.
- Manual and automatic exits are guarded against overlap; risk evaluation pauses during a manual close batch and resumes when it completes.
- SL/TP is software-managed and cannot protect positions during process or network outages.
- Startup resumes risk monitoring only for a single matching short Call/Put pair with no pending BTC option orders; all other observed state stops new trading.
- P&L is gross and excludes commissions.

## Current file structure

```text
config/
  __init__.py
  settings.py
dashboard/
  __init__.py
  app.py
  index.html
  script.js
  style.css
services/
  __init__.py
  instrument_selector.py
  risk_manager.py
  runtime_state.py
  trading_runtime.py
strategy/
  __init__.py
  straddle.py
main.py
.env.example
.gitignore
requirements.txt
README.md
PROJECT_TRACKER.md
```

## Manual testing handoff

Static Python AST parsing and safe imports, JavaScript syntax checks, dependency consistency, secret-file ignore checks, and diff whitespace checks passed. Pylance diagnostics were unavailable in this session. No tests, simulations, application startup, exchange connection, or orders were run for this incremental upgrade. Keep `STRATEGY_ENABLED=false`, restart only after deliberately stopping any older process, verify Demo execution connectivity and absence of unexpected open positions/orders, then inspect live quotes/status before manually testing the controls.
