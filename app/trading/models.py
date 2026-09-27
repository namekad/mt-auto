from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field

from app.utils.time import utc_now


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    BUY_LIMIT = "BUY_LIMIT"
    SELL_LIMIT = "SELL_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_STOP = "SELL_STOP"


class ValidationStatus(str, Enum):
    PENDING = "PENDING"
    PARSED = "PARSED"
    INVALID = "INVALID"
    REJECTED = "REJECTED"
    SEMANTIC_DUPLICATE = "SEMANTIC_DUPLICATE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ExecutionStatus(str, Enum):
    NONE = "NONE"
    OBSERVED = "OBSERVED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    DRY_RUN = "DRY_RUN"


class SetupState(str, Enum):
    RECEIVED = "RECEIVED"
    WAITING_ENTRY = "WAITING_ENTRY"
    ACTIVE = "ACTIVE"
    BREAK_EVEN = "BREAK_EVEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"
    CLOSED_BY_SIGNAL = "CLOSED_BY_SIGNAL"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


FINAL_SETUP_STATES = frozenset(
    {
        SetupState.CLOSED,
        SetupState.CLOSED_BY_SIGNAL,
        SetupState.CANCELLED,
    }
)


class MessageStatus(str, Enum):
    RECEIVED = "RECEIVED"
    IGNORED = "IGNORED"
    PARSED = "PARSED"
    PARSE_FAILED = "PARSE_FAILED"
    WAITING_ENTRY = "WAITING_ENTRY"
    INVALID = "INVALID"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    DELETED = "DELETED"
    EDITED_AFTER_EXECUTE = "EDITED_AFTER_EXECUTE"
    SEMANTIC_DUPLICATE = "SEMANTIC_DUPLICATE"


class TradeSignal(BaseModel):
    signal_id: str = Field(default_factory=lambda: uuid4().hex)
    telegram_channel_id: int | None = None
    telegram_message_id: int | None = None
    telegram_message_date: datetime | None = None
    received_at: datetime = Field(default_factory=utc_now)
    raw_message: str = ""
    symbol: str | None = None
    normalized_symbol: str | None = None
    broker_symbol: str | None = None
    direction: Direction | None = None
    order_type: OrderType | None = None
    entry_price: float | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop_loss: float | None = None
    take_profits: list[float] = Field(default_factory=list)
    volume: float | None = None
    risk_percent: float | None = None
    comment: str | None = None
    parser_confidence: float = 0.0
    validation_status: ValidationStatus = ValidationStatus.PENDING
    rejection_reason: str | None = None
    execution_status: ExecutionStatus = ExecutionStatus.NONE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    is_forwarded: bool = False
    is_edited: bool = False
    source_authorized: bool = True

    def has_entry_range(self) -> bool:
        return self.entry_low is not None and self.entry_high is not None

    def selected_take_profit(self, strategy: str, selected_index: int = 1) -> float | None:
        if not self.take_profits:
            return None
        if strategy == "FIRST_TP":
            return self.take_profits[0]
        if strategy == "SELECTED_TP":
            index = selected_index - 1
            if 0 <= index < len(self.take_profits):
                return self.take_profits[index]
            return None
        return self.take_profits[-1]


class Setup(BaseModel):
    setup_id: str
    telegram_channel_id: int
    telegram_message_id: int
    symbol: str
    broker_symbol: str | None = None
    direction: Direction
    entry_min: float
    entry_max: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float | None = None
    state: SetupState = SetupState.WAITING_ENTRY
    trade_1_ticket: int | None = None
    trade_2_ticket: int | None = None
    break_even_applied: bool = False
    close_requested: bool = False
    raw_message: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    executed_at: datetime | None = None
    closed_at: datetime | None = None

    def leg_comment(self, leg: int) -> str:
        if leg == 1:
            return f"TG_{self.telegram_message_id}_TP1"
        if leg == 2:
            return f"TG_{self.telegram_message_id}_TP2"
        raise ValueError(f"Unhandled setup leg: {leg}")


class ValidationResult(BaseModel):
    ok: bool
    status: ValidationStatus
    reasons: list[str] = Field(default_factory=list)
    requires_review: bool = False

    @property
    def reason_text(self) -> str | None:
        if not self.reasons:
            return None
        return "; ".join(self.reasons)
