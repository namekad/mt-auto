from __future__ import annotations

from datetime import datetime

from app.config import TradingRules
from app.trading.models import (
    Direction,
    OrderType,
    TradeSignal,
    ValidationResult,
    ValidationStatus,
)
from app.trading.symbol_resolver import symbol_allowed
from app.utils.telegram_ids import is_allowed_channel
from app.utils.time import age_seconds

DUPLICATE_REASON = (
    "Same symbol, direction, entry, stop, and take-profits were already "
    "recorded in the last 24 hours."
)

PENDING_TYPES = {
    OrderType.BUY_LIMIT,
    OrderType.SELL_LIMIT,
    OrderType.BUY_STOP,
    OrderType.SELL_STOP,
}


def validate_signal_age(
    message_time: datetime,
    rules: TradingRules,
    now: datetime | None = None,
) -> ValidationResult:
    age = age_seconds(message_time, now)
    if age > rules.maximum_signal_age_seconds:
        return ValidationResult(
            ok=False,
            status=ValidationStatus.REJECTED,
            reasons=[
                f"Signal is stale ({int(age)}s old; limit {rules.maximum_signal_age_seconds}s)."
            ],
        )
    return ValidationResult(ok=True, status=ValidationStatus.PARSED)


def _zone_direction_errors(
    signal: TradeSignal, entry_min: float, entry_max: float
) -> list[str]:
    if signal.direction is None:
        return []
    errors: list[str] = []
    take_profits = signal.take_profits
    if signal.direction is Direction.BUY:
        if signal.stop_loss is not None and signal.stop_loss >= entry_min:
            errors.append(
                "Direction detected as BUY, but Stop Loss is not below the entry zone."
            )
        if take_profits and take_profits[0] <= entry_max:
            errors.append(
                "Direction detected as BUY, but TP1 is not above the entry zone."
            )
        if len(take_profits) >= 2 and take_profits[1] <= take_profits[0]:
            errors.append("Direction detected as BUY, but TP2 is not above TP1.")
        if len(take_profits) >= 3 and take_profits[2] <= take_profits[1]:
            errors.append("Direction detected as BUY, but TP3 is not above TP2.")
        return errors
    if signal.direction is Direction.SELL:
        if signal.stop_loss is not None and signal.stop_loss <= entry_max:
            errors.append(
                "Direction detected as SELL, but Stop Loss is not above the entry zone."
            )
        if take_profits and take_profits[0] >= entry_min:
            errors.append(
                "Direction detected as SELL, but TP1 is not below the entry zone."
            )
        if len(take_profits) >= 2 and take_profits[1] >= take_profits[0]:
            errors.append("Direction detected as SELL, but TP2 is not below TP1.")
        if len(take_profits) >= 3 and take_profits[2] >= take_profits[1]:
            errors.append("Direction detected as SELL, but TP3 is not below TP2.")
        return errors
    never: Direction = signal.direction
    raise ValueError(f"Unhandled direction: {never}")


def validate_signal(
    signal: TradeSignal,
    rules: TradingRules,
    *,
    allowed_channel_ids: list[int] | None = None,
    now: datetime | None = None,
    mt5_connected: bool = False,
    symbol_exists: bool | None = None,
    market_price: float | None = None,
    open_positions: int | None = None,
    already_executed: bool = False,
    semantic_duplicate: bool = False,
) -> ValidationResult:
    reasons: list[str] = []
    requires_review = False

    if already_executed:
        return ValidationResult(
            ok=False,
            status=ValidationStatus.REJECTED,
            reasons=["Message ID has already been executed."],
        )

    if allowed_channel_ids is not None and signal.telegram_channel_id is not None:
        if not is_allowed_channel(signal.telegram_channel_id, allowed_channel_ids):
            reasons.append("Signal source is not authorized.")

    if signal.direction is None:
        reasons.append("Direction is missing.")
    elif signal.direction.value not in rules.allowed_directions:
        reasons.append(f"Direction {signal.direction.value} is not allowed.")

    if signal.order_type is None:
        reasons.append("Order type is missing.")
    else:
        if signal.order_type is OrderType.MARKET and not rules.allow_market_orders:
            reasons.append("Market orders are not allowed.")
        if signal.order_type in PENDING_TYPES and not rules.allow_pending_orders:
            reasons.append("Pending orders are not allowed.")

    if not signal.normalized_symbol:
        reasons.append("Symbol is missing.")
    elif not symbol_allowed(
        signal.normalized_symbol, signal.broker_symbol, rules.allowed_symbols
    ):
        reasons.append(f"Symbol {signal.normalized_symbol} is not allowed.")

    if not signal.has_entry_range():
        reasons.append("Entry zone is required.")
    else:
        entry_min = min(signal.entry_low or 0.0, signal.entry_high or 0.0)
        entry_max = max(signal.entry_low or 0.0, signal.entry_high or 0.0)
        signal.entry_low = entry_min
        signal.entry_high = entry_max
        reasons.extend(_zone_direction_errors(signal, entry_min, entry_max))

    if rules.require_stop_loss and signal.stop_loss is None:
        reasons.append("Stop loss is required.")

    if rules.require_take_profit and len(signal.take_profits) < 2:
        reasons.append("TP1 and TP2 are required.")
    elif len(signal.take_profits) < rules.minimum_take_profits:
        reasons.append(
            f"At least {rules.minimum_take_profits} take-profit level(s) required."
        )

    signal.volume = rules.lot_size
    if signal.volume <= 0:
        reasons.append("Volume must be greater than zero.")

    if any("Direction detected" in item for item in reasons):
        requires_review = True

    if mt5_connected:
        if symbol_exists is False:
            label = signal.symbol or signal.normalized_symbol or "this"
            reasons.append(f"No tradable {label} symbol on this MetaTrader account.")
    if semantic_duplicate:
        requires_review = True
        reasons.append(DUPLICATE_REASON)

    other_reasons = [item for item in reasons if item != DUPLICATE_REASON]
    if semantic_duplicate and not other_reasons:
        return ValidationResult(
            ok=False,
            status=ValidationStatus.SEMANTIC_DUPLICATE,
            reasons=reasons,
            requires_review=True,
        )
    if semantic_duplicate:
        return ValidationResult(
            ok=False,
            status=ValidationStatus.REJECTED,
            reasons=reasons,
            requires_review=True,
        )
    if reasons:
        return ValidationResult(
            ok=False,
            status=ValidationStatus.REJECTED,
            reasons=reasons,
            requires_review=requires_review,
        )
    return ValidationResult(ok=True, status=ValidationStatus.PARSED)
