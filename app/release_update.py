from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.build_stamp import GIT_SHA
from app.paths import user_dir

logger = logging.getLogger("application")

RELEASES_URL = "https://api.github.com/repos/namekad/mt-auto/releases/latest"
ASSET_NAME = "TelegramMT5.zip"
EXE_NAME = "TelegramMT5.exe"
_APPLY_PS1 = r"""
param(
    [string]$AppPid,
    [string]$Source,
    [string]$Dest
)

# Wait until the old process is fully gone
$intPid = [int]$AppPid
while ($null -ne (Get-Process -Id $intPid -ErrorAction SilentlyContinue)) {
    Start-Sleep -Milliseconds 400
}

# Give Windows 2 seconds to release any remaining file-mapping locks
Start-Sleep -Seconds 2

# Replace the executable
$exeDest = Join-Path $Dest "TelegramMT5.exe"
$exeSrc  = Join-Path $Source "TelegramMT5.exe"
Copy-Item -Path $exeSrc -Destination $exeDest -Force -ErrorAction Stop

# Sync the _internal bundle
$srcInt = Join-Path $Source "_internal"
$dstInt = Join-Path $Dest "_internal"
if (Test-Path $dstInt) {
    Remove-Item $dstInt -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path $srcInt) {
    Copy-Item -Path $srcInt -Destination $dstInt -Recurse -Force -ErrorAction Stop
}

# Relaunch the updated app from its own directory
Start-Process -FilePath $exeDest -WorkingDirectory $Dest

# Clean up staging area
Start-Sleep -Seconds 3
$stageRoot = Split-Path $Source -Parent
if ($stageRoot -and (Test-Path $stageRoot)) {
    Remove-Item $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
}
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


APP_VERSION = "0.0.3"


def installed_sha() -> str:
    return GIT_SHA.strip()


def version_label() -> str:
    return f"[{APP_VERSION}]"


def build_label(sha: str) -> str:
    cleaned = sha.strip()
    if not cleaned:
        return "Build dev"
    return f"Build {cleaned[:7]}"


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


def _download(
    url: str,
    dest: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "mt5-auto", "Accept": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if on_progress is not None:
                    on_progress(downloaded, total)
    except urllib.error.URLError as error:
        raise ReleaseCheckError(str(error.reason)) from error


def check_release_update(local_sha: str) -> ReleaseAsset | None:
    """Check GitHub for a newer release.  Returns the asset if an update is needed, else None."""
    release = fetch_latest_release()
    logger.info(
        "Installed %s (%s), latest release %s",
        version_label(),
        local_sha.strip() or "dev",
        release.sha,
    )
    if not release_needs_update(local_sha, release.sha):
        return None
    return release


def download_release_update(
    asset: ReleaseAsset,
    on_progress: Callable[[int, int], None] | None = None,
) -> PendingApply:
    """Download and extract a release asset, returning a PendingApply ready to launch."""
    stage = Path(tempfile.gettempdir()) / "mt5-auto-update" / asset.sha
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    zip_path = stage / ASSET_NAME
    _download(asset.download_url, zip_path, on_progress=on_progress)
    package = extract_package(zip_path, stage / "pkg")
    script = stage / "apply-update.ps1"
    script.write_text(_APPLY_PS1, encoding="utf-8")
    return PendingApply(script=script, source=package, dest=user_dir())


def prepare_release_update(local_sha: str) -> PendingApply | None:
    """Legacy: check and download in one call. Prefer check_release_update + download_release_update."""
    asset = check_release_update(local_sha)
    if asset is None:
        return None
    return download_release_update(asset)


def launch_apply(pending: PendingApply, pid: int) -> None:
    command = [
        "powershell",
        "-ExecutionPolicy", "Bypass",
        "-NonInteractive",
        "-WindowStyle", "Hidden",
        "-File", str(pending.script),
        "-AppPid", str(pid),
        "-Source", str(pending.source),
        "-Dest", str(pending.dest),
    ]
    if os.name != "nt":
        subprocess.Popen(command, close_fds=True)
        return
    detached = (
        subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.CREATE_BREAKAWAY_FROM_JOB
    )
    try:
        subprocess.Popen(command, creationflags=detached, close_fds=True)
    except OSError:
        subprocess.Popen(
            command,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
