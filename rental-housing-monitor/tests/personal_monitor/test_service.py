from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from personal_monitor.service import (
    NullDeliverySender,
    PersonalMonitorService,
    TelegramDeliverySender,
    next_maintenance_run,
)
from personal_monitor.storage.runtime import MonitorLease
from personal_monitor.telegram.types import TelegramUpdate


class FakeTelegram:
    def __init__(self) -> None:
        self.concurrent = 0
        self.max_concurrent = 0
        self.offsets: list[int] = []
        self.closed = 0
        self.sent: list[tuple[object, str]] = []

    async def get_updates(self, *, offset: int, timeout: int = 30):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.offsets.append(offset)
        try:
            if len(self.offsets) == 1:
                return [TelegramUpdate(7, None, None)]
            return []
        finally:
            self.concurrent -= 1

    async def send_message(self, chat_id, text, **_kwargs):
        self.sent.append((chat_id, text))
        return "remote-1"

    async def aclose(self) -> None:
        self.closed += 1


class FakeGateway:
    def __init__(self) -> None:
        self.ids: list[int] = []

    async def handle_update(self, update: TelegramUpdate, *, now=None) -> None:
        self.ids.append(update.update_id)


class FakeScheduler:
    def __init__(self) -> None:
        self.calls = 0

    def tick(self, now: datetime):
        self.calls += 1
        return [MonitorLease("monitor", self.calls)] if self.calls == 1 else []


class FakeRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, _lease: MonitorLease) -> None:
        self.calls += 1


class FakePeriodic:
    def __init__(self) -> None:
        self.calls = 0

    async def drain_once(self, *, now: datetime) -> int:
        self.calls += 1
        return 0

    def run(self, *, now: datetime) -> None:
        self.calls += 1


class StopHeartbeat:
    def __init__(self) -> None:
        self.service: PersonalMonitorService | None = None
        self.calls = 0

    async def beat(self, *, now: datetime) -> None:
        self.calls += 1
        assert self.service is not None
        self.service.request_stop()


class CloseTracker:
    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


async def immediate_sleep(_delay: float) -> None:
    await asyncio.sleep(0)


def test_service_runs_exactly_one_sequential_poller_and_periodic_components() -> None:
    telegram = FakeTelegram()
    gateway = FakeGateway()
    scheduler = FakeScheduler()
    runner = FakeRunner()
    outbox = FakePeriodic()
    maintenance = FakePeriodic()
    heartbeat = StopHeartbeat()
    resources = [telegram]
    service = PersonalMonitorService(
        telegram_api=telegram,
        telegram_gateway=gateway,
        scheduler=scheduler,
        runner=runner,
        outbox=outbox,
        maintenance=maintenance,
        heartbeat=heartbeat,
        resources=resources,
        clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=immediate_sleep,
    )
    heartbeat.service = service

    asyncio.run(service.run())

    assert telegram.max_concurrent == 1
    assert telegram.offsets[0] == 0
    assert gateway.ids == [7]
    assert service.telegram_offset == 8
    assert scheduler.calls >= 1
    assert runner.calls == 1
    assert outbox.calls >= 1
    assert maintenance.calls == 0
    assert heartbeat.calls == 1
    assert telegram.closed == 1


def test_null_delivery_never_calls_remote_and_returns_stable_nonempty_id() -> None:
    sender = NullDeliverySender()

    first = asyncio.run(sender.send("private-address", {"text": "private message"}))
    second = asyncio.run(sender.send("other", {"text": "different"}))

    assert first == second
    assert first


def test_telegram_delivery_uses_configured_address_not_untrusted_outbox_address() -> None:
    telegram = FakeTelegram()
    sender = TelegramDeliverySender(telegram, delivery_chat_id=123)

    message_id = asyncio.run(sender.send("attacker-address", {"text": "hello"}))

    assert message_id == "remote-1"
    assert telegram.sent == [(123, "hello")]


def test_cancellation_propagates_only_after_resources_close() -> None:
    started = asyncio.Event()
    tracker = CloseTracker()

    class WaitingHeartbeat:
        async def beat(self, *, now: datetime) -> None:
            started.set()
            await asyncio.Event().wait()

    async def scenario() -> None:
        service = PersonalMonitorService(
            telegram_api=FakeTelegram(),
            telegram_gateway=FakeGateway(),
            scheduler=FakeScheduler(),
            runner=FakeRunner(),
            outbox=FakePeriodic(),
            maintenance=FakePeriodic(),
            heartbeat=WaitingHeartbeat(),
            resources=(tracker, tracker),
            clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
            sleeper=asyncio.sleep,
        )
        task = asyncio.create_task(service.run())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert tracker.closed == 1


def test_fatal_component_error_propagates_after_cleanup() -> None:
    tracker = CloseTracker()

    class Fatal(BaseException):
        pass

    class FatalHeartbeat:
        async def beat(self, *, now: datetime) -> None:
            raise Fatal

    service = PersonalMonitorService(
        telegram_api=FakeTelegram(),
        telegram_gateway=FakeGateway(),
        scheduler=FakeScheduler(),
        runner=FakeRunner(),
        outbox=FakePeriodic(),
        maintenance=FakePeriodic(),
        heartbeat=FatalHeartbeat(),
        resources=(tracker,),
        clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=immediate_sleep,
    )

    with pytest.raises(BaseExceptionGroup) as caught:
        asyncio.run(service.run())

    assert any(isinstance(error, Fatal) for error in caught.value.exceptions)
    assert tracker.closed == 1


def test_next_maintenance_run_is_the_next_local_0330_without_drift() -> None:
    seoul = ZoneInfo("Asia/Seoul")

    before = datetime(2026, 7, 23, 3, 29, tzinfo=seoul)
    exact = datetime(2026, 7, 23, 3, 30, tzinfo=seoul)

    assert next_maintenance_run(before, seoul).astimezone(seoul) == exact
    assert next_maintenance_run(exact, seoul).astimezone(seoul) == datetime(
        2026, 7, 24, 3, 30, tzinfo=seoul
    )
