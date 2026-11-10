from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.bybit import BYBIT_VENUE
from nautilus_trader.model.data import IndexPriceUpdate, InstrumentStatus, QuoteTick
from nautilus_trader.model.enums import (
    OptionKind,
    OrderSide,
    OrderStatus,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
    PositionChanged,
    PositionClosed,
    PositionOpened,
)
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.trading import Strategy

from config.settings import Settings
from services.instrument_selector import select_btc_atm_pair, select_btc_pair
from services.risk_manager import LegExposure, RiskManager
from services.runtime_state import RuntimeState

logger = logging.getLogger(__name__)
_NS_PER_SECOND = 1_000_000_000
_MAX_QUOTE_AGE_NS = 10 * _NS_PER_SECOND
_MAX_EXIT_ATTEMPTS = 3


@dataclass
class LegState:
    instrument: CryptoOption
    kind: str
    requested_quantity: Decimal
    filled_quantity: Decimal = Decimal(0)
    exited_quantity: Decimal = Decimal(0)
    entry_notional: Decimal = Decimal(0)
    exit_notional: Decimal = Decimal(0)
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    bid_size: Decimal = Decimal(0)
    ask_size: Decimal = Decimal(0)
    quote_timestamp_ns: int = 0
    entry_status: str = "NOT_SUBMITTED"
    exit_status: str = "NOT_SUBMITTED"
    entry_terminal: bool = False
    active_exit_order_id: str | None = None
    exit_attempts: int = 0
    actual_position_quantity: Decimal | None = None

    @property
    def multiplier(self) -> Decimal:
        return Decimal(str(self.instrument.multiplier))

    @property
    def remaining_quantity(self) -> Decimal:
        return max(Decimal(0), self.filled_quantity - self.exited_quantity)

    @property
    def average_entry_price(self) -> Decimal | None:
        denominator = self.filled_quantity * self.multiplier
        if denominator == 0:
            return None
        return self.entry_notional / denominator


class StraddleStrategy(Strategy):
    def __init__(self, settings: Settings, runtime_state: RuntimeState) -> None:
        super().__init__()
        self._settings = settings
        self._runtime_state = runtime_state
        self._risk_manager = RiskManager(
            stop_loss_percent=settings.stop_loss_percent,
            take_profit_percent=settings.take_profit_percent,
        )
        self._btc_options: list[CryptoOption] = []
        self._legs: dict[str, LegState] = {}
        self._order_context: dict[str, tuple[str, str]] = {}
        self._order_requested_quantities: dict[str, Decimal] = {}
        self._order_fills: dict[str, tuple[Decimal, Decimal]] = {}
        self._order_types: dict[str, str] = {}
        self._manual_exit_orders: set[str] = set()
        self._manual_exit_batch_active = False
        self._entry_expected_kinds: set[str] = set()
        self._order_timestamps: dict[str, str] = {}
        self._closed_realized_pnl = Decimal(0)
        self._closed_trade_count = 0
        self._entry_started = False
        self._exit_reason: str | None = None
        self._submission_uncertain = False
        self._btc_reference_price: Decimal | None = None
        self._btc_reference_timestamp_ns: int | None = None
        self._index_subscriptions: set[str] = set()
        self._quote_subscriptions: set[str] = set()
        self._status_subscriptions: set[str] = set()
        self._instrument_trading_status: dict[str, bool | None] = {}
        self._reference_instrument_id: Any | None = None
        self._selected_expiry_ns: int | None = None
        self._selected_strike: Decimal | None = None
        self._strategy_status = "INITIALIZING"
        self._last_risk_snapshot = None

    def on_start(self) -> None:
        self._runtime_state.update(
            engine_error=None,
        )
        self.clock.set_timer(
            name="api-command-poller",
            interval=timedelta(milliseconds=100),
            callback=self._process_engine_commands,
            fire_immediately=True,
        )
        logger.info("Bybit Demo strategy started")
        self._btc_options = [
            instrument
            for instrument in self.cache.instruments(venue=BYBIT_VENUE)
            if isinstance(instrument, CryptoOption)
            and instrument.underlying.code == "BTC"
        ]
        self._publish_contract_universe()
        if not self._btc_options:
            self._set_status("INSTRUMENTS_UNAVAILABLE")
            self._runtime_state.update(
                connection_message="Bybit connected, but no BTC options were discovered",
            )
            logger.error("Bybit connected without any discovered BTC option instruments")
            self._publish()
            return

        if self._reconcile_cached_state():
            return

        first_live_option = min(
            (
                instrument
                for instrument in self._btc_options
                if instrument.expiration_ns > time.time_ns()
            ),
            key=lambda instrument: instrument.expiration_ns,
            default=None,
        )
        if first_live_option is None:
            self._set_status("INSTRUMENTS_UNAVAILABLE")
            self._runtime_state.update(
                connection_message="No unexpired BTC option contracts are available",
            )
            logger.error("No unexpired BTC option instruments were discovered")
            self._publish()
            return

        self._reference_instrument_id = first_live_option.id
        self._subscribe_index(first_live_option)
        self._set_status("WAITING_FOR_REFERENCE_PRICE")
        logger.info("Subscribed to BTC option index price feed")
        self._publish()

    def on_stop(self) -> None:
        self.clock.cancel_timer("api-command-poller")
        logger.info("Bybit Demo strategy stopped")
        self._runtime_state.update(
            connected=False,
            data_connected=False,
            execution_connected=False,
            connection_message="Trading engine stopped",
        )
        self._publish()

    def on_index_price(self, index_price: IndexPriceUpdate) -> None:
        instrument = self.cache.instrument(index_price.instrument_id)
        if not isinstance(instrument, CryptoOption) or instrument.underlying.code != "BTC":
            return

        self._btc_reference_price = Decimal(str(index_price.value))
        self._btc_reference_timestamp_ns = index_price.ts_event
        self._runtime_state.update(
            btc_reference_price=str(self._btc_reference_price),
            btc_reference_timestamp=datetime.fromtimestamp(
                index_price.ts_event / _NS_PER_SECOND,
                tz=timezone.utc,
            ).isoformat(),
        )

        if not self._legs:
            self._select_and_subscribe()
        self._publish()

    def on_quote_tick(self, tick: QuoteTick) -> None:
        leg = next(
            (
                candidate
                for candidate in self._legs.values()
                if candidate.instrument.id == tick.instrument_id
            ),
            None,
        )
        if leg is None:
            return

        leg.bid_price = Decimal(str(tick.bid_price))
        leg.ask_price = Decimal(str(tick.ask_price))
        leg.bid_size = Decimal(str(tick.bid_size))
        leg.ask_size = Decimal(str(tick.ask_size))
        leg.quote_timestamp_ns = tick.ts_event

        if not self._entry_started:
            if self._settings.strategy_enabled:
                self._submit_entry_if_ready()
        elif self._exit_reason is not None:
            self._submit_exits_if_ready()
        elif self._strategy_status in {
            "OPEN",
            "RECOVERED_OPEN",
        }:
            self._evaluate_risk()
        self._publish()

    def on_instrument_status(self, event: InstrumentStatus) -> None:
        instrument_id = str(event.instrument_id)
        self._instrument_trading_status[instrument_id] = event.is_trading
        logger.info(
            "Instrument trading status for %s: %s",
            instrument_id,
            event.action.name,
        )
        self._publish()

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        self._set_order_status(event.client_order_id, "SUBMITTED")

    def on_order_accepted(self, event: OrderAccepted) -> None:
        self._set_order_status(event.client_order_id, "ACKNOWLEDGED")

    def on_order_filled(self, event: OrderFilled) -> None:
        context = self._order_context.get(str(event.client_order_id))
        if context is None:
            logger.warning("Received fill for an untracked order %s", event.client_order_id)
            return

        leg_name, action = context
        leg = self._legs[leg_name]
        quantity = Decimal(str(event.last_qty))
        price = Decimal(str(event.last_px))
        notional = quantity * price * leg.multiplier
        order_key = str(event.client_order_id)
        order_filled, order_notional = self._order_fills.get(
            order_key,
            (Decimal(0), Decimal(0)),
        )
        self._order_fills[order_key] = (
            order_filled + quantity,
            order_notional + notional,
        )

        if action == "ENTRY":
            leg.filled_quantity += quantity
            leg.entry_notional += notional
            leg.entry_status = "PARTIALLY_FILLED"
        else:
            leg.exited_quantity += quantity
            leg.exit_notional += notional
            leg.exit_status = "PARTIALLY_FILLED"

        order = self.cache.order(event.client_order_id)
        order_is_filled = order is not None and order.status == OrderStatus.FILLED
        order_is_closed = order is not None and order.is_closed
        if order_is_filled:
            if action == "ENTRY":
                leg.entry_status = "FILLED"
                leg.entry_terminal = True
            else:
                leg.exit_status = "FILLED"
                leg.active_exit_order_id = None
                self._manual_exit_orders.discard(order_key)
        elif order_is_closed:
            if action == "ENTRY":
                leg.entry_status = order.status.name
                leg.entry_terminal = True
            else:
                leg.exit_status = order.status.name
                leg.active_exit_order_id = None
                self._manual_exit_orders.discard(order_key)

        self._update_order_record(
            event.client_order_id,
            leg,
            action,
            (
                "FILLED"
                if order_is_filled
                else order.status.name
                if order_is_closed and order is not None
                else "PARTIALLY_FILLED"
            ),
            event.venue_order_id,
        )
        self._runtime_state.append(
            "trades",
            {
                "timestamp": datetime.fromtimestamp(
                    event.ts_event / _NS_PER_SECOND,
                    tz=timezone.utc,
                ).isoformat(),
                "order_id": str(event.client_order_id),
                "instrument": str(event.instrument_id),
                "order_side": event.order_side.name,
                "order_type": event.order_type.name,
                "quantity": str(quantity),
                "filled_quantity": str(quantity),
                "average_fill_price": str(price),
                "status": "FILLED",
                "exchange_order_id": (
                    None
                    if event.venue_order_id is None
                    else str(event.venue_order_id)
                ),
            },
        )
        logger.info(
            "%s %s fill: %s %s at %s",
            action,
            leg.kind,
            quantity,
            leg.instrument.id,
            price,
        )

        if action == "ENTRY":
            self._finish_entry_if_terminal()
        else:
            self._finish_exit_if_flat()
            self._finish_manual_exit_batch()
            if not self._manual_exit_batch_active and not self._manual_exit_orders:
                self._evaluate_risk()
        self._publish()
        if order_is_closed:
            self._forget_order_tracking(order_key)

    def on_order_rejected(self, event: OrderRejected) -> None:
        self._handle_order_terminal(
            event.client_order_id,
            "REJECTED",
            event.reason,
        )

    def on_order_denied(self, event: OrderDenied) -> None:
        self._handle_order_terminal(event.client_order_id, "DENIED", event.reason)

    def on_order_canceled(self, event: OrderCanceled) -> None:
        self._handle_order_terminal(event.client_order_id, "CANCELED")

    def on_order_expired(self, event: OrderExpired) -> None:
        self._handle_order_terminal(event.client_order_id, "EXPIRED")

    def on_position_opened(self, event: PositionOpened) -> None:
        self._update_actual_position(event.instrument_id, event.quantity, event.side)

    def on_position_changed(self, event: PositionChanged) -> None:
        self._update_actual_position(event.instrument_id, event.quantity, event.side)

    def on_position_closed(self, event: PositionClosed) -> None:
        leg = next(
            (
                candidate
                for candidate in self._legs.values()
                if candidate.instrument.id == event.instrument_id
            ),
            None,
        )
        if leg is None:
            return
        leg.actual_position_quantity = Decimal(0)
        logger.info("Position closed on Bybit: %s", event.instrument_id)
        self._finish_exit_if_flat()
        self._finish_manual_exit_batch()
        if self._exit_reason is None:
            self._set_status(self._open_exposure_status())
        if not self._manual_exit_batch_active and not self._manual_exit_orders:
            self._evaluate_risk()
        self._publish()

    def _update_actual_position(
        self,
        instrument_id: Any,
        quantity: Any,
        side: PositionSide,
    ) -> None:
        leg = next(
            (
                candidate
                for candidate in self._legs.values()
                if candidate.instrument.id == instrument_id
            ),
            None,
        )
        if leg is None:
            instrument = self.cache.instrument(instrument_id)
            if (
                isinstance(instrument, CryptoOption)
                and instrument.underlying.code == "BTC"
            ):
                self._submission_uncertain = True
                self._set_status("RECONCILIATION_REQUIRED")
                self._runtime_state.update(
                    connection_message=(
                        f"Untracked BTC option position observed for {instrument_id}; "
                        "trading is halted until account state is reconciled"
                    ),
                )
                logger.error(
                    "Untracked BTC option position observed for %s",
                    instrument_id,
                )
                self._publish()
            return
        if side != PositionSide.SHORT:
            self._submission_uncertain = True
            self._set_status("RECONCILIATION_REQUIRED")
            self._runtime_state.update(
                connection_message=(
                    f"Unexpected non-short position observed for {instrument_id}; "
                    "automatic orders are halted"
                ),
            )
            logger.error("Unexpected non-short position observed for %s", instrument_id)
            self._publish()
            return
        leg.actual_position_quantity = abs(Decimal(str(quantity)))
        logger.info(
            "Bybit position update for %s: %s contracts short",
            instrument_id,
            leg.actual_position_quantity,
        )
        if not self._manual_exit_batch_active and not self._manual_exit_orders:
            self._evaluate_risk()
        self._publish()

    def _reconcile_cached_state(self) -> bool:
        btc_positions = []
        for position in self.cache.positions_open(venue=BYBIT_VENUE):
            instrument = self.cache.instrument(position.instrument_id)
            if (
                isinstance(instrument, CryptoOption)
                and instrument.underlying.code == "BTC"
            ):
                btc_positions.append((position, instrument))

        btc_open_orders = []
        for order in self.cache.orders_open(venue=BYBIT_VENUE):
            instrument = self.cache.instrument(order.instrument_id)
            if (
                isinstance(instrument, CryptoOption)
                and instrument.underlying.code == "BTC"
            ):
                btc_open_orders.append(order)

        if not btc_positions and not btc_open_orders:
            return False

        if btc_open_orders or not 1 <= len(btc_positions) <= 2:
            self._set_status("RECONCILIATION_REQUIRED")
            self._submission_uncertain = True
            self._runtime_state.update(
                connection_message=(
                    "Bybit reports BTC option orders or positions that cannot be "
                    "matched safely; automatic trading is halted"
                ),
            )
            logger.error(
                "Failing closed on startup reconciliation: %d BTC option positions, "
                "%d open orders",
                len(btc_positions),
                len(btc_open_orders),
            )
            self._publish()
            return True

        positions_by_kind: dict[OptionKind, tuple[Any, CryptoOption]] = {}
        for position, instrument in btc_positions:
            if position.side != PositionSide.SHORT or instrument.option_kind in positions_by_kind:
                self._set_status("RECONCILIATION_REQUIRED")
                self._submission_uncertain = True
                self._runtime_state.update(
                    connection_message=(
                        "Existing BTC option exposure cannot be represented as "
                        "one short Call and one short Put"
                    ),
                )
                logger.error("Existing BTC option positions are ambiguous")
                self._publish()
                return True
            positions_by_kind[instrument.option_kind] = (position, instrument)

        active_instrument = next(iter(positions_by_kind.values()))[1]
        pair = select_btc_pair(
            self._btc_options,
            active_instrument.expiration_ns,
            Decimal(str(active_instrument.strike_price)),
            time.time_ns(),
        )
        if pair is None:
            self._set_status("RECONCILIATION_REQUIRED")
            self._submission_uncertain = True
            self._runtime_state.update(
                connection_message=(
                    "Existing BTC option position has no matching Call/Put contract pair"
                ),
            )
            logger.error("Existing BTC option position has no matching contract pair")
            self._publish()
            return True

        call_instrument, put_instrument = pair
        expected_instruments = {
            OptionKind.CALL: call_instrument,
            OptionKind.PUT: put_instrument,
        }
        positions_mismatch = any(
            instrument.expiration_ns != expected_instruments[option_kind].expiration_ns
            or instrument.strike_price != expected_instruments[option_kind].strike_price
            or instrument.quote_currency != expected_instruments[option_kind].quote_currency
            or instrument.settlement_currency
            != expected_instruments[option_kind].settlement_currency
            for option_kind, (_, instrument) in positions_by_kind.items()
        )
        if positions_mismatch:
            self._set_status("RECONCILIATION_REQUIRED")
            self._submission_uncertain = True
            self._runtime_state.update(
                connection_message="Existing Call and Put do not form a matching straddle",
            )
            logger.error("Existing BTC option positions do not form a matching straddle")
            self._publish()
            return True

        for kind, option_kind, instrument in (
            ("CALL", OptionKind.CALL, call_instrument),
            ("PUT", OptionKind.PUT, put_instrument),
        ):
            position_data = positions_by_kind.get(option_kind)
            quantity = (
                Decimal(0)
                if position_data is None
                else abs(Decimal(str(position_data[0].quantity)))
            )
            average_price = (
                Decimal(0)
                if position_data is None
                else Decimal(str(position_data[0].avg_px_open))
            )
            leg = LegState(
                instrument=instrument,
                kind=kind,
                requested_quantity=quantity,
                filled_quantity=quantity,
                entry_notional=average_price * quantity * Decimal(str(instrument.multiplier)),
                entry_status="RECONCILED" if position_data is not None else "NOT_SUBMITTED",
                actual_position_quantity=quantity if position_data is not None else Decimal(0),
            )
            self._legs[kind] = leg
            self._subscribe_quote(leg.instrument)
            self._subscribe_status(leg.instrument)
        self._selected_expiry_ns = call_instrument.expiration_ns
        self._selected_strike = Decimal(str(call_instrument.strike_price))
        if self._reference_instrument_id is None:
            self._reference_instrument_id = active_instrument.id
        self._subscribe_index(active_instrument)

        self._entry_started = True
        self._set_status("RECOVERED_OPEN")
        logger.info("Reconciled BTC option positions from Bybit state")
        self._publish()
        return True

    def _select_and_subscribe(self) -> None:
        if self._btc_reference_price is None or not self._reference_is_fresh():
            return
        pair = select_btc_atm_pair(
            self._btc_options,
            self._btc_reference_price,
            time.time_ns(),
        )
        if pair is None:
            if self._strategy_status != "NO_MATCHING_CONTRACTS":
                logger.warning(
                    "No matching BTC option pair found at the current reference price",
                )
            self._set_status("NO_MATCHING_CONTRACTS")
            self._runtime_state.update(
                connection_message=(
                    "No matching unexpired BTC Call and Put contracts are available"
                ),
            )
            return

        self._install_selected_pair(pair)
        call, put = pair
        quantity_is_valid = all(
            self._quantity_is_valid(
                leg.instrument,
                self._settings.straddle_quantity,
            )
            and Decimal(
                str(
                    leg.instrument.make_qty(
                        float(self._settings.straddle_quantity),
                    ),
                ),
            )
            == self._settings.straddle_quantity
            for leg in self._legs.values()
        )
        if not quantity_is_valid:
            self._runtime_state.update(
                connection_message=(
                    "Configured quantity is not an exact valid quantity increment "
                    "for both selected options"
                ),
            )
            logger.error(
                "Configured quantity %s is incompatible with selected option quantity steps",
                self._settings.straddle_quantity,
            )
        self._set_status(
            "INSTRUMENTS_SELECTED" if quantity_is_valid else "INVALID_QUANTITY",
        )
        if quantity_is_valid:
            self._runtime_state.update(
                connection_message="Selected matching BTC options; waiting for fresh quotes",
            )
        logger.info(
            "Selected BTC short straddle: Call=%s Put=%s reference=%s",
            call.id,
            put.id,
            self._btc_reference_price,
        )
        self._publish()

    def _publish_contract_universe(self) -> None:
        grouped: dict[int, dict[Decimal, dict[OptionKind, CryptoOption]]] = {}
        for instrument in self._btc_options:
            if instrument.expiration_ns <= time.time_ns():
                continue
            strike = Decimal(str(instrument.strike_price))
            strikes = grouped.setdefault(instrument.expiration_ns, {})
            legs = strikes.setdefault(strike, {})
            legs[instrument.option_kind] = instrument

        contracts = []
        for expiry_ns, strikes in sorted(grouped.items()):
            available_strikes = []
            for strike, legs in sorted(strikes.items()):
                call = legs.get(OptionKind.CALL)
                put = legs.get(OptionKind.PUT)
                if (
                    call is None
                    or put is None
                    or call.quote_currency != put.quote_currency
                    or call.settlement_currency != put.settlement_currency
                ):
                    continue
                available_strikes.append(
                    {
                        "strike": str(strike),
                        "call": str(call.id),
                        "put": str(put.id),
                    },
                )
            if available_strikes:
                contracts.append(
                    {
                        "expiry_ns": expiry_ns,
                        "expiry": datetime.fromtimestamp(
                            expiry_ns / _NS_PER_SECOND,
                            tz=timezone.utc,
                        ).isoformat(),
                        "strikes": available_strikes,
                    },
                )
        self._runtime_state.update(available_contracts=contracts)

    def _install_selected_pair(
        self,
        pair: tuple[CryptoOption, CryptoOption],
    ) -> None:
        call, put = pair
        retained = {str(call.id), str(put.id)}
        for leg in self._legs.values():
            if leg.filled_quantity > 0 and self._open_quantity(leg) == 0:
                self._archive_closed_leg(leg)
            old_id = str(leg.instrument.id)
            if old_id not in retained and old_id in self._quote_subscriptions:
                self.unsubscribe_quote_ticks(leg.instrument.id)
                self._quote_subscriptions.discard(old_id)
            if old_id not in retained and old_id in self._status_subscriptions:
                self.unsubscribe_instrument_status(leg.instrument.id)
                self._status_subscriptions.discard(old_id)
                self._instrument_trading_status.pop(old_id, None)

        call_quantity = Decimal(
            str(call.make_qty(float(self._settings.straddle_quantity))),
        )
        put_quantity = Decimal(
            str(put.make_qty(float(self._settings.straddle_quantity))),
        )
        self._legs = {
            "CALL": LegState(call, "CALL", call_quantity),
            "PUT": LegState(put, "PUT", put_quantity),
        }
        self._selected_expiry_ns = call.expiration_ns
        self._selected_strike = Decimal(str(call.strike_price))
        self._entry_started = False
        self._entry_expected_kinds.clear()
        self._subscribe_quote(call)
        self._subscribe_quote(put)
        self._subscribe_status(call)
        self._subscribe_status(put)

    def _apply_contract_selection(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._btc_reference_price is None or not self._reference_is_fresh():
            raise ValueError("A fresh BTC reference price is required")
        if self._has_open_exposure() or self._has_open_orders():
            raise ValueError("Contracts cannot be changed while exposure or orders are open")

        expiry_value = payload.get("expiry_ns")
        expiry_ns = None if expiry_value is None else int(expiry_value)
        strike_value = payload.get("strike")
        strike = None if strike_value is None else Decimal(str(strike_value))
        if expiry_ns is None:
            pair = select_btc_atm_pair(
                self._btc_options,
                self._btc_reference_price,
                time.time_ns(),
            )
        else:
            expiries = sorted(
                {
                    instrument.expiration_ns
                    for instrument in self._btc_options
                    if instrument.expiration_ns == expiry_ns
                    and instrument.expiration_ns > time.time_ns()
                },
            )
            if not expiries:
                raise ValueError("The selected expiry is unavailable or expired")
            available_strikes = sorted(
                {
                    Decimal(str(instrument.strike_price))
                    for instrument in self._btc_options
                    if instrument.expiration_ns == expiry_ns
                    and instrument.expiration_ns > time.time_ns()
                },
            )
            if strike is None:
                strike = min(
                    available_strikes,
                    key=lambda value: (
                        abs(value - self._btc_reference_price),
                        value,
                    ),
                )
            pair = select_btc_pair(
                self._btc_options,
                expiry_ns,
                strike,
                time.time_ns(),
            )
        if pair is None:
            raise ValueError("No matching Call and Put contracts are available")

        current_ids = {
            str(leg.instrument.id) for leg in self._legs.values()
        }
        pair_ids = {str(instrument.id) for instrument in pair}
        if current_ids != pair_ids or self._exit_reason is not None:
            self._install_selected_pair(pair)
            self._exit_reason = None
            self._set_status("INSTRUMENTS_SELECTED")
            self._runtime_state.update(
                connection_message="Selected contracts; waiting for fresh option quotes",
            )
        self._publish()
        selected = self._runtime_state.snapshot()["selected_instruments"]
        return {"selected": selected}

    def _subscribe_quote(self, instrument: CryptoOption) -> None:
        key = str(instrument.id)
        if key in self._quote_subscriptions:
            return
        self.subscribe_quote_ticks(instrument.id)
        self._quote_subscriptions.add(key)

    def _subscribe_index(self, instrument: CryptoOption) -> None:
        key = str(instrument.id)
        if key in self._index_subscriptions:
            return
        self.subscribe_index_prices(instrument.id)
        self._index_subscriptions.add(key)

    def _subscribe_status(self, instrument: CryptoOption) -> None:
        key = str(instrument.id)
        if key in self._status_subscriptions:
            return
        self.subscribe_instrument_status(instrument.id)
        self._status_subscriptions.add(key)

    def _process_engine_commands(self, _: Any = None) -> None:
        for _ in range(20):
            command = self._runtime_state.next_command()
            if command is None:
                return
            if not command.claim():
                continue
            try:
                if command.action == "select_contracts":
                    result = self._apply_contract_selection(command.payload)
                elif command.action == "manual_order":
                    result = self._apply_manual_order(command.payload)
                else:
                    raise ValueError(f"Unknown engine command: {command.action}")
            except (ValueError, RuntimeError) as exc:
                logger.warning("Rejected dashboard command %s: %s", command.action, exc)
                command.resolve({"ok": False, "message": str(exc)})
            except Exception:
                logger.exception("Failed to process dashboard command %s", command.action)
                command.resolve(
                    {
                        "ok": False,
                        "message": "The engine could not process the requested command",
                    },
                )
            else:
                command.resolve({"ok": True, "data": result})

    def _apply_manual_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._settings.credentials_configured:
            raise RuntimeError("Bybit Demo credentials are not configured")
        if self._settings.strategy_enabled:
            raise RuntimeError("Manual orders are unavailable while automatic entry is enabled")
        connection = self._runtime_state.snapshot()
        if not connection["data_connected"] or not connection["execution_connected"]:
            raise RuntimeError("Bybit Demo data and execution engines must both be connected")
        if self._submission_uncertain or self._exit_reason is not None:
            raise RuntimeError("Trading is paused until existing engine state is resolved")
        if self._has_open_orders():
            raise RuntimeError("Cancel or resolve existing BTC option orders first")
        if self._btc_reference_price is None or not self._reference_is_fresh():
            raise RuntimeError("A fresh BTC reference price is required")

        action = str(payload["action"]).upper()
        order_type = str(payload["order_type"]).upper()
        if order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("Order type must be MARKET or LIMIT")
        if order_type == "LIMIT" and not isinstance(payload.get("limit_prices"), dict):
            raise ValueError("Limit prices are required for limit orders")

        if action in {"OPEN_STRADDLE", "CLOSE_BOTH"}:
            kinds = ("CALL", "PUT")
        elif action == "CLOSE_CALL":
            kinds = ("CALL",)
        elif action == "CLOSE_PUT":
            kinds = ("PUT",)
        else:
            raise ValueError("Unsupported manual trading action")
        is_entry = action == "OPEN_STRADDLE"
        legs = [self._legs.get(kind) for kind in kinds]
        if any(leg is None for leg in legs):
            raise ValueError("Selected option contracts are not available")
        selected_legs = [leg for leg in legs if leg is not None]
        if action == "CLOSE_BOTH":
            selected_legs = [
                leg for leg in selected_legs if self._open_quantity(leg) > 0
            ]
            if not selected_legs:
                raise ValueError("No open short Call or Put positions are available to close")
        if is_entry and len(selected_legs) != 2:
            raise ValueError("Open Short Straddle requires both Call and Put contracts")
        if len(selected_legs) == 2 and (
            selected_legs[0].kind != "CALL"
            or selected_legs[1].kind != "PUT"
            or selected_legs[0].instrument.option_kind != OptionKind.CALL
            or selected_legs[1].instrument.option_kind != OptionKind.PUT
            or selected_legs[0].instrument.underlying.code != "BTC"
            or selected_legs[1].instrument.underlying.code != "BTC"
            or selected_legs[0].instrument.expiration_ns
            != selected_legs[1].instrument.expiration_ns
            or selected_legs[0].instrument.strike_price
            != selected_legs[1].instrument.strike_price
            or selected_legs[0].instrument.quote_currency
            != selected_legs[1].instrument.quote_currency
            or selected_legs[0].instrument.settlement_currency
            != selected_legs[1].instrument.settlement_currency
        ):
            raise ValueError("Open Short Straddle/Close Both requires a matching Call/Put pair")

        prices = payload.get("limit_prices") or {}
        order_details = []
        quantities: dict[str, Decimal] = {}
        for leg in selected_legs:
            instrument = leg.instrument
            if instrument.expiration_ns <= time.time_ns():
                raise ValueError(f"{instrument.id} has expired")
            if is_entry and not self._instrument_is_tradable(instrument):
                raise ValueError(
                    f"{instrument.id} has no confirmed tradable status; "
                    "entry requires a confirmed TRADING status",
                )
            if not self._quote_is_fresh(leg):
                raise ValueError(f"A fresh quote is unavailable for {instrument.id}")

            if is_entry:
                if self._open_quantity(leg) > 0 or leg.active_exit_order_id is not None:
                    raise ValueError(f"{instrument.id} already has open exposure or an exit")
                quantity = Decimal(str(payload["quantity"]))
                if not self._quantity_is_valid(instrument, quantity):
                    raise ValueError(
                        f"Quantity for {instrument.id} must respect its minimum, "
                        "maximum, and size increment",
                    )
                if not self._entry_quote_is_usable(leg):
                    raise ValueError(f"Fresh two-sided quotes are required for {instrument.id}")
                side = OrderSide.SELL
            else:
                if leg.active_exit_order_id is not None:
                    raise ValueError(f"An exit order is already pending for {instrument.id}")
                quantity = self._open_quantity(leg)
                if quantity <= 0:
                    raise ValueError(f"No remaining short quantity exists for {instrument.id}")
                if not self._exit_quote_is_usable(leg):
                    raise ValueError(f"A fresh ask quote is required to close {instrument.id}")
                side = OrderSide.BUY

            limit_price = None
            if order_type == "LIMIT":
                raw_price = prices.get(leg.kind)
                if raw_price is None:
                    raise ValueError(f"A limit price is required for {leg.kind}")
                limit_price = Decimal(str(raw_price))
                increment = Decimal(str(instrument.price_increment))
                if (
                    not limit_price.is_finite()
                    or limit_price <= 0
                    or increment <= 0
                    or limit_price % increment != 0
                ):
                    raise ValueError(
                        f"Limit price for {leg.kind} must be positive and match "
                        "the instrument price increment",
                    )
                if instrument.max_price is not None and limit_price > Decimal(
                    str(instrument.max_price),
                ):
                    raise ValueError(f"Limit price exceeds the maximum for {leg.kind}")
                if instrument.min_price is not None and limit_price < Decimal(
                    str(instrument.min_price),
                ):
                    raise ValueError(f"Limit price is below the minimum for {leg.kind}")
            quantities[leg.kind] = quantity
            order_details.append(
                {
                    "instrument": str(instrument.id),
                    "side": side.name,
                    "order_type": order_type,
                    "quantity": str(quantity),
                    "limit_price": None if limit_price is None else str(limit_price),
                },
            )

        if is_entry:
            if any(
                self._open_quantity(leg) > 0 for leg in selected_legs
            ):
                raise ValueError("Open Short Straddle requires both selected legs to be flat")
            for leg in selected_legs:
                self._prepare_leg_for_entry(leg, quantities[leg.kind])
            self._entry_started = True
            self._entry_expected_kinds = set(kinds)
            self._set_status("MANUAL_ENTRY_PENDING")
            for index, leg in enumerate(selected_legs):
                try:
                    submitted = self._submit_order(
                        leg=leg,
                        action="ENTRY",
                        side=OrderSide.SELL,
                        quantity=quantities[leg.kind],
                        order_type=order_type,
                        price=(
                            None
                            if order_type == "MARKET"
                            else Decimal(str(prices[leg.kind]))
                        ),
                        reduce_only=False,
                        manual=True,
                    )
                except Exception:
                    leg.entry_terminal = True
                    leg.entry_status = "SUBMISSION_FAILED"
                    raise
                if self._submission_uncertain:
                    self._mark_entry_legs_not_submitted(selected_legs[index + 1 :])
                    break
                if not submitted:
                    self._mark_entry_legs_not_submitted(selected_legs[index + 1 :])
                    break
            self._finish_entry_if_terminal()
            if self._submission_uncertain:
                self._publish()
                raise RuntimeError(
                    "Order submission outcome is uncertain; check Order History "
                    "and reconcile exchange state before retrying",
                )
            failed_entries = [
                leg for leg in selected_legs if leg.entry_status == "SUBMISSION_FAILED"
            ]
            if failed_entries:
                self._publish()
                failed_instruments = ", ".join(
                    str(leg.instrument.id) for leg in failed_entries
                )
                raise RuntimeError(
                    f"Entry submission failed locally for {failed_instruments}; "
                    "check Order History for any other submitted leg before retrying",
                )
        else:
            if self._exit_reason is not None:
                raise RuntimeError("An automatic risk exit is already active")
            self._manual_exit_orders.clear()
            self._manual_exit_batch_active = True
            self._set_status("MANUAL_EXIT_PENDING")
            for leg in selected_legs:
                self._submit_order(
                    leg=leg,
                    action="EXIT",
                    side=OrderSide.BUY,
                    quantity=quantities[leg.kind],
                    order_type=order_type,
                    price=(
                        None
                        if order_type == "MARKET"
                        else Decimal(str(prices[leg.kind]))
                    ),
                    reduce_only=True,
                    manual=True,
                )
                if self._submission_uncertain:
                    break
            self._finish_manual_exit_batch()

        self._publish()
        return {"orders": order_details, "environment": "Demo"}

    @staticmethod
    def _mark_entry_legs_not_submitted(legs: list[LegState]) -> None:
        for leg in legs:
            leg.entry_terminal = True
            leg.entry_status = "NOT_SUBMITTED"

    def _prepare_leg_for_entry(self, leg: LegState, quantity: Decimal) -> None:
        if leg.filled_quantity > 0 and self._open_quantity(leg) == 0:
            self._archive_closed_leg(leg)
            leg.filled_quantity = Decimal(0)
            leg.exited_quantity = Decimal(0)
            leg.entry_notional = Decimal(0)
            leg.exit_notional = Decimal(0)
            leg.exit_attempts = 0
        leg.requested_quantity = quantity
        leg.entry_terminal = False
        leg.entry_status = "NOT_SUBMITTED"
        leg.exit_status = "NOT_SUBMITTED"
        leg.actual_position_quantity = None

    def _quantity_is_valid(
        self,
        instrument: CryptoOption,
        quantity: Decimal,
    ) -> bool:
        increment = Decimal(str(instrument.size_increment))
        minimum = (
            increment
            if instrument.min_quantity is None
            else Decimal(str(instrument.min_quantity))
        )
        maximum = (
            None
            if instrument.max_quantity is None
            else Decimal(str(instrument.max_quantity))
        )
        return (
            quantity.is_finite()
            and quantity > 0
            and increment > 0
            and quantity >= minimum
            and quantity % increment == 0
            and (maximum is None or quantity <= maximum)
        )

    def _open_quantity(self, leg: LegState) -> Decimal:
        return (
            leg.remaining_quantity
            if leg.actual_position_quantity is None
            else min(leg.remaining_quantity, leg.actual_position_quantity)
        )

    def _has_open_exposure(self) -> bool:
        return any(self._open_quantity(leg) > 0 for leg in self._legs.values())

    def _has_confirmed_open_exposure(self) -> bool:
        return any(
            (
                leg.remaining_quantity > 0
                if leg.actual_position_quantity is None
                else leg.actual_position_quantity > 0
            )
            for leg in self._legs.values()
        )

    def _open_exposure_status(self) -> str:
        open_legs = [
            leg for leg in self._legs.values() if self._open_quantity(leg) > 0
        ]
        if not open_legs:
            return "FLAT"
        if len(open_legs) == 2 and all(
            self._open_quantity(leg) == leg.requested_quantity
            for leg in open_legs
        ):
            return "OPEN"
        return "PARTIALLY_OPEN"

    def _has_open_orders(self) -> bool:
        if self._order_context:
            return True
        for order in self.cache.orders_open(venue=BYBIT_VENUE):
            instrument = self.cache.instrument(order.instrument_id)
            if isinstance(instrument, CryptoOption) and instrument.underlying.code == "BTC":
                return True
        return False

    def _leg_realized_pnl(self, leg: LegState) -> Decimal:
        exposure = LegExposure(
            filled_quantity=leg.filled_quantity,
            exited_quantity=leg.exited_quantity,
            entry_notional=leg.entry_notional,
            exit_notional=leg.exit_notional,
            multiplier=leg.multiplier,
            ask_price=None,
        )
        return exposure.realized_pnl

    def _archive_closed_leg(self, leg: LegState) -> None:
        if leg.exited_quantity <= 0:
            return
        self._closed_realized_pnl += self._leg_realized_pnl(leg)
        self._closed_trade_count += 1

    def _finish_manual_exit_batch(self) -> None:
        if (
            not self._manual_exit_batch_active
            or self._manual_exit_orders
            or self._submission_uncertain
        ):
            return
        self._manual_exit_batch_active = False
        self._set_status(self._open_exposure_status())

    def _submit_entry_if_ready(self) -> None:
        if self._entry_started or set(self._legs) != {"CALL", "PUT"}:
            return
        if self._btc_reference_price is None or not self._reference_is_fresh():
            return
        if any(
            not self._instrument_is_tradable(leg.instrument)
            for leg in self._legs.values()
        ):
            return
        if any(not self._entry_quote_is_usable(leg) for leg in self._legs.values()):
            return

        self._entry_started = True
        self._entry_expected_kinds = set(self._legs)
        self._set_status("ENTRY_PENDING")
        legs = list(self._legs.items())
        for index, (kind, leg) in enumerate(legs):
            if not self._quantity_is_valid(
                leg.instrument,
                leg.requested_quantity,
            ):
                self._entry_submission_failed(
                    kind,
                    f"Requested quantity is invalid for {leg.instrument.id}",
                )
                self._mark_entry_legs_not_submitted(
                    [item for _, item in legs[index + 1 :]],
                )
                break
            submitted = self._submit_limit_order(
                leg=leg,
                action="ENTRY",
                side=OrderSide.SELL,
                quantity=leg.requested_quantity,
                price=leg.bid_price,
                reduce_only=False,
            )
            if self._submission_uncertain:
                self._mark_entry_legs_not_submitted(
                    [item for _, item in legs[index + 1 :]],
                )
                break
            if not submitted:
                self._mark_entry_legs_not_submitted(
                    [item for _, item in legs[index + 1 :]],
                )
                break
        self._finish_entry_if_terminal()

    def _submit_limit_order(
        self,
        leg: LegState,
        action: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal | None,
        reduce_only: bool,
    ) -> bool:
        return self._submit_order(
            leg=leg,
            action=action,
            side=side,
            quantity=quantity,
            order_type="LIMIT",
            price=price,
            reduce_only=reduce_only,
            manual=False,
        )

    def _submit_order(
        self,
        leg: LegState,
        action: str,
        side: OrderSide,
        quantity: Decimal,
        order_type: str,
        price: Decimal | None,
        reduce_only: bool,
        manual: bool,
    ) -> bool:
        order_type = order_type.upper()
        if quantity <= 0:
            reason = "Order quantity must be greater than zero"
            if action == "ENTRY":
                self._entry_submission_failed(leg.kind, reason)
            else:
                self._exit_submission_failed(leg, reason)
            return False
        if order_type == "LIMIT" and (price is None or price <= 0):
            reason = "Missing valid limit price or quantity"
            if action == "ENTRY":
                self._entry_submission_failed(leg.kind, reason)
            else:
                self._exit_submission_failed(leg, reason)
            return False

        try:
            if order_type == "MARKET":
                order = self.order_factory.market(
                    instrument_id=leg.instrument.id,
                    order_side=side,
                    quantity=leg.instrument.make_qty(float(quantity)),
                    time_in_force=TimeInForce.IOC,
                    reduce_only=reduce_only,
                    tags=[f"BTC_STRADDLE_{action}_{leg.kind}"],
                )
            elif order_type == "LIMIT":
                if price is None:
                    raise ValueError("A limit order requires a price")
                order = self.order_factory.limit(
                    instrument_id=leg.instrument.id,
                    order_side=side,
                    quantity=leg.instrument.make_qty(float(quantity)),
                    price=leg.instrument.make_price(float(price)),
                    time_in_force=TimeInForce.IOC,
                    reduce_only=reduce_only,
                    tags=[f"BTC_STRADDLE_{action}_{leg.kind}"],
                )
            else:
                raise ValueError(f"Unsupported order type: {order_type}")
        except Exception as exc:
            logger.exception(
                "Failed to construct %s order for %s",
                action.lower(),
                leg.instrument.id,
            )
            if action == "ENTRY":
                self._entry_submission_failed(leg.kind, str(exc))
            else:
                if not manual:
                    leg.exit_attempts += 1
                self._exit_submission_failed(leg, str(exc))
            return False

        order_id = str(order.client_order_id)
        self._order_timestamps.setdefault(
            order_id,
            datetime.now(timezone.utc).isoformat(),
        )
        self._order_context[order_id] = (leg.kind, action)
        self._order_requested_quantities[order_id] = quantity
        self._order_fills[order_id] = (Decimal(0), Decimal(0))
        self._order_types[order_id] = order_type
        if action == "EXIT":
            leg.active_exit_order_id = order_id
            if not manual:
                leg.exit_attempts += 1
            leg.exit_status = "SUBMITTED"
            if manual:
                self._manual_exit_orders.add(order_id)
        else:
            leg.entry_status = "SUBMITTED"
        self._upsert_order_record(order_id, leg, action, "SUBMITTED", None)
        try:
            self.submit_order(order)
            logger.info(
                "Submitted %s %s %s IOC for %s: quantity=%s price=%s",
                action.lower(),
                leg.kind,
                order_type.lower(),
                leg.instrument.id,
                quantity,
                price,
            )
        except Exception as exc:
            logger.exception(
                "Order submission outcome is uncertain for %s on %s",
                action.lower(),
                leg.instrument.id,
            )
            self._submission_uncertain = True
            self._set_status("RECONCILIATION_REQUIRED")
            if action == "ENTRY":
                leg.entry_status = "SUBMISSION_UNKNOWN"
            else:
                leg.exit_status = "SUBMISSION_UNKNOWN"
            self._update_order_record(
                order.client_order_id,
                leg,
                action,
                "SUBMISSION_UNKNOWN",
                None,
            )
            self._runtime_state.update(
                engine_error=f"Order submission outcome uncertain: {type(exc).__name__}: {exc}",
                connection_message=(
                    f"Submission outcome for {leg.instrument.id} is uncertain; "
                    "automatic orders are halted until exchange state is reconciled"
                ),
            )
            return False
        return True

    def _entry_submission_failed(self, leg_name: str, reason: str) -> None:
        leg = self._legs[leg_name]
        leg.entry_status = "SUBMISSION_FAILED"
        leg.entry_terminal = True
        self._runtime_state.update(connection_message=reason)
        logger.error("Entry submission failed for %s: %s", leg.instrument.id, reason)

    def _finish_entry_if_terminal(self) -> None:
        if not self._entry_started or not self._entry_expected_kinds:
            return
        if self._submission_uncertain:
            self._set_status("RECONCILIATION_REQUIRED")
            return
        if not all(
            self._legs[kind].entry_terminal
            for kind in self._entry_expected_kinds
        ):
            return

        expected_legs = [
            self._legs[kind] for kind in self._entry_expected_kinds
        ]
        if all(leg.filled_quantity == 0 for leg in expected_legs):
            self._set_status("ENTRY_FAILED")
            self._entry_expected_kinds.clear()
            return

        fully_filled_straddle = (
            len(expected_legs) == 2
            and all(
                leg.filled_quantity == leg.requested_quantity
                for leg in expected_legs
            )
        )
        if fully_filled_straddle:
            self._set_status("OPEN")
            logger.info("Both straddle legs reached their requested fill quantities")
        else:
            self._set_status("PARTIALLY_OPEN")
            logger.error(
                "Straddle entry incomplete; actual fills: %s",
                {
                    leg.kind: (
                        str(leg.filled_quantity),
                        str(leg.requested_quantity),
                    )
                    for leg in expected_legs
                },
            )
        self._entry_expected_kinds.clear()
        self._evaluate_risk()

    def _handle_order_terminal(
        self,
        client_order_id: Any,
        status: str,
        reason: str | None = None,
    ) -> None:
        order_key = str(client_order_id)
        context = self._order_context.get(order_key)
        if context is None:
            logger.warning("Received terminal event for an untracked order %s", order_key)
            return
        leg_name, action = context
        leg = self._legs[leg_name]
        order = self.cache.order(client_order_id)
        venue_order_id = None if order is None else order.venue_order_id
        self._update_order_record(
            client_order_id,
            leg,
            action,
            status,
            venue_order_id,
        )

        if action == "ENTRY":
            leg.entry_status = status
            leg.entry_terminal = True
            self._finish_entry_if_terminal()
        else:
            leg.exit_status = status
            leg.active_exit_order_id = None
            self._manual_exit_orders.discard(order_key)
            if (
                leg.exit_attempts >= _MAX_EXIT_ATTEMPTS
                and self._open_quantity(leg) > 0
            ):
                self._set_status("EXIT_FAILED_MANUAL")
                self._runtime_state.update(
                    connection_message=(
                        f"Exit for {leg.instrument.id} failed after "
                        f"{_MAX_EXIT_ATTEMPTS} confirmed attempts"
                    ),
                )
                logger.error(
                    "Exit for %s remains open after %d attempts",
                    leg.instrument.id,
                    leg.exit_attempts,
                )
        log = logger.warning if status in {"REJECTED", "DENIED"} else logger.info
        log(
            "%s %s order %s for %s%s",
            status,
            action.lower(),
            order_key,
            leg.instrument.id,
            "" if reason is None else f": {reason}",
        )
        self._finish_exit_if_flat()
        self._finish_manual_exit_batch()
        if not self._manual_exit_orders:
            self._evaluate_risk()
        self._publish()
        self._forget_order_tracking(order_key)

    def _forget_order_tracking(self, order_key: str) -> None:
        self._order_context.pop(order_key, None)
        self._order_requested_quantities.pop(order_key, None)
        self._order_fills.pop(order_key, None)
        self._order_types.pop(order_key, None)
        self._order_timestamps.pop(order_key, None)

    def _begin_exit(self, reason: str) -> None:
        if self._submission_uncertain:
            return
        if self._exit_reason is None:
            self._exit_reason = reason
            self._set_status("EXIT_PENDING")
            logger.warning("Exit triggered: %s", reason)
        self._submit_exits_if_ready()

    def _submit_exits_if_ready(self) -> None:
        if (
            self._submission_uncertain
            or self._manual_exit_orders
            or self._manual_exit_batch_active
            or self._exit_reason is None
            or self._strategy_status == "EXIT_FAILED_MANUAL"
        ):
            return
        open_legs = [
            leg for leg in self._legs.values() if self._open_quantity(leg) > 0
        ]
        if not open_legs:
            self._finish_exit_if_flat()
            return
        if any(
            not self._exit_quote_is_usable(leg) for leg in open_legs
        ):
            return

        for leg in open_legs:
            if leg.active_exit_order_id is not None:
                continue
            if leg.exit_attempts >= _MAX_EXIT_ATTEMPTS:
                self._set_status("EXIT_FAILED_MANUAL")
                self._runtime_state.update(
                    connection_message=f"Manual action is required to close {leg.instrument.id}",
                )
                return
            if not self._exit_quote_is_usable(leg) or leg.ask_price is None:
                continue
            self._submit_limit_order(
                leg=leg,
                action="EXIT",
                side=OrderSide.BUY,
                quantity=self._open_quantity(leg),
                price=leg.ask_price,
                reduce_only=True,
            )
            if self._submission_uncertain:
                break
        self._finish_exit_if_flat()

    def _exit_submission_failed(self, leg: LegState, reason: str) -> None:
        leg.active_exit_order_id = None
        leg.exit_status = "SUBMISSION_FAILED"
        logger.error("Exit submission failed for %s: %s", leg.instrument.id, reason)
        self._runtime_state.update(connection_message=reason)
        if leg.exit_attempts >= _MAX_EXIT_ATTEMPTS:
            self._set_status("EXIT_FAILED_MANUAL")

    def _finish_exit_if_flat(self) -> None:
        if self._exit_reason is None or not self._legs:
            return
        if not any(
            (
                leg.remaining_quantity > 0
                if leg.actual_position_quantity is None
                else leg.actual_position_quantity > 0
            )
            or leg.active_exit_order_id is not None
            for leg in self._legs.values()
        ):
            self._set_status("CLOSED")
            logger.info("Both straddle legs are flat; strategy position is closed")
            return
        if self._submission_uncertain:
            return
        if self._strategy_status == "EXIT_FAILED_MANUAL":
            return
        if self._has_confirmed_open_exposure():
            self._set_status("EXIT_PARTIALLY_FILLED")

    def _evaluate_risk(self) -> None:
        if (
            self._strategy_status
            not in {"OPEN", "RECOVERED_OPEN", "PARTIALLY_OPEN"}
            or self._manual_exit_batch_active
        ):
            return
        exposures = {
            name: LegExposure(
                filled_quantity=leg.filled_quantity,
                exited_quantity=leg.exited_quantity,
                entry_notional=leg.entry_notional,
                exit_notional=leg.exit_notional,
                multiplier=leg.multiplier,
                ask_price=leg.ask_price if self._exit_quote_is_usable(leg) else None,
                actual_quantity=(
                    None
                    if leg.actual_position_quantity is None
                    else self._open_quantity(leg)
                ),
            )
            for name, leg in self._legs.items()
        }
        snapshot = self._risk_manager.evaluate(exposures)
        self._last_risk_snapshot = snapshot
        if snapshot.stop_loss_triggered:
            self._begin_exit("STOP_LOSS")
        elif snapshot.take_profit_triggered:
            self._begin_exit("TAKE_PROFIT")

    def _quote_is_fresh(self, leg: LegState) -> bool:
        if leg.quote_timestamp_ns <= 0:
            return False
        age_ns = time.time_ns() - leg.quote_timestamp_ns
        return -_MAX_QUOTE_AGE_NS <= age_ns <= _MAX_QUOTE_AGE_NS

    def _instrument_is_tradable(self, instrument: CryptoOption) -> bool:
        return self._known_instrument_trading_status(instrument) is True

    def _known_instrument_trading_status(
        self,
        instrument: CryptoOption,
    ) -> bool | None:
        status = self.cache.instrument_status(instrument.id)
        if status is not None:
            return status.is_trading
        return self._instrument_trading_status.get(str(instrument.id))

    def _entry_quote_is_usable(self, leg: LegState) -> bool:
        return (
            self._quote_is_fresh(leg)
            and leg.bid_price is not None
            and leg.ask_price is not None
            and leg.bid_price > 0
            and leg.ask_price > 0
            and leg.bid_size > 0
            and leg.ask_size > 0
        )

    def _exit_quote_is_usable(self, leg: LegState) -> bool:
        return (
            self._quote_is_fresh(leg)
            and leg.ask_price is not None
            and leg.ask_price > 0
            and leg.ask_size > 0
        )

    def _reference_is_fresh(self) -> bool:
        if self._btc_reference_timestamp_ns is None:
            return False
        age_ns = time.time_ns() - self._btc_reference_timestamp_ns
        return -_MAX_QUOTE_AGE_NS <= age_ns <= _MAX_QUOTE_AGE_NS

    def _set_order_status(self, client_order_id: Any, status: str) -> None:
        context = self._order_context.get(str(client_order_id))
        if context is None:
            logger.warning("Received order status for untracked order %s", client_order_id)
            return
        leg_name, action = context
        leg = self._legs[leg_name]
        if action == "ENTRY":
            leg.entry_status = status
        else:
            leg.exit_status = status
        order = self.cache.order(client_order_id)
        self._update_order_record(
            client_order_id,
            leg,
            action,
            status,
            None if order is None else order.venue_order_id,
        )
        logger.info(
            "%s %s order %s for %s",
            status,
            action.lower(),
            client_order_id,
            leg.instrument.id,
        )
        self._publish()

    def _update_order_record(
        self,
        client_order_id: Any,
        leg: LegState,
        action: str,
        status: str,
        venue_order_id: Any,
    ) -> None:
        order_key = str(client_order_id)
        quantity = self._order_requested_quantities.get(
            order_key,
            leg.requested_quantity if action == "ENTRY" else Decimal(0),
        )
        filled, notional = self._order_fills.get(
            order_key,
            (Decimal(0), Decimal(0)),
        )
        multiplier = leg.multiplier
        average = (
            None
            if filled == 0 or multiplier == 0
            else str(notional / (filled * multiplier))
        )
        updated_at = datetime.now(timezone.utc).isoformat()
        self._runtime_state.upsert(
            "orders",
            "client_order_id",
            {
                "timestamp": self._order_timestamps.setdefault(
                    order_key,
                    updated_at,
                ),
                "updated_at": updated_at,
                "client_order_id": order_key,
                "instrument": str(leg.instrument.id),
                "option_type": leg.kind,
                "order_side": "SELL" if action == "ENTRY" else "BUY",
                "order_type": (
                    f"{self._order_types.get(order_key, 'LIMIT')} / IOC"
                ),
                "requested_quantity": str(quantity),
                "filled_quantity": str(filled),
                "average_fill_price": average,
                "status": status,
                "exchange_order_id": (
                    None if venue_order_id is None else str(venue_order_id)
                ),
            },
        )

    def _publish(self) -> None:
        positions = []
        legs_for_risk = {}
        leg_buyback_values: dict[str, Decimal | None] = {}
        leg_entry_values: dict[str, Decimal] = {}
        for name, leg in self._legs.items():
            quote_is_fresh = self._quote_is_fresh(leg)
            has_fills = leg.filled_quantity > 0
            exposure = LegExposure(
                filled_quantity=leg.filled_quantity,
                exited_quantity=leg.exited_quantity,
                entry_notional=leg.entry_notional,
                exit_notional=leg.exit_notional,
                multiplier=leg.multiplier,
                ask_price=(
                    leg.ask_price
                    if self._exit_quote_is_usable(leg)
                    else None
                ),
                actual_quantity=(
                    None
                    if leg.actual_position_quantity is None
                    else self._open_quantity(leg)
                ),
            )
            legs_for_risk[name] = exposure
            leg_buyback_values[name] = exposure.current_buyback_notional
            leg_entry_values[name] = exposure.entry_notional
            if not has_fills and self._open_quantity(leg) <= 0:
                continue
            remaining_quantity = (
                leg.remaining_quantity
                if leg.actual_position_quantity is None
                else leg.actual_position_quantity
            )
            positions.append(
                {
                    "instrument": str(leg.instrument.id),
                    "option_type": name,
                    "strike": str(leg.instrument.strike_price),
                    "expiry": datetime.fromtimestamp(
                        leg.instrument.expiration_ns / _NS_PER_SECOND,
                        tz=timezone.utc,
                    ).isoformat(),
                    "requested_quantity": str(leg.requested_quantity),
                    "filled_quantity": str(leg.filled_quantity),
                    "remaining_quantity": str(remaining_quantity),
                    "entry_premium": (
                        None
                        if leg.average_entry_price is None
                        else str(leg.average_entry_price)
                    ),
                    "current_bid": (
                        None
                        if not quote_is_fresh or leg.bid_price is None or leg.bid_price <= 0
                        else str(leg.bid_price)
                    ),
                    "current_ask": (
                        None
                        if not quote_is_fresh or leg.ask_price is None or leg.ask_price <= 0
                        else str(leg.ask_price)
                    ),
                    "current_buyback_value": (
                        None
                        if not has_fills or exposure.current_buyback_notional is None
                        else str(exposure.current_buyback_notional)
                    ),
                    "unrealized_pnl": (
                        None
                        if not has_fills or exposure.current_buyback_notional is None
                        else exposure.remaining_entry_notional
                        - exposure.current_buyback_notional
                    ),
                    "position_status": (
                        "CLOSED"
                        if has_fills
                        and remaining_quantity == 0
                        else self._strategy_status
                    ),
                    "quote_currency": leg.instrument.quote_currency.code,
                    "multiplier": str(leg.multiplier),
                    "trading_status": self._status_label(
                        self._known_instrument_trading_status(leg.instrument),
                    ),
                }
            )

        current_has_fills = any(
            leg.filled_quantity > 0 for leg in self._legs.values()
        )
        has_trade_history = (
            current_has_fills
            or self._closed_trade_count > 0
            or any(leg.exited_quantity > 0 for leg in self._legs.values())
        )
        risk = self._risk_manager.evaluate(legs_for_risk)
        selected = None
        if set(self._legs) == {"CALL", "PUT"}:
            call = self._legs["CALL"].instrument
            put = self._legs["PUT"].instrument
            call_leg = self._legs["CALL"]
            put_leg = self._legs["PUT"]

            def quote_value(leg: LegState, value: Decimal | None) -> str | None:
                if (
                    not self._quote_is_fresh(leg)
                    or value is None
                    or value <= 0
                ):
                    return None
                return str(value)

            selected = {
                "call": str(call.id),
                "put": str(put.id),
                "strike": str(call.strike_price),
                "expiry_ns": call.expiration_ns,
                "expiry": datetime.fromtimestamp(
                    call.expiration_ns / _NS_PER_SECOND,
                    tz=timezone.utc,
                ).isoformat(),
                "quote_currency": call.quote_currency.code,
                "reference_price": (
                    str(self._btc_reference_price)
                    if self._btc_reference_price is not None
                    and self._reference_is_fresh()
                    else None
                ),
                "reference_timestamp": (
                    datetime.fromtimestamp(
                        self._btc_reference_timestamp_ns / _NS_PER_SECOND,
                        tz=timezone.utc,
                    ).isoformat()
                    if self._btc_reference_timestamp_ns is not None
                    else None
                ),
                "call_bid": quote_value(call_leg, call_leg.bid_price),
                "call_ask": quote_value(call_leg, call_leg.ask_price),
                "call_quote_timestamp": (
                    datetime.fromtimestamp(
                        call_leg.quote_timestamp_ns / _NS_PER_SECOND,
                        tz=timezone.utc,
                    ).isoformat()
                    if self._quote_is_fresh(call_leg)
                    else None
                ),
                "put_bid": quote_value(put_leg, put_leg.bid_price),
                "put_ask": quote_value(put_leg, put_leg.ask_price),
                "put_quote_timestamp": (
                    datetime.fromtimestamp(
                        put_leg.quote_timestamp_ns / _NS_PER_SECOND,
                        tz=timezone.utc,
                    ).isoformat()
                    if self._quote_is_fresh(put_leg)
                    else None
                ),
                "call_trading_status": self._status_label(
                    self._known_instrument_trading_status(call),
                ),
                "put_trading_status": self._status_label(
                    self._known_instrument_trading_status(put),
                ),
            }

        self._runtime_state.update(
            strategy_status=self._strategy_status,
            selected_instruments=selected,
            positions=positions,
            pnl={
                "combined_entry_premium": (
                    str(risk.entry_notional) if current_has_fills else None
                ),
                "call_entry_premium": (
                    str(leg_entry_values["CALL"])
                    if leg_entry_values.get("CALL", Decimal(0)) > 0
                    else None
                ),
                "put_entry_premium": (
                    str(leg_entry_values["PUT"])
                    if leg_entry_values.get("PUT", Decimal(0)) > 0
                    else None
                ),
                "call_buyback_value": (
                    None
                    if leg_buyback_values.get("CALL") is None
                    or self._open_quantity(self._legs["CALL"]) <= 0
                    else str(leg_buyback_values["CALL"])
                ),
                "put_buyback_value": (
                    None
                    if leg_buyback_values.get("PUT") is None
                    or self._open_quantity(self._legs["PUT"]) <= 0
                    else str(leg_buyback_values["PUT"])
                ),
                "current_buyback_value": (
                    None
                    if not current_has_fills or risk.buyback_notional is None
                    else str(risk.buyback_notional)
                ),
                "unrealized_pnl": (
                    None
                    if not current_has_fills or risk.unrealized_pnl is None
                    else str(risk.unrealized_pnl)
                ),
                "realized_pnl": (
                    str(self._closed_realized_pnl + risk.realized_pnl)
                    if has_trade_history
                    else None
                ),
                "quote_currency": (
                    self._legs["CALL"].instrument.quote_currency.code
                    if "CALL" in self._legs
                    else None
                ),
                "stop_loss_triggered": risk.stop_loss_triggered,
                "take_profit_triggered": risk.take_profit_triggered,
                "stop_loss_percent": str(self._settings.stop_loss_percent),
                "take_profit_percent": str(self._settings.take_profit_percent),
                "exit_reason": self._exit_reason,
            },
        )

    @staticmethod
    def _status_label(is_trading: bool | None) -> str:
        if is_trading is True:
            return "TRADING"
        if is_trading is False:
            return "NOT_TRADING"
        return "UNKNOWN"

    def _set_status(self, status: str) -> None:
        self._strategy_status = status
        self._runtime_state.update(
            strategy_status=status,
            strategy_message=(
                "Straddle exposure is partial. Review actual quantities in Open "
                "Positions; recovery orders are not submitted automatically, while "
                "configured SL/TP monitoring remains active."
                if status == "PARTIALLY_OPEN"
                else None
            ),
        )
