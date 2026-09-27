from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.paths import user_dir


@dataclass
class ChannelRow:
    channel_id: int
    name: str
    authorized: bool


def catalog_path() -> Path:
    return user_dir() / "data" / "channels.json"


def authorized_csv(rows: list[ChannelRow]) -> str:
    ids = [str(row.channel_id) for row in rows if row.authorized]
    return ",".join(ids)


def parse_channel_ids(raw: str) -> list[int]:
    if not raw.strip():
        return []
    values: list[int] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        values.append(int(item))
    return values


def load_catalog(env_ids: list[int], path: Path | None = None) -> list[ChannelRow]:
    target = path or catalog_path()
    stored: list[ChannelRow] = []
    if target.exists():
        payload = json.loads(target.read_text(encoding="utf-8"))
        items = payload.get("channels", []) if isinstance(payload, dict) else []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    channel_id = int(item["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                stored.append(
                    ChannelRow(
                        channel_id=channel_id,
                        name=str(item.get("name") or ""),
                        authorized=bool(item.get("authorized")),
                    )
                )
    by_id = {row.channel_id: row for row in stored}
    for channel_id in env_ids:
        if channel_id not in by_id:
            by_id[channel_id] = ChannelRow(channel_id=channel_id, name="", authorized=True)
    return _sorted(list(by_id.values()))


def save_catalog(rows: list[ChannelRow], path: Path | None = None) -> None:
    target = path or catalog_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "channels": [
            {"id": row.channel_id, "name": row.name, "authorized": row.authorized}
            for row in _sorted(rows)
        ]
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def merge_discovered(rows: list[ChannelRow], found: list[tuple[int, str]]) -> list[ChannelRow]:
    by_id = {row.channel_id: row for row in rows}
    for channel_id, name in found:
        current = by_id.get(channel_id)
        if current is None:
            by_id[channel_id] = ChannelRow(channel_id=channel_id, name=name, authorized=False)
            continue
        if name:
            current.name = name
    return _sorted(list(by_id.values()))


def _sorted(rows: list[ChannelRow]) -> list[ChannelRow]:
    return sorted(rows, key=lambda row: (not row.authorized, row.name.lower(), row.channel_id))
