from __future__ import annotations

import asyncio
import logging
import multiprocessing
import queue
from multiprocessing.queues import Queue as MpQueue
from typing import Any

from app.config import load_settings
from app.telegram.channels import fetch_joined_channels
from app.trading.loop import SignalLoopRecord
from app.utils.health import RuntimeState

Command = tuple[str, ...]


class _ProcessLogHandler(logging.Handler):
    def __init__(self, log_queue: MpQueue[dict[str, str]]) -> None:
        super().__init__()
        self._queue = log_queue
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(name)s  %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._queue.qsize() > 200:
                return
            self._queue.put_nowait(
                {
                    "level": record.levelname,
                    "name": record.name,
                    "message": self.format(record),
                }
            )
        except Exception:
            return


def status_snapshot(worker_running: bool, state: RuntimeState) -> dict[str, Any]:
    try:
        channels = [str(item) for item in state.listening_channels]
    except Exception:
        channels = []
    try:
        events: list[SignalLoopRecord] | None = list(state.loop_events)
    except Exception:
        events = None
    payload: dict[str, Any] = {
        "worker_running": worker_running,
        "paused": bool(state.paused),
        "telegram_connected": bool(state.telegram_connected),
        "listening_channels": channels,
        "mt5_account_verified": bool(state.mt5_account_verified),
        "mt5_installation": state.mt5_installation,
        "mt5_login": state.mt5_login,
        "mt5_server": state.mt5_server,
        "mt5_balance": state.mt5_balance,
        "open_positions": int(state.open_positions),
        "last_exception": state.last_exception,
    }
    if events is not None:
        payload["loop_events"] = events
    return payload


def merge_pause(goal: bool | None, reported: bool) -> tuple[bool, bool | None]:
    if goal is None or reported == goal:
        return reported, None
    return goal, goal


def run_runtime_process(
    state_queue: MpQueue[dict[str, Any]],
    command_queue: MpQueue[tuple[Any, ...]],
    auth_queue: MpQueue[str],
    auth_reply_queue: MpQueue[str],
    log_queue: MpQueue[dict[str, str]],
    channel_queue: MpQueue[tuple[str, Any]],
) -> None:
    from app.runtime import AutomationRuntime

    logging.getLogger().addHandler(_ProcessLogHandler(log_queue))

    def ask(kind: str) -> str:
        auth_queue.put(kind)
        try:
            return auth_reply_queue.get(timeout=300)
        except queue.Empty:
            return ""

    runtime = AutomationRuntime(
        code_callback=lambda: ask("code"),
        password_callback=lambda: ask("password"),
    )
    runtime.start()
    while True:
        _publish(state_queue, runtime.running, runtime.state)
        try:
            command = command_queue.get(timeout=0.4)
        except queue.Empty:
            continue
        name = command[0] if command else ""
        if name == "stop":
            runtime.stop()
            _publish(state_queue, False, runtime.state)
            return
        if name == "pause":
            runtime.pause()
            _publish(state_queue, runtime.running, runtime.state)
            continue
        if name == "resume":
            runtime.resume()
            _publish(state_queue, runtime.running, runtime.state)
            continue
        if name == "channels":
            try:
                rows = runtime.list_joined_channels()
            except Exception as error:
                channel_queue.put(("error", str(error)))
            else:
                channel_queue.put(("ok", rows))
            continue
        raise ValueError(f"Unhandled runtime command: {name}")


def _publish(state_queue: MpQueue[dict[str, Any]], running: bool, state: RuntimeState) -> None:
    try:
        payload = status_snapshot(running, state)
    except Exception:
        return
    try:
        while True:
            state_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        state_queue.put_nowait(payload)
    except queue.Full:
        return


class RuntimeHost:
    def __init__(self) -> None:
        context = multiprocessing.get_context("spawn")
        self.state = RuntimeState()
        self._context = context
        self._state_queue: MpQueue[dict[str, Any]] = context.Queue(maxsize=1)
        self._command_queue: MpQueue[tuple[Any, ...]] = context.Queue()
        self._auth_queue: MpQueue[str] = context.Queue()
        self._auth_reply_queue: MpQueue[str] = context.Queue()
        self._log_queue: MpQueue[dict[str, str]] = context.Queue()
        self._channel_queue: MpQueue[tuple[str, Any]] = context.Queue()
        self._process: multiprocessing.Process | None = None
        self._pause_goal: bool | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._reap()
        self._pause_goal = None
        self.state = RuntimeState()
        process = self._context.Process(
            target=run_runtime_process,
            args=(
                self._state_queue,
                self._command_queue,
                self._auth_queue,
                self._auth_reply_queue,
                self._log_queue,
                self._channel_queue,
            ),
            daemon=True,
        )
        process.start()
        self._process = process

    def stop(self) -> None:
        process = self._process
        if process is None:
            self._pause_goal = None
            return
        if process.is_alive():
            try:
                self._command_queue.put(("stop",))
            except Exception:
                pass
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
        self._process = None
        self._pause_goal = None

    def pause(self) -> None:
        self._pause_goal = True
        self.state.paused = True
        self.state.processing_active = False
        self._command(("pause",))

    def resume(self) -> None:
        self._pause_goal = False
        self.state.paused = False
        self.state.processing_active = True
        self._command(("resume",))

    def poll(
        self,
        log_sink: queue.Queue[dict[str, Any]],
        code_callback: Any,
        password_callback: Any,
    ) -> None:
        self._drain_logs(log_sink)
        self._drain_state()
        self._answer_auth(code_callback, password_callback)
        self._reap()

    def list_joined_channels(self) -> list[tuple[int, str]]:
        if self._process is not None and self._process.is_alive():
            self._command(("channels",))
            kind, payload = self._channel_queue.get(timeout=50)
            if kind == "error":
                raise RuntimeError(str(payload))
            if kind != "ok" or not isinstance(payload, list):
                raise RuntimeError("Could not read the channel list.")
            return [(int(item[0]), str(item[1])) for item in payload]
        return asyncio.run(fetch_joined_channels(load_settings()))

    def _command(self, command: tuple[Any, ...]) -> None:
        if self._process is None or not self._process.is_alive():
            return
        self._command_queue.put(command)

    def _drain_logs(self, log_sink: queue.Queue[dict[str, Any]]) -> None:
        moved = 0
        while moved < 40:
            try:
                item = self._log_queue.get_nowait()
            except queue.Empty:
                return
            moved += 1
            try:
                log_sink.put_nowait(item)
            except queue.Full:
                return

    def _drain_state(self) -> None:
        payload: dict[str, Any] | None = None
        while True:
            try:
                item = self._state_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, dict):
                payload = item
        if payload is None:
            return
        self._apply_status(payload)

    def _apply_status(self, payload: dict[str, Any]) -> None:
        reported = bool(payload.get("paused"))
        paused, self._pause_goal = merge_pause(self._pause_goal, reported)
        state = self.state
        state.paused = paused
        state.processing_active = not paused
        state.telegram_connected = bool(payload.get("telegram_connected"))
        channels = payload.get("listening_channels")
        if isinstance(channels, list):
            state.listening_channels = [str(item) for item in channels]
        state.mt5_account_verified = bool(payload.get("mt5_account_verified"))
        installation = payload.get("mt5_installation")
        if isinstance(installation, str):
            state.mt5_installation = installation
        login = payload.get("mt5_login")
        state.mt5_login = int(login) if isinstance(login, int) else None
        server = payload.get("mt5_server")
        state.mt5_server = server if isinstance(server, str) else None
        balance = payload.get("mt5_balance")
        state.mt5_balance = float(balance) if isinstance(balance, int | float) else None
        positions = payload.get("open_positions")
        state.open_positions = int(positions) if isinstance(positions, int) else 0
        error = payload.get("last_exception")
        state.last_exception = error if isinstance(error, str) else None
        events = payload.get("loop_events")
        if isinstance(events, list):
            state.loop_events = [item for item in events if isinstance(item, SignalLoopRecord)]

    def _answer_auth(self, code_callback: Any, password_callback: Any) -> None:
        try:
            kind = self._auth_queue.get_nowait()
        except queue.Empty:
            return
        value = ""
        if kind == "code":
            value = str(code_callback() or "")
        elif kind == "password":
            value = str(password_callback() or "")
        else:
            raise ValueError(f"Unhandled auth prompt: {kind}")
        self._auth_reply_queue.put(value)

    def _reap(self) -> None:
        process = self._process
        if process is not None and not process.is_alive():
            process.join(timeout=0)
            self._process = None
            self._pause_goal = None
