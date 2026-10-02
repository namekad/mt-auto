from __future__ import annotations

from telegram import Bot

from app.config import Settings
from app.trading.models import TradeSignal
from app.utils.logging import get_logger
from app.utils.time import format_local, utc_now

logger = get_logger("telegram")


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._last_error_key: str | None = None

    async def send(self, text: str) -> None:
        if not self._settings.has_control_bot_credentials():
            logger.info("Notification skipped; control bot is not configured")
            return
        bot = Bot(self._settings.control_bot_token)
        await bot.send_message(
            chat_id=self._settings.control_chat_id,
            text=text,
        )

    async def system_started(self) -> None:
        await self.send("SYSTEM STARTED\nAUTO-TRADER is running.")

    async def system_stopped(self) -> None:
        await self.send("SYSTEM STOPPED")

    async def mt5_connected(self) -> None:
        await self.send("MT5 connected.")

    async def mt5_disconnected(self) -> None:
        await self.send(
            "SYSTEM WARNING\n\nMT5 connection lost.\nAutomatic execution has been paused.\n"
            f"Time:\n{format_local(utc_now(), self._settings.local_timezone)}"
        )

    async def signal_received(self, signal: TradeSignal) -> None:
        await self.send(
            "SIGNAL RECEIVED\n"
            f"Symbol: {signal.normalized_symbol}\n"
            f"Direction: {signal.direction}\n"
            f"Message ID: {signal.telegram_message_id}"
        )

    async def signal_rejected(self, signal: TradeSignal) -> None:
        await self.send(
            "SIGNAL REJECTED\n\n"
            f"Reason:\n{signal.rejection_reason}\n\n"
            f"Message:\n{signal.telegram_message_id}"
        )

    async def unexpected_error(self, message: str) -> None:
        if message == self._last_error_key:
            return
        self._last_error_key = message
        await self.send(f"SYSTEM WARNING\n\n{message}")
