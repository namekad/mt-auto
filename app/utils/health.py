from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.trading.loop import SignalLoopRecord
from app.utils.time import utc_now


@dataclass
class RuntimeState:
    started_at: datetime = field(default_factory=utc_now)
    paused: bool = False
    telegram_connected: bool = False
    telegram_listener_active: bool = False
    control_bot_active: bool = False
    mt5_initialized: bool = False
    mt5_connected: bool = False
    mt5_account_verified: bool = False
    mt5_account_type: str | None = None
    mt5_balance: float | None = None
    mt5_login: int | None = None
    mt5_server: str | None = None
    open_positions: int = 0
    database_ok: bool = False
    processing_active: bool = True
    last_telegram_event: datetime | None = None
    last_mt5_health: datetime | None = None
    last_exception: str | None = None
    last_signal_summary: str | None = None
    last_successful_execution: str | None = None
    loop_events: list[SignalLoopRecord] = field(default_factory=list)
    listening_channels: list[str] = field(default_factory=list)
    execution_blocked: bool = True
    symbol_mapping_ok: bool = False
    mt5_installation: str = "UNKNOWN"
    mt5_last_error: str | None = None

    def mark_exception(self, error: BaseException) -> None:
        self.last_exception = f"{type(error).__name__}: {error}"

    def record_loop(self, record: SignalLoopRecord) -> None:
        self.last_signal_summary = f"{record.headline} · {record.outcome}"
        self.loop_events.insert(0, record)
        self.loop_events = self.loop_events[:25]

    def uptime_seconds(self) -> float:
        return (utc_now() - self.started_at).total_seconds()
