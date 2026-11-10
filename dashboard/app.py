from __future__ import annotations

import asyncio
import ipaddress
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import AsyncGenerator, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config.settings import Settings
from services.runtime_state import RuntimeState
from services.trading_runtime import TradingRuntime

_DASHBOARD_DIR = Path(__file__).resolve().parent


class ContractSelectionRequest(BaseModel):
    expiry_ns: int | None = None
    strike: Decimal | None = None
    auto_atm: bool = False


class ManualOrderRequest(BaseModel):
    action: Literal[
        "SELL_CALL",
        "SELL_PUT",
        "SELL_BOTH",
        "CLOSE_CALL",
        "CLOSE_PUT",
        "CLOSE_BOTH",
    ]
    quantity: Decimal | None = Field(default=None, gt=0)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price_call: Decimal | None = Field(default=None, gt=0)
    limit_price_put: Decimal | None = Field(default=None, gt=0)


def create_app(
    settings: Settings,
    state: RuntimeState,
    runtime: TradingRuntime,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
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
        market_fresh = _market_data_is_fresh(snapshot)
        healthy = market_fresh and snapshot["connected"]
        response.status_code = 200 if healthy else 503
        return {
            "status": "connected" if healthy else "disconnected_or_stale",
            "engine_active": snapshot["connected"],
            "market_data_fresh": market_fresh,
            "message": (
                snapshot["connection_message"]
                if snapshot["connected"] and market_fresh
                else "Trading engine is inactive"
                if not snapshot["connected"]
                else "BTC market data is unavailable or stale"
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
            "engine_active": snapshot["connected"],
            "market_data_fresh": market_fresh,
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
        market_fresh = _market_data_is_fresh(snapshot)
        return {
            "reference_price": (
                snapshot["btc_reference_price"] if market_fresh else None
            ),
            "timestamp": snapshot["btc_reference_timestamp"],
            "fresh": market_fresh,
        }

    @app.get("/api/strategy/instruments")
    async def instruments() -> dict:
        return {"selected": state.snapshot()["selected_instruments"]}

    @app.get("/api/market/contracts")
    async def available_contracts() -> dict:
        snapshot = state.snapshot()
        return {
            "items": snapshot["available_contracts"],
            "selected": snapshot["selected_instruments"],
        }

    @app.post("/api/market/selection")
    async def select_contracts(
        body: ContractSelectionRequest,
        request: Request,
    ) -> dict:
        _require_local_request(request)
        _require_engine_ready(settings, state)
        strike = None if body.auto_atm else body.strike
        if not body.auto_atm and body.expiry_ns is not None and strike is None:
            raise HTTPException(
                status_code=422,
                detail="Provide a strike or set auto_atm=true",
            )
        return await _dispatch_command(
            state,
            "select_contracts",
            {
                "expiry_ns": body.expiry_ns,
                "strike": None if strike is None else str(strike),
            },
        )

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

    @app.get("/api/dashboard")
    async def dashboard_snapshot() -> dict:
        snapshot = state.snapshot()
        market_fresh = _market_data_is_fresh(snapshot)
        return {
            "status": {
                "status": snapshot["strategy_status"],
                "connected": snapshot["connected"] and market_fresh,
                "engine_active": snapshot["connected"],
                "market_data_fresh": market_fresh,
                "message": (
                    snapshot["connection_message"]
                    if market_fresh
                    else "BTC market data is unavailable or stale; new orders are paused"
                ),
                "error": snapshot["engine_error"],
            },
            "market": {
                "reference_price": (
                    snapshot["btc_reference_price"] if market_fresh else None
                ),
                "timestamp": snapshot["btc_reference_timestamp"],
                "fresh": market_fresh,
            },
            "instruments": {"selected": snapshot["selected_instruments"]},
            "contracts": {
                "items": snapshot["available_contracts"],
                "selected": snapshot["selected_instruments"],
            },
            "positions": {"items": snapshot["positions"]},
            "orders": {"items": snapshot["orders"]},
            "trades": {"items": snapshot["trades"]},
            "pnl": snapshot["pnl"],
            "config": {
                "environment": "Demo",
                "strategy_enabled": settings.strategy_enabled,
                "quantity": str(settings.straddle_quantity),
                "stop_loss_percent": str(settings.stop_loss_percent),
                "take_profit_percent": str(settings.take_profit_percent),
                "credentials_configured": settings.credentials_configured,
            },
        }

    @app.post("/api/trading/order")
    async def manual_order(
        body: ManualOrderRequest,
        request: Request,
    ) -> dict:
        _require_local_request(request)
        _require_engine_ready(settings, state)
        is_entry = body.action.startswith("SELL_")
        if is_entry and body.quantity is None:
            raise HTTPException(status_code=422, detail="Quantity is required to open a position")
        if body.order_type == "LIMIT":
            required_prices = (
                ("CALL", "PUT")
                if body.action.endswith("BOTH")
                else (("CALL",) if body.action.endswith("CALL") else ("PUT",))
            )
            if not is_entry:
                open_kinds = {
                    item["option_type"]
                    for item in state.snapshot()["positions"]
                    if Decimal(str(item["remaining_quantity"])) > 0
                }
                required_prices = tuple(
                    kind for kind in required_prices if kind in open_kinds
                )
            price_values = {
                "CALL": body.limit_price_call,
                "PUT": body.limit_price_put,
            }
            missing = [
                kind for kind in required_prices if price_values[kind] is None
            ]
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail=f"Limit price is required for: {', '.join(missing)}",
                )
        return await _dispatch_command(
            state,
            "manual_order",
            {
                "action": body.action,
                "quantity": None if body.quantity is None else str(body.quantity),
                "order_type": body.order_type,
                "limit_prices": {
                    "CALL": (
                        None
                        if body.limit_price_call is None
                        else str(body.limit_price_call)
                    ),
                    "PUT": (
                        None
                        if body.limit_price_put is None
                        else str(body.limit_price_put)
                    ),
                },
            },
        )

    return app


def _require_local_request(request: Request) -> None:
    client = request.client
    if client is None:
        raise HTTPException(status_code=403, detail="Local requests are required")
    try:
        is_loopback = ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(status_code=403, detail="Local requests are required")


def _require_engine_ready(settings: Settings, state: RuntimeState) -> None:
    snapshot = state.snapshot()
    if not settings.credentials_configured:
        raise HTTPException(status_code=503, detail="Bybit Demo credentials are not configured")
    if not snapshot["connected"] or not _market_data_is_fresh(snapshot):
        raise HTTPException(
            status_code=503,
            detail="Bybit Demo engine and fresh market data are required",
        )


async def _dispatch_command(
    state: RuntimeState,
    action: str,
    payload: dict,
) -> dict:
    command = state.enqueue_command(action, payload)
    result = await asyncio.to_thread(command.wait, 10)
    if result is None:
        if command.cancel_if_pending():
            raise HTTPException(
                status_code=503,
                detail="Engine did not accept the request; it was cancelled",
            )
        result = await asyncio.to_thread(command.wait, 10)
    if result is None:
        raise HTTPException(
            status_code=504,
            detail=(
                "Engine command is still processing. Check order history before "
                "submitting another request."
            ),
        )
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["message"])
    return result["data"]


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
