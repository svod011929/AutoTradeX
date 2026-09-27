"""Async SQLAlchemy engine and session factory."""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import Settings
from database.sqlite_utils import register_sqlite_pragmas, sqlite_file_from_url

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_db(settings: Settings) -> None:
    """Create the engine and session factory. Does not drop or create tables."""
    global _engine, _sessionmaker
    if _engine is not None:
        raise RuntimeError("Database is already initialized; call close_db() first")

    database_path = sqlite_file_from_url(settings.database_url)
    if database_path is not None:
        database_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(settings.database_url, echo=False)
    register_sqlite_pragmas(engine.sync_engine, settings)
    _engine = engine
    _sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database is not initialized")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Database is not initialized")
    return _sessionmaker


async def close_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
