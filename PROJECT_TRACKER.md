# Project Tracker

## Current status

Implementation files are present. Static syntax/import checks are complete; runtime behavior and manual testing remain unverified. No exchange connection, simulated trade, or order was performed.

## Phase status

| Phase | Status | Notes |
|---|---|---|
| 0. Environment and architecture | Complete | Existing Python 3.13 `.venv` retained; stable NautilusTrader 1.231.0 and Windows wheel compatibility confirmed. |
| 1. Bybit Demo connection and market data | Implemented; runtime unverified | Official Bybit adapter is configured for Demo BTC options; market data subscribes to option quotes and index prices. |
| 2. Instrument selection | Implemented; runtime unverified | Chooses the nearest unexpired common expiry and closest strike with matching Call/Put and currencies. |
| 3. Level 1 strategy | Implemented; runtime unverified | Demo-only IOC limit entries, execution callbacks, actual fill accounting, and one-leg recovery exits. |
| 4. Combined risk management | Implemented; runtime unverified | Combined ask-side buyback cost, actual fill quantities, instrument multipliers, SL/TP, and exit tracking. |
| 5. Restart reconciliation | Implemented; runtime unverified | Nautilus execution reconciliation enabled; exact matching pair can resume, ambiguous startup state fails closed. |
| 6. FastAPI backend | Implemented; runtime unverified | Read-only endpoints expose thread-safe engine state. |
| 7. Dashboard | Implemented; runtime unverified | Responsive HTML/CSS/JavaScript UI polls the API and shows engine/exchange state. |
| 8. Documentation | Complete | README, limitations, and manual handoff included. |
| 9. Static review | Complete | Parsed and imported application modules without starting the app; JavaScript syntax check passed. No runtime or exchange testing was performed. |

## Completed features

- Dependencies installed into the existing virtual environment.
- `.gitignore` and `.env.example` created; `.env` remains private and ignored.
- Bybit client configs explicitly use `BybitEnvironment.DEMO` and the option product type.
- Strategy entry defaults to disabled and requires explicit Demo credentials to enable.
- API responses do not include credentials.
- Order acknowledgement, rejection, cancellation, expiry, and fill callbacks update dashboard state.
- Exchange position events update displayed quantities; unexpected non-short positions fail closed.
- Startup with unresolved BTC option positions or open orders fails closed.
- Exit orders use actual outstanding quantity and are guarded against duplicate active orders.
- Risk state and execution history are published through a lock-protected runtime state object.

## Verified technical decisions

- Existing interpreter: Python 3.13.3, 64-bit Windows.
- Stable NautilusTrader package installed: 1.231.0, CPython 3.13 Windows wheel.
- Installed adapter exports `BybitDataClientConfig`, `BybitExecClientConfig`, and live client factories.
- `BybitProductType.OPTION`, `BybitEnvironment.DEMO`, and `CryptoOption` are available in the installed package.
- The Bybit adapter supports option quote and index-price subscriptions and IOC limit order interfaces.
- NautilusTrader live execution configuration defaults to reconciliation enabled.
- Current `latest` NautilusTrader docs cover the 2.x API; implementation targets the installed stable 1.x API instead.
- Official Bybit Demo documentation states Demo supports market endpoints and place/cancel/open-order operations; Demo WebSocket trade is unavailable, so the adapter uses HTTP.

## Unresolved compatibility and runtime issues

- Windows 11 is not listed in NautilusTrader's official supported Windows OS table (Windows Server 2022+ is listed).
- Actual availability, permissions, and liquidity of BTC options in the user's Bybit Demo account have not been checked.
- No exchange connection or runtime behavior has been verified.
- Automatic strategy behavior must be reviewed manually before enabling Demo order entry.

## Important implementation decisions

- No live trading mode or automatic environment switching is provided.
- `STRATEGY_ENABLED=false` is the default and blocks new entries; software risk management remains active for reconciled open positions.
- Entries and exits are price-limited IOC orders; option order status/fills are taken from NautilusTrader callbacks, not assumed.
- Combined risk uses the remaining short quantities, average actual entry fills, quote currency, and instrument multipliers.
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

Python syntax parsing and application-module imports completed without starting the application; the dashboard JavaScript syntax check passed. No test suite, app runtime, exchange connection, simulation, or order was run. Keep strategy entry disabled while checking the Demo connection, index price, option pair, quotes, and dashboard. AI-assisted testing authorization status: **not authorized**.
