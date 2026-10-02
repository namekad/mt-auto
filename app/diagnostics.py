from __future__ import annotations

from app.config import Settings
from app.utils.health import RuntimeState


def _flag(ok: bool, true_text: str = "OK", false_text: str = "NOT CONFIGURED") -> str:
    return true_text if ok else false_text


def render_banner(settings: Settings, state: RuntimeState) -> str:
    channels = state.listening_channels or [
        str(item) for item in settings.allowed_channel_id_list()
    ]
    channel_lines = "\n".join(f"Channel ID: {item}" for item in channels) or "Channel ID: none"
    telegram_creds = (
        settings.has_telegram_user_credentials()
        if settings.telegram_listener_mode.value == "USER"
        else settings.has_telegram_bot_credentials()
    )
    return "\n".join(
        [
            "========================================",
            "AUTO-TRADER",
            "=========================",
            "",
            f"Mode: {settings.resolved_app_mode.value}",
            f"Execution: {settings.execution_mode.value}",
            f"Dry Run: {str(settings.dry_run).upper()}",
            "",
            f"Database ............. {_flag(state.database_ok, 'OK', 'FAIL')}",
            f"Telegram credentials . {_flag(telegram_creds)}",
            f"Telegram connection .. {_flag(state.telegram_connected, 'OK', 'NOT CONNECTED')}",
            f"Control bot .......... {_flag(state.control_bot_active, 'OK', 'NOT CONFIGURED')}",
            f"MT5 installation ..... {state.mt5_installation}",
            f"MT5 connection ....... {_flag(state.mt5_connected, 'OK', 'NOT CONNECTED')}",
            f"MT5 account .......... {_flag(state.mt5_account_verified, 'VERIFIED', 'NOT VERIFIED')}",
            f"Symbol mapping ....... {_flag(state.symbol_mapping_ok, 'OK', 'FAIL')}",
            "",
            "Listening to:",
            "",
            channel_lines,
            "",
            "========================================",
            "SYSTEM READY" if state.database_ok else "SYSTEM NOT READY",
            "============",
        ]
    )


def render_account_banner(state: RuntimeState) -> str:
    return "\n".join(
        [
            "MT5 ACCOUNT",
            "",
            f"Login: {state.mt5_login if state.mt5_login is not None else 'n/a'}",
            f"Server: {state.mt5_server or 'n/a'}",
            f"Account type: {state.mt5_account_type or 'n/a'}",
            f"Balance: {state.mt5_balance if state.mt5_balance is not None else 'n/a'}",
            "Mode: DEMO connectivity only (no order submission)",
        ]
    )
