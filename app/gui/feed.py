from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.trading.loop import SignalLoopRecord
from app.trading.models import Direction, Setup, SetupState

FeedName = Literal["live", "cancelled", "closed"]

LIVE_STATES = frozenset(
    {
        SetupState.RECEIVED,
        SetupState.WAITING_ENTRY,
        SetupState.ACTIVE,
        SetupState.BREAK_EVEN,
        SetupState.PARTIALLY_CLOSED,
        SetupState.ERROR,
    }
)
CANCELLED_STATES = frozenset({SetupState.CANCELLED, SetupState.CLOSED_BY_SIGNAL})
CLOSED_STATES = frozenset({SetupState.CLOSED})


def feed_for(state: SetupState) -> FeedName:
    if state in LIVE_STATES:
        return "live"
    if state in CANCELLED_STATES:
        return "cancelled"
    if state in CLOSED_STATES:
        return "closed"
    never: SetupState = state
    raise ValueError(f"Unhandled setup state: {never}")


def state_label(state: SetupState) -> str:
    if state is SetupState.RECEIVED:
        return "Received"
    if state is SetupState.WAITING_ENTRY:
        return "Waiting"
    if state is SetupState.ACTIVE:
        return "Open"
    if state is SetupState.BREAK_EVEN:
        return "Break-even"
    if state is SetupState.PARTIALLY_CLOSED:
        return "One leg left"
    if state is SetupState.ERROR:
        return "Needs attention"
    if state is SetupState.CANCELLED:
        return "Cancelled"
    if state is SetupState.CLOSED_BY_SIGNAL:
        return "Closed by reply"
    if state is SetupState.CLOSED:
        return "Closed"
    never: SetupState = state
    raise ValueError(f"Unhandled setup state: {never}")


def status_text(setup: Setup) -> str:
    label = state_label(setup.state)
    if setup.break_even_applied:
        return f"{label} · BE"
    return label


def format_price(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def format_zone(setup: Setup) -> str:
    return f"{format_price(setup.entry_min)}–{format_price(setup.entry_max)}"


def format_legs(setup: Setup) -> str:
    first = "TP1 open" if setup.trade_1_ticket is not None else "TP1 —"
    second = "TP2 open" if setup.trade_2_ticket is not None else "TP2 —"
    return f"{first} · {second}"


def format_when(moment: datetime) -> str:
    local = moment.astimezone() if moment.tzinfo is not None else moment
    now = datetime.now(local.tzinfo) if local.tzinfo is not None else datetime.now()
    if local.date() == now.date():
        return local.strftime("%H:%M:%S")
    return local.strftime("%m-%d %H:%M")


def side_text(direction: Direction) -> str:
    if direction is Direction.BUY:
        return "BUY"
    if direction is Direction.SELL:
        return "SELL"
    never: Direction = direction
    raise ValueError(f"Unhandled direction: {never}")


def detail_line(setup: Setup) -> str:
    first = str(setup.trade_1_ticket) if setup.trade_1_ticket is not None else "—"
    second = str(setup.trade_2_ticket) if setup.trade_2_ticket is not None else "—"
    snippet = " ".join(setup.raw_message.split())
    if len(snippet) > 140:
        snippet = snippet[:139] + "…"
    parts = [
        f"{setup.symbol} {side_text(setup.direction)}",
        format_zone(setup),
        f"tickets {first} / {second}",
    ]
    if snippet:
        parts.append(snippet)
    return "  ·  ".join(parts)


def is_ignored_event(record: SignalLoopRecord) -> bool:
    if record.outcome.startswith("Setup is "):
        return False
    if "Waiting for the entry zone" in record.outcome:
        return False
    return True


def ignored_line(record: SignalLoopRecord) -> str:
    reason = " ".join((record.outcome or record.headline).split())
    if len(reason) > 120:
        reason = reason[:119] + "…"
    return f"{format_when(record.at)}  {record.headline}  {reason}"


def count_feeds(setups: list[Setup]) -> dict[FeedName, int]:
    counts: dict[FeedName, int] = {"live": 0, "cancelled": 0, "closed": 0}
    for setup in setups:
        counts[feed_for(setup.state)] += 1
    return counts
