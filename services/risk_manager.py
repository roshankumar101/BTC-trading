from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


@dataclass(frozen=True)
class LegExposure:
    filled_quantity: Decimal
    exited_quantity: Decimal
    entry_notional: Decimal
    exit_notional: Decimal
    multiplier: Decimal
    ask_price: Decimal | None
    actual_quantity: Decimal | None = None

    @property
    def remaining_quantity(self) -> Decimal:
        if self.actual_quantity is not None:
            return self.actual_quantity
        return max(Decimal(0), self.filled_quantity - self.exited_quantity)

    @property
    def average_entry_price(self) -> Decimal:
        if self.filled_quantity == 0 or self.multiplier == 0:
            return Decimal(0)
        return self.entry_notional / (self.filled_quantity * self.multiplier)

    @property
    def remaining_entry_notional(self) -> Decimal:
        return self.average_entry_price * self.remaining_quantity * self.multiplier

    @property
    def current_buyback_notional(self) -> Decimal | None:
        if self.remaining_quantity == 0:
            return Decimal(0)
        if self.ask_price is None:
            return None
        return self.ask_price * self.remaining_quantity * self.multiplier

    @property
    def realized_pnl(self) -> Decimal:
        closed_entry_basis = (
            self.average_entry_price * self.exited_quantity * self.multiplier
        )
        return closed_entry_basis - self.exit_notional


@dataclass(frozen=True)
class RiskSnapshot:
    entry_notional: Decimal
    open_entry_notional: Decimal
    buyback_notional: Decimal | None
    unrealized_pnl: Decimal | None
    realized_pnl: Decimal
    stop_loss_triggered: bool
    take_profit_triggered: bool


class RiskManager:
    def __init__(
        self,
        stop_loss_percent: Decimal,
        take_profit_percent: Decimal,
    ) -> None:
        self._stop_loss_ratio = stop_loss_percent / Decimal(100)
        self._take_profit_ratio = take_profit_percent / Decimal(100)

    def evaluate(self, legs: Mapping[str, LegExposure]) -> RiskSnapshot:
        entry_notional = sum(
            (leg.entry_notional for leg in legs.values()),
            start=Decimal(0),
        )
        open_entry_notional = sum(
            (leg.remaining_entry_notional for leg in legs.values()),
            start=Decimal(0),
        )
        buyback_values = [
            leg.current_buyback_notional for leg in legs.values()
        ]
        buyback_notional = (
            None
            if any(value is None for value in buyback_values)
            else sum(
                (value for value in buyback_values if value is not None),
                start=Decimal(0),
            )
        )
        unrealized_pnl = (
            None
            if buyback_notional is None
            else open_entry_notional - buyback_notional
        )
        realized_pnl = sum(
            (leg.realized_pnl for leg in legs.values()),
            start=Decimal(0),
        )

        has_exposure = open_entry_notional > 0
        stop_loss_triggered = (
            has_exposure
            and buyback_notional is not None
            and buyback_notional
            >= open_entry_notional * (Decimal(1) + self._stop_loss_ratio)
        )
        take_profit_triggered = (
            has_exposure
            and buyback_notional is not None
            and buyback_notional
            <= open_entry_notional * (Decimal(1) - self._take_profit_ratio)
        )
        return RiskSnapshot(
            entry_notional=entry_notional,
            open_entry_notional=open_entry_notional,
            buyback_notional=buyback_notional,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl,
            stop_loss_triggered=stop_loss_triggered,
            take_profit_triggered=take_profit_triggered,
        )
