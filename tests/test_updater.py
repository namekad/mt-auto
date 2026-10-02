from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.release_update import (
    build_label,
    version_label,
    extract_package,
    pack_dist,
    parse_latest_release,
    release_needs_update,
    release_sha_from_tag,
)
from app.updater import UpdateAction, plan_update, relaunch_command, requirements_touched


def test_plan_stays_put_when_shas_match() -> None:
    plan = plan_update(
        startup_sha="aaa",
        head_sha="aaa",
        remote_sha="aaa",
        dirty=False,
        can_fast_forward=True,
    )
    assert plan.action is UpdateAction.NONE
    assert plan.detail == "current"


def test_plan_restarts_when_local_commit_moves() -> None:
    plan = plan_update(
        startup_sha="aaa",
        head_sha="bbb",
        remote_sha="bbb",
        dirty=False,
        can_fast_forward=True,
    )
    assert plan.action is UpdateAction.RESTART


def test_plan_pulls_when_github_is_ahead() -> None:
    plan = plan_update(
        startup_sha="aaa",
        head_sha="aaa",
        remote_sha="ccc",
        dirty=False,
        can_fast_forward=True,
    )
    assert plan.action is UpdateAction.PULL
    assert plan.detail == "remote-ahead"


def test_plan_skips_dirty_or_diverged_history() -> None:
    dirty = plan_update(
        startup_sha="aaa",
        head_sha="aaa",
        remote_sha="ccc",
        dirty=True,
        can_fast_forward=True,
    )
    diverged = plan_update(
        startup_sha="aaa",
        head_sha="aaa",
        remote_sha="ccc",
        dirty=False,
        can_fast_forward=False,
    )
    assert dirty.detail == "dirty"
    assert diverged.detail == "diverged"
    assert dirty.action is UpdateAction.NONE
    assert diverged.action is UpdateAction.NONE


def test_requirements_touch_detects_only_the_requirements_file() -> None:
    assert requirements_touched(["app/gui/app.py", "requirements.txt"])
    assert not requirements_touched(["app/updater.py"])


def test_relaunch_uses_the_module_when_not_frozen() -> None:
    command = relaunch_command("app.gui")
    assert command[-2:] == ["-m", "app.gui"]


def test_release_needs_update_when_build_sha_differs() -> None:
    assert release_needs_update("aaa", "bbb")
    assert not release_needs_update("aaa", "aaa")
    assert release_needs_update("", "bbb")
    assert not release_needs_update("aaa", "")


def test_version_label_is_the_app_version() -> None:
    assert version_label() == "[0.0.2]"


def test_build_label_uses_a_short_sha() -> None:
    assert build_label("") == "Build dev"
    assert build_label("cfd8ed0fadca0346") == "Build cfd8ed0"


def test_release_sha_from_tag_strips_build_prefix() -> None:
    assert release_sha_from_tag("build-abc123") == "abc123"
    assert release_sha_from_tag("abc123") == "abc123"


def test_parse_latest_release_reads_windows_zip() -> None:
    parsed = parse_latest_release(
        {
            "tag_name": "build-abc123",
            "assets": [
                {"name": "notes.txt", "browser_download_url": "https://example.test/notes"},
                {"name": "TelegramMT5.zip", "browser_download_url": "https://example.test/app.zip"},
            ],
        }
    )
    assert parsed is not None
    assert parsed.sha == "abc123"
    assert parsed.download_url == "https://example.test/app.zip"


def test_pack_roundtrip_keeps_exe_and_drops_secrets(tmp_path: Path) -> None:
    source = tmp_path / "TelegramMT5"
    internal = source / "_internal"
    internal.mkdir(parents=True)
    (source / "TelegramMT5.exe").write_bytes(b"exe")
    (source / ".env").write_text("MT5_PASSWORD=secret", encoding="utf-8")
    (source / "data").mkdir()
    (source / "data" / "telegram.session").write_bytes(b"session")
    (internal / "app.py").write_text("print(1)", encoding="utf-8")
    archive = tmp_path / "TelegramMT5.zip"
    pack_dist(source, archive)
    names = zipfile.ZipFile(archive).namelist()
    assert "TelegramMT5/.env" not in names
    assert "TelegramMT5/data/telegram.session" not in names
    extracted = extract_package(archive, tmp_path / "out")
    assert extracted.name == "TelegramMT5"
    assert (extracted / "TelegramMT5.exe").read_bytes() == b"exe"
    assert (extracted / "_internal" / "app.py").read_text(encoding="utf-8") == "print(1)"


def test_extract_package_rejects_parent_paths(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../evil.txt", "nope")
    with pytest.raises(Exception, match="unsafe"):
        extract_package(archive_path, tmp_path / "out")
