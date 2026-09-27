from __future__ import annotations

from dataclasses import dataclass

from app.config import ExecutionMode, Settings, TradingRules
from app.database.repository import Repository
from app.services.mt5_service import OrderResult, Quote
from app.trading.execution_controller import ExecutionController
from app.trading.models import ExecutionStatus, TradeSignal, ValidationStatus
from app.trading.processor import SignalProcessor
from app.utils.health import RuntimeState
from tests.fixtures import ZONE_BUY


@dataclass
class FakeMt5:
    verified: bool = True

    def symbol_exists(self, symbol: str) -> bool:
        del symbol
        return True

    def ensure_symbol(self, symbol: str) -> bool:
        del symbol
        return True

    def get_bid_ask(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, bid=3640.0, ask=3640.5, spread=0.5)

    def get_positions(self) -> list[object]:
        return []

    def submit_demo_order(self, signal: TradeSignal, rules: TradingRules) -> OrderResult:
        del rules
        return OrderResult(
            ok=True,
            retcode=10009,
            comment="done",
            ticket=555,
            order=555,
            deal=777,
            volume=signal.volume or 0.01,
            price=signal.entry_price or 1.0,
            request={"symbol": signal.normalized_symbol or ""},
            response={"retcode": 10009},
        )


def test_observe_never_sends(processor: SignalProcessor) -> None:
    signal = processor.handle_new_message(1001, 50, ZONE_BUY)
    assert signal is not None
    assert signal.execution_status is ExecutionStatus.OBSERVED
    latest = processor._state.loop_events[0]
    assert latest.order_sent is False


def test_auto_demo_dry_run_does_not_send(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    settings.execution_mode = ExecutionMode.AUTO_DEMO
    settings.dry_run = True
    state = RuntimeState()
    execution = ExecutionController(settings, rules, FakeMt5(), repository)
    processor = SignalProcessor(settings, rules, aliases, repository, state, execution)
    signal = processor.handle_new_message(1001, 80, ZONE_BUY)
    assert signal is not None
    assert signal.execution_status is ExecutionStatus.DRY_RUN
    assert state.loop_events[0].order_sent is False
    assert "Keep orders off" in state.loop_events[0].outcome


def test_auto_demo_sends_when_orders_allowed(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    settings.execution_mode = ExecutionMode.AUTO_DEMO
    settings.dry_run = False
    state = RuntimeState()
    execution = ExecutionController(settings, rules, FakeMt5(), repository)
    processor = SignalProcessor(settings, rules, aliases, repository, state, execution)
    signal = processor.handle_new_message(1001, 81, ZONE_BUY)
    assert signal is not None
    assert signal.validation_status is ValidationStatus.PARSED
    assert state.loop_events[0].order_sent is False
    assert repository.get_setup(1001, 81) is not None
    assert repository.get_setup(1001, 81).state.value == "WAITING_ENTRY"


def test_auto_demo_fails_without_mt5(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    settings.execution_mode = ExecutionMode.AUTO_DEMO
    settings.dry_run = False
    state = RuntimeState()
    execution = ExecutionController(settings, rules, None, repository)
    processor = SignalProcessor(settings, rules, aliases, repository, state, execution)
    signal = processor.handle_new_message(1001, 82, ZONE_BUY)
    assert signal is not None
    assert signal.validation_status is ValidationStatus.PARSED
    assert state.loop_events[0].order_sent is False
    assert repository.get_setup(1001, 82) is not None


def test_decide_observe_on_parsed() -> None:
    settings = Settings(
        execution_mode="OBSERVE",
        dry_run=True,
        database_url="sqlite:///:memory:",
    )
    controller = ExecutionController(settings)
    signal = TradeSignal(validation_status=ValidationStatus.PARSED)
    assert controller.decide(signal) is ExecutionStatus.OBSERVED
