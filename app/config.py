from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.exceptions import LiveModeDisabledError
from app.paths import user_dir

PROJECT_ROOT = user_dir()


class AppMode(str, Enum):
    DEMO = "DEMO"
    BLOCKED = "BLOCKED"


class ExecutionMode(str, Enum):
    OBSERVE = "OBSERVE"
    APPROVAL = "APPROVAL"
    AUTO_DEMO = "AUTO_DEMO"


class TelegramListenerMode(str, Enum):
    BOT = "BOT"
    USER = "USER"


class TakeProfitStrategy(str, Enum):
    FIRST_TP = "FIRST_TP"
    LAST_TP = "LAST_TP"
    SELECTED_TP = "SELECTED_TP"


class TradingRules(BaseModel):
    require_stop_loss: bool = True
    require_take_profit: bool = True
    minimum_take_profits: int = 2
    maximum_signal_age_seconds: int = 120
    default_volume: float = 0.01
    lot_size: float = 0.01
    positions_per_setup: int = 2
    break_even_distance: float = 4.0
    maximum_volume: float = 0.05
    maximum_open_positions: int = 3
    allow_market_orders: bool = True
    allow_pending_orders: bool = True
    allowed_symbols: list[str] = Field(default_factory=lambda: ["XAUUSD", "EURUSD"])
    allowed_directions: list[str] = Field(default_factory=lambda: ["BUY", "SELL"])
    duplicate_window_hours: int = 24
    take_profit_strategy: TakeProfitStrategy = TakeProfitStrategy.FIRST_TP
    selected_take_profit_index: int = 1


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_api_id: int | None = None
    telegram_api_hash: str | None = None
    telegram_phone: str | None = None
    telegram_bot_token: str | None = None
    telegram_listener_mode: TelegramListenerMode = TelegramListenerMode.USER
    control_bot_token: str | None = None
    control_chat_id: int | None = None
    authorized_control_user_ids: str = ""
    allowed_channel_ids: str = ""

    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None
    mt5_path: str | None = None

    app_mode: str = "DEMO"
    execution_mode: ExecutionMode = ExecutionMode.OBSERVE
    dry_run: bool = True
    local_timezone: str = "Asia/Baghdad"
    database_url: str = "sqlite:///data/automation.db"
    telegram_session_path: str = "data/telegram.session"
    symbols_path: Path = PROJECT_ROOT / "config" / "symbols.yaml"
    trading_rules_path: Path = PROJECT_ROOT / "config" / "trading_rules.yaml"

    @field_validator(
        "telegram_api_id",
        "control_chat_id",
        "mt5_login",
        mode="before",
    )
    @classmethod
    def _empty_int_as_none(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "telegram_api_hash",
        "telegram_bot_token",
        "control_bot_token",
        "mt5_password",
        "mt5_server",
        mode="before",
    )
    @classmethod
    def _empty_str_as_none(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        cleaned = value.strip().strip('"').strip("'").strip()
        return cleaned or None

    @field_validator("telegram_phone", mode="before")
    @classmethod
    def _normalize_phone(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        cleaned = value.strip().strip('"').strip("'")
        cleaned = cleaned.replace(" ", "").replace("-", "")
        return cleaned or None

    @field_validator("mt5_path", mode="before")
    @classmethod
    def _normalize_mt5_path(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        cleaned = value.strip().strip('"').strip("'")
        cleaned = cleaned.replace("\terminal64.exe", r"\terminal64.exe")
        return cleaned.replace("/", "\\") or None

    @field_validator("telegram_listener_mode", mode="before")
    @classmethod
    def _normalize_listener_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("execution_mode", mode="before")
    @classmethod
    def _normalize_execution_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = value.strip().upper()
            allowed = {item.value for item in ExecutionMode}
            if cleaned not in allowed:
                return ExecutionMode.OBSERVE
            return cleaned
        return value

    @property
    def resolved_app_mode(self) -> AppMode:
        if self.app_mode.strip().upper() == AppMode.DEMO.value:
            return AppMode.DEMO
        return AppMode.BLOCKED

    @property
    def live_trading_enabled(self) -> bool:
        return False

    def assert_not_live(self) -> None:
        if self.app_mode.strip().upper() == "LIVE" or self.live_trading_enabled:
            raise LiveModeDisabledError(
                "LIVE trading is disabled. Only DEMO is accepted."
            )

    def allowed_channel_id_list(self) -> list[int]:
        return _parse_int_list(self.allowed_channel_ids)

    def authorized_control_user_id_list(self) -> list[int]:
        return _parse_int_list(self.authorized_control_user_ids)

    def has_telegram_user_credentials(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash)

    def has_telegram_bot_credentials(self) -> bool:
        return bool(self.telegram_bot_token)

    def has_control_bot_credentials(self) -> bool:
        return bool(self.control_bot_token and self.control_chat_id)

    def has_mt5_credentials(self) -> bool:
        return bool(self.mt5_login and self.mt5_password and self.mt5_server)

    def sqlite_path(self) -> Path | None:
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix) and ":memory:" not in self.database_url:
            raw = self.database_url[len(prefix) :]
            path = Path(raw)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            return path
        return None


def _parse_int_list(raw: str) -> list[int]:
    if not raw or not raw.strip():
        return []
    values: list[int] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        values.append(int(item))
    return values


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return loaded


def load_symbol_aliases(path: Path | None = None) -> dict[str, str]:
    source = path or (PROJECT_ROOT / "config" / "symbols.yaml")
    raw = load_yaml(source)
    aliases: dict[str, str] = {}
    for key, value in raw.items():
        aliases[str(key).strip().upper()] = str(value).strip().upper()
    return aliases


def load_trading_rules(path: Path | None = None) -> TradingRules:
    source = path or (PROJECT_ROOT / "config" / "trading_rules.yaml")
    raw = load_yaml(source)
    return TradingRules.model_validate(raw)


def load_settings() -> Settings:
    return Settings()
