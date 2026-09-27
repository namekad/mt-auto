from __future__ import annotations

from datetime import datetime, timezone

from app.database.repository import Repository
from app.gui.feed import (
    detail_line,
    feed_for,
    format_legs,
    format_price,
    ignored_line,
    is_ignored_event,
    state_label,
    status_text,
)
from app.trading.loop import SignalLoopRecord
from app.trading.models import Direction, Setup, SetupState


def _setup(message_id: int, state: SetupState, *, break_even: bool = False) -> Setup:
    return Setup(
        setup_id=f"1001:{message_id}",
        telegram_channel_id=1001,
        telegram_message_id=message_id,
        symbol="XAUUSD",
        direction=Direction.BUY,
        entry_min=4322,
        entry_max=4323,
        stop_loss=4315,
        tp1=4340,
        tp2=4345,
        state=state,
        break_even_applied=break_even,
        trade_1_ticket=10 if state is SetupState.ACTIVE else None,
        raw_message="GOLD buy 4322/4323",
        created_at=datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc),
    )


def test_feed_covers_every_state() -> None:
    expected = {
        SetupState.RECEIVED: "live",
        SetupState.WAITING_ENTRY: "live",
        SetupState.ACTIVE: "live",
        SetupState.BREAK_EVEN: "live",
        SetupState.PARTIALLY_CLOSED: "live",
        SetupState.ERROR: "live",
        SetupState.CANCELLED: "cancelled",
        SetupState.CLOSED_BY_SIGNAL: "cancelled",
        SetupState.CLOSED: "closed",
    }
    for state in SetupState:
        assert feed_for(state) == expected[state]
        assert state_label(state)


def test_break_even_and_legs_are_compact() -> None:
    waiting = _setup(1, SetupState.WAITING_ENTRY)
    assert format_legs(waiting) == "TP1 — · TP2 —"
    assert status_text(waiting) == "Waiting"
    live = _setup(2, SetupState.BREAK_EVEN, break_even=True)
    live.trade_1_ticket = 11
    live.trade_2_ticket = 12
    assert format_legs(live) == "TP1 open · TP2 open"
    assert status_text(live) == "Break-even · BE"
    assert format_price(4322.0) == "4322"
    assert "4322–4323" in detail_line(waiting)


def test_ignored_events_skip_setup_notes() -> None:
    quiet = SignalLoopRecord(
        headline="Not a trade post",
        outcome="The post was not treated as a signal.",
        kind="idle",
        order_sent=False,
        at=datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc),
    )
    setup_note = SignalLoopRecord(
        headline="XAUUSD BUY",
        outcome="Setup is ACTIVE.",
        kind="ok",
        order_sent=True,
    )
    waiting = SignalLoopRecord(
        headline="XAUUSD BUY",
        outcome="Waiting for the entry zone. No MetaTrader order was sent.",
        kind="warn",
        order_sent=False,
    )
    assert is_ignored_event(quiet) is True
    assert is_ignored_event(setup_note) is False
    assert is_ignored_event(waiting) is False
    assert quiet.headline in ignored_line(quiet)


def test_list_recent_setups_newest_first(repository: Repository) -> None:
    repository.save_setup(_setup(1, SetupState.WAITING_ENTRY))
    repository.save_setup(_setup(2, SetupState.CANCELLED))
    rows = repository.list_recent_setups()
    assert [row.telegram_message_id for row in rows] == [2, 1]
