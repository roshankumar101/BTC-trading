from __future__ import annotations

import logging

import uvicorn

from config.settings import Settings
from dashboard.app import create_app
from services.runtime_state import RuntimeState
from services.trading_runtime import TradingRuntime


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_environment()
    state = RuntimeState()
    state.update(
        config={
            "environment": "Demo",
            "strategy_enabled": settings.strategy_enabled,
            "quantity": str(settings.straddle_quantity),
            "stop_loss_percent": str(settings.stop_loss_percent),
            "take_profit_percent": str(settings.take_profit_percent),
        },
    )
    runtime = TradingRuntime(settings, state)
    app = create_app(settings, state, runtime)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
