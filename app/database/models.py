from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils.time import utc_now


class Base(DeclarativeBase):
    pass


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"
    __table_args__ = (
        UniqueConstraint(
            "telegram_channel_id",
            "telegram_message_id",
            name="uq_telegram_channel_message",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_channel_id: Mapped[int] = mapped_column(Integer, index=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    previous_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    is_edited: Mapped[bool] = mapped_column(default=False)
    is_deleted: Mapped[bool] = mapped_column(default=False)
    is_forwarded: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(64), default="RECEIVED")
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class TradeSignalRecord(Base):
    __tablename__ = "trade_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    telegram_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_message_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    raw_message: Mapped[str] = mapped_column(Text, default="")
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    order_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profits_json: Mapped[str] = mapped_column(Text, default="[]")
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    parser_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    validation_status: Mapped[str] = mapped_column(String(64), default="PENDING")
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_status: Mapped[str] = mapped_column(String(64), default="NONE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TradeAttempt(Base):
    __tablename__ = "trade_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(64), index=True)
    requested_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    requested_order_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    requested_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    requested_stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    requested_take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    mt5_request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    mt5_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    status: Mapped[str] = mapped_column(String(64), default="CREATED")
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExecutedTrade(Base):
    __tablename__ = "executed_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(64), index=True)
    ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    requested_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    broker_retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_broker_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SetupRecord(Base):
    __tablename__ = "setups"
    __table_args__ = (
        UniqueConstraint(
            "telegram_channel_id",
            "telegram_message_id",
            name="uq_setup_channel_message",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    setup_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    telegram_channel_id: Mapped[int] = mapped_column(Integer, index=True)
    telegram_message_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    broker_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direction: Mapped[str] = mapped_column(String(16))
    entry_min: Mapped[float] = mapped_column(Float)
    entry_max: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    tp2: Mapped[float] = mapped_column(Float)
    tp3: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="WAITING_ENTRY", index=True)
    trade_1_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trade_2_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    break_even_applied: Mapped[bool] = mapped_column(default=False)
    close_requested: Mapped[bool] = mapped_column(default=False)
    raw_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ApplicationEvent(Base):
    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    level: Mapped[str] = mapped_column(String(32), default="INFO")
    category: Mapped[str] = mapped_column(String(64), default="app")
    message: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
