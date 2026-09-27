"""Shared fixtures for database tests."""

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from core.config import Settings
from database.models import Base
from database.session import close_db, get_engine, init_db


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    database_path = tmp_path / "data" / "autotrade.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "database_url": f"sqlite+aiosqlite:///{database_path}",
        "encryption_key": Fernet.generate_key().decode("ascii"),
        "sqlite_busy_timeout": 5000,
        "sqlite_wal_autocheckpoint": 1000,
        "backup_retention": 7,
        # The synthetic entry series is tighter than fees plus slippage.
        # Order-flow tests keep the cost gate off. Settings() itself stays at 1.5.
        "min_net_reward_risk": Decimal("0"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
async def settings(tmp_path: Path) -> AsyncIterator[Settings]:
    configured = make_settings(tmp_path)
    init_db(configured)
    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield configured
    finally:
        await close_db()
