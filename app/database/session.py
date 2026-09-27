from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import PROJECT_ROOT, Settings
from app.database.models import Base


def _normalize_sqlite_url(url: str) -> str:
    prefix = "sqlite:///"
    if url.startswith(prefix) and ":memory:" not in url:
        raw = url[len(prefix) :]
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{path.as_posix()}"
    return url


def _sqlite_on_connect(dbapi_connection: Any, _record: object) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=2000")
    cursor.close()


def create_db_engine(settings: Settings) -> Engine:
    url = _normalize_sqlite_url(settings.database_url)
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 2
    engine = create_engine(url, future=True, connect_args=connect_args)
    if url.startswith("sqlite") and ":memory:" not in url:
        event.listen(engine, "connect", _sqlite_on_connect)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def database_available(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
