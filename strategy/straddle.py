from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.bybit import BYBIT_VENUE
from nautilus_trader.model.data import IndexPriceUpdate, QuoteTick
from nautilus_trader.model.enums import OptionKind, OrderSide, PositionSide, TimeInForce
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
from services.instrument_selector import select_btc_atm_pair
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
        self._entry_started = False
        self._exit_reason: str | None = None
        self._emergency_exit = False
        self._submission_uncertain = False
        self._btc_reference_price: Decimal | None = None
        self._btc_reference_timestamp_ns: int | None = None
        self._index_subscriptions: set[str] = set()
        self._quote_subscriptions: set[str] = set()
        self._strategy_status = "INITIALIZING"
        self._last_risk_snapshot = None

    def on_start(self) -> None:
        self._runtime_state.update(
            connected=True,
            connection_message="Connected to Bybit Demo",
            engine_error=None,
        )
        logger.info("Connected to Bybit Demo")
        self._btc_options = [
            instrument
            for instrument in self.cache.instruments(venue=BYBIT_VENUE)
            if isinstance(instrument, CryptoOption)
            and instrument.underlying.code == "BTC"
        ]
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

        self._subscribe_index(first_live_option)
        self._set_status("WAITING_FOR_REFERENCE_PRICE")
        logger.info("Subscribed to BTC option index price feed")
        self._publish()

    def on_stop(self) -> None:
        logger.info("Bybit Demo strategy stopped")
        self._runtime_state.update(
            connected=False,
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
        if order is not None and order.is_closed:
            if action == "ENTRY":
                leg.entry_status = "FILLED"
                leg.entry_terminal = True
            else:
                leg.exit_status = "FILLED"
                leg.active_exit_order_id = None

        self._update_order_record(
            event.client_order_id,
            leg,
            action,
            "FILLED" if order is not None and order.is_closed else "PARTIALLY_FILLED",
            event.venue_order_id,
        )
        self._runtime_state.append(
            "trades",
            {
                "timestamp": datetime.fromtimestamp(
                    event.ts_event / _NS_PER_SECOND,
                    tz=timezone.utc,
                ).isoformat(),
                "instrument": str(event.instrument_id),
                "order_side": event.order_side.name,
                "order_type": event.order_type.name,
                "quantity": str(quantity),
                "filled_quantity": str(quantity),
                "average_fill_price": str(price),
                "status": "FILLED",
                "exchange_order_id": str(event.venue_order_id),
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
        self._publish()

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

        if btc_open_orders or len(btc_positions) != 2:
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
                        "Existing BTC option exposure is not a single short Call/Put pair; "
                        "automatic trading is halted"
                    ),
                )
                logger.error("Existing BTC option exposure is not a single short straddle")
                self._publish()
                return True
            positions_by_kind[instrument.option_kind] = (position, instrument)

        call_data = positions_by_kind.get(OptionKind.CALL)
        put_data = positions_by_kind.get(OptionKind.PUT)
        if call_data is None or put_data is None:
            self._set_status("RECONCILIATION_REQUIRED")
            self._submission_uncertain = True
            self._runtime_state.update(
                connection_message="Existing positions do not contain a Call and Put pair",
            )
            logger.error("Existing BTC option positions do not contain both option kinds")
            self._publish()
            return True

        call_position, call_instrument = call_data
        put_position, put_instrument = put_data
        if (
            call_instrument.expiration_ns != put_instrument.expiration_ns
            or call_instrument.strike_price != put_instrument.strike_price
            or call_instrument.quote_currency != put_instrument.quote_currency
            or call_instrument.settlement_currency != put_instrument.settlement_currency
        ):
            self._set_status("RECONCILIATION_REQUIRED")
            self._submission_uncertain = True
            self._runtime_state.update(
                connection_message="Existing Call and Put do not form a matching straddle",
            )
            logger.error("Existing BTC option positions do not form a matching straddle")
            self._publish()
            return True

        for kind, position, instrument in (
            ("CALL", call_position, call_instrument),
            ("PUT", put_position, put_instrument),
        ):
            quantity = abs(Decimal(str(position.quantity)))
            average_price = Decimal(str(position.avg_px_open))
            leg = LegState(
                instrument=instrument,
                kind=kind,
                requested_quantity=quantity,
                filled_quantity=quantity,
                entry_notional=average_price * quantity * Decimal(str(instrument.multiplier)),
                entry_status="RECONCILED",
                actual_position_quantity=quantity,
            )
            self._legs[kind] = leg
            self._subscribe_quote(leg.instrument)
            self._subscribe_index(leg.instrument)

        self._entry_started = True
        self._set_status("RECOVERED_OPEN")
        logger.info("Reconciled one matching short BTC option straddle from Bybit state")
        self._publish()
        return True

    def _select_and_subscribe(self) -> None:
        if self._btc_reference_price is None:
            return
        pair = select_btc_atm_pair(
            self.cache.instruments(venue=BYBIT_VENUE),
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

        call, put = pair
        call_quantity = Decimal(str(call.make_qty(float(self._settings.straddle_quantity))))
        put_quantity = Decimal(str(put.make_qty(float(self._settings.straddle_quantity))))
        self._legs = {
            "CALL": LegState(call, "CALL", call_quantity),
            "PUT": LegState(put, "PUT", put_quantity),
        }
        quantity_is_valid = not (
            call_quantity != self._settings.straddle_quantity
            or put_quantity != self._settings.straddle_quantity
            or call_quantity <= 0
            or put_quantity <= 0
        )
        if not quantity_is_valid:
            self._entry_started = True
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
        self._subscribe_quote(call)
        self._subscribe_quote(put)
        self._subscribe_index(call)
        self._subscribe_index(put)
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

    def _submit_entry_if_ready(self) -> None:
        if self._entry_started or set(self._legs) != {"CALL", "PUT"}:
            return
        if self._btc_reference_price is None or not self._reference_is_fresh():
            return
        if any(not self._entry_quote_is_usable(leg) for leg in self._legs.values()):
            return

        self._entry_started = True
        self._set_status("ENTRY_PENDING")
        for kind, leg in self._legs.items():
            if leg.requested_quantity < Decimal(str(leg.instrument.min_quantity or 0)):
                self._entry_submission_failed(
                    kind,
                    f"Requested quantity is below the instrument minimum for {leg.instrument.id}",
                )
                continue
            if (
                leg.instrument.max_quantity is not None
                and leg.requested_quantity > Decimal(str(leg.instrument.max_quantity))
            ):
                self._entry_submission_failed(
                    kind,
                    f"Requested quantity exceeds the instrument maximum for {leg.instrument.id}",
                )
                continue
            self._submit_limit_order(
                leg=leg,
                action="ENTRY",
                side=OrderSide.SELL,
                quantity=leg.requested_quantity,
                price=leg.bid_price,
                reduce_only=False,
            )
        self._finish_entry_if_terminal()

    def _submit_limit_order(
        self,
        leg: LegState,
        action: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal | None,
        reduce_only: bool,
    ) -> None:
        if price is None or price <= 0 or quantity <= 0:
            if action == "ENTRY":
                self._entry_submission_failed(leg.kind, "Missing valid limit price or quantity")
            else:
                self._exit_submission_failed(leg, "Missing valid exit price or quantity")
            return

        try:
            order = self.order_factory.limit(
                instrument_id=leg.instrument.id,
                order_side=side,
                quantity=leg.instrument.make_qty(float(quantity)),
                price=leg.instrument.make_price(float(price)),
                time_in_force=TimeInForce.IOC,
                reduce_only=reduce_only,
                tags=[f"BTC_STRADDLE_{action}_{leg.kind}"],
            )
        except Exception as exc:
            logger.exception(
                "Failed to construct %s order for %s",
                action.lower(),
                leg.instrument.id,
            )
            if action == "ENTRY":
                self._entry_submission_failed(leg.kind, str(exc))
            else:
                leg.exit_attempts += 1
                self._exit_submission_failed(leg, str(exc))
            return

        order_id = str(order.client_order_id)
        self._order_context[order_id] = (leg.kind, action)
        self._order_requested_quantities[order_id] = quantity
        self._order_fills[order_id] = (Decimal(0), Decimal(0))
        if action == "EXIT":
            leg.active_exit_order_id = order_id
            leg.exit_attempts += 1
            leg.exit_status = "SUBMITTED"
        else:
            leg.entry_status = "SUBMITTED"
        self._upsert_order_record(order_id, leg, action, "SUBMITTED", None)
        try:
            self.submit_order(order)
            logger.info(
                "Submitted %s %s limit IOC for %s: quantity=%s price=%s",
                action.lower(),
                leg.kind,
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

    def _entry_submission_failed(self, leg_name: str, reason: str) -> None:
        leg = self._legs[leg_name]
        leg.entry_status = "SUBMISSION_FAILED"
        leg.entry_terminal = True
        self._runtime_state.update(connection_message=reason)
        logger.error("Entry submission failed for %s: %s", leg.instrument.id, reason)

    def _finish_entry_if_terminal(self) -> None:
        if not self._entry_started or not self._legs:
            return
        if self._submission_uncertain:
            self._set_status("RECONCILIATION_REQUIRED")
            return
        if not all(leg.entry_terminal for leg in self._legs.values()):
            return

        call = self._legs["CALL"]
        put = self._legs["PUT"]
        if call.filled_quantity == 0 and put.filled_quantity == 0:
            self._set_status("ENTRY_FAILED")
            return

        if call.filled_quantity != put.filled_quantity:
            self._begin_exit("UNBALANCED_ENTRY", emergency=True)
            return

        self._set_status("OPEN")
        logger.info(
            "Short straddle established with actual filled quantity %s per leg",
            call.filled_quantity,
        )
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
            if leg.exit_attempts >= _MAX_EXIT_ATTEMPTS and leg.remaining_quantity > 0:
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
        self._publish()

    def _begin_exit(self, reason: str, emergency: bool = False) -> None:
        if self._submission_uncertain:
            return
        if self._exit_reason is None:
            self._exit_reason = reason
            self._emergency_exit = emergency
            self._set_status("EXIT_PENDING")
            logger.warning("Exit triggered: %s", reason)
        self._submit_exits_if_ready()

    def _submit_exits_if_ready(self) -> None:
        if (
            self._submission_uncertain
            or self._exit_reason is None
            or self._strategy_status == "EXIT_FAILED_MANUAL"
        ):
            return
        open_legs = [
            leg for leg in self._legs.values() if leg.remaining_quantity > 0
        ]
        if not open_legs:
            self._finish_exit_if_flat()
            return
        if not self._emergency_exit and any(
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
                quantity=leg.remaining_quantity,
                price=leg.ask_price,
                reduce_only=True,
            )
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
            leg.remaining_quantity > 0 or leg.active_exit_order_id is not None
            for leg in self._legs.values()
        ):
            self._set_status("CLOSED")
            logger.info("Both straddle legs are flat; strategy position is closed")
            return
        if self._submission_uncertain:
            return
        if self._strategy_status == "EXIT_FAILED_MANUAL":
            return
        if any(leg.remaining_quantity > 0 for leg in self._legs.values()):
            self._set_status("EXIT_PARTIALLY_FILLED")

    def _evaluate_risk(self) -> None:
        if self._strategy_status not in {"OPEN", "RECOVERED_OPEN"}:
            return
        exposures = {
            name: LegExposure(
                filled_quantity=leg.filled_quantity,
                exited_quantity=leg.exited_quantity,
                entry_notional=leg.entry_notional,
                exit_notional=leg.exit_notional,
                multiplier=leg.multiplier,
                ask_price=leg.ask_price if self._exit_quote_is_usable(leg) else None,
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
        self._runtime_state.upsert(
            "orders",
            "client_order_id",
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "client_order_id": order_key,
                "instrument": str(leg.instrument.id),
                "option_type": leg.kind,
                "order_side": "SELL" if action == "ENTRY" else "BUY",
                "order_type": "LIMIT / IOC",
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
            )
            legs_for_risk[name] = exposure
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
                    "remaining_quantity": str(
                        leg.remaining_quantity
                        if leg.actual_position_quantity is None
                        else leg.actual_position_quantity
                    ),
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
                        and (
                            leg.remaining_quantity
                            if leg.actual_position_quantity is None
                            else leg.actual_position_quantity
                        )
                        == 0
                        else self._strategy_status
                    ),
                    "quote_currency": leg.instrument.quote_currency.code,
                    "multiplier": str(leg.multiplier),
                }
            )

        has_fills = any(leg.filled_quantity > 0 for leg in self._legs.values())
        risk = self._risk_manager.evaluate(legs_for_risk)
        selected = None
        if set(self._legs) == {"CALL", "PUT"}:
            call = self._legs["CALL"].instrument
            put = self._legs["PUT"].instrument
            selected = {
                "call": str(call.id),
                "put": str(put.id),
                "strike": str(call.strike_price),
                "expiry": datetime.fromtimestamp(
                    call.expiration_ns / _NS_PER_SECOND,
                    tz=timezone.utc,
                ).isoformat(),
                "quote_currency": call.quote_currency.code,
            }

        self._runtime_state.update(
            strategy_status=self._strategy_status,
            selected_instruments=selected,
            positions=positions,
            pnl={
                "combined_entry_premium": (
                    str(risk.entry_notional) if has_fills else None
                ),
                "current_buyback_value": (
                    None
                    if not has_fills or risk.buyback_notional is None
                    else str(risk.buyback_notional)
                ),
                "unrealized_pnl": (
                    None
                    if not has_fills or risk.unrealized_pnl is None
                    else str(risk.unrealized_pnl)
                ),
                "realized_pnl": str(risk.realized_pnl) if has_fills else None,
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

    def _set_status(self, status: str) -> None:
        self._strategy_status = status
