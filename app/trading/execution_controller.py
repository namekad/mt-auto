from __future__ import annotations

from dataclasses import dataclass

from app.config import ExecutionMode, Settings, TradingRules
from app.database.repository import Repository
from app.exceptions import LiveModeDisabledError, Mt5AccountMismatchError, Mt5ServiceError
from app.services.mt5_service import Mt5Service
from app.trading.loop import describe_action
from app.trading.models import ExecutionStatus, TradeSignal
from app.trading.symbol_resolver import BrokerSymbol, resolve_broker_symbol
from app.utils.logging import get_logger

logger = get_logger("trades")


@dataclass
class ExecutionOutcome:
    status: ExecutionStatus
    order_sent: bool
    detail: str
    ticket: int | None = None


class ExecutionController:
    def __init__(
        self,
        settings: Settings,
        rules: TradingRules | None = None,
        mt5: Mt5Service | None = None,
        repository: Repository | None = None,
    ) -> None:
        self._settings = settings
        self._rules = rules
        self._mt5 = mt5
        self._repository = repository

    def market_snapshot(
        self, symbol: str | None
    ) -> tuple[bool | None, float | None, int | None]:
        if self._mt5 is None or not self._mt5.verified or not symbol:
            return None, None, None
        if not hasattr(self._mt5, "symbol_exists"):
            return None, None, None
        try:
            exists = self._mt5.symbol_exists(symbol)
            price = None
            if exists and hasattr(self._mt5, "ensure_symbol") and hasattr(self._mt5, "get_bid_ask"):
                self._mt5.ensure_symbol(symbol)
                quote = self._mt5.get_bid_ask(symbol)
                price = (quote.bid + quote.ask) / 2
            opens = len(self._mt5.get_positions()) if hasattr(self._mt5, "get_positions") else None
            return exists, price, opens
        except Mt5ServiceError as error:
            logger.warning("Could not read MT5 market data: %s", error)
            return None, None, None

    def resolve_for_signal(
        self, signal: TradeSignal
    ) -> tuple[bool | None, float | None, int | None]:
        catalog = self._symbol_catalog()
        if catalog is None:
            return self.market_snapshot(signal.normalized_symbol)
        match = resolve_broker_symbol(
            signal.symbol,
            signal.normalized_symbol,
            catalog,
            price_hint=_price_hint(signal),
        )
        if match is None:
            logger.info(
                "No broker symbol for %s/%s",
                signal.symbol,
                signal.normalized_symbol,
            )
            return False, None, self._position_count()
        signal.broker_symbol = match.broker_symbol
        logger.info(
            "Resolved %s/%s to MT5 symbol %s",
            signal.symbol,
            signal.normalized_symbol,
            match.broker_symbol,
        )
        exists, price, opens = self.market_snapshot(match.broker_symbol)
        if price is None:
            chosen = next(
                (item for item in catalog if item.name == match.broker_symbol),
                None,
            )
            if chosen is not None and chosen.mid is not None:
                price = chosen.mid
        if exists is False:
            return False, price, opens
        return True, price, opens

    def decide(self, signal: TradeSignal) -> ExecutionStatus:
        if signal.validation_status.value not in {"PARSED"}:
            return ExecutionStatus.NONE
        if self._settings.execution_mode is ExecutionMode.OBSERVE:
            return ExecutionStatus.OBSERVED
        if self._settings.execution_mode is ExecutionMode.APPROVAL:
            return ExecutionStatus.WAITING_APPROVAL
        if self._settings.execution_mode is ExecutionMode.AUTO_DEMO:
            if self._settings.dry_run:
                return ExecutionStatus.DRY_RUN
            return ExecutionStatus.EXECUTING
        never: ExecutionMode = self._settings.execution_mode
        raise ValueError(f"Unhandled execution mode: {never}")

    def apply(self, signal: TradeSignal) -> ExecutionOutcome:
        status = self.decide(signal)
        if status is ExecutionStatus.DRY_RUN:
            self._record_attempt(signal, status="DRY_RUN", error_text="DRY_RUN is on.")
            return ExecutionOutcome(
                status=status,
                order_sent=False,
                detail="Auto on demo is selected, but Keep orders off is on. No MetaTrader order was sent.",
            )
        if status is not ExecutionStatus.EXECUTING:
            text, sent = describe_action(
                status,
                mode=self._settings.execution_mode,
                dry_run=self._settings.dry_run,
            )
            return ExecutionOutcome(status=status, order_sent=sent, detail=text)
        return self._send_demo(signal)

    def _send_demo(self, signal: TradeSignal) -> ExecutionOutcome:
        self._settings.assert_not_live()
        if self._rules is None:
            return self._fail(signal, "Trading rules are not loaded.")
        if self._mt5 is None or not self._mt5.verified:
            return self._fail(
                signal,
                "MetaTrader is not connected and verified. Open MT5, check Setup, then Start again.",
            )
        try:
            result = self._mt5.submit_demo_order(signal, self._rules)
        except (Mt5ServiceError, LiveModeDisabledError, Mt5AccountMismatchError) as error:
            return self._fail(signal, str(error))
        self._record_attempt(
            signal,
            status="EXECUTED" if result.ok else "FAILED",
            request=result.request,
            response=result.response,
            retcode=result.retcode,
            error_text=result.error_text,
        )
        if not result.ok:
            return ExecutionOutcome(
                status=ExecutionStatus.FAILED,
                order_sent=False,
                detail=f"MetaTrader rejected the order: {result.error_text or result.retcode}.",
            )
        if self._repository is not None:
            self._repository.save_executed(
                signal,
                ticket=result.ticket,
                order_id=result.order,
                position_id=result.deal,
                requested_price=signal.entry_price,
                actual_price=result.price,
                requested_volume=signal.volume,
                executed_volume=result.volume,
                spread=None,
                retcode=result.retcode,
                raw=result.response,
            )
        ticket = result.ticket or result.order
        extra = f" Ticket {ticket}." if ticket else ""
        return ExecutionOutcome(
            status=ExecutionStatus.EXECUTED,
            order_sent=True,
            detail=f"MetaTrader demo order was sent.{extra}",
            ticket=ticket,
        )

    def _fail(self, signal: TradeSignal, reason: str) -> ExecutionOutcome:
        logger.error("Demo order not sent: %s", reason)
        self._record_attempt(signal, status="FAILED", error_text=reason)
        return ExecutionOutcome(
            status=ExecutionStatus.FAILED,
            order_sent=False,
            detail=reason,
        )

    def _symbol_catalog(self) -> list[BrokerSymbol] | None:
        if self._mt5 is None or not self._mt5.verified:
            return None
        if not hasattr(self._mt5, "list_symbols"):
            return None
        try:
            return list(self._mt5.list_symbols())
        except Mt5ServiceError as error:
            logger.warning("Could not read MT5 symbols: %s", error)
            return None

    def _position_count(self) -> int | None:
        if self._mt5 is None or not hasattr(self._mt5, "get_positions"):
            return None
        try:
            return len(self._mt5.get_positions())
        except Mt5ServiceError as error:
            logger.warning("Could not read MT5 positions: %s", error)
            return None

    def _record_attempt(
        self,
        signal: TradeSignal,
        *,
        status: str,
        request: dict[str, object] | None = None,
        response: dict[str, object] | None = None,
        retcode: int | None = None,
        error_text: str | None = None,
    ) -> None:
        if self._repository is None:
            return
        self._repository.save_attempt(
            signal,
            request=request,
            response=response,
            retcode=retcode,
            status=status,
            error_text=error_text,
        )


def _price_hint(signal: TradeSignal) -> float | None:
    if signal.entry_price is not None:
        return signal.entry_price
    if signal.entry_low is not None and signal.entry_high is not None:
        return (signal.entry_low + signal.entry_high) / 2
    return None
