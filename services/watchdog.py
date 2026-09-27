"""Heartbeat writer. A quiet service is reported stale by age, not by guessing."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.repositories.heartbeats import HeartbeatRepository


class Watchdog:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, stale_after_seconds: int = 120) -> None:
        self._factory = session_factory
        self._stale_after = stale_after_seconds
        self._beats = HeartbeatRepository()

    async def beat(self, service: str, *, status: str = "ok", details: str | None = None) -> None:
        async with self._factory() as session:
            await self._beats.beat(session, service, status=status, details=details)

    async def stale_services(self, services: list[str], *, now: datetime | None = None) -> list[str]:
        moment = _aware(now or datetime.now(timezone.utc))
        stale: list[str] = []
        async with self._factory() as session:
            for name in services:
                row = await self._beats.get(session, name)
                if row is None or _aware(row.last_seen_at) < moment - timedelta(seconds=self._stale_after):
                    stale.append(name)
        return stale


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment
