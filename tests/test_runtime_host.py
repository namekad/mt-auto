from __future__ import annotations

from app.runtime_host import RuntimeHost, merge_pause, status_snapshot
from app.trading.loop import SignalLoopRecord
from app.utils.health import RuntimeState


def test_snapshot_carries_pause_and_connections() -> None:
    state = RuntimeState()
    state.paused = True
    state.telegram_connected = True
    state.listening_channels = ["-1001"]
    state.mt5_account_verified = True
    state.mt5_login = 42
    state.mt5_server = "Demo"
    state.mt5_balance = 10.5
    state.open_positions = 2
    state.loop_events = [SignalLoopRecord("GOLD", "ignored", "warn", False)]
    snap = status_snapshot(True, state)
    assert snap["paused"] is True
    assert snap["telegram_connected"] is True
    assert snap["listening_channels"] == ["-1001"]
    assert snap["mt5_login"] == 42
    assert snap["mt5_balance"] == 10.5
    assert len(snap["loop_events"]) == 1


def test_merge_pause_keeps_a_click_until_the_worker_agrees() -> None:
    shown, goal = merge_pause(True, False)
    assert shown is True
    assert goal is True
    shown, goal = merge_pause(goal, True)
    assert shown is True
    assert goal is None
    shown, goal = merge_pause(None, False)
    assert shown is False
    assert goal is None


def test_host_pause_shows_immediately() -> None:
    host = RuntimeHost()
    host.pause()
    assert host.state.paused is True
    host._apply_status({"paused": False, "telegram_connected": True, "listening_channels": ["gold"]})
    assert host.state.paused is True
    assert host.state.telegram_connected is True
    assert host.state.listening_channels == ["gold"]
    host._apply_status({"paused": True, "mt5_account_verified": True, "mt5_login": 7, "mt5_server": "Demo"})
    assert host.state.paused is True
    assert host._pause_goal is None
    assert host.state.mt5_account_verified is True
    assert host.state.mt5_login == 7
