from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite

from dotenv import load_dotenv


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _read_decimal(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a valid decimal number") from exc
    try:
        representable_as_float = isfinite(float(value))
    except (OverflowError, ValueError):
        representable_as_float = False
    if not value.is_finite() or value <= 0 or not representable_as_float:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class Settings:
    bybit_demo_api_key: str
    bybit_demo_api_secret: str
    strategy_enabled: bool
    straddle_quantity: Decimal
    stop_loss_percent: Decimal
    take_profit_percent: Decimal
    api_host: str
    api_port: int

    @classmethod
    def from_environment(cls) -> Settings:
        load_dotenv()

        environment = os.getenv("TRADING_ENV", "demo").strip().lower()
        if environment != "demo":
            raise ValueError("This application only supports TRADING_ENV=demo")

        strategy_enabled = _read_bool("STRATEGY_ENABLED", False)
        api_key = os.getenv("BYBIT_DEMO_API_KEY", "").strip()
        api_secret = os.getenv("BYBIT_DEMO_API_SECRET", "").strip()
        if strategy_enabled and (not api_key or not api_secret):
            raise ValueError(
                "STRATEGY_ENABLED requires BYBIT_DEMO_API_KEY and "
                "BYBIT_DEMO_API_SECRET",
            )

        try:
            port = int(os.getenv("API_PORT", "8000"))
        except ValueError as exc:
            raise ValueError("API_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("API_PORT must be between 1 and 65535")
        api_host = os.getenv("API_HOST", "127.0.0.1").strip()
        if not api_host:
            raise ValueError("API_HOST must not be empty")

        stop_loss = _read_decimal("STOP_LOSS_PERCENT", "1")
        take_profit = _read_decimal("TAKE_PROFIT_PERCENT", "2")
        if stop_loss >= 100 or take_profit >= 100:
            raise ValueError("STOP_LOSS_PERCENT and TAKE_PROFIT_PERCENT must be below 100")

        return cls(
            bybit_demo_api_key=api_key,
            bybit_demo_api_secret=api_secret,
            strategy_enabled=strategy_enabled,
            straddle_quantity=_read_decimal("STRADDLE_QUANTITY", "1"),
            stop_loss_percent=stop_loss,
            take_profit_percent=take_profit,
            api_host=api_host,
            api_port=port,
        )

    @property
    def credentials_configured(self) -> bool:
        return bool(self.bybit_demo_api_key and self.bybit_demo_api_secret)
