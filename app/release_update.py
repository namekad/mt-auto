from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.build_stamp import GIT_SHA
from app.paths import user_dir

RELEASES_URL = "https://api.github.com/repos/namekad/mt-auto/releases/latest"
ASSET_NAME = "TelegramMT5.zip"
EXE_NAME = "TelegramMT5.exe"
_APPLY_BAT = """@echo off
setlocal
set "PID=%~1"
set "SOURCE=%~2"
set "DEST=%~3"
:wait
tasklist /FI "PID eq %PID%" 2>nul | findstr /I "TelegramMT5" >nul
if not errorlevel 1 (
  ping 127.0.0.1 -n 2 >nul
  goto wait
)
robocopy "%SOURCE%" "%DEST%" TelegramMT5.exe /R:10 /W:2 /NFL /NDL /NJH /NJS
if exist "%DEST%\\_internal" rmdir /s /q "%DEST%\\_internal"
robocopy "%SOURCE%\\_internal" "%DEST%\\_internal" /E /R:5 /W:1 /NFL /NDL /NJH /NJS
start "" "%DEST%\\TelegramMT5.exe"
rmdir /s /q "%SOURCE%"
"""


class ReleaseCheckError(Exception):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    sha: str
    download_url: str


@dataclass(frozen=True)
class PendingApply:
    script: Path
    source: Path
    dest: Path


def installed_sha() -> str:
    return GIT_SHA.strip()


def release_needs_update(local_sha: str, remote_sha: str) -> bool:
    remote = remote_sha.strip()
    if not remote:
        return False
    return local_sha.strip() != remote


def release_sha_from_tag(tag_name: str) -> str:
    tag = tag_name.strip()
    prefix = "build-"
    if tag.startswith(prefix):
        return tag[len(prefix) :].strip()
    return tag


def parse_latest_release(payload: dict[str, object]) -> ReleaseAsset | None:
    sha = release_sha_from_tag(str(payload.get("tag_name") or ""))
    assets = payload.get("assets")
    if not sha or not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        if asset.get("name") != ASSET_NAME:
            continue
        url = str(asset.get("browser_download_url") or "").strip()
        if url:
            return ReleaseAsset(sha=sha, download_url=url)
    return None


def pack_dist(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            if path.name == ".env" or path.suffix == ".session":
                continue
            archive.write(path, Path("TelegramMT5") / path.relative_to(source))


def extract_package(zip_path: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            target = (dest / info.filename).resolve()
            if target != root and root not in target.parents:
                raise ReleaseCheckError("package path is unsafe")
        archive.extractall(dest)
    matches = [path for path in dest.rglob(EXE_NAME) if path.is_file()]
    if len(matches) != 1:
        raise ReleaseCheckError("package is missing TelegramMT5.exe")
    return matches[0].parent


def fetch_latest_release() -> ReleaseAsset:
    request = urllib.request.Request(
        RELEASES_URL,
        headers={"User-Agent": "mt5-auto", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise ReleaseCheckError(str(error.code)) from error
    except urllib.error.URLError as error:
        raise ReleaseCheckError(str(error.reason)) from error
    if not isinstance(payload, dict):
        raise ReleaseCheckError("release response was not an object")
    parsed = parse_latest_release(payload)
    if parsed is None:
        raise ReleaseCheckError("release has no Windows package")
    return parsed


def _download(url: str, dest: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "mt5-auto", "Accept": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    except urllib.error.URLError as error:
        raise ReleaseCheckError(str(error.reason)) from error


def prepare_release_update(local_sha: str) -> PendingApply | None:
    release = fetch_latest_release()
    if not release_needs_update(local_sha, release.sha):
        return None
    stage = Path(tempfile.gettempdir()) / "mt5-auto-update" / release.sha
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    zip_path = stage / ASSET_NAME
    _download(release.download_url, zip_path)
    package = extract_package(zip_path, stage / "pkg")
    script = stage / "apply-update.bat"
    script.write_text(_APPLY_BAT, encoding="utf-8")
    return PendingApply(script=script, source=package, dest=user_dir())


def launch_apply(pending: PendingApply, pid: int) -> None:
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        ["cmd", "/c", str(pending.script), str(pid), str(pending.source), str(pending.dest)],
        creationflags=flags,
        close_fds=True,
    )
