from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient, events
from telethon.errors import PhoneNumberInvalidError

from app.config import PROJECT_ROOT, Settings, TelegramListenerMode
from app.telegram.channels import collect_channels
from app.notifications.telegram_notifier import TelegramNotifier
from app.trading.models import TradeSignal, ValidationStatus
from app.trading.processor import SignalProcessor
from app.utils.health import RuntimeState
from app.utils.logging import get_logger
from app.utils.time import ensure_utc

logger = get_logger("telegram")


class TelegramListener:
    def __init__(
        self,
        settings: Settings,
        processor: SignalProcessor,
        state: RuntimeState,
        notifier: TelegramNotifier,
        code_callback: Callable[[], str] | None = None,
        password_callback: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._processor = processor
        self._state = state
        self._notifier = notifier
        self._code_callback = code_callback
        self._password_callback = password_callback
        self._user_client: TelegramClient | None = None
        self._bot_app: Application | None = None

    def credentials_ready(self) -> bool:
        if self._settings.telegram_listener_mode is TelegramListenerMode.USER:
            return self._settings.has_telegram_user_credentials()
        if self._settings.telegram_listener_mode is TelegramListenerMode.BOT:
            return self._settings.has_telegram_bot_credentials()
        never: TelegramListenerMode = self._settings.telegram_listener_mode
        raise ValueError(f"Unhandled listener mode: {never}")

    async def connect(self) -> None:
        if not self.credentials_ready():
            logger.info("Telegram listener credentials are not configured")
            return
        if self._settings.telegram_listener_mode is TelegramListenerMode.USER:
            await self._connect_user()
            return
        if self._settings.telegram_listener_mode is TelegramListenerMode.BOT:
            await self._start_bot()
            return
        never: TelegramListenerMode = self._settings.telegram_listener_mode
        raise ValueError(f"Unhandled listener mode: {never}")

    async def listen(self) -> None:
        if self._user_client is not None:
            await self._user_client.run_until_disconnected()
            return
        if self._bot_app is not None:
            while self._state.telegram_listener_active:
                await asyncio.sleep(3600)

    async def start(self) -> None:
        await self.connect()
        await self.listen()

    async def list_channels(self) -> list[tuple[int, str]]:
        if self._user_client is None:
            raise RuntimeError("Sign in first. Press Start and enter the Telegram code.")
        return await collect_channels(self._user_client)

    async def stop(self) -> None:
        self._state.telegram_listener_active = False
        if self._user_client is not None:
            await self._user_client.disconnect()
        if self._bot_app is not None:
            await self._bot_app.stop()
            await self._bot_app.shutdown()

    async def _connect_user(self) -> None:
        session_path = (PROJECT_ROOT / self._settings.telegram_session_path).resolve()
        session_path.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(session_path),
            self._settings.telegram_api_id or 0,
            self._settings.telegram_api_hash or "",
            catch_up=True,
        )
        client.add_event_handler(self._safe_handle_new, events.NewMessage)
        client.add_event_handler(self._safe_handle_edit, events.MessageEdited)
        client.add_event_handler(self._safe_handle_delete, events.MessageDeleted)
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            if self._settings.telegram_phone and not _same_phone(
                self._settings.telegram_phone, getattr(me, "phone", None)
            ):
                logger.warning(
                    "TELEGRAM_PHONE does not match the saved session. "
                    "Keeping the existing Telegram login. "
                    "Use Clear Telegram session only if you want to switch accounts."
                )
        if not await client.is_user_authorized():
            if not self._settings.telegram_phone:
                logger.error("TELEGRAM_PHONE is not set")
                await client.disconnect()
                return
            if self._code_callback is None and not sys.stdin.isatty():
                logger.error(
                    "Telegram user session is not authorized. "
                    "Start from the GUI or an interactive terminal."
                )
                await client.disconnect()
                return
            try:
                start_kwargs: dict[str, object] = {"phone": self._settings.telegram_phone}
                if self._code_callback is not None:
                    start_kwargs["code_callback"] = self._code_callback
                if self._password_callback is not None:
                    start_kwargs["password"] = self._password_callback
                await client.start(**start_kwargs)
            except PhoneNumberInvalidError:
                logger.error(
                    "TELEGRAM_PHONE is invalid. Use international format "
                    "with country code, for example +9647XXXXXXXX. "
                    "+960 is Maldives; Iraq is +964."
                )
                await client.disconnect()
                return
        me = await client.get_me()
        logger.info(
            "Signed in as %s (id=%s phone=%s)",
            getattr(me, "first_name", "unknown"),
            getattr(me, "id", "?"),
            getattr(me, "phone", "?"),
        )
        self._user_client = client
        self._state.telegram_connected = True
        self._state.telegram_listener_active = True
        self._state.listening_channels = [
            str(item) for item in self._settings.allowed_channel_id_list()
        ]
        await self._subscribe_allowed_channels(client)
        logger.info("Telethon listener is running")

    async def _subscribe_allowed_channels(self, client: TelegramClient) -> None:
        for channel_id in self._settings.allowed_channel_id_list():
            try:
                entity = await client.get_entity(channel_id)
                title = getattr(entity, "title", channel_id)
                logger.info("Subscribed to channel %s (%s)", title, channel_id)
            except Exception as error:
                logger.error(
                    "Could not subscribe to channel %s: %s", channel_id, error
                )

    async def _start_bot(self) -> None:
        application = Application.builder().token(
            self._settings.telegram_bot_token or ""
        ).build()
        application.add_handler(
            MessageHandler(filters.UpdateType.CHANNEL_POST, self._on_bot_channel_post)
        )
        application.add_handler(
            MessageHandler(
                filters.UpdateType.EDITED_CHANNEL_POST, self._on_bot_channel_edit
            )
        )
        application.add_handler(
            ChatMemberHandler(self._on_bot_membership, ChatMemberHandler.MY_CHAT_MEMBER)
        )
        await application.initialize()
        await application.start()
        if application.updater is None:
            raise RuntimeError("Telegram bot updater is unavailable.")
        await application.updater.start_polling(
            allowed_updates=["channel_post", "edited_channel_post", "my_chat_member"]
        )
        self._bot_app = application
        self._state.telegram_connected = True
        self._state.telegram_listener_active = True
        self._state.listening_channels = [
            str(item) for item in self._settings.allowed_channel_id_list()
        ]
        me = await application.bot.get_me()
        logger.info(
            "Bot API listener is running as @%s. Add this bot as admin in the private channel.",
            me.username,
        )

    async def _on_bot_channel_post(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        post = update.channel_post
        if post is None or post.chat is None:
            return
        preview = " ".join((post.text or post.caption or "").split())[:120]
        logger.info(
            "Incoming Telegram chat=%s message=%s text=%s",
            post.chat.id,
            post.message_id,
            preview or "<empty>",
        )
        try:
            signal = self._processor.handle_new_message(
                post.chat.id,
                post.message_id,
                post.text or post.caption or "",
                channel_name=post.chat.title,
                message_date=_as_utc(post.date),
                is_forwarded=post.forward_origin is not None,
                reply_to_message_id=_bot_reply_id(post),
            )
            await self._notify_signal(signal)
        except Exception as error:
            await self._record_error(error)

    async def _on_bot_membership(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        chat = update.effective_chat
        if chat is None:
            return
        logger.info("Bot added/updated in chat %s id=%s", chat.title, chat.id)

    async def _on_bot_channel_edit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        post = update.edited_channel_post
        if post is None or post.chat is None:
            return
        try:
            signal = self._processor.handle_edit(
                post.chat.id,
                post.message_id,
                post.text or post.caption or "",
                message_date=_as_utc(post.edit_date or post.date),
            )
            await self._notify_signal(signal)
        except Exception as error:
            await self._record_error(error)

    async def _safe_handle_new(self, event: events.NewMessage.Event) -> None:
        try:
            message = event.message
            chat_id = int(event.chat_id)
            text = message.raw_text or ""
            preview = " ".join(text.split())[:120]
            logger.info(
                "Incoming Telegram chat=%s message=%s text=%s",
                chat_id,
                message.id,
                preview or "<empty>",
            )
            chat = event.chat
            channel_name = getattr(chat, "title", None) if chat is not None else None
            signal = self._processor.handle_new_message(
                chat_id,
                int(message.id),
                text,
                channel_name=channel_name,
                message_date=_as_utc(message.date),
                is_forwarded=bool(message.fwd_from),
                reply_to_message_id=_telethon_reply_id(message),
            )
            await self._notify_signal(signal)
        except Exception as error:
            await self._record_error(error)

    async def _safe_handle_edit(self, event: events.MessageEdited.Event) -> None:
        try:
            message = event.message
            signal = self._processor.handle_edit(
                int(event.chat_id),
                int(message.id),
                message.raw_text or "",
                message_date=_as_utc(message.date),
            )
            await self._notify_signal(signal)
        except Exception as error:
            await self._record_error(error)

    async def _safe_handle_delete(self, event: events.MessageDeleted.Event) -> None:
        try:
            chat_id = event.chat_id
            if chat_id is None:
                return
            for message_id in event.deleted_ids:
                self._processor.handle_delete(int(chat_id), int(message_id))
        except Exception as error:
            await self._record_error(error)

    async def _notify_signal(self, signal: TradeSignal | None) -> None:
        if signal is None:
            return
        rejected = {
            ValidationStatus.INVALID,
            ValidationStatus.REJECTED,
            ValidationStatus.SEMANTIC_DUPLICATE,
        }
        if signal.validation_status in rejected:
            await self._notifier.signal_rejected(signal)
            return
        await self._notifier.signal_received(signal)

    async def _record_error(self, error: BaseException) -> None:
        logger.exception("Telegram listener error")
        self._state.mark_exception(error)
        await self._notifier.unexpected_error(str(error))


def _digits_only(value: str | None) -> str:
    if not value:
        return ""
    return "".join(char for char in value if char.isdigit())


def _same_phone(configured: str | None, session_phone: str | None) -> bool:
    expected = _digits_only(configured)
    actual = _digits_only(session_phone)
    if not expected or not actual:
        return True
    return expected == actual or expected.endswith(actual) or actual.endswith(expected)


def _bot_reply_id(post: object) -> int | None:
    reply = getattr(post, "reply_to_message", None)
    if reply is None:
        return None
    message_id = getattr(reply, "message_id", None)
    if message_id is None:
        return None
    return int(message_id)


def _telethon_reply_id(message: object) -> int | None:
    reply = getattr(message, "reply_to", None)
    if reply is None:
        return None
    message_id = getattr(reply, "reply_to_msg_id", None)
    if message_id is None:
        return None
    return int(message_id)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return ensure_utc(value)
