from __future__ import annotations

from app.config import ExecutionMode, Settings, TradingRules
from app.database.repository import Repository
from app.services.mt5_service import OrderResult, Quote
from app.trading.broker import BrokerPosition
from app.trading.execution_controller import ExecutionController
from app.trading.models import Direction, SetupState
from app.trading.processor import SignalProcessor
from app.trading.setup_manager import SetupManager
from app.utils.health import RuntimeState
from tests.fixtures import ZONE_BUY, ZONE_EUR, ZONE_SELL


class MemoryBroker:
    def __init__(self) -> None:
        self.verified = True
        self.quotes: dict[str, Quote] = {}
        self.positions: list[BrokerPosition] = []
        self.history: set[int] = set()
        self.next_ticket = 100
        self.fail_comments: set[str] = set()
        self.opened: list[str] = []

    def quote(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    def list_tracked_positions(self) -> list[BrokerPosition]:
        return list(self.positions)

    def history_contains(self, ticket: int) -> bool:
        return ticket in self.history

    def open_market_leg(
        self,
        *,
        symbol: str,
        direction: Direction,
        volume: float,
        stop_loss: float,
        take_profit: float,
        comment: str,
    ) -> OrderResult:
        del direction
        if comment in self.fail_comments:
            return OrderResult(
                ok=False,
                retcode=10006,
                comment="rejected",
                ticket=None,
                order=None,
                deal=None,
                volume=volume,
                price=None,
                request={"comment": comment},
                response={},
                error_text="rejected",
            )
        ticket = self.next_ticket
        self.next_ticket += 1
        self.positions.append(
            BrokerPosition(
                ticket=ticket,
                symbol=symbol,
                sl=stop_loss,
                tp=take_profit,
                volume=volume,
                comment=comment,
                magic=260921,
            )
        )
        self.opened.append(comment)
        return OrderResult(
            ok=True,
            retcode=10009,
            comment="done",
            ticket=ticket,
            order=ticket,
            deal=ticket,
            volume=volume,
            price=None,
            request={"comment": comment},
            response={},
        )

    def modify_stop_loss(self, ticket: int, stop_loss: float) -> OrderResult:
        for position in self.positions:
            if position.ticket == ticket:
                position.sl = stop_loss
                return OrderResult(
                    ok=True,
                    retcode=10009,
                    comment="modified",
                    ticket=ticket,
                    order=ticket,
                    deal=None,
                    volume=position.volume,
                    price=None,
                    request={"sl": stop_loss},
                    response={},
                )
        return OrderResult(
            ok=False,
            retcode=None,
            comment="missing",
            ticket=ticket,
            order=None,
            deal=None,
            volume=None,
            price=None,
            request={},
            response={},
            error_text="Position is not open.",
        )

    def close_position(self, ticket: int) -> OrderResult:
        self.positions = [item for item in self.positions if item.ticket != ticket]
        self.history.add(ticket)
        return OrderResult(
            ok=True,
            retcode=10009,
            comment="closed",
            ticket=ticket,
            order=ticket,
            deal=ticket,
            volume=0.01,
            price=None,
            request={},
            response={},
        )


def _wire(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> tuple[SignalProcessor, SetupManager, MemoryBroker]:
    settings.execution_mode = ExecutionMode.AUTO_DEMO
    settings.dry_run = False
    state = RuntimeState()
    state.mt5_connected = True
    broker = MemoryBroker()
    execution = ExecutionController(settings, rules, None, repository)
    manager = SetupManager(settings, rules, repository, broker, state)
    processor = SignalProcessor(
        settings, rules, aliases, repository, state, execution, manager
    )
    return processor, manager, broker


def test_waits_until_price_enters_then_opens_two_legs(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    processor, manager, broker = _wire(settings, rules, aliases, repository)
    processor.handle_new_message(1001, 92841, ZONE_BUY)
    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4328.0, ask=4328.4, spread=0.4)
    manager.tick()
    assert broker.opened == []
    setup = repository.get_setup(1001, 92841)
    assert setup is not None
    assert setup.state is SetupState.WAITING_ENTRY

    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4322.2, ask=4322.6, spread=0.4)
    manager.tick()
    setup = repository.get_setup(1001, 92841)
    assert setup is not None
    assert setup.state is SetupState.ACTIVE
    assert broker.opened == ["TG_92841_TP1", "TG_92841_TP2"]
    tps = sorted(position.tp for position in broker.positions)
    assert tps == [4340.0, 4345.0]
    assert all(position.volume == 0.01 for position in broker.positions)
    assert all(position.sl == 4315 for position in broker.positions)


def test_buy_and_sell_break_even(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    processor, manager, broker = _wire(settings, rules, aliases, repository)
    processor.handle_new_message(1001, 1, ZONE_BUY)
    processor.handle_new_message(1001, 2, ZONE_SELL)
    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4322.2, ask=4322.6, spread=0.4)
    manager.tick()
    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4326.0, ask=4326.4, spread=0.4)
    manager.tick()
    buy = repository.get_setup(1001, 1)
    sell = repository.get_setup(1001, 2)
    assert buy is not None and sell is not None
    assert buy.break_even_applied is True
    assert buy.state is SetupState.BREAK_EVEN
    assert sell.break_even_applied is False
    buy_positions = [item for item in broker.positions if item.comment.startswith("TG_1_")]
    assert {item.sl for item in buy_positions} == {4322.0}

    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4318.6, ask=4319.0, spread=0.4)
    manager.tick()
    sell = repository.get_setup(1001, 2)
    assert sell is not None
    assert sell.break_even_applied is True
    sell_positions = [item for item in broker.positions if item.comment.startswith("TG_2_")]
    assert {item.sl for item in sell_positions} == {4323.0}


def test_partial_close_and_break_even_repairs_only_open_leg(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    processor, manager, broker = _wire(settings, rules, aliases, repository)
    processor.handle_new_message(1001, 7, ZONE_BUY)
    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4322.2, ask=4322.6, spread=0.4)
    manager.tick()
    first = next(item for item in broker.positions if item.comment.endswith("TP1"))
    broker.positions = [item for item in broker.positions if item.ticket != first.ticket]
    broker.history.add(first.ticket)
    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4326.0, ask=4326.4, spread=0.4)
    manager.tick()
    setup = repository.get_setup(1001, 7)
    assert setup is not None
    assert setup.state is SetupState.PARTIALLY_CLOSED
    assert setup.break_even_applied is True
    assert len(broker.positions) == 1
    assert broker.positions[0].sl == 4322.0


def test_close_before_entry_never_opens(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    processor, manager, broker = _wire(settings, rules, aliases, repository)
    processor.handle_new_message(1001, 3, ZONE_BUY)
    outcome = manager.request_close(1001, 3)
    assert outcome == "CANCELLED"
    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4322.2, ask=4322.6, spread=0.4)
    manager.tick()
    assert broker.opened == []
    setup = repository.get_setup(1001, 3)
    assert setup is not None
    assert setup.state is SetupState.CANCELLED


def test_close_reply_closes_only_that_setup(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    processor, manager, broker = _wire(settings, rules, aliases, repository)
    processor.handle_new_message(1001, 11, ZONE_BUY)
    processor.handle_new_message(1001, 12, ZONE_EUR)
    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4322.2, ask=4322.6, spread=0.4)
    broker.quotes["EURUSD"] = Quote("EURUSD", bid=1.1002, ask=1.1004, spread=0.0002)
    manager.tick()
    assert len(broker.positions) == 4
    assert manager.request_close(1001, None) == "UNKNOWN_CLOSE_SETUP"
    assert len(broker.positions) == 4
    processor.handle_new_message(1001, 99, "close setup", reply_to_message_id=11)
    assert repository.get_setup(1001, 11) is not None
    assert repository.get_setup(1001, 11).state is SetupState.CLOSED_BY_SIGNAL
    remaining = {item.comment for item in broker.positions}
    assert remaining == {"TG_12_TP1", "TG_12_TP2"}


def test_restart_does_not_open_a_second_pair(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    processor, manager, broker = _wire(settings, rules, aliases, repository)
    processor.handle_new_message(1001, 15, ZONE_BUY)
    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4322.2, ask=4322.6, spread=0.4)
    manager.tick()
    opened = list(broker.opened)
    restored = Repository(repository._session_factory)
    assert restored.get_setup(1001, 15) is not None
    again = SetupManager(settings, rules, restored, broker, RuntimeState())
    again.tick()
    assert broker.opened == opened
    assert len(broker.positions) == 2


def test_break_even_flag_repairs_old_stop(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    processor, manager, broker = _wire(settings, rules, aliases, repository)
    processor.handle_new_message(1001, 16, ZONE_BUY)
    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4326.0, ask=4322.6, spread=0.2)
    manager.tick()
    setup = repository.get_setup(1001, 16)
    assert setup is not None
    setup.break_even_applied = True
    repository.save_setup(setup)
    for position in broker.positions:
        position.sl = 4315
    manager.tick()
    assert {item.sl for item in broker.positions} == {4322.0}


def test_failed_leg_is_retried_without_duplicating_the_first(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    processor, manager, broker = _wire(settings, rules, aliases, repository)
    processor.handle_new_message(1001, 17, ZONE_BUY)
    broker.fail_comments.add("TG_17_TP2")
    broker.quotes["XAUUSD"] = Quote("XAUUSD", bid=4322.2, ask=4322.6, spread=0.4)
    manager.tick()
    setup = repository.get_setup(1001, 17)
    assert setup is not None
    assert setup.state is SetupState.ERROR
    assert setup.trade_1_ticket is not None
    assert setup.trade_2_ticket is None
    broker.fail_comments.clear()
    manager.tick()
    setup = repository.get_setup(1001, 17)
    assert setup is not None
    assert setup.trade_2_ticket is not None
    assert broker.opened.count("TG_17_TP1") == 1
    assert broker.opened.count("TG_17_TP2") == 1
