from __future__ import annotations

from pathlib import Path

from app.gui.channels_store import (
    ChannelRow,
    authorized_csv,
    load_catalog,
    merge_discovered,
    save_catalog,
)
from app.gui.prefs import load_trades_tab, save_trades_tab


def test_env_ids_join_catalog_as_authorized(tmp_path: Path) -> None:
    path = tmp_path / "channels.json"
    save_catalog(
        [ChannelRow(channel_id=-1002, name="Other", authorized=False)],
        path,
    )
    rows = load_catalog([-1001, -1002], path)
    by_id = {row.channel_id: row for row in rows}
    assert by_id[-1001].authorized is True
    assert by_id[-1002].authorized is False
    assert by_id[-1002].name == "Other"
    assert authorized_csv(rows) == "-1001"


def test_discovered_channels_stay_unauthorized_until_checked(tmp_path: Path) -> None:
    path = tmp_path / "channels.json"
    rows = merge_discovered(
        [ChannelRow(channel_id=-1001, name="", authorized=True)],
        [(-1001, "VIP"), (-1003, "News")],
    )
    save_catalog(rows, path)
    loaded = load_catalog([], path)
    by_id = {row.channel_id: row for row in loaded}
    assert by_id[-1001].name == "VIP"
    assert by_id[-1001].authorized is True
    assert by_id[-1003].authorized is False


def test_trades_tab_is_remembered(tmp_path: Path) -> None:
    path = tmp_path / "ui_prefs.json"
    assert load_trades_tab(path) == "live"
    save_trades_tab("cancelled", path)
    assert load_trades_tab(path) == "cancelled"
    path.write_text("{", encoding="utf-8")
    assert load_trades_tab(path) == "live"
