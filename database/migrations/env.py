"""Alembic environment. Uses the async SQLite URL from alembic.ini."""

import asyncio

from alembic import context
from sqlalchemy import event, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from database.models import Base
from database.sqlite_utils import apply_sqlite_pragmas

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    def _on_connect(dbapi_connection, _connection_record) -> None:
        if connectable.dialect.name == "sqlite":
            apply_sqlite_pragmas(dbapi_connection, busy_timeout_ms=5000, wal_autocheckpoint=1000)

    event.listen(connectable.sync_engine, "connect", _on_connect)

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_migrations_online())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
