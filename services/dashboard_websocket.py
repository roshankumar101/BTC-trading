from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket

from services.runtime_state import RuntimeState

logger = logging.getLogger(__name__)


class DashboardWebSocketManager:
    def __init__(self, snapshot_factory: Callable[[], dict[str, Any]]) -> None:
        self._snapshot_factory = snapshot_factory
        self._clients: dict[WebSocket, dict[str, Any]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._changed: asyncio.Event | None = None
        self._broadcast_lock = asyncio.Lock()
        self._unsubscribe: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self, state: RuntimeState) -> None:
        self._loop = asyncio.get_running_loop()
        self._changed = asyncio.Event()
        self._unsubscribe = state.subscribe(self.notify_changed)
        self._task = asyncio.create_task(self._run(), name="dashboard-websocket-broadcast")

    def notify_changed(self) -> None:
        loop = self._loop
        changed = self._changed
        if loop is not None and changed is not None and not loop.is_closed():
            loop.call_soon_threadsafe(changed.set)

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        clients = tuple(self._clients)
        self._clients.clear()
        if clients:
            await asyncio.gather(
                *(client.close(code=1001, reason="Dashboard is shutting down") for client in clients),
                return_exceptions=True,
            )
        self._loop = None
        self._changed = None

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._broadcast_lock:
            snapshot = self._snapshot_factory()
            await websocket.send_json({"type": "snapshot", "data": snapshot})
            self._clients[websocket] = snapshot

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.pop(websocket, None)

    async def _run(self) -> None:
        while True:
            changed = self._changed
            if changed is None:
                return
            try:
                await asyncio.wait_for(changed.wait(), timeout=1)
            except TimeoutError:
                pass
            changed.clear()
            if self._clients:
                await self._broadcast(self._snapshot_factory())

    @staticmethod
    def _changed_messages(
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> list[dict[str, Any]]:
        messages = []
        previous_status = previous["status"]
        current_status = current["status"]
        connection_fields = (
            "connected",
            "engine_active",
            "data_connected",
            "execution_connected",
            "market_data_fresh",
            "option_quotes_fresh",
            "message",
            "error",
        )
        if any(previous_status.get(key) != current_status.get(key) for key in connection_fields):
            messages.append(
                {
                    "type": "connection_status",
                    "data": {
                        key: current_status.get(key)
                        for key in connection_fields
                    },
                },
            )
        strategy_fields = ("status", "strategy_message")
        if any(previous_status.get(key) != current_status.get(key) for key in strategy_fields):
            messages.append(
                {
                    "type": "strategy_state",
                    "data": {
                        key: current_status.get(key)
                        for key in strategy_fields
                    },
                },
            )

        market_data = {}
        for key in ("market", "instruments", "contracts"):
            if previous.get(key) != current.get(key):
                market_data[key] = current[key]
        if market_data:
            messages.append({"type": "market_data", "data": market_data})

        for key, message_type in (
            ("orders", "order_update"),
            ("positions", "position_update"),
            ("pnl", "risk_update"),
            ("trades", "order_update"),
        ):
            if previous.get(key) != current.get(key):
                existing = next(
                    (message for message in messages if message["type"] == message_type),
                    None,
                )
                if existing is None:
                    existing = {"type": message_type, "data": {}}
                    messages.append(existing)
                existing["data"][key] = current[key]
        return messages

    async def _broadcast(self, current: dict[str, Any]) -> None:
        async with self._broadcast_lock:
            clients = tuple(self._clients.items())
            pending = [
                (client, self._changed_messages(previous, current))
                for client, previous in clients
            ]
            pending = [(client, messages) for client, messages in pending if messages]
            if not pending:
                return
            results = await asyncio.gather(
                *(
                    self._send_messages(client, messages)
                    for client, messages in pending
                ),
                return_exceptions=True,
            )
            for (client, _), result in zip(pending, results, strict=True):
                if isinstance(result, Exception):
                    self._clients.pop(client, None)
                    logger.warning(
                        "Removed dashboard WebSocket client after send failure: %s",
                        type(result).__name__,
                    )
                else:
                    self._clients[client] = current

    @staticmethod
    async def _send_messages(
        client: WebSocket,
        messages: list[dict[str, Any]],
    ) -> None:
        for message in messages:
            await client.send_json(message)
