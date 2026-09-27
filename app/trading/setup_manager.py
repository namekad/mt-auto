from __future__ import annotations

from app.config import ExecutionMode, Settings, TradingRules
from app.database.repository import Repository
from app.exceptions import LiveModeDisabledError, Mt5AccountMismatchError, Mt5ServiceError
from app.services.mt5_service import Quote
from app.trading.broker import BrokerPosition, SetupBroker
from app.trading.loop import LoopStep, SignalLoopRecord, StatusKind, snippet_of
from app.trading.models import FINAL_SETUP_STATES, Direction, Setup, SetupState
from app.utils.health import RuntimeState
from app.utils.logging import get_logger
from app.utils.time import utc_now

logger = get_logger("trades")


class SetupManager:
    def __init__(
        self,
        settings: Settings,
        rules: TradingRules,
        repository: Repository,
        broker: SetupBroker | None,
        state: RuntimeState | None = None,
    ) -> None:
        self._settings = settings
        self._rules = rules
        self._repository = repository
        self._broker = broker
        self._state = state

    def orders_allowed(self) -> bool:
        if self._settings.execution_mode is not ExecutionMode.AUTO_DEMO:
            return False
        if self._settings.dry_run:
            return False
        if self._broker is None or not self._broker.verified:
            return False
        return True

    def reconcile(self) -> None:
        self.tick()

    def tick(self) -> None:
        for setup in self._repository.list_open_setups():
            self._advance(setup)

    def request_close(self, channel_id: int, reply_to_message_id: int | None) -> str:
        if reply_to_message_id is None:
            self._unknown_close(channel_id, None)
            return "UNKNOWN_CLOSE_SETUP"
        setup = self._repository.get_setup(channel_id, reply_to_message_id)
        if setup is None:
            self._unknown_close(channel_id, reply_to_message_id)
            return "UNKNOWN_CLOSE_SETUP"
        if setup.state in FINAL_SETUP_STATES:
            return setup.state.value
        if (
            setup.state is SetupState.WAITING_ENTRY
            and setup.trade_1_ticket is None
            and setup.trade_2_ticket is None
        ):
            setup.state = SetupState.CANCELLED
            setup.closed_at = utc_now()
            self._repository.save_setup(setup)
            self._note(setup, "Cancelled before entry.", "warn")
            return SetupState.CANCELLED.value
        setup.close_requested = True
        self._repository.save_setup(setup)
        self._close_legs(setup)
        self._refresh(setup)
        self._repository.save_setup(setup)
        self._note(setup, "Close setup applied to this signal only.", "ok")
        return setup.state.value

    def _advance(self, setup: Setup) -> None:
        before = (
            setup.state,
            setup.trade_1_ticket,
            setup.trade_2_ticket,
            setup.break_even_applied,
        )
        if (
            setup.close_requested
            and setup.state is SetupState.WAITING_ENTRY
            and setup.trade_1_ticket is None
            and setup.trade_2_ticket is None
        ):
            setup.state = SetupState.CANCELLED
            setup.closed_at = utc_now()
            self._repository.save_setup(setup)
            return
        if setup.close_requested:
            self._close_legs(setup)
            self._refresh(setup)
            self._repository.save_setup(setup)
            self._note_if_changed(setup, before)
            return
        self._refresh(setup)
        if setup.state is SetupState.WAITING_ENTRY or (
            setup.state is SetupState.ERROR and self._missing_leg(setup)
        ):
            self._try_fill(setup)
            self._refresh(setup)
        if setup.state in {
            SetupState.ACTIVE,
            SetupState.BREAK_EVEN,
            SetupState.PARTIALLY_CLOSED,
            SetupState.ERROR,
        }:
            self._protect(setup)
            self._refresh(setup)
        self._repository.save_setup(setup)
        self._note_if_changed(setup, before)

    def _try_fill(self, setup: Setup) -> None:
        if setup.state in FINAL_SETUP_STATES or setup.close_requested:
            return
        if not self.orders_allowed() or self._broker is None:
            return
        symbol = setup.broker_symbol or setup.symbol
        quote = self._broker.quote(symbol)
        has_ticket = setup.trade_1_ticket is not None or setup.trade_2_ticket is not None
        if not has_ticket and (quote is None or not _in_zone(setup, quote)):
            return
        self._open_leg(setup, 1, symbol)
        self._repository.save_setup(setup)
        self._open_leg(setup, 2, symbol)
        self._repository.save_setup(setup)
        if setup.trade_1_ticket is not None and setup.trade_2_ticket is not None:
            if setup.executed_at is None:
                setup.executed_at = utc_now()
            if setup.state is not SetupState.BREAK_EVEN:
                setup.state = SetupState.ACTIVE
            return
        setup.state = SetupState.ERROR

    def _open_leg(self, setup: Setup, leg: int, symbol: str) -> None:
        if self._broker is None:
            return
        if leg == 1 and setup.trade_1_ticket is not None:
            return
        if leg == 2 and setup.trade_2_ticket is not None:
            return
        comment = setup.leg_comment(leg)
        for position in self._broker.list_tracked_positions():
            if position.comment != comment:
                continue
            self._assign_ticket(setup, leg, position.ticket)
            return
        take_profit = setup.tp1 if leg == 1 else setup.tp2
        try:
            result = self._broker.open_market_leg(
                symbol=symbol,
                direction=setup.direction,
                volume=self._rules.lot_size,
                stop_loss=setup.stop_loss,
                take_profit=take_profit,
                comment=comment,
            )
        except (Mt5ServiceError, LiveModeDisabledError, Mt5AccountMismatchError) as error:
            logger.error("Setup %s leg %s failed: %s", setup.setup_id, leg, error)
            self._repository.add_event(
                f"MT5 order failed for setup {setup.setup_id} leg {leg}.",
                level="ERROR",
                category="trades",
                details={"setup_id": setup.setup_id, "error": str(error)},
            )
            setup.state = SetupState.ERROR
            return
        if not result.ok or result.ticket is None:
            logger.error(
                "Setup %s leg %s rejected: %s",
                setup.setup_id,
                leg,
                result.error_text,
            )
            self._repository.add_event(
                f"MT5 order rejected for setup {setup.setup_id} leg {leg}.",
                level="ERROR",
                category="trades",
                details={"setup_id": setup.setup_id, "error": result.error_text},
            )
            setup.state = SetupState.ERROR
            return
        self._assign_ticket(setup, leg, result.ticket)

    def _protect(self, setup: Setup) -> None:
        if not self.orders_allowed() or self._broker is None:
            return
        symbol = setup.broker_symbol or setup.symbol
        quote = self._broker.quote(symbol)
        if quote is None:
            return
        trigger, target = _break_even(setup, self._rules.break_even_distance)
        triggered = _break_even_hit(setup.direction, quote, trigger)
        opens = self._open_positions(setup)
        if not opens:
            return
        mismatched = [item for item in opens if not _sl_matches(item.sl, target)]
        if setup.break_even_applied:
            if not mismatched:
                return
        elif not triggered:
            return
        else:
            mismatched = opens
        failed = False
        for position in mismatched:
            if _sl_matches(position.sl, target):
                continue
            try:
                result = self._broker.modify_stop_loss(position.ticket, target)
            except (Mt5ServiceError, LiveModeDisabledError, Mt5AccountMismatchError) as error:
                logger.error("Break-even failed for %s: %s", position.ticket, error)
                failed = True
                continue
            if not result.ok:
                logger.error(
                    "Break-even rejected for %s: %s",
                    position.ticket,
                    result.error_text,
                )
                failed = True
        if not failed:
            setup.break_even_applied = True

    def _close_legs(self, setup: Setup) -> None:
        if not self.orders_allowed() or self._broker is None:
            return
        for position in self._open_positions(setup):
            try:
                result = self._broker.close_position(position.ticket)
            except (Mt5ServiceError, LiveModeDisabledError, Mt5AccountMismatchError) as error:
                logger.error("Close failed for %s: %s", position.ticket, error)
                setup.state = SetupState.ERROR
                return
            if not result.ok:
                logger.error("Close rejected for %s: %s", position.ticket, result.error_text)
                setup.state = SetupState.ERROR
                return

    def _refresh(self, setup: Setup) -> None:
        if setup.state in FINAL_SETUP_STATES:
            return
        self._adopt(setup)
        if setup.trade_1_ticket is None and setup.trade_2_ticket is None:
            if setup.state is not SetupState.ERROR:
                setup.state = SetupState.WAITING_ENTRY
            return
        marks = [
            self._ticket_mark(setup.trade_1_ticket),
            self._ticket_mark(setup.trade_2_ticket),
        ]
        if "unknown" in marks:
            return
        open_count = marks.count("open")
        if "missing" in marks:
            if open_count:
                setup.state = SetupState.ERROR
            return
        if open_count == 2:
            setup.state = (
                SetupState.BREAK_EVEN if setup.break_even_applied else SetupState.ACTIVE
            )
            return
        if open_count == 1:
            setup.state = SetupState.PARTIALLY_CLOSED
            return
        setup.state = (
            SetupState.CLOSED_BY_SIGNAL if setup.close_requested else SetupState.CLOSED
        )
        if setup.closed_at is None:
            setup.closed_at = utc_now()

    def _adopt(self, setup: Setup) -> None:
        if self._broker is None:
            return
        comments = {setup.leg_comment(1): 1, setup.leg_comment(2): 2}
        for position in self._broker.list_tracked_positions():
            leg = comments.get(position.comment)
            if leg is None:
                continue
            self._assign_ticket(setup, leg, position.ticket)

    def _ticket_mark(self, ticket: int | None) -> str:
        if ticket is None:
            return "missing"
        if any(position.ticket == ticket for position in self._open_positions_raw()):
            return "open"
        if self._broker is not None and self._broker.history_contains(ticket):
            return "closed"
        return "unknown"

    def _open_positions(self, setup: Setup) -> list[BrokerPosition]:
        tickets = {
            ticket
            for ticket in (setup.trade_1_ticket, setup.trade_2_ticket)
            if ticket is not None
        }
        comments = {setup.leg_comment(1), setup.leg_comment(2)}
        return [
            position
            for position in self._open_positions_raw()
            if position.ticket in tickets or position.comment in comments
        ]

    def _open_positions_raw(self) -> list[BrokerPosition]:
        if self._broker is None:
            return []
        return self._broker.list_tracked_positions()

    def _missing_leg(self, setup: Setup) -> bool:
        return setup.trade_1_ticket is None or setup.trade_2_ticket is None

    def _assign_ticket(self, setup: Setup, leg: int, ticket: int) -> None:
        if leg == 1:
            setup.trade_1_ticket = ticket
            return
        if leg == 2:
            setup.trade_2_ticket = ticket
            return
        raise ValueError(f"Unhandled setup leg: {leg}")

    def _unknown_close(self, channel_id: int, reply_to_message_id: int | None) -> None:
        logger.warning(
            "UNKNOWN_CLOSE_SETUP channel=%s reply_to=%s",
            channel_id,
            reply_to_message_id,
        )
        self._repository.add_event(
            "UNKNOWN_CLOSE_SETUP",
            level="WARNING",
            category="telegram",
            details={
                "channel_id": channel_id,
                "reply_to_message_id": reply_to_message_id,
            },
        )

    def _note_if_changed(
        self,
        setup: Setup,
        before: tuple[SetupState, int | None, int | None, bool],
    ) -> None:
        current = (
            setup.state,
            setup.trade_1_ticket,
            setup.trade_2_ticket,
            setup.break_even_applied,
        )
        if current == before:
            return
        self._note(setup, f"Setup is {setup.state.value}.", "ok")

    def _note(self, setup: Setup, outcome: str, kind: StatusKind) -> None:
        if self._state is None:
            return
        self._state.record_loop(
            SignalLoopRecord(
                headline=f"{setup.symbol} {setup.direction.value}",
                outcome=outcome,
                kind=kind,
                order_sent=setup.trade_1_ticket is not None or setup.trade_2_ticket is not None,
                steps=[
                    LoopStep("Telegram", "Setup", setup.setup_id, "ok"),
                    LoopStep("Parser", "Levels", f"{setup.entry_min}-{setup.entry_max}", "ok"),
                    LoopStep("Check", setup.state.value, "", "ok"),
                    LoopStep("Action", setup.state.value, outcome, kind),
                ],
                snippet=snippet_of(setup.raw_message),
            )
        )


def _in_zone(setup: Setup, quote: Quote) -> bool:
    if setup.direction is Direction.BUY:
        return setup.entry_min <= quote.ask <= setup.entry_max
    if setup.direction is Direction.SELL:
        return setup.entry_min <= quote.bid <= setup.entry_max
    never: Direction = setup.direction
    raise ValueError(f"Unhandled direction: {never}")


def _break_even(setup: Setup, distance: float) -> tuple[float, float]:
    if setup.direction is Direction.BUY:
        return setup.entry_min + distance, setup.entry_min
    if setup.direction is Direction.SELL:
        return setup.entry_max - distance, setup.entry_max
    never: Direction = setup.direction
    raise ValueError(f"Unhandled direction: {never}")


def _break_even_hit(direction: Direction, quote: Quote, trigger: float) -> bool:
    if direction is Direction.BUY:
        return quote.bid >= trigger
    if direction is Direction.SELL:
        return quote.ask <= trigger
    never: Direction = direction
    raise ValueError(f"Unhandled direction: {never}")


def _sl_matches(current: float, target: float) -> bool:
    return abs(current - target) <= 1e-4
