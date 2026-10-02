# -*- mode: python ; coding: utf-8 -*-

import os
import subprocess
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

_stamp_path = Path("app/build_stamp.py")
_stamp_previous = _stamp_path.read_text(encoding="utf-8") if _stamp_path.exists() else 'GIT_SHA = ""\n'
_sha = os.environ.get("GIT_SHA", "").strip()
if not _sha:
    try:
        _sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        _sha = ""
_stamp_path.write_text(f'GIT_SHA = "{_sha}"\n', encoding="utf-8")

datas = [
    ("config/symbols.yaml", "config"),
    ("config/trading_rules.yaml", "config"),
    ("assets/auto-trader.ico", "assets"),
    (".env.example", "."),
]
binaries = []
hiddenimports = [
    "app",
    "app.build_stamp",
    "app.release_update",
    "app.updater",
    "app.gui",
    "app.gui.app",
    "app.gui.theme",
    "app.gui.feed",
    "app.gui.channels_store",
    "app.gui.prefs",
    "app.telegram.channels",
    "app.runtime_host",
    "multiprocessing",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "MetaTrader5",
    "numpy",
    "telethon",
    "telegram",
    "pydantic",
    "pydantic_settings",
    "yaml",
    "sqlalchemy",
    "sqlite3",
    "tzdata",
    "dotenv",
    "httpx",
]

for package in ("telethon", "telegram", "tzdata", "MetaTrader5", "numpy"):
    collected_datas, collected_binaries, collected_hidden = collect_all(package)
    datas += collected_datas
    binaries += collected_binaries
    hiddenimports += collected_hidden

try:
    a = Analysis(
        ["run_gui.py"],
        pathex=["."],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=["pytest", "pytest_asyncio"],
        noarchive=False,
    )
    pyz = PYZ(a.pure)

    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="TelegramMT5",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        icon="assets/auto-trader.ico",
        disable_windowed_traceback=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="TelegramMT5",
    )
finally:
    _stamp_path.write_text(_stamp_previous, encoding="utf-8")
