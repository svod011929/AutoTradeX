"""SQLite notification queue. Trading does not wait on Telegram."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import Notification, NotificationStatus
from database.retry import run_with_sqlite_lock_retry
from services.watchdog import Watchdog

Sender = Callable[[Notification], Awaitable[None]]


class NotificationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_attempts: int = 4,
        sender: Sender | None = None,
    ) -> None:
        if max_attempts < 3 or max_attempts > 5:
            raise ValueError("notification attempts must be from 3 to 5")
        self._factory = session_factory
        self._max_attempts = max_attempts
        self._sender = sender or _drop
        self._running = False

    async def enqueue(self, user_id: int, event_type: str, payload_json: str | None = None) -> int:
        async def _op() -> int:
            async with self._factory() as session:
                note = Notification(
                    user_id=user_id,
                    event_type=event_type,
                    payload_json=payload_json,
                    sent=False,
                    status=NotificationStatus.PENDING.value,
                    attempts=0,
                )
                session.add(note)
                await session.commit()
                return note.id

        return await run_with_sqlite_lock_retry(_op)

    async def deliver_pending(self) -> int:
        """Try each pending row once. Failed sends stay queued until the attempt cap."""
        async with self._factory() as session:
            pending = list(
                (
                    await session.scalars(
                        select(Notification).where(Notification.status == NotificationStatus.PENDING.value)
                    )
                ).all()
            )
            ids = [row.id for row in pending]
        delivered = 0
        for note_id in ids:
            if await self._attempt(note_id):
                delivered += 1
        return delivered

    async def run_once(self) -> int:
        return await self.deliver_pending()

    async def attempt(self, note_id: int) -> bool:
        """One delivery try. A down sender stays in the queue and does not raise."""
        return await self._attempt(note_id)

    async def serve(
        self,
        *,
        poll_seconds: float = 2.0,
        watchdog: Watchdog | None = None,
    ) -> None:
        self._running = True
        while self._running:
            await self.deliver_pending()
            if watchdog is not None:
                await watchdog.beat("notifications")
            if not self._running:
                return
            await asyncio.sleep(poll_seconds)

    async def stop(self) -> None:
        self._running = False

    async def _attempt(self, note_id: int) -> bool:
        async with self._factory() as session:
            note = await session.get(Notification, note_id)
            if note is None or note.status != NotificationStatus.PENDING.value:
                return False
            note.attempts += 1
            try:
                await self._sender(note)
            except Exception as exc:
                note.last_error = str(exc)[:300]
                if note.attempts >= self._max_attempts:
                    note.status = NotificationStatus.FAILED.value
                await session.commit()
                return False
            note.sent = True
            note.sent_at = datetime.now(timezone.utc)
            note.status = NotificationStatus.SENT.value
            note.last_error = None
            await session.commit()
            return True


async def _drop(note: Notification) -> None:
    del note
