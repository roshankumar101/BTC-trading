from __future__ import annotations

from copy import deepcopy
from threading import Lock
from typing import Any


class RuntimeState:
    def __init__(self) -> None:
        self._lock = Lock()
        self._data: dict[str, Any] = {
            "connected": False,
            "connection_message": "Waiting for Bybit Demo configuration",
            "engine_error": None,
            "strategy_status": "DISCONNECTED",
            "btc_reference_price": None,
            "btc_reference_timestamp": None,
            "selected_instruments": None,
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
        with self._lock:
            self._data.update(values)

    def append(self, key: str, value: Any, limit: int = 500) -> None:
        with self._lock:
            items = list(self._data.get(key, []))
            items.append(value)
            self._data[key] = items[-limit:]

    def upsert(self, key: str, identifier: str, value: dict[str, Any]) -> None:
        with self._lock:
            items = list(self._data.get(key, []))
            for index, item in enumerate(items):
                if item.get(identifier) == value.get(identifier):
                    items[index] = value
                    self._data[key] = items
                    return
            items.append(value)
            self._data[key] = items

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data)
