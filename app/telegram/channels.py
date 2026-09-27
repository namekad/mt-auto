from __future__ import annotations

from telethon import TelegramClient
from telethon.tl.types import Channel

from app.config import PROJECT_ROOT, Settings, TelegramListenerMode


async def collect_channels(client: TelegramClient) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    seen: set[int] = set()
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, Channel):
            continue
        if not (entity.broadcast or entity.megagroup):
            continue
        channel_id = int(dialog.id)
        if channel_id in seen:
            continue
        seen.add(channel_id)
        found.append((channel_id, dialog.name or str(channel_id)))
    found.sort(key=lambda item: item[1].lower())
    return found


async def fetch_joined_channels(settings: Settings) -> list[tuple[int, str]]:
    if settings.telegram_listener_mode is not TelegramListenerMode.USER:
        if settings.telegram_listener_mode is TelegramListenerMode.BOT:
            raise RuntimeError("Load channels after you sign in with My Telegram account.")
        never: TelegramListenerMode = settings.telegram_listener_mode
        raise ValueError(f"Unhandled listener mode: {never}")
    if not settings.has_telegram_user_credentials():
        raise RuntimeError("Add the API ID and API hash in Setup first.")
    session_path = (PROJECT_ROOT / settings.telegram_session_path).resolve()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(session_path),
        settings.telegram_api_id or 0,
        settings.telegram_api_hash or "",
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Sign in first. Press Start and enter the Telegram code.")
        return await collect_channels(client)
    finally:
        await client.disconnect()
