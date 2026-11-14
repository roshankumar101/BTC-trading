# Project Tracker

## Current status

The existing Bybit Demo connection, dashboard, and native WebSocket updates are preserved. Strategy, instrument selection, order tracking, reconciliation, and risk-management code are present. A custom subclass of the installed official data client now forwards its initially seeded option trading status into NautilusTrader, fixing the identified code-path that could leave instruments permanently `UNKNOWN`. The latest observed Bybit execution error 10002 reports the local request clock about 4.8 seconds ahead; Bybit allows less than one second of future skew, so execution is not ready until the Windows clock is synchronized. This change was not tested; this assistant did not start the application or submit an order.

## Phase status

| Phase | Status | Notes |
|---|---|---|
| 0. Environment and architecture | Complete | Existing Python 3.13 `.venv` retained; stable NautilusTrader 1.231.0 and Windows wheel compatibility confirmed. |
| 1. Bybit Demo connection and market data | Implemented; execution blocked/unverified | Official adapter is configured for Demo BTC options and subscribes to index/option data. A narrow subclass forwards cached initial instrument statuses. Latest execution authentication failed with timestamp error 10002; system clock synchronization is required. |
| 2. Instrument selection | Implemented; runtime unverified | Original ATM selector retained; expiry/strike selection and Auto ATM use loaded BTC option instruments, with data subscribed only for the selected pair. |
| 3. Level 1 strategy | Issue fixed; Demo unverified | One `Open Short Straddle` action independently tracks both legs; `OPEN` requires both requested fill quantities. Incomplete fills are `PARTIALLY_OPEN` with no automatic recovery submission. |
| 4. Combined risk management | Implemented; runtime unverified | Ask-side buyback, actual position quantities, multipliers, 1% SL / 2% TP, and a guard against overlapping manual/automatic exits. |
| 5. Restart reconciliation | Implemented; runtime unverified | Nautilus execution reconciliation is enabled; one or two short positions are represented only if they map to a matching pair and there are no open BTC option orders; ambiguous state fails closed. |
| 6. FastAPI backend | Issue fixed; runtime unverified | Health and order readiness distinguish NautilusTrader data and execution connection states; uncertain submissions are returned as errors. |
| 7. Dashboard and WebSocket | Implemented; not tested in this change | Initial dashboard data is loaded over REST; state changes are pushed through `/ws/dashboard`. REST selection/trading actions remain. No periodic dashboard polling. |
| 8. Documentation | Complete | README, limitations, and manual handoff included. |
| 9. Static review | Prior baseline only | Previous Python compilation, 11 mocked safety tests, JavaScript syntax, dependency consistency, and diff checks passed before the latest WebSocket/UI changes. No tests or validation commands were run for those changes. |
| 10. Call/Put card UI refinement | Complete; statically verified | Reduced only the Call/Put symbol and quote text sizes in scoped CSS; these cards do not render expiry dates, so no date formatting was changed. `node --check` and `git diff --check` passed. |
| 11. Core functionality audit | Fixes implemented; Demo unverified | Added full-fill gating, no-silent-recovery behavior, confirmed tradability gating, order outcome reporting, and separate connection-state monitoring. |

## Completed features

- Dependencies installed into the existing virtual environment.
- `.gitignore` and `.env.example` created; `.env` remains private and ignored.
- Bybit client configs explicitly use `BybitEnvironment.DEMO` and the option product type.
- Strategy entry defaults to disabled and requires explicit Demo credentials to enable.
- API responses do not include credentials.
- Order acknowledgement, rejection, cancellation, expiry, and fill callbacks update dashboard state.
- Exchange position events update displayed quantities; unexpected non-short positions fail closed.
- Dashboard trading commands are processed on the existing NautilusTrader strategy thread, not by a second exchange connection.
- The single `Open Short Straddle` entry requires local requests, Demo credentials, fresh BTC/option data, valid quantity/price increments, a matching BTC Call/Put pair, both legs confirmed `TRADING`, and no open orders or positions.
- Call and Put orders are tracked independently. Only full requested fills on both legs produce `OPEN`; zero fills produce `ENTRY_FAILED`, and underfilled or asymmetric exposure is reported as `PARTIALLY_OPEN` without automatic recovery orders.
- Manual closes use actual remaining short position quantities and reduce-only buys.
- Unknown or non-trading instrument status blocks new entries. The dashboard does not enable entry unless both selected legs are confirmed `TRADING`.
- A Demo-only subclass of NautilusTrader 1.231.0's official Bybit data client forwards the adapter's seeded `InstrumentStatus` for subscribed contracts, avoiding the adapter's unchanged-status/no-initial-event gap without creating another Bybit connection.
- Execution-client and market-data-client connection states are sampled separately; API order readiness requires both plus fresh BTC reference data.
- Uncertain order submissions return an explicit API error and pause further trading for reconciliation instead of returning a success-shaped response.
- Startup with unresolved BTC option positions or open orders fails closed.
- Exit orders use actual outstanding quantity and are guarded against duplicate active orders.
- Duplicate commands are blocked by both UI pending state and outstanding local/exchange order tracking.
- Re-entry after a fully closed position resets the existing leg state in place while preserving the selected instrument and live quote.
- Risk state and execution history are published through a lock-protected runtime state object.
- Untracked BTC option positions observed after startup now halt trading for reconciliation instead of being silently ignored.
- Dashboard loads one atomic initial snapshot over REST and receives changed state through native FastAPI/browser WebSockets; engine activity is distinguished from fresh BTC reference data, and stale BTC/option prices are withheld.
- Order history is bounded to the most recent 500 records, matching the existing execution-history retention; order creation timestamps remain stable across status updates.
- Date-only and timestamp displays use slightly smaller, consistent typography.
- Call/Put contract symbols and quote-status lines have a scoped, slight font-size reduction; card structure and dimensions are unchanged.

## Code-verified versus manual verification

- **Prior code/static verification:** 11 safe mocked tests, Python compilation, JavaScript syntax, `pip check`, and `git diff --check` passed before the latest WebSocket/UI edits. The present WebSocket implementation and latest documentation/UI changes are not tested.
- **Requires manual Bybit Demo verification:** clock synchronization and execution authentication, reconnect behavior, fresh quotes, contract discovery, exchange reconciliation, order acknowledgements/fills/rejections/cancellations, close controls, and risk exits. The user-provided log showed market-data connectivity but an execution authentication failure; no order was submitted by this assistant.

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
- Initial instrument-status forwarding uses private members of the installed Bybit adapter (`_status_cache` and `_emit_instrument_status`) because its public subscription path does not emit the seeded status. This is scoped to the pinned NautilusTrader 1.231.0 version and must be re-reviewed if the dependency changes. Runtime behavior remains unverified; unknown status still blocks orders.
- Latest observed Bybit Demo execution error 10002: request timestamp `1794634750905` versus server timestamp `1794634746121` (4,784 ms ahead), while receive window was 5,000 ms. Official Bybit V5 authentication guidance requires `timestamp < server_time + 1000 ms`; widening `recv_window` will not correct this future-clock skew. Synchronize Windows time before a restart. Execution remains disconnected/unverified.
- `MASTER_BUILD_PROMPT.md` was requested but is not present in the repository root; the current user task and `PROJECT_TRACKER.md` were used as the available requirements.
- No order or simulated trade has been performed for the incremental features.

## Important implementation decisions

- No live trading mode or automatic environment switching is provided.
- `STRATEGY_ENABLED=false` is the default and blocks new entries; software risk management remains active for reconciled open positions.
- Automatic entries remain disabled by default. Manual entry is one IOC Market or Limit Short Straddle action; limit prices must match contract ticks. Separate Call/Put/Both close controls remain.
- Incomplete entry fills are visible partial exposure, not a fully opened straddle; recovery is manual and explicitly confirmed.
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
tests/
  test_straddle_safety.py
main.py
.env.example
.gitignore
requirements.txt
README.md
PROJECT_TRACKER.md
```

## Manual testing handoff

**Safe code validation:** `python -m unittest discover -s tests -v` (11 tests), Python compile check, `node --check dashboard/script.js`, `pip check`, and `git diff --check` passed. Pylance diagnostics were unavailable. No application startup, simulated trade, Bybit connection, or exchange order was performed. Keep `STRATEGY_ENABLED=false`; before manually checking trading features, verify Demo execution connectivity, absence of unexpected open positions/orders, fresh quotes, and confirmed `TRADING` status for both options.
