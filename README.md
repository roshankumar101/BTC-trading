# BTC ATM Short Straddle — Bybit Demo

A learning project for monitoring and managing a BTC at-the-money short straddle with NautilusTrader, the official Bybit adapter, FastAPI, and a small read-only web dashboard.

The application is restricted to Bybit Demo. It does not configure or automatically switch to a live Bybit environment. A short straddle can lose substantially, including theoretically unlimited losses on the call side. This software is for technical demonstration, not financial advice.

## Strategy

The strategy finds a BTC Call and Put with the same strike, expiry, quote currency, and settlement currency. It chooses the nearest unexpired common expiry and the available strike closest to the live BTC index price.

When `STRATEGY_ENABLED=true`, the automatic strategy submits one sell limit IOC order per leg using current bid quotes. Automatic entry remains disabled by default. With automatic entry disabled, the dashboard also provides manually confirmed Demo market or limit IOC orders for one selected Call, Put, or matching pair. All manual requests are validated and submitted by the existing NautilusTrader strategy; the browser never talks to Bybit.

The engine tracks exchange acknowledgements, fills, and position events. An unbalanced two-leg entry triggers reduce-only recovery orders for actual filled exposure. Manual and automatic exits share the same per-leg pending-order guard.

While open, the software monitors the combined value of the remaining short legs using current ask quotes, actual position quantities, and instrument multipliers:

- Remaining entry basis is the average actual sell-fill price multiplied by the confirmed remaining quantity and contract multiplier, summed across open legs.
- Stop-loss: combined ask-side buyback value is at least remaining entry basis × 1.01.
- Take-profit: combined ask-side buyback value is at most remaining entry basis × 0.98.
- Risk is evaluated by the strategy on engine market-data events, not by dashboard polling. Missing or stale quotes suppress both P&L estimates and risk triggers.

These are software-managed exits, not exchange-hosted stops. They cannot protect a position while the application or its market-data connection is unavailable. Displayed P&L is gross price P&L and does not subtract commissions.

## Technology

- Python 3.13
- NautilusTrader 1.231.0
- Official NautilusTrader Bybit adapter
- Bybit Demo environment
- FastAPI and Uvicorn
- HTML, CSS, and vanilla JavaScript
- `python-dotenv`

The stable 1.x package is used because the latest 2.x documentation describes a different, pre-release API. Bybit option product types, Demo configuration, option quote/index subscriptions, IOC limit orders, and the installed interfaces were checked against the installed 1.231.0 package. No trading behavior or exchange connection was tested by this implementation.

## Project layout

```text
config/
  settings.py              Environment parsing and Demo-only configuration
dashboard/
  app.py                   FastAPI state and local manual-command API
  index.html               Dashboard structure
  style.css                Responsive light styling
  script.js                Periodic API refresh and rendering
services/
  instrument_selector.py   BTC option pair and ATM selection
  risk_manager.py          Combined premium and P&L calculation
  runtime_state.py         Thread-safe engine state for API reads
  trading_runtime.py       NautilusTrader node lifecycle and Bybit wiring
strategy/
  straddle.py              Order, position, reconciliation, and exit lifecycle
main.py                    Application entry point
requirements.txt
.env.example
.gitignore
PROJECT_TRACKER.md
```

## Prerequisites

- A 64-bit Python 3.13 installation and the existing `.venv`.
- A Bybit account with a separate Demo Trading API key and secret.
- Bybit Unified Trading Account access for options.

The current NautilusTrader installation guide lists Windows Server 2022 and later, not Windows 11, as its officially supported Windows platform. The project's current Windows 11 environment accepted the Python 3.13 wheel, but NautilusTrader does not list Windows 11 as an officially supported platform.

## Install

From the project directory in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The existing virtual environment was retained. `requirements.txt` selects the verified stable NautilusTrader release and leaves compatible web dependencies unpinned.

## Configure Bybit Demo

1. Switch the Bybit account interface to Demo Trading.
2. Create the API key and secret from the Demo Trading account, not from a live API page or the separate Bybit testnet.
3. Copy `.env.example` to `.env`.
4. Put the Demo credentials in `BYBIT_DEMO_API_KEY` and `BYBIT_DEMO_API_SECRET`.
5. Keep `TRADING_ENV=demo`. The application rejects other values.
6. Leave `STRATEGY_ENABLED=false` for data monitoring without new entries. Existing reconciled positions continue to be monitored for software SL/TP regardless of this flag. Enabling it permits automatic Demo order entry once fresh contracts and quotes are available.

The credential values are never sent to the dashboard or included in application status responses. `.env` is ignored by Git.

## Start

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

Open `http://127.0.0.1:8000` (or the configured `API_HOST` and `API_PORT`). With no Demo credentials, the dashboard still starts but reports that exchange data is unavailable. With credentials configured, the node connects to Bybit Demo even when strategy entry is disabled.

## Dashboard and API

The dashboard displays live BTC index data, expiry/strike selectors, matching Call/Put symbols and quotes, order states, fills, positions, risk values, and active settings. It refreshes one coherent dashboard snapshot from FastAPI every two seconds and skips a refresh if the previous request is still in flight. Data-client and execution-client connectivity are reported separately from BTC reference-data freshness; stale BTC prices are withheld and previous dashboard values are visibly marked stale if refresh fails. Mutating selection and order endpoints accept local loopback requests only.

Manual trading controls expose one `Open Short Straddle` action and explicit Call, Put, and Both closing actions. Opening requires fresh BTC and option quotes, confirmed `TRADING` status for both selected instruments, valid quantity, and matching contracts. The Call and Put orders are submitted and tracked independently; the strategy reports `PARTIALLY_OPEN` unless both legs reach their requested fill quantities. It does not automatically submit recovery orders for incomplete entry batches. Limit prices are validated against each instrument's tick size. Market and limit orders use IOC; closing actions use actual remaining position quantities and reduce-only buys. The dashboard asks for confirmation before submission and does not present submission as a fill confirmation.

Available endpoints:

- `GET /api/health`
- `GET /api/strategy/status`
- `GET /api/market/btc`
- `GET /api/market/contracts`
- `GET /api/strategy/instruments`
- `GET /api/positions`
- `GET /api/orders`
- `GET /api/trades`
- `GET /api/pnl`
- `GET /api/config`
- `GET /api/dashboard`
- `POST /api/market/selection`
- `POST /api/trading/order`

No example market prices, strikes, order fills, or P&L values are fabricated. Empty values mean the engine has not received that data.

## Restart and recovery

NautilusTrader's live execution engine reconciliation is enabled by its default configuration. Before the strategy begins, it checks the exchange-reconciled cache. It resumes risk monitoring only when it finds exactly one matching short Call and Put with no pending BTC option orders. If open orders, unmatched positions, or ambiguous state are found, the strategy fails closed and reports `RECONCILIATION_REQUIRED`; it does not blindly recreate orders.

The application does not keep a local database or persist strategy history. Order and fill history in the dashboard is in-memory, retains up to 500 records per list, and starts empty after restart; active positions are reconciled from Bybit.

## Safety and known limitations

- Bybit Demo only; no live environment is configurable.
- A straddle is not atomic. A one-leg fill or underfill is reported as `PARTIALLY_OPEN` with the actual exposure displayed; no automatic recovery order is submitted. Existing configured SL/TP risk checks remain active. Use the confirmed close controls to manage the resulting exposure.
- An uncertain order-submission outcome is reported as an error and pauses further trading until exchange state is reconciled; do not retry based only on the API response.
- Manual trading controls are local-loopback only and stay unavailable when automatic entries are enabled.
- The Bybit adapter seeds its private instrument-status cache and emits subscribed status events only when a later poll detects a change. `UNKNOWN` is shown as unknown and blocks new entries until a confirmed `TRADING` status is available; this may prevent entry until the adapter reports a status update.
- Recovery exits use current ask-side quotes and IOC limits. After three confirmed unsuccessful exit attempts, the strategy stops retrying and reports that manual action is required.
- Software SL/TP is unavailable during process, connection, or data-feed outages; Bybit options do not support the conditional order types needed for native stop orders through this adapter.
- An unfamiliar startup position or pending order halts trading instead of attempting an unsafe automatic interpretation. An untracked BTC option position event observed after startup also halts trading and requires account-state reconciliation.
- If an exchange does not expose a matching Call/Put or a fresh usable quote, the strategy does not enter.
- Dashboard order and execution history is process-local; only exchange positions and orders are available for startup reconciliation.
- Windows 11 is not listed as an officially supported NautilusTrader Windows platform.
- Premiums and P&L use instrument quote currency and multiplier; commission costs are not included in displayed gross P&L.

## Manual testing handoff

Safe mocked checks can be run with `python -m unittest discover -s tests -v`; these do not start the application or connect to Bybit. No simulated trade or order submission was run for this audit. Before manual Demo testing, verify execution connectivity, confirm there are no unexpected open BTC option positions or orders, keep `STRATEGY_ENABLED=false`, and inspect the dashboard for fresh BTC/option quotes and confirmed `TRADING` status for both legs. Manual actions require separate confirmation. Confirm each actual fill and resulting exchange position in Order History and Open Positions. Risk exits are software managed and are not available during application, connection, or data-feed outages.
