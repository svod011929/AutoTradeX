"""PRAGMA integrity_check helper."""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession


@dataclass(frozen=True)
class IntegrityReport:
    ok: bool
    messages: tuple[str, ...]


async def check_integrity(connection: AsyncSession | AsyncConnection) -> IntegrityReport:
    result = await connection.execute(text("PRAGMA integrity_check"))
    messages = tuple(str(row[0]) for row in result)
    return IntegrityReport(ok=messages == ("ok",), messages=messages)
