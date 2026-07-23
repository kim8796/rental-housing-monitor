from __future__ import annotations

import asyncio
import inspect
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from personal_monitor.storage.runtime import MonitorLease

_LOGGER = logging.getLogger(__name__)
_MAX_COMPONENT_FAILURES = 5


class ServiceFailure(RuntimeError):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("personal monitor service failed")

    def __repr__(self) -> str:
        return "ServiceFailure(<redacted>)"


class NullDeliverySender:
    """A transaction-safe one-shot sender that deliberately performs no I/O."""

    async def send(self, _address: str, _payload: dict[str, object]) -> str:
        return "local-null-delivery"


class TelegramDeliverySender:
    def __init__(self, telegram_api: object, *, delivery_chat_id: int) -> None:
        send = getattr(telegram_api, "send_message", None)
        if not callable(send) or type(delivery_chat_id) is not int or delivery_chat_id == 0:
            raise ValueError("invalid Telegram delivery configuration")
        self._send = send
        self._delivery_chat_id = delivery_chat_id

    async def send(self, _address: str, payload: dict[str, object]) -> str:
        if type(payload) is not dict or set(payload) != {"text"}:
            raise ValueError("invalid delivery payload")
        text = payload.get("text")
        if type(text) is not str or not text:
            raise ValueError("invalid delivery payload")
        result = await self._send(
            self._delivery_chat_id,
            text,
            disable_web_page_preview=True,
        )
        if type(result) is not str or not result:
            raise RuntimeError("Telegram delivery failed")
        return result


def next_maintenance_run(after: datetime, timezone: ZoneInfo) -> datetime:
    """Return the next valid local 03:30 as an aware UTC instant."""
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("after must be timezone-aware")
    local = after.astimezone(timezone)
    start_day = local.date()
    for day_offset in range(0, 370):
        candidate_day = start_day + timedelta(days=day_offset)
        candidate = _valid_local_time(candidate_day, timezone)
        if candidate is not None and candidate > after.astimezone(UTC):
            return candidate
    raise RuntimeError("unable to calculate maintenance schedule")


def _valid_local_time(day: date, timezone: ZoneInfo) -> datetime | None:
    # 03:30 is normally valid. The round-trip protects unusual historical/future DST rules.
    for minute_offset in range(0, 181):
        local_minutes = 3 * 60 + 30 + minute_offset
        hour, minute = divmod(local_minutes, 60)
        if hour >= 24:
            return None
        candidate = datetime.combine(
            day,
            datetime_time(hour=hour, minute=minute),
            tzinfo=timezone,
        )
        utc_candidate = candidate.astimezone(UTC)
        round_trip = utc_candidate.astimezone(timezone)
        if round_trip.date() == day and round_trip.hour == hour and round_trip.minute == minute:
            return utc_candidate
    return None


class PersonalMonitorService:
    """Own the five long-running personal-monitor loops and their shutdown boundary."""

    def __init__(
        self,
        *,
        telegram_api: object,
        telegram_gateway: object,
        scheduler: object,
        runner: object,
        outbox: object,
        maintenance: object,
        heartbeat: object,
        resources: Iterable[object] = (),
        timezone: ZoneInfo | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        shutdown_grace_seconds: float = 90.0,
    ) -> None:
        if shutdown_grace_seconds < 0:
            raise ValueError("shutdown grace must be nonnegative")
        dependencies = (
            (telegram_api, "get_updates"),
            (telegram_api, "ensure_webhook_disabled"),
            (telegram_gateway, "handle_update"),
            (scheduler, "tick"),
            (runner, "run"),
            (outbox, "drain_once"),
            (maintenance, "run"),
            (heartbeat, "beat"),
        )
        if any(not callable(getattr(owner, name, None)) for owner, name in dependencies):
            raise TypeError("invalid service dependency")
        self.telegram_api = telegram_api
        self.telegram_gateway = telegram_gateway
        self.scheduler = scheduler
        self.runner = runner
        self.outbox = outbox
        self.maintenance = maintenance
        self.heartbeat = heartbeat
        self.resources = tuple(resources)
        self.timezone = timezone or ZoneInfo("Asia/Seoul")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.sleeper = sleeper or asyncio.sleep
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self._stop_event = asyncio.Event()
        self._active_runs: set[asyncio.Task[None]] = set()
        self._task_group: asyncio.TaskGroup | None = None
        self._running = False
        self._closed = False
        self._telegram_offset = 0
        self._telegram_last_poll: datetime | None = None
        self._telegram_last_update: datetime | None = None
        self._scheduler_last_loop: datetime | None = None

    @property
    def telegram_offset(self) -> int:
        return self._telegram_offset

    @property
    def telegram_last_poll(self) -> datetime | None:
        return self._telegram_last_poll

    @property
    def telegram_last_update(self) -> datetime | None:
        return self._telegram_last_update

    @property
    def scheduler_last_loop(self) -> datetime | None:
        return self._scheduler_last_loop

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run_until(self, trigger: Awaitable[object]) -> None:
        async def stop_when_done() -> None:
            try:
                await trigger
            finally:
                self.request_stop()

        watcher = asyncio.create_task(stop_when_done())
        try:
            await self.run()
        finally:
            if not watcher.done():
                watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def run(self) -> None:
        if self._running or self._closed:
            raise ServiceFailure
        self._running = True
        installed: tuple[tuple[signal.Signals, Any], ...] = ()
        pending: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        try:
            try:
                installed = self._install_signal_handlers()
                await self.telegram_api.ensure_webhook_disabled()
                async with asyncio.TaskGroup() as group:
                    self._task_group = group
                    periodic = (
                        group.create_task(self._telegram_loop(), name="telegram-poller"),
                        group.create_task(self._scheduler_loop(), name="scheduler"),
                        group.create_task(self._outbox_loop(), name="outbox"),
                        group.create_task(self._maintenance_loop(), name="maintenance"),
                        group.create_task(self._heartbeat_loop(), name="heartbeat"),
                    )
                    await self._stop_event.wait()
                    for task in periodic:
                        task.cancel()
                    await asyncio.gather(*periodic, return_exceptions=True)
                    await self._finish_active_runs()
            except asyncio.CancelledError as error:
                self.request_stop()
                pending = error
            except BaseException as error:
                self.request_stop()
                pending = error
        finally:
            self._task_group = None
            self._restore_signal_handlers(installed)
            cleanup_errors = await self._close_resources()
            self._running = False

        if pending is not None:
            if not isinstance(pending, Exception):
                for error in cleanup_errors:
                    pending.add_note(f"cleanup failed: {type(error).__name__}")
                raise pending
            failure = ServiceFailure()
            for error in cleanup_errors:
                failure.add_note(f"cleanup failed: {type(error).__name__}")
            raise failure from None
        if cleanup_errors:
            failure = ServiceFailure()
            for error in cleanup_errors:
                failure.add_note(f"cleanup failed: {type(error).__name__}")
            raise failure

    async def _telegram_loop(self) -> None:
        failures = 0
        while not self.stopping:
            try:
                updates = await self.telegram_api.get_updates(
                    offset=self._telegram_offset,
                    timeout=30,
                )
                self._telegram_last_poll = self._now()
                failures = 0
                for update in updates:
                    if self.stopping:
                        return
                    update_id = getattr(update, "update_id", None)
                    if type(update_id) is not int or update_id < self._telegram_offset:
                        raise RuntimeError("invalid Telegram update sequence")
                    try:
                        await self.telegram_gateway.handle_update(update, now=self._now())
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        _safe_log("telegram_update_failed", error)
                    self._telegram_offset = update_id + 1
                    self._telegram_last_update = self._now()
                # A real long poll suspends. This explicit checkpoint also prevents a
                # misbehaving/test transport returning immediately from monopolizing the loop.
                await self.sleeper(0)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures = _component_failure("telegram_iteration_failed", error, failures)
                await self.sleeper(min(30.0, float(2 ** min(failures - 1, 5))))

    async def _scheduler_loop(self) -> None:
        deadline = self.monotonic()
        failures = 0
        while not self.stopping:
            try:
                now = self._now()
                leases = self.scheduler.tick(now)
                self._scheduler_last_loop = now
                failures = 0
                if not self.stopping:
                    for lease in leases:
                        if self.stopping:
                            break
                        self._start_monitor(lease)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures = _component_failure("scheduler_iteration_failed", error, failures)
            deadline = _next_deadline(deadline, 15.0, self.monotonic())
            await self.sleeper(max(0.0, deadline - self.monotonic()))

    async def _outbox_loop(self) -> None:
        deadline = self.monotonic()
        failures = 0
        while not self.stopping:
            try:
                await self.outbox.drain_once(now=self._now())
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures = _component_failure("outbox_iteration_failed", error, failures)
            deadline = _next_deadline(deadline, 5.0, self.monotonic())
            await self.sleeper(max(0.0, deadline - self.monotonic()))

    async def _maintenance_loop(self) -> None:
        failures = 0
        while not self.stopping:
            now = self._now()
            scheduled = next_maintenance_run(now, self.timezone)
            delay = max(0.0, (scheduled - now.astimezone(UTC)).total_seconds())
            deadline = self.monotonic() + delay
            await self.sleeper(max(0.0, deadline - self.monotonic()))
            if self.stopping:
                return
            try:
                result = self.maintenance.run(now=self._now())
                if inspect.isawaitable(result):
                    await result
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures = _component_failure("maintenance_iteration_failed", error, failures)

    async def _heartbeat_loop(self) -> None:
        deadline = self.monotonic()
        failures = 0
        while not self.stopping:
            try:
                await self.heartbeat.beat(now=self._now())
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures = _component_failure("heartbeat_iteration_failed", error, failures)
            deadline = _next_deadline(deadline, 60.0, self.monotonic())
            await self.sleeper(max(0.0, deadline - self.monotonic()))

    def _start_monitor(self, lease: object) -> None:
        if self.stopping or type(lease) is not MonitorLease or self._task_group is None:
            return
        task = self._task_group.create_task(self._run_monitor(lease))
        self._active_runs.add(task)
        task.add_done_callback(self._active_runs.discard)

    async def _run_monitor(self, lease: MonitorLease) -> None:
        try:
            await self.runner.run(lease)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _safe_log("monitor_run_failed", error, monitor_id=lease.monitor_id)

    async def _finish_active_runs(self) -> None:
        if not self._active_runs:
            return
        _done, pending = await asyncio.wait(
            tuple(self._active_runs),
            timeout=self.shutdown_grace_seconds,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("clock returned an invalid instant")
        return value.astimezone(UTC)

    async def _close_resources(self) -> list[BaseException]:
        if self._closed:
            return []
        self._closed = True
        errors: list[BaseException] = []
        seen: set[int] = set()
        for resource in self.resources:
            identity = id(resource)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                close = getattr(resource, "aclose", None)
                if not callable(close):
                    close = getattr(resource, "close", None)
                if not callable(close):
                    continue
                result = close()
                if inspect.isawaitable(result):
                    await asyncio.shield(result)
            except BaseException as error:
                errors.append(error)
        return errors

    def _install_signal_handlers(
        self,
    ) -> tuple[tuple[signal.Signals, Any], ...]:
        if threading.current_thread() is not threading.main_thread():
            return ()
        loop = asyncio.get_running_loop()
        installed: list[tuple[signal.Signals, Any]] = []
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous = signal.getsignal(signum)
                loop.add_signal_handler(signum, self.request_stop)
                installed.append((signum, previous))
            except (NotImplementedError, RuntimeError, ValueError):
                continue
        return tuple(installed)

    def _restore_signal_handlers(
        self,
        installed: tuple[tuple[signal.Signals, Any], ...],
    ) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        loop = asyncio.get_running_loop()
        for signum, previous in installed:
            with suppress(Exception):
                loop.remove_signal_handler(signum)
                signal.signal(signum, previous)


def _safe_log(event: str, error: BaseException, **context: object) -> None:
    try:
        _LOGGER.error(
            "",
            extra={
                "event": event,
                "context": context,
                "error": error,
            },
        )
    except BaseException:
        return


def _component_failure(event: str, error: Exception, previous: int) -> int:
    failures = previous + 1
    _safe_log(event, error, retry_count=failures)
    if failures >= _MAX_COMPONENT_FAILURES:
        raise ServiceFailure from None
    return failures


def _next_deadline(previous: float, interval: float, now: float) -> float:
    candidate = previous + interval
    if candidate > now:
        return candidate
    missed = int((now - candidate) // interval) + 1
    return candidate + missed * interval


class _SystemResolver:
    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return tuple(record[4][0] for record in records)


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _RegistryMonitorProvider:
    def __init__(self, registry: object) -> None:
        self.registry = registry

    def list_monitors(self, owner_id: str) -> tuple[object, ...]:
        from personal_monitor.control.intents import OwnedMonitorSummary

        return tuple(
            OwnedMonitorSummary(
                owner_id=row.owner_id,
                id=row.id,
                name=row.name,
                status=row.status.value,
            )
            for row in self.registry.list_monitors(owner_id)
        )


class _PlanningProbe:
    """Use the same sealed egress/robots components for pre-approval page sampling."""

    def __init__(self, adapter: object) -> None:
        self.adapter = adapter

    async def probe(self, _owner_id: str, target: object) -> object:
        from personal_monitor.control.planner import ProbeResult
        from personal_monitor.domain.spec import FetchStrategy
        from personal_monitor.security.url_policy import MAX_REDIRECTS, ResolvedTarget

        if type(target) is not ResolvedTarget:
            raise ValueError("invalid planning target")
        current = target
        seen = {current.normalized_url}
        first_decision = None
        for redirect_count in range(MAX_REDIRECTS + 1):
            await self.adapter._rate_limiter.acquire(current.hostname)
            decision, retry_after = await self.adapter._robots_decision(current)
            decision.require_allowed()
            if first_decision is None:
                first_decision = decision
            delay = decision.crawl_delay_seconds
            if retry_after is not None:
                delay = max(delay or 0.0, retry_after)
            await self.adapter._rate_limiter.acquire(current.hostname, delay)
            document = await self.adapter._dispatch_backend(
                FetchStrategy.HTTP,
                current,
                profile=None,
            )
            await self.adapter._validate_document(document, requested=current)
            if document.status not in {301, 302, 303, 307, 308}:
                return ProbeResult(
                    target=target,
                    document=document,
                    robots=first_decision,
                )
            if document.redirect_location is None or redirect_count == MAX_REDIRECTS:
                raise ValueError("planning redirect rejected")
            current = await self.adapter._url_policy.validate_redirect(
                document.redirect_location,
                redirect_count=redirect_count + 1,
            )
            if current.normalized_url in seen:
                raise ValueError("planning redirect rejected")
            seen.add(current.normalized_url)
        raise ValueError("planning probe failed")


def _prepare_personal_identity(
    connection: object,
    *,
    telegram_user_id: int,
    delivery_chat_id: int,
) -> tuple[str, str]:
    from personal_monitor.storage.schema import transaction, utc_now

    owner_id = f"telegram-user:{telegram_user_id}"
    target_id = "telegram-primary"
    with transaction(connection, immediate=True):
        user = connection.execute(
            "SELECT telegram_user_id FROM users WHERE id = ?",
            (owner_id,),
        ).fetchone()
        if user is None:
            connection.execute(
                "INSERT INTO users(id, telegram_user_id, status, created_at) "
                "VALUES (?, ?, 'active', ?)",
                (owner_id, telegram_user_id, utc_now().isoformat()),
            )
        elif user["telegram_user_id"] != telegram_user_id:
            raise ValueError("personal owner identity mismatch")
        target = connection.execute(
            "SELECT owner_id FROM delivery_targets WHERE id = ?",
            (target_id,),
        ).fetchone()
        if target is None:
            connection.execute(
                "INSERT INTO delivery_targets(id, owner_id, kind, address, created_at) "
                "VALUES (?, ?, 'telegram', ?, ?)",
                (target_id, owner_id, str(delivery_chat_id), utc_now().isoformat()),
            )
        elif target["owner_id"] != owner_id:
            raise ValueError("personal delivery identity mismatch")
        else:
            connection.execute(
                "UPDATE delivery_targets SET address = ? WHERE id = ?",
                (str(delivery_chat_id), target_id),
            )
    return owner_id, target_id


def _private_root(path: object) -> None:
    candidate = os.fspath(path)
    with suppress(FileExistsError):
        os.mkdir(candidate, 0o700)
    metadata = os.lstat(candidate)
    if not os.path.isabs(candidate) or not stat_is_private_directory(metadata):
        raise ValueError("private runtime root is invalid")


def stat_is_private_directory(metadata: os.stat_result) -> bool:
    import stat

    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _runtime_components(
    settings: object,
    connection: object,
    *,
    sender: object,
    worker_id: str,
) -> tuple[object, object, tuple[object, ...], object, object]:
    from personal_monitor.adapters._policy import BoundedPolicyHttpClient
    from personal_monitor.adapters.official_api import OfficialJsonAdapter
    from personal_monitor.adapters.registry import DefaultAdapterRegistry
    from personal_monitor.adapters.scrapling import ScraplingSourceAdapter
    from personal_monitor.engine.outbox import OutboxWorker
    from personal_monitor.engine.runner import MonitorRunner
    from personal_monitor.observability import OperatorEventRepository
    from personal_monitor.scraping.profiles import BrowserProfileStore
    from personal_monitor.security.rate_limit import HostRateLimiter
    from personal_monitor.security.url_policy import UrlPolicy
    from personal_monitor.security.vault import CredentialVault
    from personal_monitor.storage import RegistryRepository, RuntimeRepository

    _private_root(settings.profiles_root)
    vault = CredentialVault(
        settings.profiles_root / "vault",
        key_path=settings.master_key_path,
    )
    try:
        profile_store = BrowserProfileStore(
            vault,
            materialization_root=settings.profiles_root / "materialized",
        )
    except BaseException:
        vault.close()
        raise
    try:
        policy = UrlPolicy(_SystemResolver())
        limiter = HostRateLimiter()
        scrapling = ScraplingSourceAdapter(
            url_policy=policy,
            rate_limiter=limiter,
            egress_proxy_url=settings.egress_proxy,
            profile_provider=profile_store,
        )
        official_client = BoundedPolicyHttpClient(
            egress_proxy_url=settings.egress_proxy,
        )
        official = OfficialJsonAdapter(
            url_policy=policy,
            rate_limiter=limiter,
            http_client=official_client,
        )
        adapters = DefaultAdapterRegistry(scrapling=scrapling, official_api=official)
        registry = RegistryRepository(connection)
        runtime = RuntimeRepository(connection)
        runner = MonitorRunner(
            registry=registry,
            runtime=runtime,
            adapters=adapters,
            clock=_UtcClock(),
            worker_id=worker_id,
        )
        outbox = OutboxWorker(
            runtime=runtime,
            registry=registry,
            sender=sender,
            health_sink=OperatorEventRepository(connection),
            worker_id=f"{worker_id}:outbox",
        )
        resources = (scrapling._backend, profile_store, vault)
        return runner, outbox, resources, registry, runtime
    except BaseException:
        with suppress(BaseException):
            profile_store.close()
        with suppress(BaseException):
            vault.close()
        raise


def build_service(settings: object) -> PersonalMonitorService:
    """Compose production dependencies; the event-loop mechanism remains independently testable."""
    from personal_monitor.ai.auth import CodexAuthGuard
    from personal_monitor.ai.worker import CodexWorkerClient
    from personal_monitor.control.actions import PendingActionService
    from personal_monitor.control.intents import IntentRouter
    from personal_monitor.control.planner import MonitorPlanner
    from personal_monitor.control.service import ControlService
    from personal_monitor.engine.scheduler import Scheduler
    from personal_monitor.maintenance import Maintenance
    from personal_monitor.observability import (
        HeartbeatMonitor,
        OperatorEventRepository,
        configure_json_logging,
    )
    from personal_monitor.storage import open_database
    from personal_monitor.telegram import TelegramApi, TelegramGateway

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise ServiceFailure
    configure_json_logging(settings.log_path)
    connection = open_database(settings.database_path)
    telegram = None
    component_resources: tuple[object, ...] = ()
    try:
        _prepare_personal_identity(
            connection,
            telegram_user_id=settings.telegram_user_id,
            delivery_chat_id=settings.delivery_chat_id,
        )
        telegram = TelegramApi(settings.telegram_bot_token)
        delivery = TelegramDeliverySender(
            telegram,
            delivery_chat_id=settings.delivery_chat_id,
        )
        worker_id = f"service-{os.getpid()}"
        runner, outbox, component_resources, registry, runtime = _runtime_components(
            settings,
            connection,
            sender=delivery,
            worker_id=worker_id,
        )
        worker = CodexWorkerClient(settings.codex_socket)
        actions = PendingActionService(connection)
        router = IntentRouter(_RegistryMonitorProvider(registry), worker)
        planner = MonitorPlanner(
            runner.adapters.scrapling._url_policy,
            _PlanningProbe(runner.adapters.scrapling),
            worker,
            actions,
        )
        control = ControlService(router, registry, planner, actions)
        gateway = TelegramGateway(
            settings.telegram_user_id,
            settings.command_chat_id,
            control,
            actions,
            telegram,
        )
        codex_home = os.environ.get(
            "PERSONAL_MONITOR_CODEX_HOME",
            "/srv/personal-monitor/codex-home",
        )
        codex_binary = os.environ.get("PERSONAL_MONITOR_CODEX_BINARY", "codex")
        node_binary = os.environ.get("PERSONAL_MONITOR_NODE_BINARY") or None
        auth_guard = CodexAuthGuard(
            codex_binary,
            Path(codex_home),
            node_binary=node_binary,
        )
        state: dict[str, PersonalMonitorService] = {}
        operator_events = OperatorEventRepository(connection)
        heartbeat = HeartbeatMonitor(
            connection=connection,
            database_path=settings.database_path,
            backup_status_path=settings.backup_status_path,
            auth_guard=auth_guard,
            operator_events=operator_events,
            scheduler_last_loop=lambda: state["service"].scheduler_last_loop,
            telegram_last_poll=lambda: state["service"].telegram_last_poll,
            telegram_last_update=lambda: state["service"].telegram_last_update,
        )
        service = PersonalMonitorService(
            telegram_api=telegram,
            telegram_gateway=gateway,
            scheduler=Scheduler(runtime, worker_id),
            runner=runner,
            outbox=outbox,
            maintenance=Maintenance(connection),
            heartbeat=heartbeat,
            resources=(telegram, *component_resources, connection),
            timezone=settings.timezone,
        )
        state["service"] = service
        return service
    except BaseException:
        for resource in ((telegram,) if telegram is not None else ()) + component_resources:
            with suppress(BaseException):
                close = getattr(resource, "aclose", None)
                if callable(close):
                    asyncio.run(close())
                    continue
                close = getattr(resource, "close", None)
                if callable(close):
                    close()
        connection.close()
        raise


async def run_monitor_once(
    *,
    database_path: object,
    monitor_id: str,
    delivery_enabled: bool,
) -> int:
    from pathlib import Path
    from uuid import uuid4

    from personal_monitor.config import Settings
    from personal_monitor.observability import configure_json_logging
    from personal_monitor.storage import open_existing_database

    if type(monitor_id) is not str or not monitor_id or type(delivery_enabled) is not bool:
        raise ValueError("invalid one-shot request")
    settings = Settings.from_env()
    database = Path(database_path)
    if not database.is_absolute():
        raise ValueError("invalid one-shot database")
    configure_json_logging(settings.log_path)
    connection = open_existing_database(database)
    telegram = None
    resources: tuple[object, ...] = ()
    try:
        if delivery_enabled:
            from personal_monitor.telegram import TelegramApi

            telegram = TelegramApi(settings.telegram_bot_token)
            sender: object = TelegramDeliverySender(
                telegram,
                delivery_chat_id=settings.delivery_chat_id,
            )
        else:
            sender = NullDeliverySender()
        worker_id = f"run-once-{uuid4().hex}"
        runner, outbox, resources, _registry, runtime = _runtime_components(
            settings,
            connection,
            sender=sender,
            worker_id=worker_id,
        )
        lease = runtime.claim_monitor(
            monitor_id,
            worker_id=worker_id,
            now=datetime.now(UTC),
        )
        result = await runner.run(lease)
        await outbox.drain_once(
            now=datetime.now(UTC),
            monitor_id=monitor_id,
            outbox_ids=result.outbox_ids,
        )
        return 0
    finally:
        for resource in ((telegram,) if telegram is not None else ()) + resources:
            with suppress(BaseException):
                close = getattr(resource, "aclose", None)
                if not callable(close):
                    close = getattr(resource, "close", None)
                if callable(close):
                    value = close()
                    if inspect.isawaitable(value):
                        await value
        connection.close()
