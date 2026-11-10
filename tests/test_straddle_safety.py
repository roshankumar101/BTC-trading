from __future__ import annotations

import time
import unittest
from decimal import Decimal
from types import SimpleNamespace

from dashboard.app import ManualOrderRequest
from services.runtime_state import RuntimeState
from services.trading_runtime import TradingRuntime
from strategy.straddle import LegState, StraddleStrategy


def make_leg(kind: str, requested: str = "1", filled: str = "0") -> LegState:
    return LegState(
        instrument=SimpleNamespace(id=kind, multiplier=Decimal("1")),
        kind=kind,
        requested_quantity=Decimal(requested),
        filled_quantity=Decimal(filled),
        entry_terminal=True,
    )


class EntryBatchHarness:
    _finish_entry_if_terminal = StraddleStrategy._finish_entry_if_terminal

    def __init__(self, call: LegState, put: LegState) -> None:
        self._entry_started = True
        self._entry_expected_kinds = {"CALL", "PUT"}
        self._submission_uncertain = False
        self._legs = {"CALL": call, "PUT": put}
        self._runtime_state = RuntimeState()
        self._strategy_status = "INITIALIZING"
        self.statuses: list[str] = []
        self.exit_attempts = 0
        self.risk_checks = 0

    def _set_status(self, status: str) -> None:
        self.statuses.append(status)
        StraddleStrategy._set_status(self, status)

    def _begin_exit(self, _: str) -> None:
        self.exit_attempts += 1

    def _evaluate_risk(self) -> None:
        self.risk_checks += 1


class CommandHarness:
    _process_engine_commands = StraddleStrategy._process_engine_commands

    def __init__(self, state: RuntimeState) -> None:
        self._runtime_state = state

    def _apply_manual_order(self, _: dict) -> dict:
        raise RuntimeError("Order submission outcome is uncertain")


class InstrumentStatusHarness:
    _known_instrument_trading_status = StraddleStrategy._known_instrument_trading_status
    _instrument_is_tradable = StraddleStrategy._instrument_is_tradable

    def __init__(self, status: bool | None) -> None:
        self.cache = SimpleNamespace(
            instrument_status=lambda _: (
                None if status is None else SimpleNamespace(is_trading=status)
            ),
        )
        self._instrument_trading_status: dict[str, bool | None] = {}


class PrepareLegHarness:
    _prepare_leg_for_entry = StraddleStrategy._prepare_leg_for_entry

    def _open_quantity(self, leg: LegState) -> Decimal:
        if leg.actual_position_quantity is None:
            return leg.remaining_quantity
        return leg.actual_position_quantity

    def _archive_closed_leg(self, _: LegState) -> None:
        self.archived = True


class OrderRecordHarness:
    _update_order_record = StraddleStrategy._update_order_record

    def __init__(self) -> None:
        self._runtime_state = RuntimeState()
        self._order_requested_quantities = {"order-1": Decimal("1")}
        self._order_fills = {"order-1": (Decimal("0"), Decimal("0"))}
        self._order_types = {"order-1": "LIMIT"}
        self._order_timestamps: dict[str, str] = {}


class SinglePollStop:
    def __init__(self) -> None:
        self.waits = 0

    def wait(self, _: float) -> bool:
        self.waits += 1
        return self.waits > 1


class StraddleSafetyTests(unittest.TestCase):
    def test_only_requested_full_fills_set_open_status(self) -> None:
        harness = EntryBatchHarness(
            make_leg("CALL", filled="1"),
            make_leg("PUT", filled="1"),
        )

        harness._finish_entry_if_terminal()

        self.assertEqual(harness.statuses, ["OPEN"])
        self.assertEqual(harness.risk_checks, 1)

    def test_equal_underfills_are_reported_without_recovery_orders(self) -> None:
        harness = EntryBatchHarness(
            make_leg("CALL", filled="0.5"),
            make_leg("PUT", filled="0.5"),
        )

        harness._finish_entry_if_terminal()

        self.assertEqual(harness.statuses, ["PARTIALLY_OPEN"])
        self.assertEqual(harness.exit_attempts, 0)
        self.assertIn(
            "recovery orders are not submitted automatically",
            harness._runtime_state.snapshot()["strategy_message"],
        )

    def test_asymmetric_fills_are_reported_without_recovery_orders(self) -> None:
        harness = EntryBatchHarness(
            make_leg("CALL", filled="1"),
            make_leg("PUT", filled="0"),
        )

        harness._finish_entry_if_terminal()

        self.assertEqual(harness.statuses, ["PARTIALLY_OPEN"])
        self.assertEqual(harness.exit_attempts, 0)

    def test_no_fills_report_entry_failure(self) -> None:
        harness = EntryBatchHarness(make_leg("CALL"), make_leg("PUT"))

        harness._finish_entry_if_terminal()

        self.assertEqual(harness.statuses, ["ENTRY_FAILED"])
        self.assertEqual(harness.risk_checks, 0)

    def test_uncertain_manual_submission_resolves_as_failure(self) -> None:
        state = RuntimeState()
        command = state.enqueue_command("manual_order", {})

        CommandHarness(state)._process_engine_commands()

        self.assertEqual(
            command.result,
            {
                "ok": False,
                "message": "Order submission outcome is uncertain",
            },
        )

    def test_api_accepts_only_the_combined_entry_action(self) -> None:
        from pydantic import ValidationError

        request = ManualOrderRequest(action="OPEN_STRADDLE", quantity="1")
        self.assertEqual(request.action, "OPEN_STRADDLE")
        with self.assertRaises(ValidationError):
            ManualOrderRequest(action="SELL_BOTH", quantity="1")

    def test_unknown_instrument_status_is_not_tradable(self) -> None:
        harness = InstrumentStatusHarness(None)

        self.assertIsNone(
            harness._known_instrument_trading_status(SimpleNamespace(id="BTC")),
        )
        self.assertFalse(harness._instrument_is_tradable(SimpleNamespace(id="BTC")))

    def test_confirmed_instrument_status_controls_tradability(self) -> None:
        self.assertTrue(
            InstrumentStatusHarness(True)._instrument_is_tradable(
                SimpleNamespace(id="BTC"),
            ),
        )
        self.assertFalse(
            InstrumentStatusHarness(False)._instrument_is_tradable(
                SimpleNamespace(id="BTC"),
            ),
        )

    def test_reentry_resets_the_existing_leg_in_place(self) -> None:
        leg = make_leg("CALL", filled="1")
        leg.exited_quantity = Decimal("1")
        leg.entry_notional = Decimal("2")
        leg.exit_notional = Decimal("1")
        leg.actual_position_quantity = Decimal("0")
        leg.quote_timestamp_ns = 123
        harness = PrepareLegHarness()

        harness._prepare_leg_for_entry(leg, Decimal("2"))

        self.assertTrue(harness.archived)
        self.assertEqual(leg.requested_quantity, Decimal("2"))
        self.assertEqual(leg.filled_quantity, Decimal(0))
        self.assertEqual(leg.exited_quantity, Decimal(0))
        self.assertEqual(leg.entry_notional, Decimal(0))
        self.assertEqual(leg.exit_notional, Decimal(0))
        self.assertEqual(leg.quote_timestamp_ns, 123)

    def test_order_timestamp_is_preserved_when_status_changes(self) -> None:
        harness = OrderRecordHarness()
        leg = make_leg("CALL")

        harness._update_order_record("order-1", leg, "ENTRY", "SUBMITTED", None)
        first = harness._runtime_state.snapshot()["orders"][0]
        time.sleep(0.001)
        harness._update_order_record("order-1", leg, "ENTRY", "ACKNOWLEDGED", None)
        updated = harness._runtime_state.snapshot()["orders"][0]

        self.assertEqual(updated["timestamp"], first["timestamp"])
        self.assertGreaterEqual(updated["updated_at"], first["updated_at"])

    def test_runtime_tracks_data_and_execution_connectivity_separately(self) -> None:
        runtime = SimpleNamespace(
            _connection_monitor_stop=SinglePollStop(),
            _node=SimpleNamespace(
                kernel=SimpleNamespace(
                    data_engine=SimpleNamespace(check_connected=lambda: True),
                    exec_engine=SimpleNamespace(check_connected=lambda: False),
                ),
            ),
            _state=RuntimeState(),
        )

        TradingRuntime._monitor_connections(runtime)

        snapshot = runtime._state.snapshot()
        self.assertTrue(snapshot["data_connected"])
        self.assertFalse(snapshot["execution_connected"])
        self.assertFalse(snapshot["connected"])
        self.assertIn("execution engine is disconnected", snapshot["connection_message"])


if __name__ == "__main__":
    unittest.main()
