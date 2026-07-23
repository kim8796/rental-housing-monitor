from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import personal_monitor.adapters._policy as adapter_policy_module
import personal_monitor.adapters.official_api as official_module
import personal_monitor.adapters.rental_housing as rental_module
import personal_monitor.adapters.scrapling as scrapling_module
import personal_monitor.engine.outbox as outbox_module
import personal_monitor.engine.runner as runner_module
import personal_monitor.observability as observability_module
import personal_monitor.scraping.profiles as profiles_module
import personal_monitor.security.rate_limit as rate_limit_module
import personal_monitor.security.url_policy as url_policy_module
import personal_monitor.security.vault as vault_module
import personal_monitor.service as service_module
import personal_monitor.storage as storage_module
from personal_monitor.domain.spec import SourceAdapterKind
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.service import (
    NullDeliverySender,
    PersonalMonitorService,
    ServiceFailure,
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
        self.preflight_calls = 0

    async def ensure_webhook_disabled(self) -> None:
        self.preflight_calls += 1

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
    assert telegram.preflight_calls == 1
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


def _composed_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    service_key: str | None,
):
    created_keys: list[str] = []
    rental_adapter = object()

    class Resource:
        def close(self) -> None:
            pass

    class Vault(Resource):
        def __init__(self, *args, **kwargs) -> None:
            pass

    class ProfileStore(Resource):
        def __init__(self, *args, **kwargs) -> None:
            pass

    class Scrapling:
        def __init__(self, *args, **kwargs) -> None:
            self._backend = Resource()

    class Official:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class RentalFactory:
        @classmethod
        def production(cls, key: str):
            created_keys.append(key)
            return rental_adapter

    class Repository:
        def __init__(self, connection) -> None:
            self.connection = connection

    class Runner:
        def __init__(self, **values) -> None:
            self.__dict__.update(values)

    class Outbox:
        def __init__(self, **values) -> None:
            self.__dict__.update(values)

    monkeypatch.setattr(service_module, "_private_root", lambda _path: None)
    monkeypatch.setattr(vault_module, "CredentialVault", Vault)
    monkeypatch.setattr(profiles_module, "BrowserProfileStore", ProfileStore)
    monkeypatch.setattr(url_policy_module, "UrlPolicy", lambda *_args: object())
    monkeypatch.setattr(rate_limit_module, "HostRateLimiter", lambda: object())
    monkeypatch.setattr(scrapling_module, "ScraplingSourceAdapter", Scrapling)
    monkeypatch.setattr(
        adapter_policy_module, "BoundedPolicyHttpClient", lambda **_kwargs: object()
    )
    monkeypatch.setattr(official_module, "OfficialJsonAdapter", Official)
    monkeypatch.setattr(rental_module, "RentalHousingAdapter", RentalFactory)
    monkeypatch.setattr(storage_module, "RegistryRepository", Repository)
    monkeypatch.setattr(storage_module, "RuntimeRepository", Repository)
    monkeypatch.setattr(runner_module, "MonitorRunner", Runner)
    monkeypatch.setattr(outbox_module, "OutboxWorker", Outbox)
    monkeypatch.setattr(observability_module, "OperatorEventRepository", Repository)

    settings = SimpleNamespace(
        profiles_root=tmp_path / "profiles",
        master_key_path=tmp_path / "master.key",
        egress_proxy="http://proxy.example:8080",
        data_go_kr_service_key=service_key,
    )
    runner, _outbox, _resources, _registry, _runtime = service_module._runtime_components(
        settings,
        object(),
        sender=object(),
        worker_id="worker",
    )
    return runner, created_keys, rental_adapter


def test_runtime_composition_registers_only_configured_rental_factory_without_key_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "encoded-data-go-key-private"

    runner, created_keys, rental_adapter = _composed_runner(
        monkeypatch,
        tmp_path,
        service_key=secret,
    )

    assert created_keys == [secret]
    assert (
        runner.adapters.resolve(SourceAdapterKind.PYTHON_PLUGIN, "rental_housing") is rental_adapter
    )
    assert secret not in repr(runner.adapters)


def test_runtime_composition_without_key_keeps_builtins_and_rejects_only_rental(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner, created_keys, _rental_adapter = _composed_runner(
        monkeypatch,
        tmp_path,
        service_key=None,
    )

    assert created_keys == []
    assert runner.adapters.resolve(SourceAdapterKind.SCRAPLING, None) is runner.adapters.scrapling
    with pytest.raises(MonitorError) as caught:
        runner.adapters.resolve(SourceAdapterKind.PYTHON_PLUGIN, "rental_housing")
    assert caught.value.error_class is ErrorClass.POLICY
    assert "key" not in str(caught.value).casefold()


def test_production_heartbeat_uses_worker_auth_without_monitor_codex_home() -> None:
    source = inspect.getsource(service_module.build_service)
    assert "CodexAuthGuard" not in source
    assert "PERSONAL_MONITOR_CODEX_HOME" not in source
    assert "PERSONAL_MONITOR_CODEX_BINARY" not in source
    assert "PERSONAL_MONITOR_NODE_BINARY" not in source
    assert "auth_guard=worker" in source


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


def test_webhook_preflight_failure_starts_no_component_loop_and_closes() -> None:
    telegram = FakeTelegram()
    scheduler = FakeScheduler()

    async def fail_preflight() -> None:
        telegram.preflight_calls += 1
        raise RuntimeError("private webhook detail")

    telegram.ensure_webhook_disabled = fail_preflight
    heartbeat = StopHeartbeat()
    service = PersonalMonitorService(
        telegram_api=telegram,
        telegram_gateway=FakeGateway(),
        scheduler=scheduler,
        runner=FakeRunner(),
        outbox=FakePeriodic(),
        maintenance=FakePeriodic(),
        heartbeat=heartbeat,
        resources=(telegram,),
        clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=immediate_sleep,
    )
    heartbeat.service = service

    with pytest.raises(Exception, match="personal monitor service failed"):
        asyncio.run(service.run())

    assert telegram.preflight_calls == 1
    assert telegram.offsets == []
    assert scheduler.calls == 0
    assert telegram.closed == 1


@pytest.mark.parametrize(
    "component",
    ["telegram", "scheduler", "outbox", "maintenance", "heartbeat"],
)
def test_persistent_component_failure_escalates_to_service_failure(component: str) -> None:
    class AlwaysFails:
        async def get_updates(self, **_kwargs):
            raise RuntimeError("private")

        def tick(self, _now):
            raise RuntimeError("private")

        async def drain_once(self, **_kwargs):
            raise RuntimeError("private")

        def run(self, **_kwargs):
            raise RuntimeError("private")

        async def beat(self, **_kwargs):
            raise RuntimeError("private")

    failing = AlwaysFails()
    telegram = FakeTelegram()
    if component == "telegram":
        telegram.get_updates = failing.get_updates
    service = PersonalMonitorService(
        telegram_api=telegram,
        telegram_gateway=FakeGateway(),
        scheduler=failing if component == "scheduler" else FakeScheduler(),
        runner=FakeRunner(),
        outbox=failing if component == "outbox" else FakePeriodic(),
        maintenance=failing if component == "maintenance" else FakePeriodic(),
        heartbeat=failing if component == "heartbeat" else StopHeartbeat(),
        clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=immediate_sleep,
    )
    if component != "heartbeat":
        # A healthy heartbeat must not stop the service while another component is
        # proving its bounded failure policy.
        service.heartbeat = FakePeriodicHeartbeat()

    with pytest.raises(Exception, match="personal monitor service failed"):
        asyncio.run(service.run())


class FakePeriodicHeartbeat:
    async def beat(self, **_kwargs) -> None:
        await asyncio.sleep(0)


def test_transient_scheduler_failure_recovers_and_resets_failure_budget() -> None:
    class Recovers:
        def __init__(self) -> None:
            self.calls = 0
            self.service: PersonalMonitorService | None = None

        def tick(self, _now):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("private")
            assert self.service is not None
            self.service.request_stop()
            return []

    scheduler = Recovers()
    service = PersonalMonitorService(
        telegram_api=FakeTelegram(),
        telegram_gateway=FakeGateway(),
        scheduler=scheduler,
        runner=FakeRunner(),
        outbox=FakePeriodic(),
        maintenance=FakePeriodic(),
        heartbeat=FakePeriodicHeartbeat(),
        clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=immediate_sleep,
    )
    scheduler.service = service

    asyncio.run(service.run())

    assert scheduler.calls == 2


def test_invalid_telegram_batches_escalate_without_resetting_failure_budget() -> None:
    class InvalidBatches(FakeTelegram):
        def __init__(self) -> None:
            super().__init__()
            self.service: PersonalMonitorService | None = None

        async def get_updates(self, **_kwargs):
            self.offsets.append(0)
            if len(self.offsets) == 6:
                assert self.service is not None
                self.service.request_stop()
            return [TelegramUpdate(-1, None, None)]

    telegram = InvalidBatches()
    service = PersonalMonitorService(
        telegram_api=telegram,
        telegram_gateway=FakeGateway(),
        scheduler=FakeScheduler(),
        runner=FakeRunner(),
        outbox=FakePeriodic(),
        maintenance=FakePeriodic(),
        heartbeat=FakePeriodicHeartbeat(),
        clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=immediate_sleep,
    )
    telegram.service = service

    with pytest.raises(ServiceFailure):
        asyncio.run(service._telegram_loop())

    assert len(telegram.offsets) == 5


def test_complete_valid_telegram_batch_resets_failure_budget() -> None:
    class BatchSequence(FakeTelegram):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def get_updates(self, **_kwargs):
            self.calls += 1
            if self.calls in {5, 10}:
                return [TelegramUpdate(0 if self.calls == 5 else 1, None, None)]
            return [TelegramUpdate(-1, None, None)]

    class StopAfterSecondValid(FakeGateway):
        def __init__(self) -> None:
            super().__init__()
            self.service: PersonalMonitorService | None = None

        async def handle_update(self, update: TelegramUpdate, *, now=None) -> None:
            await super().handle_update(update, now=now)
            if update.update_id == 1:
                assert self.service is not None
                self.service.request_stop()

    telegram = BatchSequence()
    gateway = StopAfterSecondValid()
    service = PersonalMonitorService(
        telegram_api=telegram,
        telegram_gateway=gateway,
        scheduler=FakeScheduler(),
        runner=FakeRunner(),
        outbox=FakePeriodic(),
        maintenance=FakePeriodic(),
        heartbeat=FakePeriodicHeartbeat(),
        clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=immediate_sleep,
    )
    gateway.service = service

    asyncio.run(service._telegram_loop())

    assert telegram.calls == 10
    assert gateway.ids == [0, 1]


def test_maintenance_retries_with_bounded_backoff_and_resets_after_success() -> None:
    delays: list[float] = []

    class MaintenanceSequence:
        def __init__(self) -> None:
            self.calls = 0
            self.service: PersonalMonitorService | None = None

        def run(self, **_kwargs) -> None:
            self.calls += 1
            if self.calls in {1, 2, 3, 4, 6, 7, 8, 9}:
                raise RuntimeError("private")
            if self.calls == 10:
                assert self.service is not None
                self.service.request_stop()

    async def recording_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    maintenance = MaintenanceSequence()
    service = PersonalMonitorService(
        telegram_api=FakeTelegram(),
        telegram_gateway=FakeGateway(),
        scheduler=FakeScheduler(),
        runner=FakeRunner(),
        outbox=FakePeriodic(),
        maintenance=maintenance,
        heartbeat=FakePeriodicHeartbeat(),
        clock=lambda: datetime(2026, 7, 22, 18, 29, tzinfo=UTC),
        monotonic=lambda: 0.0,
        sleeper=recording_sleep,
    )
    maintenance.service = service

    asyncio.run(service._maintenance_loop())

    assert maintenance.calls == 10
    assert delays == [60.0, 1.0, 2.0, 4.0, 8.0, 60.0, 1.0, 2.0, 4.0, 8.0]


def test_maintenance_escalates_on_fifth_consecutive_scheduled_failure() -> None:
    delays: list[float] = []

    class AlwaysFails:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, **_kwargs) -> None:
            self.calls += 1
            raise RuntimeError("private")

    async def recording_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    maintenance = AlwaysFails()
    service = PersonalMonitorService(
        telegram_api=FakeTelegram(),
        telegram_gateway=FakeGateway(),
        scheduler=FakeScheduler(),
        runner=FakeRunner(),
        outbox=FakePeriodic(),
        maintenance=maintenance,
        heartbeat=FakePeriodicHeartbeat(),
        clock=lambda: datetime(2026, 7, 22, 18, 29, tzinfo=UTC),
        monotonic=lambda: 0.0,
        sleeper=recording_sleep,
    )

    with pytest.raises(ServiceFailure):
        asyncio.run(service._maintenance_loop())

    assert maintenance.calls == 5
    assert delays == [60.0, 1.0, 2.0, 4.0, 8.0]
