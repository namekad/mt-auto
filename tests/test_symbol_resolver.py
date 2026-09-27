from __future__ import annotations

from app.config import ExecutionMode, Settings, TradingRules
from app.database.repository import Repository
from app.services.mt5_service import OrderResult, Quote
from app.trading.execution_controller import ExecutionController
from app.trading.models import TradeSignal, ValidationStatus
from app.trading.processor import SignalProcessor
from app.trading.symbol_resolver import BrokerSymbol, resolve_broker_symbol
from app.utils.health import RuntimeState
from tests.fixtures import ZONE_BUY
from tests.test_execution import FakeMt5


def test_gold_uses_broker_suffix_when_plain_name_is_missing() -> None:
    catalog = [
        BrokerSymbol("EURUSDm", bid=1.08, ask=1.0802),
        BrokerSymbol("XAUUSDm", bid=4348.1, ask=4348.6),
        BrokerSymbol("XAUEUR", bid=3900.0, ask=3901.0),
    ]
    match = resolve_broker_symbol("GOLD", "XAUUSD", catalog, price_hint=4348.0)
    assert match is not None
    assert match.broker_symbol == "XAUUSDm"
    assert match.instrument == "XAUUSD"


def test_exact_broker_name_beats_a_suffixed_copy() -> None:
    catalog = [
        BrokerSymbol("XAUUSD", bid=4348.0, ask=4348.4),
        BrokerSymbol("XAUUSDm", bid=4348.2, ask=4348.7),
        BrokerSymbol("GOLD", bid=4347.0, ask=4347.5),
    ]
    match = resolve_broker_symbol("GOLD", "XAUUSD", catalog, price_hint=4348.0)
    assert match is not None
    assert match.broker_symbol == "XAUUSD"


def test_price_picks_the_live_gold_contract() -> None:
    catalog = [
        BrokerSymbol("XAUUSD", bid=1.2, ask=1.21),
        BrokerSymbol("GOLD.", bid=4347.8, ask=4348.3),
    ]
    match = resolve_broker_symbol("GOLD", "XAUUSD", catalog, price_hint=4348.0)
    assert match is not None
    assert match.broker_symbol == "GOLD."


def test_index_aliases_share_one_broker_symbol() -> None:
    catalog = [BrokerSymbol("NAS100.cash")]
    for raw, normalized in (("US100", "USTEC"), ("NAS100", "USTEC"), ("USTEC", "USTEC")):
        match = resolve_broker_symbol(raw, normalized, catalog)
        assert match is not None
        assert match.broker_symbol == "NAS100.cash"


def test_disabled_symbol_is_not_used() -> None:
    catalog = [
        BrokerSymbol("XAUUSD", trade_mode=0, bid=4348.0, ask=4348.4),
        BrokerSymbol("XAUUSD.", bid=4348.1, ask=4348.5),
    ]
    match = resolve_broker_symbol("GOLD", "XAUUSD", catalog, price_hint=4348.0)
    assert match is not None
    assert match.broker_symbol == "XAUUSD."


def test_missing_instrument_is_not_invented() -> None:
    catalog = [BrokerSymbol("EURUSDm", bid=1.08, ask=1.0802)]
    assert resolve_broker_symbol("GOLD", "XAUUSD", catalog, price_hint=4348.0) is None


class CatalogMt5(FakeMt5):
    def __init__(self, names: list[BrokerSymbol]) -> None:
        super().__init__()
        self.names = names
        self.sent_symbol: str | None = None

    def list_symbols(self) -> list[BrokerSymbol]:
        return self.names

    def symbol_exists(self, symbol: str) -> bool:
        return any(item.name == symbol for item in self.names)

    def get_bid_ask(self, symbol: str) -> Quote:
        for item in self.names:
            if item.name == symbol and item.bid and item.ask:
                return Quote(symbol=symbol, bid=item.bid, ask=item.ask, spread=item.ask - item.bid)
        return Quote(symbol=symbol, bid=4348.0, ask=4348.4, spread=0.4)

    def submit_demo_order(self, signal: TradeSignal, rules: TradingRules) -> OrderResult:
        self.sent_symbol = signal.broker_symbol
        return super().submit_demo_order(signal, rules)


def test_processor_trades_broker_gold_name(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    settings.execution_mode = ExecutionMode.AUTO_DEMO
    settings.dry_run = False
    state = RuntimeState()
    state.mt5_connected = True
    mt5 = CatalogMt5(
        [BrokerSymbol("XAUUSDm", bid=3642.0, ask=3642.4), BrokerSymbol("EURUSDm")]
    )
    execution = ExecutionController(settings, rules, mt5, repository)
    processor = SignalProcessor(settings, rules, aliases, repository, state, execution)
    signal = processor.handle_new_message(1001, 90, ZONE_BUY)
    assert signal is not None
    assert signal.broker_symbol == "XAUUSDm"
    assert signal.validation_status is ValidationStatus.PARSED
    setup = repository.get_setup(1001, 90)
    assert setup is not None
    assert setup.broker_symbol == "XAUUSDm"
    assert setup.state.value == "WAITING_ENTRY"
    assert mt5.sent_symbol is None


def test_rejected_copy_does_not_block_the_real_symbol(
    settings: Settings,
    rules: TradingRules,
    aliases: dict[str, str],
    repository: Repository,
) -> None:
    settings.execution_mode = ExecutionMode.OBSERVE
    state = RuntimeState()
    state.mt5_connected = True
    empty = CatalogMt5([])
    processor = SignalProcessor(
        settings,
        rules,
        aliases,
        repository,
        state,
        ExecutionController(settings, rules, empty, repository),
    )
    first = processor.handle_new_message(1001, 91, ZONE_BUY)
    assert first is not None
    assert first.validation_status is ValidationStatus.REJECTED

    live = CatalogMt5([BrokerSymbol("XAUUSD.", bid=3642.0, ask=3642.4)])
    processor._execution = ExecutionController(settings, rules, live, repository)
    second = processor.handle_new_message(1001, 92, ZONE_BUY)
    assert second is not None
    assert second.validation_status is ValidationStatus.PARSED
    assert second.broker_symbol == "XAUUSD."
