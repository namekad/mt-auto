from __future__ import annotations

from datetime import datetime

from app.config import ExecutionMode, Settings, TradingRules
from app.database.repository import Repository
from app.parser.detector import is_close_setup
from app.parser.parser import parse_signal
from app.trading.duplicates import (
    message_already_executed,
    message_already_processed,
)
from app.trading.execution_controller import ExecutionController
from app.trading.setup_manager import SetupManager
from app.trading.loop import (
    LoopStep,
    SignalLoopRecord,
    StatusKind,
    describe_validation,
    snippet_of,
)
from app.trading.models import (
    ExecutionStatus,
    MessageStatus,
    Setup,
    SetupState,
    TradeSignal,
    ValidationStatus,
)
from app.trading.validation import validate_signal
from app.utils.health import RuntimeState
from app.utils.logging import get_logger
from app.utils.telegram_ids import is_allowed_channel
from app.utils.time import utc_now

logger = get_logger("application")
parser_logger = get_logger("parser")
telegram_logger = get_logger("telegram")


class SignalProcessor:
    def __init__(
        self,
        settings: Settings,
        rules: TradingRules,
        aliases: dict[str, str],
        repository: Repository,
        state: RuntimeState,
        execution: ExecutionController,
        setup_manager: SetupManager | None = None,
    ) -> None:
        self._settings = settings
        self._rules = rules
        self._aliases = aliases
        self._repository = repository
        self._state = state
        self._execution = execution
        self._setup_manager = setup_manager

    def handle_new_message(
        self,
        channel_id: int,
        message_id: int,
        raw_text: str,
        *,
        channel_name: str | None = None,
        message_date: datetime | None = None,
        is_forwarded: bool = False,
        reply_to_message_id: int | None = None,
        market_price: float | None = None,
        symbol_exists: bool | None = None,
        open_positions: int | None = None,
    ) -> TradeSignal | None:
        self._state.last_telegram_event = utc_now()
        allowed = self._settings.allowed_channel_id_list()
        if not is_allowed_channel(channel_id, allowed):
            telegram_logger.info("Ignored unauthorized channel %s", channel_id)
            self._repository.save_message(
                channel_id,
                message_id,
                raw_text,
                channel_name=channel_name,
                message_date=message_date,
                is_forwarded=is_forwarded,
                status=MessageStatus.IGNORED.value,
            )
            self._record_loop(
                headline="Channel not allowed",
                snippet=raw_text,
                steps=[
                    LoopStep(
                        "Telegram",
                        "Ignored",
                        f"Channel {channel_id} is not in Allowed channel IDs.",
                        "bad",
                    ),
                    LoopStep("Parser", "Skipped", "The post was not opened.", "idle"),
                    LoopStep("Check", "Stopped", "Unauthorized source.", "bad"),
                    LoopStep("Action", "No MetaTrader order was sent.", "", "idle"),
                ],
                outcome="Not from an allowed channel. No order was sent.",
                kind="bad",
            )
            return None

        if is_close_setup(raw_text):
            self.handle_close_setup(
                channel_id,
                message_id,
                raw_text,
                reply_to_message_id=reply_to_message_id,
                channel_name=channel_name,
                message_date=message_date,
            )
            return None

        if message_already_processed(self._repository, channel_id, message_id):
            telegram_logger.info(
                "Duplicate Telegram message %s/%s ignored", channel_id, message_id
            )
            self._record_loop(
                headline="Same Telegram post",
                snippet=raw_text,
                steps=[
                    LoopStep(
                        "Telegram",
                        "Already seen",
                        f"Message {message_id} was processed before.",
                        "warn",
                    ),
                    LoopStep("Parser", "Skipped", "The same post is not parsed twice.", "idle"),
                    LoopStep("Check", "Duplicate post", "Not a new signal.", "warn"),
                    LoopStep("Action", "No MetaTrader order was sent.", "", "idle"),
                ],
                outcome="This Telegram post was already handled. No second order.",
                kind="warn",
            )
            return None

        self._repository.save_message(
            channel_id,
            message_id,
            raw_text,
            channel_name=channel_name,
            message_date=message_date,
            is_forwarded=is_forwarded,
            status=MessageStatus.RECEIVED.value,
        )
        if self._state.paused:
            self._repository.update_message(
                channel_id, message_id, status=MessageStatus.RECEIVED.value
            )
            self._record_loop(
                headline="Paused",
                snippet=raw_text,
                steps=[
                    LoopStep("Telegram", "Received", f"Message {message_id} from {channel_id}.", "ok"),
                    LoopStep("Parser", "On hold", "Processing is paused.", "warn"),
                    LoopStep("Check", "Skipped", "Press Resume to handle new posts.", "warn"),
                    LoopStep("Action", "No MetaTrader order was sent.", "", "idle"),
                ],
                outcome="App is paused. The post was saved, not traded.",
                kind="warn",
            )
            return None
        return self._parse_and_store(
            channel_id,
            message_id,
            raw_text,
            message_date=message_date,
            is_forwarded=is_forwarded,
            is_edited=False,
            market_price=market_price,
            symbol_exists=symbol_exists,
            open_positions=open_positions,
        )

    def handle_edit(
        self,
        channel_id: int,
        message_id: int,
        raw_text: str,
        *,
        message_date: datetime | None = None,
        market_price: float | None = None,
        symbol_exists: bool | None = None,
        open_positions: int | None = None,
    ) -> TradeSignal | None:
        self._state.last_telegram_event = utc_now()
        existing = self._repository.get_message(channel_id, message_id)
        if existing is None:
            return self.handle_new_message(
                channel_id,
                message_id,
                raw_text,
                message_date=message_date,
                market_price=market_price,
                symbol_exists=symbol_exists,
                open_positions=open_positions,
            )
        if self._repository.get_setup(channel_id, message_id) is not None:
            self._repository.update_message(
                channel_id,
                message_id,
                previous_text=existing.raw_text,
                raw_text=raw_text,
                is_edited=True,
                status=MessageStatus.EDITED_AFTER_EXECUTE.value,
            )
            self._record_loop(
                headline="Edited setup",
                snippet=raw_text,
                steps=[
                    LoopStep("Telegram", "Edit received", f"Message {message_id} changed.", "warn"),
                    LoopStep("Parser", "Skipped", "This setup already exists.", "warn"),
                    LoopStep("Check", "Locked", "Edits do not open another setup.", "warn"),
                    LoopStep("Action", "No MetaTrader order was sent.", "", "idle"),
                ],
                outcome="This Telegram setup already exists. No second pair of trades.",
                kind="warn",
            )
            return None
        if message_already_executed(self._repository, channel_id, message_id):
            self._repository.update_message(
                channel_id,
                message_id,
                previous_text=existing.raw_text,
                raw_text=raw_text,
                is_edited=True,
                status=MessageStatus.EDITED_AFTER_EXECUTE.value,
            )
            self._repository.add_event(
                "Executed signal edited; manual review required.",
                level="WARNING",
                category="telegram",
                details={"channel_id": channel_id, "message_id": message_id},
            )
            self._record_loop(
                headline="Edited after a past execute flag",
                snippet=raw_text,
                steps=[
                    LoopStep("Telegram", "Edit received", f"Message {message_id} changed.", "warn"),
                    LoopStep("Parser", "Skipped", "This post was already marked executed.", "warn"),
                    LoopStep("Check", "Locked", "Edits do not open a new trade.", "warn"),
                    LoopStep("Action", "No MetaTrader order was sent.", "", "idle"),
                ],
                outcome="An already-handled trade post was edited. No new order.",
                kind="warn",
            )
            return None
        self._repository.update_message(
            channel_id,
            message_id,
            previous_text=existing.raw_text,
            raw_text=raw_text,
            is_edited=True,
            status=MessageStatus.RECEIVED.value,
        )
        return self._parse_and_store(
            channel_id,
            message_id,
            raw_text,
            message_date=message_date or existing.message_date,
            is_forwarded=existing.is_forwarded,
            is_edited=True,
            market_price=market_price,
            symbol_exists=symbol_exists,
            open_positions=open_positions,
        )

    def handle_delete(self, channel_id: int, message_id: int) -> None:
        self._state.last_telegram_event = utc_now()
        existing = self._repository.get_message(channel_id, message_id)
        if existing is None:
            return
        if message_already_executed(self._repository, channel_id, message_id):
            self._repository.update_message(
                channel_id,
                message_id,
                is_deleted=True,
                status=MessageStatus.EXECUTED.value,
            )
            self._repository.add_event(
                "Executed signal deleted in Telegram; MT5 position was not closed.",
                level="WARNING",
                category="telegram",
                details={"channel_id": channel_id, "message_id": message_id},
            )
            return
        self._repository.update_message(
            channel_id,
            message_id,
            is_deleted=True,
            status=MessageStatus.DELETED.value,
        )

    def _parse_and_store(
        self,
        channel_id: int,
        message_id: int,
        raw_text: str,
        *,
        message_date: datetime | None,
        is_forwarded: bool,
        is_edited: bool,
        market_price: float | None,
        symbol_exists: bool | None,
        open_positions: int | None,
    ) -> TradeSignal | None:
        parsed = parse_signal(raw_text, self._aliases)
        if not parsed.is_signal or parsed.signal is None:
            self._repository.update_message(
                channel_id, message_id, status=MessageStatus.IGNORED.value
            )
            self._record_loop(
                headline="Not a trade post",
                snippet=raw_text,
                steps=[
                    LoopStep(
                        "Telegram",
                        "Received",
                        f"Message {message_id} from allowed channel.",
                        "ok",
                    ),
                    LoopStep(
                        "Parser",
                        "No trade found",
                        "Missing a clear symbol/direction/entry pattern.",
                        "idle",
                    ),
                    LoopStep("Check", "Skipped", "Nothing to validate.", "idle"),
                    LoopStep("Action", "No MetaTrader order was sent.", "", "idle"),
                ],
                outcome="The post was not treated as a signal. No order was sent.",
                kind="idle",
            )
            return None

        signal = parsed.signal
        signal.telegram_channel_id = channel_id
        signal.telegram_message_id = message_id
        signal.telegram_message_date = message_date
        signal.is_forwarded = is_forwarded
        signal.is_edited = is_edited
        telegram_step = LoopStep(
            "Telegram",
            "Received",
            f"Message {message_id} from allowed channel.",
            "ok",
        )
        if parsed.rejection_reason:
            signal.validation_status = ValidationStatus.INVALID
            signal.rejection_reason = parsed.rejection_reason
            self._repository.save_signal(signal)
            self._repository.update_message(
                channel_id, message_id, status=MessageStatus.PARSE_FAILED.value
            )
            self._repository.add_event(
                "PARSE_FAILED",
                level="WARNING",
                category="parser",
                details={"channel_id": channel_id, "message_id": message_id},
            )
            self._record_loop(
                headline=self._headline(signal),
                snippet=raw_text,
                steps=[
                    telegram_step,
                    LoopStep("Parser", "Incomplete", parsed.rejection_reason, "bad"),
                    LoopStep("Check", "Not valid", describe_validation(signal.validation_status), "bad"),
                    LoopStep("Action", "No MetaTrader order was sent.", "", "idle"),
                ],
                outcome=f"Could not use this post: {parsed.rejection_reason}",
                kind="bad",
            )
            parser_logger.info("Parse rejected message %s: %s", message_id, parsed.rejection_reason)
            return signal

        if symbol_exists is None or market_price is None or open_positions is None:
            exists, price, opens = self._execution.resolve_for_signal(signal)
            if symbol_exists is None:
                symbol_exists = exists
            if market_price is None:
                market_price = price
            if open_positions is None:
                open_positions = opens
        traded_as = signal.broker_symbol or signal.normalized_symbol
        parse_detail = ", ".join(
            part
            for part in (
                traded_as,
                signal.direction.value if signal.direction else None,
                signal.order_type.value if signal.order_type else None,
                f"SL {signal.stop_loss}" if signal.stop_loss is not None else None,
                f"TP {', '.join(str(item) for item in signal.take_profits)}" if signal.take_profits else None,
            )
            if part
        )
        if self._repository.get_setup(channel_id, message_id) is not None:
            self._record_loop(
                headline=self._headline(signal),
                snippet=raw_text,
                steps=[
                    telegram_step,
                    LoopStep("Parser", "Skipped", "This Telegram message already has a setup.", "warn"),
                    LoopStep("Check", "Duplicate post", "Not a new setup.", "warn"),
                    LoopStep("Action", "No MetaTrader order was sent.", "", "idle"),
                ],
                outcome="This Telegram post was already handled. No second order.",
                kind="warn",
            )
            return None
        result = validate_signal(
            signal,
            self._rules,
            allowed_channel_ids=self._settings.allowed_channel_id_list(),
            mt5_connected=self._state.mt5_connected,
            symbol_exists=symbol_exists,
            market_price=market_price,
            open_positions=open_positions,
            already_executed=message_already_executed(
                self._repository, channel_id, message_id
            ),
            semantic_duplicate=False,
        )
        signal.validation_status = result.status
        signal.rejection_reason = result.reason_text
        if result.ok:
            self._store_waiting_setup(signal)
            signal.execution_status = self._waiting_execution_status()
            action_text = self._waiting_action_text()
            order_sent = False
            status = MessageStatus.WAITING_ENTRY
            check_kind = "ok"
            action_kind = "warn"
            loop_kind = "warn"
        elif result.status is ValidationStatus.SEMANTIC_DUPLICATE:
            signal.execution_status = ExecutionStatus.WAITING_APPROVAL
            status = MessageStatus.SEMANTIC_DUPLICATE
            check_kind = "warn"
            action_text = "Held because this trade was already recorded. No MetaTrader order was sent."
            order_sent = False
            action_kind = "warn"
            loop_kind = "warn"
        else:
            signal.execution_status = ExecutionStatus.NONE
            status = MessageStatus.PARSE_FAILED if _is_parse_gap(result.reason_text) else MessageStatus.REJECTED
            check_kind = "bad"
            action_text = "No MetaTrader order was sent."
            order_sent = False
            action_kind = "idle"
            loop_kind = "bad"
            if status is MessageStatus.PARSE_FAILED:
                self._repository.add_event(
                    "PARSE_FAILED",
                    level="WARNING",
                    category="parser",
                    details={"channel_id": channel_id, "message_id": message_id, "reason": result.reason_text},
                )
        self._repository.save_signal(signal)
        self._repository.update_message(channel_id, message_id, status=status.value)
        reason = signal.rejection_reason or describe_validation(result.status)
        outcome = f"{describe_validation(result.status)}. {action_text}"
        if signal.rejection_reason:
            outcome = f"{reason} {action_text}"
        self._record_loop(
            headline=self._headline(signal),
            snippet=raw_text,
            steps=[
                telegram_step,
                LoopStep("Parser", "Read the trade", parse_detail, "ok"),
                LoopStep("Check", describe_validation(result.status), reason if not result.ok else "Rules passed.", check_kind),
                LoopStep(
                    "Action",
                    "Sent" if order_sent else "No order",
                    action_text,
                    action_kind,
                ),
            ],
            outcome=outcome,
            kind=loop_kind,
            order_sent=order_sent,
        )
        parser_logger.info(
            "Processed message %s status=%s reason=%s",
            message_id,
            status.value,
            signal.rejection_reason,
        )
        return signal

    def handle_close_setup(
        self,
        channel_id: int,
        message_id: int,
        raw_text: str,
        *,
        reply_to_message_id: int | None,
        channel_name: str | None = None,
        message_date: datetime | None = None,
    ) -> None:
        if message_already_processed(self._repository, channel_id, message_id):
            return
        self._repository.save_message(
            channel_id,
            message_id,
            raw_text,
            channel_name=channel_name,
            message_date=message_date,
            status=MessageStatus.RECEIVED.value,
        )
        if self._setup_manager is not None:
            outcome = self._setup_manager.request_close(channel_id, reply_to_message_id)
        else:
            outcome = self._close_without_broker(channel_id, reply_to_message_id)
        kind: StatusKind = "warn" if outcome == "UNKNOWN_CLOSE_SETUP" else "ok"
        self._record_loop(
            headline="Close setup",
            snippet=raw_text,
            steps=[
                LoopStep("Telegram", "Reply", f"Reply to {reply_to_message_id}.", kind),
                LoopStep("Parser", "Close setup", "Matched by reply only.", kind),
                LoopStep("Check", outcome, "No other setup is touched.", kind),
                LoopStep("Action", outcome, "Only the replied setup is closed or cancelled.", kind),
            ],
            outcome=outcome,
            kind=kind,
        )

    def _close_without_broker(self, channel_id: int, reply_to_message_id: int | None) -> str:
        if reply_to_message_id is None:
            self._repository.add_event(
                "UNKNOWN_CLOSE_SETUP",
                level="WARNING",
                category="telegram",
                details={"channel_id": channel_id},
            )
            return "UNKNOWN_CLOSE_SETUP"
        setup = self._repository.get_setup(channel_id, reply_to_message_id)
        if setup is None:
            self._repository.add_event(
                "UNKNOWN_CLOSE_SETUP",
                level="WARNING",
                category="telegram",
                details={
                    "channel_id": channel_id,
                    "reply_to_message_id": reply_to_message_id,
                },
            )
            return "UNKNOWN_CLOSE_SETUP"
        if (
            setup.state is SetupState.WAITING_ENTRY
            and setup.trade_1_ticket is None
            and setup.trade_2_ticket is None
        ):
            setup.state = SetupState.CANCELLED
            setup.closed_at = utc_now()
            self._repository.save_setup(setup)
            return SetupState.CANCELLED.value
        setup.close_requested = True
        self._repository.save_setup(setup)
        return setup.state.value

    def _store_waiting_setup(self, signal: TradeSignal) -> None:
        if (
            signal.telegram_channel_id is None
            or signal.telegram_message_id is None
            or signal.direction is None
            or signal.entry_low is None
            or signal.entry_high is None
            or signal.stop_loss is None
            or len(signal.take_profits) < 2
            or not (signal.normalized_symbol or signal.symbol)
        ):
            return
        channel_id = signal.telegram_channel_id
        message_id = signal.telegram_message_id
        direction = signal.direction
        symbol = signal.normalized_symbol or signal.symbol or ""
        setup = Setup(
            setup_id=f"{channel_id}:{message_id}",
            telegram_channel_id=channel_id,
            telegram_message_id=message_id,
            symbol=symbol,
            broker_symbol=signal.broker_symbol,
            direction=direction,
            entry_min=signal.entry_low,
            entry_max=signal.entry_high,
            stop_loss=signal.stop_loss,
            tp1=signal.take_profits[0],
            tp2=signal.take_profits[1],
            tp3=signal.take_profits[2] if len(signal.take_profits) > 2 else None,
            state=SetupState.WAITING_ENTRY,
            raw_message=signal.raw_message,
        )
        self._repository.save_setup(setup)

    def _waiting_execution_status(self) -> ExecutionStatus:
        decided = self._execution.decide(
            TradeSignal(validation_status=ValidationStatus.PARSED)
        )
        if decided is ExecutionStatus.EXECUTING:
            return ExecutionStatus.NONE
        return decided

    def _waiting_action_text(self) -> str:
        mode = self._settings.execution_mode
        if mode is ExecutionMode.OBSERVE:
            return (
                "Waiting for the entry zone. Stored only. Mode is Watch only. "
                "No MetaTrader order was sent."
            )
        if mode is ExecutionMode.APPROVAL:
            return (
                "Waiting for the entry zone. Held for review. No MetaTrader order was sent."
            )
        if mode is ExecutionMode.AUTO_DEMO and self._settings.dry_run:
            return (
                "Waiting for the entry zone. Auto on demo is selected, but Keep orders off is on. "
                "No MetaTrader order was sent."
            )
        if mode is ExecutionMode.AUTO_DEMO:
            return "Waiting for the entry zone. No MetaTrader order was sent."
        never: ExecutionMode = mode
        raise ValueError(f"Unhandled execution mode: {never}")

    def _headline(self, signal: TradeSignal) -> str:
        symbol = signal.broker_symbol or signal.normalized_symbol or signal.symbol or "Signal"
        direction = signal.direction.value if signal.direction else ""
        return f"{symbol} {direction}".strip()

    def _record_loop(
        self,
        *,
        headline: str,
        snippet: str,
        steps: list[LoopStep],
        outcome: str,
        kind: StatusKind,
        order_sent: bool = False,
    ) -> None:
        self._state.record_loop(
            SignalLoopRecord(
                headline=headline,
                outcome=outcome,
                kind=kind,
                order_sent=order_sent,
                steps=steps,
                snippet=snippet_of(snippet),
            )
        )


def _is_parse_gap(reason: str | None) -> bool:
    if not reason:
        return False
    markers = (
        "Entry zone is required",
        "TP1 and TP2 are required",
        "Stop loss is required",
        "Direction is missing",
        "Symbol is missing",
        "Take profit",
        "Malformed",
    )
    return any(item in reason for item in markers)

