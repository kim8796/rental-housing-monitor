from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable


class HostRateLimiter:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        minimum_interval_seconds: float = 10.0,
    ) -> None:
        if not math.isfinite(minimum_interval_seconds) or minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds must be finite and non-negative")
        self._clock = clock
        self._sleeper = sleeper
        self._minimum_interval = float(minimum_interval_seconds)
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_started_at: dict[str, float] = {}

    async def acquire(self, host: str, retry_after_seconds: float | None = None) -> None:
        normalized_host = _normalize_host(host)
        retry_after = _normalize_retry_after(retry_after_seconds)
        interval = max(self._minimum_interval, retry_after)
        lock = self._locks.setdefault(normalized_host, asyncio.Lock())
        async with lock:
            now = self._clock()
            previous = self._last_started_at.get(normalized_host)
            if previous is not None:
                wait_seconds = previous + interval - now
                if wait_seconds > 0:
                    await self._sleeper(wait_seconds)
            self._last_started_at[normalized_host] = self._clock()


def _normalize_host(host: str) -> str:
    if not isinstance(host, str):
        raise ValueError("host must be non-empty")
    normalized = host.strip().rstrip(".").casefold()
    if not normalized:
        raise ValueError("host must be non-empty")
    return normalized


def _normalize_retry_after(value: float | None) -> float:
    if value is None:
        return 0.0
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise ValueError("retry_after_seconds must be finite and non-negative") from None
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("retry_after_seconds must be finite and non-negative")
    return normalized
