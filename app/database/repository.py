from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import (
    ApplicationEvent,
    ExecutedTrade,
    SetupRecord,
    TelegramMessage,
    TradeAttempt,
    TradeSignalRecord,
)
from app.trading.models import (
    FINAL_SETUP_STATES,
    Direction,
    MessageStatus,
    Setup,
    SetupState,
    TradeSignal,
)
from app.utils.time import utc_now


class Repository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def save_message(
        self,
        channel_id: int,
        message_id: int,
        raw_text: str,
        *,
        channel_name: str | None = None,
        message_date: datetime | None = None,
        is_forwarded: bool = False,
        status: str = MessageStatus.RECEIVED.value,
    ) -> TelegramMessage:
        with self._session_factory() as session:
            record = TelegramMessage(
                telegram_channel_id=channel_id,
                telegram_message_id=message_id,
                channel_name=channel_name,
                raw_text=raw_text,
                message_date=message_date,
                received_at=utc_now(),
                updated_at=utc_now(),
                is_forwarded=is_forwarded,
                status=status,
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self.get_message(channel_id, message_id)
                if existing is None:
                    raise
                return existing
            session.refresh(record)
            return record

    def get_message(self, channel_id: int, message_id: int) -> TelegramMessage | None:
        with self._session_factory() as session:
            statement: Select[tuple[TelegramMessage]] = select(TelegramMessage).where(
                TelegramMessage.telegram_channel_id == channel_id,
                TelegramMessage.telegram_message_id == message_id,
            )
            return session.scalars(statement).first()

    def update_message(
        self,
        channel_id: int,
        message_id: int,
        **changes: object,
    ) -> TelegramMessage | None:
        with self._session_factory() as session:
            statement = select(TelegramMessage).where(
                TelegramMessage.telegram_channel_id == channel_id,
                TelegramMessage.telegram_message_id == message_id,
            )
            record = session.scalars(statement).first()
            if record is None:
                return None
            for key, value in changes.items():
                setattr(record, key, value)
            record.updated_at = utc_now()
            session.commit()
            session.refresh(record)
            return record

    def save_signal(self, signal: TradeSignal) -> TradeSignalRecord:
        with self._session_factory() as session:
            record = TradeSignalRecord(
                signal_id=signal.signal_id,
                telegram_channel_id=signal.telegram_channel_id,
                telegram_message_id=signal.telegram_message_id,
                telegram_message_date=signal.telegram_message_date,
                received_at=signal.received_at,
                raw_message=signal.raw_message,
                symbol=signal.symbol,
                normalized_symbol=signal.normalized_symbol,
                direction=signal.direction.value if signal.direction else None,
                order_type=signal.order_type.value if signal.order_type else None,
                entry_price=signal.entry_price,
                entry_low=signal.entry_low,
                entry_high=signal.entry_high,
                stop_loss=signal.stop_loss,
                take_profits_json=json.dumps(signal.take_profits),
                volume=signal.volume,
                risk_percent=signal.risk_percent,
                comment=signal.comment,
                parser_confidence=signal.parser_confidence,
                validation_status=signal.validation_status.value,
                rejection_reason=signal.rejection_reason,
                execution_status=signal.execution_status.value,
                created_at=signal.created_at,
                updated_at=signal.updated_at,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def get_signal_by_message(
        self, channel_id: int, message_id: int
    ) -> TradeSignalRecord | None:
        with self._session_factory() as session:
            statement = (
                select(TradeSignalRecord)
                .where(
                    TradeSignalRecord.telegram_channel_id == channel_id,
                    TradeSignalRecord.telegram_message_id == message_id,
                )
                .order_by(TradeSignalRecord.id.desc())
            )
            return session.scalars(statement).first()

    def latest_executed_for_message(
        self, channel_id: int, message_id: int
    ) -> TradeSignalRecord | None:
        with self._session_factory() as session:
            statement = select(TradeSignalRecord).where(
                TradeSignalRecord.telegram_channel_id == channel_id,
                TradeSignalRecord.telegram_message_id == message_id,
                TradeSignalRecord.execution_status == "EXECUTED",
            )
            return session.scalars(statement).first()

    def find_semantic_duplicates(
        self,
        symbol: str,
        direction: str,
        entry_price: float | None,
        stop_loss: float | None,
        take_profits: list[float],
        window_hours: int,
        exclude_signal_id: str | None = None,
    ) -> list[TradeSignalRecord]:
        cutoff = utc_now() - timedelta(hours=window_hours)
        with self._session_factory() as session:
            statement = select(TradeSignalRecord).where(
                TradeSignalRecord.normalized_symbol == symbol,
                TradeSignalRecord.direction == direction,
                TradeSignalRecord.created_at >= cutoff,
            )
            rows = list(session.scalars(statement))
        matches: list[TradeSignalRecord] = []
        for row in rows:
            if exclude_signal_id and row.signal_id == exclude_signal_id:
                continue
            if row.validation_status in {"REJECTED", "INVALID"}:
                continue
            if row.entry_price != entry_price:
                continue
            if row.stop_loss != stop_loss:
                continue
            stored_tps = json.loads(row.take_profits_json or "[]")
            if stored_tps != take_profits:
                continue
            matches.append(row)
        return matches

    def list_recent_signals(self, limit: int = 10) -> list[TradeSignalRecord]:
        with self._session_factory() as session:
            statement = select(TradeSignalRecord).order_by(
                TradeSignalRecord.id.desc()
            ).limit(limit)
            return list(session.scalars(statement))

    def list_recent_errors(self, limit: int = 10) -> list[ApplicationEvent]:
        with self._session_factory() as session:
            statement = (
                select(ApplicationEvent)
                .where(ApplicationEvent.level.in_(("ERROR", "CRITICAL")))
                .order_by(ApplicationEvent.id.desc())
                .limit(limit)
            )
            return list(session.scalars(statement))

    def add_event(
        self,
        message: str,
        *,
        level: str = "INFO",
        category: str = "app",
        details: dict[str, object] | None = None,
    ) -> ApplicationEvent:
        with self._session_factory() as session:
            event = ApplicationEvent(
                created_at=utc_now(),
                level=level,
                category=category,
                message=message,
                details_json=json.dumps(details) if details else None,
            )
            session.add(event)
            session.commit()
            session.refresh(event)
            return event

    def save_attempt(
        self,
        signal: TradeSignal,
        *,
        request: dict[str, object] | None = None,
        response: dict[str, object] | None = None,
        retcode: int | None = None,
        status: str,
        error_text: str | None = None,
    ) -> TradeAttempt:
        with self._session_factory() as session:
            record = TradeAttempt(
                signal_id=signal.signal_id,
                requested_symbol=signal.broker_symbol
                or signal.normalized_symbol
                or signal.symbol,
                requested_direction=signal.direction.value if signal.direction else None,
                requested_order_type=signal.order_type.value if signal.order_type else None,
                requested_volume=signal.volume,
                requested_price=signal.entry_price,
                requested_stop_loss=signal.stop_loss,
                requested_take_profit=signal.take_profits[0] if signal.take_profits else None,
                mt5_request_json=json.dumps(request) if request else None,
                mt5_response_json=json.dumps(response) if response else None,
                broker_retcode=retcode,
                status=status,
                error_text=error_text,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def save_executed(
        self,
        signal: TradeSignal,
        *,
        ticket: int | None,
        order_id: int | None,
        position_id: int | None,
        requested_price: float | None,
        actual_price: float | None,
        requested_volume: float | None,
        executed_volume: float | None,
        spread: float | None,
        retcode: int | None,
        raw: dict[str, object] | None,
    ) -> ExecutedTrade:
        with self._session_factory() as session:
            record = ExecutedTrade(
                signal_id=signal.signal_id,
                ticket=ticket,
                order_id=order_id,
                position_id=position_id,
                requested_price=requested_price,
                actual_price=actual_price,
                requested_volume=requested_volume,
                executed_volume=executed_volume,
                spread=spread,
                broker_retcode=retcode,
                raw_broker_response=json.dumps(raw) if raw else None,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record

    def count_open_attempts(self) -> int:
        with self._session_factory() as session:
            statement = select(TradeAttempt).where(TradeAttempt.status == "OPEN")
            return len(list(session.scalars(statement)))

    def save_setup(self, setup: Setup) -> Setup:
        with self._session_factory() as session:
            statement = select(SetupRecord).where(SetupRecord.setup_id == setup.setup_id)
            record = session.scalars(statement).first()
            if record is None:
                record = SetupRecord(setup_id=setup.setup_id)
                session.add(record)
            _write_setup(record, setup)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self.get_setup(setup.telegram_channel_id, setup.telegram_message_id)
                if existing is None:
                    raise
                return existing
            session.refresh(record)
            return _read_setup(record)

    def get_setup(self, channel_id: int, message_id: int) -> Setup | None:
        with self._session_factory() as session:
            statement = select(SetupRecord).where(
                SetupRecord.telegram_channel_id == channel_id,
                SetupRecord.telegram_message_id == message_id,
            )
            record = session.scalars(statement).first()
            if record is None:
                return None
            return _read_setup(record)

    def list_open_setups(self) -> list[Setup]:
        finished = {item.value for item in FINAL_SETUP_STATES}
        with self._session_factory() as session:
            statement = select(SetupRecord).where(SetupRecord.state.notin_(finished))
            return [_read_setup(record) for record in session.scalars(statement)]

    def list_recent_setups(self, limit: int = 200) -> list[Setup]:
        with self._session_factory() as session:
            statement = (
                select(SetupRecord)
                .order_by(SetupRecord.updated_at.desc(), SetupRecord.id.desc())
                .limit(limit)
            )
            return [_read_setup(record) for record in session.scalars(statement)]

    def list_executed(self, limit: int = 10) -> list[ExecutedTrade]:
        with self._session_factory() as session:
            statement = select(ExecutedTrade).order_by(ExecutedTrade.id.desc()).limit(limit)
            return list(session.scalars(statement))


def _write_setup(record: SetupRecord, setup: Setup) -> None:
    record.setup_id = setup.setup_id
    record.telegram_channel_id = setup.telegram_channel_id
    record.telegram_message_id = setup.telegram_message_id
    record.symbol = setup.symbol
    record.broker_symbol = setup.broker_symbol
    record.direction = setup.direction.value
    record.entry_min = setup.entry_min
    record.entry_max = setup.entry_max
    record.stop_loss = setup.stop_loss
    record.tp1 = setup.tp1
    record.tp2 = setup.tp2
    record.tp3 = setup.tp3
    record.state = setup.state.value
    record.trade_1_ticket = setup.trade_1_ticket
    record.trade_2_ticket = setup.trade_2_ticket
    record.break_even_applied = setup.break_even_applied
    record.close_requested = setup.close_requested
    record.raw_message = setup.raw_message
    record.created_at = setup.created_at
    record.executed_at = setup.executed_at
    record.closed_at = setup.closed_at
    record.updated_at = utc_now()


def _read_setup(record: SetupRecord) -> Setup:
    return Setup(
        setup_id=record.setup_id,
        telegram_channel_id=record.telegram_channel_id,
        telegram_message_id=record.telegram_message_id,
        symbol=record.symbol,
        broker_symbol=record.broker_symbol,
        direction=Direction(record.direction),
        entry_min=record.entry_min,
        entry_max=record.entry_max,
        stop_loss=record.stop_loss,
        tp1=record.tp1,
        tp2=record.tp2,
        tp3=record.tp3,
        state=SetupState(record.state),
        trade_1_ticket=record.trade_1_ticket,
        trade_2_ticket=record.trade_2_ticket,
        break_even_applied=bool(record.break_even_applied),
        close_requested=bool(record.close_requested),
        raw_message=record.raw_message or "",
        created_at=record.created_at,
        executed_at=record.executed_at,
        closed_at=record.closed_at,
    )
