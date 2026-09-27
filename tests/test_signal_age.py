from __future__ import annotations

from datetime import timedelta

from app.config import TradingRules
from app.parser.parser import parse_signal
from app.trading.validation import validate_signal, validate_signal_age
from app.utils.time import utc_now
from tests.fixtures import ZONE_BUY


def test_fresh_signal_ok(rules: TradingRules) -> None:
    now = utc_now()
    result = validate_signal_age(now, rules, now=now)
    assert result.ok


def test_stale_signal_rejected(rules: TradingRules) -> None:
    now = utc_now()
    message_time = now - timedelta(seconds=rules.maximum_signal_age_seconds + 5)
    result = validate_signal_age(message_time, rules, now=now)
    assert result.ok is False
    assert "stale" in (result.reason_text or "").lower()


def test_validate_signal_ignores_age(aliases: dict[str, str], rules: TradingRules) -> None:
    parsed = parse_signal(ZONE_BUY, aliases)
    assert parsed.signal is not None
    parsed.signal.telegram_message_date = utc_now() - timedelta(seconds=500)
    result = validate_signal(parsed.signal, rules)
    assert result.ok, result.reasons
    assert not any("stale" in item.lower() for item in result.reasons)
