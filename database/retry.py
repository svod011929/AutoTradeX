"""Bounded retry when SQLite reports that the database is locked."""

import asyncio
import time
from collections.abc import Awaitable, Callable

LOCK_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0, 2.0)


def is_database_locked(exc: BaseException) -> bool:
    return "database is locked" in str(exc).lower()


async def run_with_sqlite_lock_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Run operation. On 'database is locked', wait 100ms, 250ms, 500ms, 1s, 2s, then raise."""
    pause = sleep or asyncio.sleep
    attempt = 0
    while True:
        try:
            return await operation()
        except Exception as exc:
            if not is_database_locked(exc) or attempt >= len(LOCK_RETRY_DELAYS_SECONDS):
                raise
            await pause(LOCK_RETRY_DELAYS_SECONDS[attempt])
            attempt += 1


def run_with_sqlite_lock_retry_sync[T](
    operation: Callable[[], T],
    *,
    sleep: Callable[[float], None] | None = None,
) -> T:
    pause = sleep or time.sleep
    attempt = 0
    while True:
        try:
            return operation()
        except Exception as exc:
            if not is_database_locked(exc) or attempt >= len(LOCK_RETRY_DELAYS_SECONDS):
                raise
            pause(LOCK_RETRY_DELAYS_SECONDS[attempt])
            attempt += 1
