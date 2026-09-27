"""Client-side cap on REST calls.

The exchange does not publish a numeric RPS. ``max_per_second`` is our ceiling.
"""

import time
from collections.abc import Awaitable, Callable


class ClientRateLimiter:
    def __init__(
        self,
        max_per_second: float,
        sleep: Callable[[float], Awaitable[None]],
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_per_second <= 0:
            raise ValueError("max_per_second must be > 0")
        self._min_interval = 1.0 / max_per_second
        self._sleep = sleep
        self._clock = clock or time.monotonic
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        now = self._clock()
        wait = self._next_allowed - now
        if wait > 0:
            await self._sleep(wait)
            now = self._clock()
        self._next_allowed = now + self._min_interval
