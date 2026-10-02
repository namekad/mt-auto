from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.paths import is_frozen, user_dir
from app.release_update import (
    PendingApply,
    ReleaseAsset,
    ReleaseCheckError,
    check_release_update,
    download_release_update,
    installed_sha,
    launch_apply,
)

logger = logging.getLogger("application")

REMOTE_URL = "https://github.com/namekad/mt-auto.git"
BRANCH = "main"
POLL_SECONDS = 90
RELEASE_POLL_SECONDS = 180
FIRST_CHECK_SECONDS = 8


class UpdateAction(str, Enum):
    NONE = "none"
    PULL = "pull"
    RESTART = "restart"


@dataclass(frozen=True)
class UpdatePlan:
    action: UpdateAction
    detail: str


def plan_update(
    *,
    startup_sha: str,
    head_sha: str,
    remote_sha: str | None,
    dirty: bool,
    can_fast_forward: bool,
) -> UpdatePlan:
    if head_sha != startup_sha:
        return UpdatePlan(UpdateAction.RESTART, "local-changed")
    if remote_sha is None:
        return UpdatePlan(UpdateAction.NONE, "offline")
    if remote_sha == head_sha:
        return UpdatePlan(UpdateAction.NONE, "current")
    if dirty:
        return UpdatePlan(UpdateAction.NONE, "dirty")
    if not can_fast_forward:
        return UpdatePlan(UpdateAction.NONE, "diverged")
    return UpdatePlan(UpdateAction.PULL, "remote-ahead")


def requirements_touched(changed_paths: list[str]) -> bool:
    normalized = {path.replace("\\", "/").strip() for path in changed_paths}
    return "requirements.txt" in normalized


def relaunch_command(module: str) -> list[str]:
    if is_frozen():
        return [sys.executable]
    return [sys.executable, "-m", module]


def repo_root() -> Path | None:
    start = user_dir()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _run_git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def local_head(root: Path) -> str | None:
    result = _run_git(root, "rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def has_tracked_changes(root: Path) -> bool:
    result = _run_git(root, "status", "--porcelain", "--untracked-files=no")
    return result.returncode == 0 and bool(result.stdout.strip())


def _ensure_origin(root: Path) -> None:
    current = _run_git(root, "remote", "get-url", "origin")
    if current.returncode == 0:
        return
    _run_git(root, "remote", "add", "origin", REMOTE_URL)


def fetch_remote_sha(root: Path) -> str | None:
    _ensure_origin(root)
    fetched = _run_git(root, "fetch", "origin", BRANCH, timeout=90)
    if fetched.returncode != 0:
        return None
    parsed = _run_git(root, "rev-parse", f"origin/{BRANCH}")
    if parsed.returncode != 0:
        return None
    sha = parsed.stdout.strip()
    return sha or None


def can_fast_forward(root: Path, head_sha: str) -> bool:
    result = _run_git(root, "merge-base", "--is-ancestor", head_sha, f"origin/{BRANCH}")
    return result.returncode == 0


def pull(root: Path) -> bool:
    result = _run_git(root, "pull", "--ff-only", "origin", BRANCH, timeout=120)
    if result.returncode != 0:
        logger.error("git pull failed: %s", (result.stderr or result.stdout).strip())
        return False
    return True


def changed_files(root: Path, old_sha: str, new_sha: str) -> list[str]:
    result = _run_git(root, "diff", "--name-only", old_sha, new_sha)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def install_requirements(root: Path) -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip()[-500:]
        logger.error("pip install failed: %s", tail)
        return False
    return True


def relaunch(module: str, root: Path) -> None:
    command = relaunch_command(module)
    kwargs: dict[str, object] = {
        "cwd": str(root),
        "env": os.environ.copy(),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(command, **kwargs)


class UpdateWatcher:
    def __init__(
        self,
        on_restart: Callable[[], None],
        on_update_available: Callable[[ReleaseAsset], None] | None = None,
    ) -> None:
        self._on_restart = on_restart
        self._on_update_available = on_update_available
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._busy = False
        self._announced: set[str] = set()
        self._startup_sha: str | None = None
        self._pending_apply: PendingApply | None = None
        self._notified_sha: str | None = None

    def start(self) -> None:
        if is_frozen():
            self._thread = threading.Thread(target=self._loop, name="update-watcher", daemon=True)
            self._thread.start()
            return
        root = repo_root()
        if root is None:
            logger.info("Auto-update is off because this folder is not a git checkout.")
            return
        self._startup_sha = local_head(root)
        if self._startup_sha is None:
            return
        self._thread = threading.Thread(target=self._loop, name="update-watcher", daemon=True)
        self._thread.start()

    def set_pending_apply(self, pending: PendingApply) -> None:
        self._pending_apply = pending

    def launch_pending_apply(self) -> bool:
        pending = self._pending_apply
        if pending is None:
            return False
        launch_apply(pending, os.getpid())
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        interval = RELEASE_POLL_SECONDS if is_frozen() else POLL_SECONDS
        if self._stop.wait(FIRST_CHECK_SECONDS):
            return
        while not self._stop.is_set():
            try:
                if is_frozen():
                    self._tick_release()
                else:
                    self._tick()
            except Exception:
                logger.exception("Update check failed")
            if self._stop.wait(interval):
                return

    def _once(self, key: str, message: str) -> None:
        if key in self._announced:
            return
        self._announced.add(key)
        logger.warning(message)

    def _fire(self) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True
        self._stop.set()
        self._on_restart()

    def _tick_release(self) -> None:
        if self._busy:
            return
        try:
            asset = check_release_update(installed_sha())
        except ReleaseCheckError as error:
            self._once("release-offline", f"Could not check GitHub for a new build: {error}")
            return
        if asset is None:
            return
        # Don't notify the user twice for the same SHA within this session.
        if self._notified_sha == asset.sha:
            return
        self._notified_sha = asset.sha
        if self._on_update_available is not None:
            # Hand off to the GUI — the user will decide when to install.
            self._on_update_available(asset)
        else:
            # Headless / legacy fallback: download silently then restart.
            with self._lock:
                if self._busy:
                    return
                self._busy = True
            try:
                pending = download_release_update(asset)
            except ReleaseCheckError as error:
                logger.error("Update download failed: %s", error)
                with self._lock:
                    self._busy = False
                self._notified_sha = None  # allow retry next cycle
                return
            self._pending_apply = pending
            logger.info("New build downloaded. Restarting to install it.")
            self._fire()

    def _tick(self) -> None:
        root = repo_root()
        startup = self._startup_sha
        if root is None or startup is None or self._busy:
            return
        head = local_head(root)
        if head is None:
            return
        remote = fetch_remote_sha(root) if head == startup else None
        dirty = has_tracked_changes(root)
        fast_forward = bool(remote) and can_fast_forward(root, head)
        plan = plan_update(
            startup_sha=startup,
            head_sha=head,
            remote_sha=remote,
            dirty=dirty,
            can_fast_forward=fast_forward,
        )
        if plan.action is UpdateAction.RESTART:
            logger.info("Local git commit changed. Restarting.")
            self._fire()
            return
        if plan.action is UpdateAction.NONE:
            if plan.detail == "dirty":
                self._once("dirty", "Skipped update because local files were edited.")
            elif plan.detail == "diverged":
                self._once("diverged", "Skipped update because local history diverged from GitHub.")
            elif plan.detail == "offline":
                self._once("offline", "Could not reach GitHub for an update check.")
            return
        if plan.action is not UpdateAction.PULL:
            return
        logger.info("New commit on GitHub. Updating.")
        if not pull(root):
            return
        new_head = local_head(root) or head
        if requirements_touched(changed_files(root, head, new_head)) and not install_requirements(root):
            return
        logger.info("Update applied. Restarting.")
        self._fire()
