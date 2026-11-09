from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse

from config.settings import Settings
from services.runtime_state import RuntimeState
from services.trading_runtime import TradingRuntime

_DASHBOARD_DIR = Path(__file__).resolve().parent


def create_app(
    settings: Settings,
    state: RuntimeState,
    runtime: TradingRuntime,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(title="BTC Demo Short Straddle", lifespan=lifespan)

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(_DASHBOARD_DIR / "index.html")

    @app.get("/static/style.css", include_in_schema=False)
    async def stylesheet() -> FileResponse:
        return FileResponse(_DASHBOARD_DIR / "style.css", media_type="text/css")

    @app.get("/static/script.js", include_in_schema=False)
    async def script() -> FileResponse:
        return FileResponse(
            _DASHBOARD_DIR / "script.js",
            media_type="application/javascript",
        )

    @app.get("/api/health")
    async def health(response: Response) -> dict:
        snapshot = state.snapshot()
        connected = _market_data_is_fresh(snapshot) and snapshot["connected"]
        response.status_code = 200 if connected else 503
        return {
            "status": "connected" if connected else "disconnected_or_stale",
            "message": (
                snapshot["connection_message"]
                if connected
                else "Exchange connection or BTC market data is unavailable or stale"
            ),
            "engine_error": snapshot["engine_error"],
        }

    @app.get("/api/strategy/status")
    async def strategy_status() -> dict:
        snapshot = state.snapshot()
        market_fresh = _market_data_is_fresh(snapshot)
        return {
            "status": snapshot["strategy_status"],
            "connected": snapshot["connected"] and market_fresh,
            "message": (
                snapshot["connection_message"]
                if market_fresh
                else "BTC market data is unavailable or stale; new orders are paused"
            ),
            "error": snapshot["engine_error"],
        }

    @app.get("/api/market/btc")
    async def btc_market() -> dict:
        snapshot = state.snapshot()
        return {
            "reference_price": snapshot["btc_reference_price"],
            "timestamp": snapshot["btc_reference_timestamp"],
            "fresh": _market_data_is_fresh(snapshot),
        }

    @app.get("/api/strategy/instruments")
    async def instruments() -> dict:
        return {"selected": state.snapshot()["selected_instruments"]}

    @app.get("/api/positions")
    async def positions() -> dict:
        return {"items": state.snapshot()["positions"]}

    @app.get("/api/orders")
    async def orders() -> dict:
        return {"items": state.snapshot()["orders"]}

    @app.get("/api/trades")
    async def trades() -> dict:
        return {"items": state.snapshot()["trades"]}

    @app.get("/api/pnl")
    async def pnl() -> dict:
        return state.snapshot()["pnl"]

    @app.get("/api/config")
    async def config() -> dict:
        return {
            "environment": "Demo",
            "strategy_enabled": settings.strategy_enabled,
            "quantity": str(settings.straddle_quantity),
            "stop_loss_percent": str(settings.stop_loss_percent),
            "take_profit_percent": str(settings.take_profit_percent),
            "credentials_configured": settings.credentials_configured,
        }

    return app


def _market_data_is_fresh(snapshot: dict) -> bool:
    timestamp = snapshot["btc_reference_timestamp"]
    if not timestamp:
        return False
    try:
        updated_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    return -10 <= age <= 10
