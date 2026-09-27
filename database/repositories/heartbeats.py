"""Read and write service heartbeats."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Heartbeat


class HeartbeatRepository:
    async def beat(
        self,
        session: AsyncSession,
        service: str,
        *,
        status: str = "ok",
        details: str | None = None,
        seen_at: datetime | None = None,
    ) -> Heartbeat:
        moment = seen_at or datetime.now(timezone.utc)
        existing = await session.scalar(select(Heartbeat).where(Heartbeat.service == service))
        if existing is None:
            existing = Heartbeat(service=service, status=status, details=details, last_seen_at=moment)
            session.add(existing)
        else:
            existing.status = status
            existing.details = details
            existing.last_seen_at = moment
        await session.commit()
        return existing

    async def get(self, session: AsyncSession, service: str) -> Heartbeat | None:
        return await session.scalar(select(Heartbeat).where(Heartbeat.service == service))
