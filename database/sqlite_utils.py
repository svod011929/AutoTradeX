"""SQLite URL parsing and connection PRAGMAs from settings."""

from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy import event

from core.config import Settings


def sqlite_file_from_url(url: str) -> Path | None:
    """Return the database file for a sqlite SQLAlchemy URL, or None for memory."""
    markers = ("sqlite+aiosqlite:///", "sqlite:///")
    for marker in markers:
        if url.startswith(marker):
            raw = url[len(marker) :]
            if raw in {"", ":memory:"} or raw.startswith(":memory:"):
                return None
            return Path(raw)
    return None


def apply_sqlite_pragmas(dbapi_connection: object, *, busy_timeout_ms: int, wal_autocheckpoint: int) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.fetchone()
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.execute(f"PRAGMA wal_autocheckpoint={int(wal_autocheckpoint)}")
    finally:
        cursor.close()


def register_sqlite_pragmas(sync_engine: Engine, settings: Settings) -> None:
    if sync_engine.dialect.name != "sqlite":
        return

    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_connection: object, _connection_record: object) -> None:
        apply_sqlite_pragmas(
            dbapi_connection,
            busy_timeout_ms=settings.sqlite_busy_timeout,
            wal_autocheckpoint=settings.sqlite_wal_autocheckpoint,
        )
