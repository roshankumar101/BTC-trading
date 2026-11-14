from __future__ import annotations

import asyncio
import ipaddress
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import AsyncGenerator, Literal

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config.settings import Settings
from services.dashboard_websocket import DashboardWebSocketManager
from services.runtime_state import RuntimeState
from services.trading_runtime import TradingRuntime

_DASHBOARD_DIR = Path(__file__).resolve().parent


class ContractSelectionRequest(BaseModel):
    expiry_ns: int | None = None
    strike: Decimal | None = None
    auto_atm: bool = False


class ManualOrderRequest(BaseModel):
    action: Literal[
        "OPEN_STRADDLE",
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
    websocket_manager = DashboardWebSocketManager(
        lambda: _dashboard_snapshot(state, settings),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        websocket_manager.start(state)
        runtime.start()
        try:
            yield
        finally:
            await websocket_manager.stop()
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
            "data_connected": snapshot["data_connected"],
            "execution_connected": snapshot["execution_connected"],
            "market_data_fresh": market_fresh,
            "message": _connection_message(snapshot, market_fresh),
            "strategy_message": snapshot["strategy_message"],
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
            "data_connected": snapshot["data_connected"],
            "execution_connected": snapshot["execution_connected"],
            "market_data_fresh": market_fresh,
            "message": _connection_message(snapshot, market_fresh),
            "strategy_message": snapshot["strategy_message"],
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
        return _dashboard_snapshot(state, settings)

    @app.websocket("/ws/dashboard")
    async def dashboard_websocket(websocket: WebSocket) -> None:
        if not _is_loopback_host(
            None if websocket.client is None else websocket.client.host,
        ):
            await websocket.close(code=1008, reason="Local connections are required")
            return
        try:
            await websocket_manager.connect(websocket)
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                await websocket.close(
                    code=1008,
                    reason="Dashboard WebSocket is read-only",
                )
                break
        except WebSocketDisconnect:
            pass
        finally:
            websocket_manager.disconnect(websocket)

    @app.post("/api/trading/order")
    async def manual_order(
        body: ManualOrderRequest,
        request: Request,
    ) -> dict:
        _require_local_request(request)
        _require_engine_ready(settings, state)
        is_entry = body.action == "OPEN_STRADDLE"
        if is_entry and body.quantity is None:
            raise HTTPException(status_code=422, detail="Quantity is required to open a position")
        if body.order_type == "LIMIT":
            if body.action in {"OPEN_STRADDLE", "CLOSE_BOTH"}:
                required_prices = ("CALL", "PUT")
            elif body.action == "CLOSE_CALL":
                required_prices = ("CALL",)
            else:
                required_prices = ("PUT",)
            if body.action == "CLOSE_BOTH":
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
    if client is None or not _is_loopback_host(client.host):
        raise HTTPException(status_code=403, detail="Local requests are required")


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_engine_ready(settings: Settings, state: RuntimeState) -> None:
    snapshot = state.snapshot()
    if not settings.credentials_configured:
        raise HTTPException(status_code=503, detail="Bybit Demo credentials are not configured")
    if (
        not snapshot["data_connected"]
        or not snapshot["execution_connected"]
        or not _market_data_is_fresh(snapshot)
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "Bybit Demo data and execution connections plus fresh market data "
                "are required"
            ),
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


def _connection_message(snapshot: dict, market_fresh: bool) -> str:
    if not snapshot["execution_connected"] or not snapshot["data_connected"]:
        return snapshot["connection_message"]
    if not market_fresh:
        return "BTC market data is unavailable or stale; new orders are paused"
    return snapshot["connection_message"]


def _timestamp_is_fresh(timestamp: str | None) -> bool:
    if not timestamp:
        return False
    try:
        updated_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    return -10 <= age <= 10


def _dashboard_snapshot(state: RuntimeState, settings: Settings) -> dict:
    snapshot = state.snapshot()
    market_fresh = _market_data_is_fresh(snapshot)
    selected = snapshot["selected_instruments"]
    if selected is not None:
        selected = dict(selected)
        for kind in ("call", "put"):
            quote_fresh = _timestamp_is_fresh(
                selected.get(f"{kind}_quote_timestamp"),
            )
            selected[f"{kind}_quote_fresh"] = quote_fresh
            if not quote_fresh:
                selected[f"{kind}_bid"] = None
                selected[f"{kind}_ask"] = None
    option_quotes_fresh = bool(
        selected
        and selected.get("call_quote_fresh")
        and selected.get("put_quote_fresh")
    )
    return {
        "status": {
            "status": snapshot["strategy_status"],
            "connected": snapshot["connected"] and market_fresh,
            "engine_active": snapshot["connected"],
            "data_connected": snapshot["data_connected"],
            "execution_connected": snapshot["execution_connected"],
            "market_data_fresh": market_fresh,
            "option_quotes_fresh": option_quotes_fresh,
            "message": _connection_message(snapshot, market_fresh),
            "strategy_message": snapshot["strategy_message"],
            "error": snapshot["engine_error"],
        },
        "market": {
            "reference_price": (
                snapshot["btc_reference_price"] if market_fresh else None
            ),
            "timestamp": snapshot["btc_reference_timestamp"],
            "fresh": market_fresh,
        },
        "instruments": {"selected": selected},
        "contracts": {
            "items": snapshot["available_contracts"],
            "selected": selected,
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
