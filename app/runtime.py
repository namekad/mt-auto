from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from queue import Queue
from typing import Any

from app.config import TelegramListenerMode, load_settings, load_symbol_aliases, load_trading_rules
from app.database.repository import Repository
from app.database.session import (
    create_db_engine,
    create_session_factory,
    database_available,
    init_db,
)
from app.exceptions import LiveModeDisabledError, Mt5AccountMismatchError, Mt5ServiceError
from app.notifications.telegram_notifier import TelegramNotifier
from app.services.mt5_service import Mt5Service, import_error as mt5_import_error
from app.telegram.channels import fetch_joined_channels
from app.telegram.control_bot import ControlBot
from app.telegram.listener import TelegramListener
from app.trading.execution_controller import ExecutionController
from app.trading.processor import SignalProcessor
from app.trading.setup_manager import SetupManager
from app.utils.health import RuntimeState
from app.paths import ensure_runtime_files
from app.utils.logging import setup_logging
from app.utils.time import utc_now

logger = logging.getLogger("application")

AuthGetter = Callable[[], str]


class AutomationRuntime:
    def __init__(
        self,
        log_queue: Queue[dict[str, Any]] | None = None,
        code_callback: AuthGetter | None = None,
        password_callback: AuthGetter | None = None,
    ) -> None:
        self.state = RuntimeState()
        self._log_queue = log_queue
        self._code_callback = code_callback
        self._password_callback = password_callback
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._running = False
        self._mt5: Mt5Service | None = None
        self._listener: TelegramListener | None = None
        self._control_bot: ControlBot | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and self._stop_event is not None:
            loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=8)
        self._running = False

    def pause(self) -> None:
        self.state.paused = True
        self.state.processing_active = False
        logger.info("Signal processing paused from UI")

    def resume(self) -> None:
        self.state.paused = False
        self.state.processing_active = True
        logger.info("Signal processing resumed from UI")

    def list_joined_channels(self) -> list[tuple[int, str]]:
        settings = load_settings()
        mode = settings.telegram_listener_mode
        if mode is TelegramListenerMode.BOT:
            raise RuntimeError("Load channels after you sign in with My Telegram account.")
        if mode is not TelegramListenerMode.USER:
            never: TelegramListenerMode = mode
            raise ValueError(f"Unhandled listener mode: {never}")
        if self._running:
            loop = self._loop
            listener = self._listener
            if loop is None or listener is None:
                raise RuntimeError("Telegram is still starting. Try again in a moment.")
            future = asyncio.run_coroutine_threadsafe(listener.list_channels(), loop)
            return future.result(timeout=45)
        return asyncio.run(fetch_joined_channels(settings))

    def _thread_main(self) -> None:
        ensure_runtime_files()
        setup_logging()
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception:
            logger.exception("Automation runtime crashed")
        finally:
            self._running = False
            self._loop.close()
            self._loop = None

    async def _run(self) -> None:
        settings = load_settings()
        paused = self.state.paused
        self.state = RuntimeState()
        self.state.paused = paused
        self.state.processing_active = not paused
        self._running = True
        try:
            settings.assert_not_live()
        except LiveModeDisabledError as error:
            logger.error("%s", error)
            self.state.execution_blocked = True

        aliases = load_symbol_aliases(settings.symbols_path)
        rules = load_trading_rules(settings.trading_rules_path)
        self.state.symbol_mapping_ok = bool(aliases)

        engine = create_db_engine(settings)
        init_db(engine)
        self.state.database_ok = database_available(engine)
        repository = Repository(create_session_factory(engine))
        repository.add_event("Application starting", category="startup")

        mt5_service = Mt5Service(settings)
        self._mt5 = mt5_service
        _connect_mt5(settings, self.state, mt5_service)
        notifier = TelegramNotifier(settings)
        execution = ExecutionController(settings, rules, mt5_service, repository)
        setup_manager = SetupManager(
            settings, rules, repository, mt5_service, self.state
        )
        processor = SignalProcessor(
            settings,
            rules,
            aliases,
            repository,
            self.state,
            execution,
            setup_manager,
        )
        listener = TelegramListener(
            settings,
            processor,
            self.state,
            notifier,
            code_callback=self._code_callback,
            password_callback=self._password_callback,
        )
        control_bot = ControlBot(settings, self.state, repository, mt5_service)
        self._listener = listener
        self._control_bot = control_bot
        self._stop_event = asyncio.Event()

        tasks: list[asyncio.Task[None]] = []
        try:
            if listener.credentials_ready():
                await listener.connect()
            if control_bot.credentials_ready():
                await control_bot.start()
            if settings.has_control_bot_credentials():
                await notifier.system_started()
                if self.state.mt5_connected:
                    await notifier.mt5_connected()
            if listener.credentials_ready() and self.state.telegram_connected:
                tasks.append(asyncio.create_task(listener.listen(), name="telegram-listener"))
            setup_manager.reconcile()
            tasks.append(
                asyncio.create_task(self._watch_setups(setup_manager), name="setup-monitor")
            )
            logger.info("Automation is running")
            tasks.append(asyncio.create_task(self._stop_event.wait(), name="stop-wait"))
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            raise
        finally:
            for task in tasks:
                task.cancel()
            await listener.stop()
            await control_bot.stop()
            mt5_service.shutdown()
            repository.add_event("Application stopped", category="shutdown")
            if settings.has_control_bot_credentials():
                await notifier.system_stopped()
            logger.info("Automation stopped")

    async def _watch_setups(self, manager: SetupManager) -> None:
        if self._stop_event is None:
            return
        while not self._stop_event.is_set():
            try:
                manager.tick()
            except Exception:
                logger.exception("Setup monitor failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
            except TimeoutError:
                continue


def _connect_mt5(settings, state: RuntimeState, service: Mt5Service) -> None:
    if not service.available():
        state.mt5_installation = "NOT FOUND"
        reason = mt5_import_error() or "the module did not load (no further detail is available)."
        error = Mt5ServiceError(
            f"The MetaTrader5 Python package failed to load inside this app: {reason}"
        )
        logger.error("%s", error)
        state.mark_exception(error)
        state.mt5_last_error = str(error)
        return
    state.mt5_installation = "FOUND"
    if not settings.has_mt5_credentials():
        logger.info("MT5 credentials are not set")
        return
    if settings.resolved_app_mode.value != "DEMO":
        logger.error("MT5 connection skipped because APP_MODE is not DEMO.")
        return
    try:
        service.initialize()
        state.mt5_initialized = True
        service.login()
        snapshot = service.verify_account()
        state.mt5_connected = True
        state.mt5_account_verified = True
        state.mt5_account_type = snapshot.trade_mode_label
        state.mt5_balance = snapshot.balance
        state.mt5_login = snapshot.login
        state.mt5_server = snapshot.server
        state.open_positions = len(service.get_positions())
        state.last_mt5_health = utc_now()
        state.mt5_last_error = None
    except (Mt5ServiceError, Mt5AccountMismatchError) as error:
        logger.error("MT5 startup failed: %s", error)
        state.mark_exception(error)
        state.mt5_last_error = str(error)
        state.mt5_connected = False
        state.mt5_account_verified = False
        state.execution_blocked = True
