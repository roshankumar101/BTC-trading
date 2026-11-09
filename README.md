# BTC ATM Short Straddle — Bybit Demo

A learning project for monitoring and managing a BTC at-the-money short straddle with NautilusTrader, the official Bybit adapter, FastAPI, and a small read-only web dashboard.

The application is restricted to Bybit Demo. It does not configure or automatically switch to a live Bybit environment. A short straddle can lose substantially, including theoretically unlimited losses on the call side. This software is for technical demonstration, not financial advice.

## Strategy

The strategy finds a BTC Call and Put with the same strike, expiry, quote currency, and settlement currency. It chooses the nearest unexpired common expiry and the available strike closest to the live BTC index price.

When `STRATEGY_ENABLED=true`, it submits one sell limit IOC order per leg using current bid quotes. It tracks exchange acknowledgements and fills and does not infer a fill from an order submission. If fills cannot establish equal quantities on both legs, it attempts to buy back the actual filled exposure with reduce-only IOC limit orders. Exit orders are not duplicated while an order is active.

While open, the software monitors the combined value of the remaining short legs using current ask quotes and instrument multipliers:

- Stop-loss: buyback value reaches 1% above remaining entry cost basis.
- Take-profit: buyback value reaches 2% below remaining entry cost basis.

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
  app.py                   FastAPI read-only API
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

The dashboard displays live BTC index data, selected contracts, order states, fills, positions, combined premium, buyback value, P&L, and active settings. It is read-only and refreshes from the FastAPI endpoints every two seconds.

Available endpoints:

- `GET /api/health`
- `GET /api/strategy/status`
- `GET /api/market/btc`
- `GET /api/strategy/instruments`
- `GET /api/positions`
- `GET /api/orders`
- `GET /api/trades`
- `GET /api/pnl`
- `GET /api/config`

No example market prices, strikes, order fills, or P&L values are fabricated. Empty values mean the engine has not received that data.

## Restart and recovery

NautilusTrader's live execution engine reconciliation is enabled by its default configuration. Before the strategy begins, it checks the exchange-reconciled cache. It resumes risk monitoring only when it finds exactly one matching short Call and Put with no pending BTC option orders. If open orders, unmatched positions, or ambiguous state are found, the strategy fails closed and reports `RECONCILIATION_REQUIRED`; it does not blindly recreate orders.

The application does not keep a local database or persist strategy history. Order and fill history in the dashboard is in-memory and starts empty after restart; active positions are reconciled from Bybit.

## Safety and known limitations

- Bybit Demo only; no live environment is configurable.
- A straddle is not atomic. A one-leg fill can happen; the strategy attempts a reduce-only buyback using actual filled quantities.
- Recovery exits use current ask-side quotes and IOC limits. After three confirmed unsuccessful exit attempts, the strategy stops retrying and reports that manual action is required.
- Software SL/TP is unavailable during process, connection, or data-feed outages; Bybit options do not support the conditional order types needed for native stop orders through this adapter.
- An unfamiliar startup position or pending order halts trading instead of attempting an unsafe automatic interpretation.
- If an exchange does not expose a matching Call/Put or a fresh usable quote, the strategy does not enter.
- Dashboard order and execution history is process-local; only exchange positions and orders are available for startup reconciliation.
- Windows 11 is not listed as an officially supported NautilusTrader Windows platform.
- Premiums and P&L use instrument quote currency and multiplier; commission costs are not included in displayed gross P&L.

## Manual testing handoff

No test suite, simulated trade, exchange connection, or order submission was run for this implementation. Before enabling automatic Demo entries, manually verify the Demo credentials and account, confirm that BTC options are available and enabled for the account, start with `STRATEGY_ENABLED=false`, and inspect the dashboard for fresh quotes and the selected matching contracts. Only after separately authorizing and reviewing a Demo order test should `STRATEGY_ENABLED` be changed to `true`.
