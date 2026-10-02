from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def app_icon_path() -> Path:
    return resource_dir() / "assets" / "auto-trader.ico"


def user_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def ensure_runtime_files() -> Path:
    root = user_dir()
    bundled = resource_dir()
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    for name in ("symbols.yaml", "trading_rules.yaml"):
        destination = root / "config" / name
        source = bundled / "config" / name
        if not destination.exists() and source.exists():
            destination.write_bytes(source.read_bytes())
    example_source = bundled / ".env.example"
    example_dest = root / ".env.example"
    if example_source.exists() and not example_dest.exists():
        example_dest.write_bytes(example_source.read_bytes())
    env_dest = root / ".env"
    if not env_dest.exists():
        for candidate in (bundled / ".env", example_source):
            if candidate.exists():
                env_dest.write_bytes(candidate.read_bytes())
                break
    return root
