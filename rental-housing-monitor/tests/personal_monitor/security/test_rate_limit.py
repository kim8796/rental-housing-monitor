from __future__ import annotations

import asyncio

import pytest

from personal_monitor.security.rate_limit import HostRateLimiter


class ManualTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_host_rate_limiter_waits_ten_seconds_between_starts() -> None:
    time = ManualTime()
    limiter = HostRateLimiter(clock=time.monotonic, sleeper=time.sleep)

    async def scenario() -> None:
        await limiter.acquire("Example.COM.")
        time.now += 3
        await limiter.acquire("example.com")

    asyncio.run(scenario())

    assert time.sleeps == [7.0]
    assert time.now == 10.0


def test_retry_after_extends_but_never_shortens_minimum_interval() -> None:
    time = ManualTime()
    limiter = HostRateLimiter(clock=time.monotonic, sleeper=time.sleep)

    async def scenario() -> None:
        await limiter.acquire("example.com")
        await limiter.acquire("example.com", retry_after_seconds=25)
        await limiter.acquire("example.com", retry_after_seconds=2)

    asyncio.run(scenario())

    assert time.sleeps == [25.0, 10.0]


def test_different_hosts_are_limited_independently() -> None:
    time = ManualTime()
    limiter = HostRateLimiter(clock=time.monotonic, sleeper=time.sleep)

    async def scenario() -> None:
        await limiter.acquire("a.example")
        await limiter.acquire("b.example")

    asyncio.run(scenario())

    assert time.sleeps == []


@pytest.mark.parametrize("retry_after", [-1, float("nan"), float("inf")])
def test_invalid_retry_after_is_rejected(retry_after: float) -> None:
    limiter = HostRateLimiter()

    with pytest.raises(ValueError, match="retry_after_seconds"):
        asyncio.run(limiter.acquire("example.com", retry_after_seconds=retry_after))


@pytest.mark.parametrize("host", ["", ".", "   "])
def test_empty_host_is_rejected(host: str) -> None:
    with pytest.raises(ValueError, match="host"):
        asyncio.run(HostRateLimiter().acquire(host))


def test_concurrent_same_host_acquisitions_are_serialized() -> None:
    class BlockingTime:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []
            self.releases: asyncio.Queue[None] = asyncio.Queue()

        def monotonic(self) -> float:
            return self.now

        async def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            await self.releases.get()
            self.now += seconds

    async def scenario() -> list[float]:
        time = BlockingTime()
        limiter = HostRateLimiter(clock=time.monotonic, sleeper=time.sleep)
        await limiter.acquire("example.com")

        second = asyncio.create_task(limiter.acquire("EXAMPLE.COM"))
        third = asyncio.create_task(limiter.acquire("example.com."))
        await asyncio.sleep(0)
        assert time.sleeps == [10.0]

        time.releases.put_nowait(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert time.sleeps == [10.0, 10.0]

        time.releases.put_nowait(None)
        await asyncio.gather(second, third)
        return time.sleeps

    assert asyncio.run(scenario()) == [10.0, 10.0]
