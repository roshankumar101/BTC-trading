from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, Lock
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class EngineCommand:
    action: str
    payload: dict[str, Any]
    completed: Event = field(default_factory=Event)
    result: dict[str, Any] | None = None
    status: str = "PENDING"
    _lock: Lock = field(default_factory=Lock, repr=False)

    def claim(self) -> bool:
        with self._lock:
            if self.status != "PENDING":
                return False
            self.status = "RUNNING"
            return True

    def resolve(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.result = result
            self.status = "COMPLETED"
            self.completed.set()

    def cancel_if_pending(self) -> bool:
        with self._lock:
            if self.status != "PENDING":
                return False
            self.status = "CANCELLED"
            self.completed.set()
            return True

    def wait(self, timeout: float) -> dict[str, Any] | None:
        if not self.completed.wait(timeout):
            return None
        return self.result


class RuntimeState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._listeners: set[Callable[[], None]] = set()
        self._commands: Queue[EngineCommand] = Queue()
        self._data: dict[str, Any] = {
            "connected": False,
            "data_connected": False,
            "execution_connected": False,
            "connection_message": "Waiting for Bybit Demo configuration",
            "engine_error": None,
            "strategy_status": "DISCONNECTED",
            "strategy_message": None,
            "btc_reference_price": None,
            "btc_reference_timestamp": None,
            "selected_instruments": None,
            "available_contracts": [],
            "positions": [],
            "orders": [],
            "trades": [],
            "pnl": {
                "combined_entry_premium": None,
                "current_buyback_value": None,
                "unrealized_pnl": None,
                "realized_pnl": None,
                "stop_loss_triggered": False,
                "take_profit_triggered": False,
            },
            "config": {},
        }

    def update(self, **values: Any) -> None:
        changed = False
        with self._lock:
            for key, value in values.items():
                if self._data.get(key) != value:
                    self._data[key] = value
                    changed = True
        if changed:
            self._notify_listeners()

    def append(self, key: str, value: Any, limit: int = 500) -> None:
        with self._lock:
            items = list(self._data.get(key, []))
            items.append(value)
            self._data[key] = items[-limit:]
        self._notify_listeners()

    def upsert(
        self,
        key: str,
        identifier: str,
        value: dict[str, Any],
        limit: int = 500,
    ) -> None:
        changed = True
        with self._lock:
            items = list(self._data.get(key, []))
            for index, item in enumerate(items):
                if item.get(identifier) == value.get(identifier):
                    if item == value:
                        changed = False
                        break
                    items[index] = value
                    self._data[key] = items
                    break
            else:
                items.append(value)
                self._data[key] = items[-limit:]
        if changed:
            self._notify_listeners()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def _notify_listeners(self) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                logger.exception("Runtime state listener failed")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data)

    def enqueue_command(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> EngineCommand:
        command = EngineCommand(action=action, payload=payload)
        self._commands.put(command)
        return command

    def next_command(self) -> EngineCommand | None:
        try:
            return self._commands.get_nowait()
        except Empty:
            return None
