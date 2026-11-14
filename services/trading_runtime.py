from __future__ import annotations

import asyncio
import logging
from threading import Event, Thread

from nautilus_trader.adapters.bybit import (
    BybitDataClientConfig,
    BybitEnvironment,
    BybitExecClientConfig,
    BybitLiveExecClientFactory,
    BybitProductType,
)
from nautilus_trader.adapters.bybit.data import BybitDataClient
from nautilus_trader.adapters.bybit.factories import (
    get_cached_bybit_http_client,
    get_cached_bybit_instrument_provider,
)
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.data.messages import SubscribeInstrumentStatus
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.node import TradingNode, TradingNodeConfig
from nautilus_trader.model.identifiers import InstrumentId

from config.settings import Settings
from services.runtime_state import RuntimeState
from strategy.straddle import StraddleStrategy

logger = logging.getLogger(__name__)


class _BybitDataClientWithInitialStatus(BybitDataClient):
    async def _subscribe_instrument_status(
        self,
        command: SubscribeInstrumentStatus,
    ) -> None:
        await super()._subscribe_instrument_status(command)
        self._publish_cached_status(command.instrument_id)

    async def _seed_instrument_status_cache(self) -> None:
        await super()._seed_instrument_status_cache()
        for instrument_id in self._instrument_status_subs:
            self._publish_cached_status(instrument_id)

    def _publish_cached_status(self, instrument_id: InstrumentId) -> None:
        action = self._status_cache.get(instrument_id)
        if action is not None:
            self._emit_instrument_status(
                instrument_id,
                action,
                self._clock.timestamp_ns(),
            )


class _BybitDemoDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: BybitDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> BybitDataClient:
        if config.environment != BybitEnvironment.DEMO:
            raise ValueError("The BTC straddle data client is restricted to Bybit Demo")

        product_types = config.product_types or (BybitProductType.OPTION,)
        client = get_cached_bybit_http_client(
            environment=BybitEnvironment.DEMO,
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_url=config.base_url_http,
            timeout_secs=None,
            max_retries=config.max_retries,
            retry_delay_ms=config.retry_delay_initial_ms,
            retry_delay_max_ms=config.retry_delay_max_ms,
            recv_window_ms=config.recv_window_ms,
            proxy_url=config.proxy_url,
        )
        provider = get_cached_bybit_instrument_provider(
            client=client,
            product_types=tuple(product_types),
            config=config.instrument_provider,
        )
        return _BybitDataClientWithInitialStatus(
            loop=loop,
            client=client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            name=name,
        )


class TradingRuntime:
    def __init__(self, settings: Settings, state: RuntimeState) -> None:
        self._settings = settings
        self._state = state
        self._node: TradingNode | None = None
        self._thread: Thread | None = None
        self._connection_monitor_stop = Event()
        self._connection_monitor_thread: Thread | None = None
        self._build_node()

    def _build_node(self) -> None:
        if not self._settings.credentials_configured:
            self._state.update(
                connected=False,
                connection_message=(
                    "Bybit Demo API credentials are not configured; "
                    "the dashboard is available without exchange data"
                ),
            )
            logger.warning("Bybit Demo credentials are not configured; engine not started")
            return

        product_types = (BybitProductType.OPTION,)
        instrument_provider = InstrumentProviderConfig(
            load_all=True,
            filters={"base_coin": "BTC"},
        )
        data_config = BybitDataClientConfig(
            api_key=self._settings.bybit_demo_api_key,
            api_secret=self._settings.bybit_demo_api_secret,
            product_types=product_types,
            environment=BybitEnvironment.DEMO,
            instrument_provider=instrument_provider,
        )
        execution_config = BybitExecClientConfig(
            api_key=self._settings.bybit_demo_api_key,
            api_secret=self._settings.bybit_demo_api_secret,
            product_types=product_types,
            environment=BybitEnvironment.DEMO,
            instrument_provider=instrument_provider,
        )
        node_config = TradingNodeConfig(
            data_clients={"BYBIT": data_config},
            exec_clients={"BYBIT": execution_config},
        )

        node = TradingNode(config=node_config)
        node.add_data_client_factory("BYBIT", _BybitDemoDataClientFactory)
        node.add_exec_client_factory("BYBIT", BybitLiveExecClientFactory)
        node.trader.add_strategy(StraddleStrategy(self._settings, self._state))
        node.build()
        self._node = node
        self._state.update(
            connection_message="Trading node built; waiting for Bybit Demo connection",
        )
        logger.info("Built NautilusTrader Bybit Demo node with BTC options only")

    def start(self) -> None:
        if self._node is None:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._connection_monitor_stop.clear()
        self._thread = Thread(
            target=self._run_node,
            name="nautilus-bybit-demo",
            daemon=True,
        )
        self._thread.start()
        self._connection_monitor_thread = Thread(
            target=self._monitor_connections,
            name="nautilus-connection-monitor",
            daemon=True,
        )
        self._connection_monitor_thread.start()

    def _monitor_connections(self) -> None:
        while not self._connection_monitor_stop.wait(0.5):
            node = self._node
            if node is None:
                return
            try:
                data_connected = node.kernel.data_engine.check_connected()
                execution_connected = node.kernel.exec_engine.check_connected()
            except Exception as exc:
                logger.exception("Unable to read NautilusTrader connection state")
                self._state.update(
                    connected=False,
                    data_connected=False,
                    execution_connected=False,
                    engine_error=f"{type(exc).__name__}: {exc}",
                    connection_message="Unable to verify Bybit Demo connectivity",
                )
                return

            connected = data_connected and execution_connected
            message = (
                "Connected to Bybit Demo"
                if connected
                else "Bybit Demo execution engine is disconnected"
                if not execution_connected
                else "Bybit Demo market-data engine is disconnected"
            )
            self._state.update(
                connected=connected,
                data_connected=data_connected,
                execution_connected=execution_connected,
                connection_message=message,
            )

    def _run_node(self) -> None:
        if self._node is None:
            return
        stopped_unexpectedly = False
        try:
            self._node.run(raise_exception=True)
        except Exception as exc:
            stopped_unexpectedly = True
            logger.exception("NautilusTrader node stopped with an error")
            self._state.update(
                engine_error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self._connection_monitor_stop.set()
            self._state.update(
                connected=False,
                data_connected=False,
                execution_connected=False,
                connection_message=(
                    "Trading engine stopped unexpectedly"
                    if stopped_unexpectedly
                    else "Trading engine stopped"
                ),
            )

    def stop(self) -> None:
        self._connection_monitor_stop.set()
        if self._node is None or self._thread is None or not self._thread.is_alive():
            if (
                self._connection_monitor_thread is not None
                and self._connection_monitor_thread.is_alive()
            ):
                self._connection_monitor_thread.join(timeout=1)
            return
        self._state.update(
            connected=False,
            data_connected=False,
            execution_connected=False,
            connection_message="Trading engine stopping",
        )
        self._node.stop()
        self._thread.join(timeout=15)
        if (
            self._connection_monitor_thread is not None
            and self._connection_monitor_thread.is_alive()
        ):
            self._connection_monitor_thread.join(timeout=1)
        if self._thread.is_alive():
            message = "Trading engine did not stop within 15 seconds"
            logger.error(message)
            self._state.update(engine_error=message)
