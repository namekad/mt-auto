from __future__ import annotations

from app.parser.detector import detect_signal, is_close_setup
from app.parser.parser import parse_signal
from app.trading.models import Direction, OrderType
from tests.fixtures import (
    ARABIC_MIXED,
    CONTRADICTORY,
    EMOJI,
    EXAMPLE_A,
    EXAMPLE_B,
    EXAMPLE_C,
    EXAMPLE_D,
    LOWERCASE,
    MALFORMED_NUMBER,
    MISSING_SL,
    MISSING_TP,
    NON_TRADING,
    UPPERCASE,
    VOLUME_SIGNAL,
    WHITESPACE,
    ZONE_BUY,
    ZONE_SELL,
)


def test_example_a(aliases: dict[str, str]) -> None:
    result = parse_signal(EXAMPLE_A, aliases)
    assert result.is_signal
    assert result.signal is not None
    assert result.signal.normalized_symbol == "XAUUSD"
    assert result.signal.direction is Direction.BUY
    assert result.signal.order_type is OrderType.MARKET
    assert result.signal.entry_price == 3642
    assert result.signal.stop_loss == 3628
    assert result.signal.take_profits == [3650.0, 3660.0, 3680.0]


def test_example_b_keeps_range(aliases: dict[str, str]) -> None:
    result = parse_signal(EXAMPLE_B, aliases)
    assert result.is_signal
    assert result.signal is not None
    assert result.signal.entry_price is None
    assert result.signal.entry_low == 3640
    assert result.signal.entry_high == 3643
    assert result.signal.take_profits == [3655.0, 3670.0]


def test_example_c(aliases: dict[str, str]) -> None:
    result = parse_signal(EXAMPLE_C, aliases)
    assert result.is_signal
    assert result.signal is not None
    assert result.signal.direction is Direction.SELL
    assert result.signal.order_type is OrderType.MARKET
    assert result.signal.stop_loss == 3685
    assert result.signal.take_profits == [3650.0]


def test_example_d(aliases: dict[str, str]) -> None:
    result = parse_signal(EXAMPLE_D, aliases)
    assert result.is_signal
    assert result.signal is not None
    assert result.signal.normalized_symbol == "EURUSD"
    assert result.signal.order_type is OrderType.SELL_LIMIT
    assert result.signal.entry_price == 1.18250
    assert result.signal.stop_loss == 1.18500
    assert result.signal.take_profits == [1.18000, 1.17750]


def test_whitespace_and_emoji_and_arabic(aliases: dict[str, str]) -> None:
    for body in (WHITESPACE, EMOJI, ARABIC_MIXED, UPPERCASE, LOWERCASE):
        result = parse_signal(body, aliases)
        assert result.is_signal
        assert result.signal is not None
        assert result.signal.normalized_symbol == "XAUUSD"
        assert result.signal.direction is Direction.BUY
        assert result.signal.entry_price == 3642


def test_missing_levels_are_not_invented(aliases: dict[str, str]) -> None:
    missing_sl = parse_signal(MISSING_SL, aliases)
    assert missing_sl.signal is not None
    assert missing_sl.signal.stop_loss is None
    missing_tp = parse_signal(MISSING_TP, aliases)
    assert missing_tp.signal is not None
    assert missing_tp.signal.take_profits == []


def test_malformed_number_rejected(aliases: dict[str, str]) -> None:
    result = parse_signal(MALFORMED_NUMBER, aliases)
    assert result.is_signal
    assert result.rejection_reason is not None
    assert "Malformed entry" in result.rejection_reason


def test_contradictory_direction(aliases: dict[str, str]) -> None:
    result = parse_signal(CONTRADICTORY, aliases)
    assert result.rejection_reason is not None
    assert "BUY and SELL" in result.rejection_reason


def test_non_trading_ignored(aliases: dict[str, str]) -> None:
    result = parse_signal(NON_TRADING, aliases)
    assert result.is_signal is False
    assert result.signal is None


def test_volume_extracted(aliases: dict[str, str]) -> None:
    result = parse_signal(VOLUME_SIGNAL, aliases)
    assert result.signal is not None
    assert result.signal.volume == 0.02


def test_zone_slash_is_ordered(aliases: dict[str, str]) -> None:
    result = parse_signal(ZONE_BUY, aliases)
    assert result.is_signal
    assert result.rejection_reason is None
    assert result.signal is not None
    assert result.signal.normalized_symbol == "XAUUSD"
    assert result.signal.direction is Direction.BUY
    assert result.signal.entry_low == 4322
    assert result.signal.entry_high == 4323
    assert result.signal.take_profits == [4340.0, 4345.0, 4350.0]
    assert result.signal.stop_loss == 4315


def test_reversed_zone_and_sell(aliases: dict[str, str]) -> None:
    result = parse_signal(ZONE_SELL, aliases)
    assert result.signal is not None
    assert result.signal.direction is Direction.SELL
    assert result.signal.entry_low == 4322
    assert result.signal.entry_high == 4323
    assert result.signal.take_profits == [4305.0, 4300.0, 4295.0]


def test_flexible_tp_labels(aliases: dict[str, str]) -> None:
    body = """Gold Buy 4322 / 4323
TP1 4340
TP 1: 4345
SL: 4315
"""
    result = parse_signal(body, aliases)
    assert result.signal is not None
    assert result.rejection_reason is None
    assert result.signal.entry_low == 4322
    assert result.signal.entry_high == 4323
    assert result.signal.take_profits == [4340.0, 4345.0]
    assert result.signal.stop_loss == 4315


def test_close_setup_phrase() -> None:
    assert is_close_setup("close setup")
    assert is_close_setup("  CLOSE   SETUP ")
    assert is_close_setup("close setup now") is False


def test_detector_rejects_chatter(aliases: dict[str, str]) -> None:
    assert detect_signal(NON_TRADING, aliases) is False
    assert detect_signal(EXAMPLE_A, aliases) is True
