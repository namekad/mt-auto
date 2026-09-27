from __future__ import annotations

import json
from pathlib import Path

from app.paths import user_dir

TRADES_TABS = ("live", "cancelled", "closed")


def prefs_path() -> Path:
    return user_dir() / "data" / "ui_prefs.json"


def load_trades_tab(path: Path | None = None) -> str:
    target = path or prefs_path()
    if not target.exists():
        return "live"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "live"
    if not isinstance(payload, dict):
        return "live"
    tab = str(payload.get("trades_tab") or "live")
    if tab not in TRADES_TABS:
        return "live"
    return tab


def save_trades_tab(tab: str, path: Path | None = None) -> None:
    if tab not in TRADES_TABS:
        raise ValueError(f"Unhandled trades tab: {tab}")
    target = path or prefs_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"trades_tab": tab}, indent=2) + "\n", encoding="utf-8")
