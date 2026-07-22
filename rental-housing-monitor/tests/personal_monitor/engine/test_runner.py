from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from personal_monitor.domain.observation import (
    ObservationBatch,
    ObservedItem,
    SourceWarning,
    content_hash,
)
from personal_monitor.domain.rules import RuleMatch
from personal_monitor.domain.spec import MonitorSpec, RuleKind
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.engine.runner import MonitorRunner, delivery_key, render_payload
from personal_monitor.ports import (
    AdapterRegistry,
    Clock,
    DeliverySender,
    OperatorHealthSink,
    SourceAdapter,
)
from personal_monitor.storage import RegistryRepository, RuntimeRepository, open_database

NOW = datetime(2026, 7, 22, 3, 0, tzinfo=UTC)


def make_spec(**overrides: object) -> MonitorSpec:
    payload: dict[str, object] = {
        "schema_version": 1,
        "owner_id": "owner-1",
        "name": "가격 감시",
        "target_url": "https://example.com/list?view=private#secret",
        "source_adapter": "scrapling",
        "schedule": "0 */6 * * *",
        "timezone": "Asia/Seoul",
        "extract": {
            "item_scope": "main",
            "fields": {"price": {"selector": ".price", "type": "krw"}},
        },
        "validators": {"min_items": 0, "max_items": 10},
        "rules": [{"kind": "new_item"}],
    }
    payload.update(overrides)
    return MonitorSpec.model_validate(payload)


@dataclass(frozen=True)
class FixedClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


class FakeAdapter:
    def __init__(self, batch: ObservationBatch | BaseException) -> None:
        self.batch = batch
        self.calls: list[tuple[str, MonitorSpec]] = []

    async def fetch(self, monitor_id: str, spec: MonitorSpec) -> ObservationBatch:
        self.calls.append((monitor_id, spec))
        if isinstance(self.batch, BaseException):
            raise self.batch
        return self.batch


class FakeAdapters:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter = adapter
        self.calls: list[tuple[object, str | None]] = []

    def resolve(self, kind: object, adapter_ref: str | None) -> FakeAdapter:
        self.calls.append((kind, adapter_ref))
        return self.adapter


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    value = open_database(":memory:")
    yield value
    value.close()


def configured_runner(
    connection: sqlite3.Connection,
    batch: ObservationBatch | BaseException,
    *,
    spec: MonitorSpec | None = None,
) -> tuple[MonitorRunner, str, RegistryRepository, RuntimeRepository, FakeAdapter]:
    registry = RegistryRepository(connection)
    runtime = RuntimeRepository(connection)
    registry.create_user("owner-1", 1)
    registry.create_delivery_target("target-1", "owner-1", "real-chat-address")
    monitor_id = registry.create_monitor(spec or make_spec(), created_by="owner-1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (NOW.isoformat(), monitor_id)
    )
    assert runtime.claim_due(worker_id="worker-1", now=NOW) == [monitor_id]
    adapter = FakeAdapter(batch)
    runner = MonitorRunner(
        registry=registry,
        runtime=runtime,
        adapters=FakeAdapters(adapter),
        clock=FixedClock(),
        worker_id="worker-1",
    )
    return runner, monitor_id, registry, runtime, adapter


def test_runtime_ports_expose_the_closed_async_boundary() -> None:
    assert inspect.iscoroutinefunction(SourceAdapter.fetch)
    assert inspect.iscoroutinefunction(DeliverySender.send)
    assert inspect.iscoroutinefunction(OperatorHealthSink.emit_once)
    assert inspect.signature(SourceAdapter.fetch).return_annotation == "ObservationBatch"
    assert inspect.signature(AdapterRegistry.resolve).parameters["kind"].annotation == (
        "SourceAdapterKind"
    )
    assert inspect.signature(Clock.now).return_annotation == "datetime"


def test_monitor_error_retains_class_stage_and_safe_detail() -> None:
    error = MonitorError(ErrorClass.AUTHENTICATION, "fetch", "credential expired")

    assert str(error) == "credential expired"
    assert error.error_class is ErrorClass.AUTHENTICATION
    assert error.stage == "fetch"
    assert error.safe_detail == "credential expired"
    assert set(ErrorClass) == {
        ErrorClass.TRANSIENT_NETWORK,
        ErrorClass.AUTHENTICATION,
        ErrorClass.STRUCTURE,
        ErrorClass.VALIDATION,
        ErrorClass.POLICY,
        ErrorClass.DELIVERY,
        ErrorClass.INTERNAL,
    }


def test_successful_run_queues_only_idempotent_outbox_and_releases_lease(
    connection: sqlite3.Connection,
) -> None:
    batch = ObservationBatch(
        monitor_id="placeholder",
        items=(ObservedItem("listing-1", {"price": 900, "html": "<b>secret</b>"}),),
        observed_at=NOW,
        source_hash="hash",
    )
    runner, monitor_id, _, runtime, adapter = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id,
        items=batch.items,
        observed_at=batch.observed_at,
        source_hash=batch.source_hash,
    )

    first = asyncio.run(runner.run(monitor_id))

    assert first.status == "success"
    assert (first.matched_count, first.warning_count) == (1, 0)
    assert runtime.load_items(monitor_id) == list(batch.items)
    row = connection.execute(
        "SELECT dedupe_key, target_id, payload_json FROM outbox"
    ).fetchone()
    assert row["dedupe_key"] == f"{monitor_id}:listing-1:new_item"
    assert row["target_id"] == "target-1"
    assert "real-chat-address" not in row["payload_json"]
    assert "view=private" not in row["payload_json"]
    assert "#secret" not in row["payload_json"]
    assert "<b>secret</b>" not in row["payload_json"]
    monitor = connection.execute(
        "SELECT lease_owner, lease_expires_at, next_run_at FROM monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert monitor["lease_owner"] is None
    assert monitor["lease_expires_at"] is None
    assert monitor["next_run_at"] > NOW.isoformat()
    run = connection.execute("SELECT status, stage FROM runs").fetchone()
    assert tuple(run) == ("success", "complete")


def test_second_identical_snapshot_does_not_duplicate_delivery(
    connection: sqlite3.Connection,
) -> None:
    batch = ObservationBatch(
        monitor_id="placeholder",
        items=(ObservedItem("listing-1", {"price": 900}),),
        observed_at=NOW,
        source_hash="hash",
    )
    runner, monitor_id, _, _, adapter = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id,
        items=batch.items,
        observed_at=NOW,
        source_hash="hash",
    )
    asyncio.run(runner.run(monitor_id))
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (NOW.isoformat(), monitor_id)
    )
    RuntimeRepository(connection).claim_due(worker_id="worker-1", now=NOW)

    result = asyncio.run(runner.run(monitor_id))

    assert result.matched_count == 0
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_partial_warning_suppresses_no_change_and_never_renders_raw_detail(
    connection: sqlite3.Connection,
) -> None:
    spec = make_spec(notify_on_no_change=True)
    batch = ObservationBatch(
        monitor_id="placeholder",
        items=(),
        observed_at=NOW,
        source_hash="hash",
        source_status={"agency": "failed <html>Cookie: session=secret</html>"},
        warnings=(
            SourceWarning(
                source="agency",
                stage="parse",
                detail="GET https://private.test/?token=x <html>Cookie: session=secret</html>",
            ),
        ),
    )
    runner, monitor_id, _, _, adapter = configured_runner(connection, batch, spec=spec)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id,
        items=(),
        observed_at=NOW,
        source_hash="hash",
        source_status=batch.source_status,
        warnings=batch.warnings,
    )

    result = asyncio.run(runner.run(monitor_id))

    assert result.status == "partial_failure"
    assert (result.matched_count, result.warning_count) == (0, 1)
    row = connection.execute("SELECT dedupe_key, payload_json FROM outbox").fetchone()
    assert row["dedupe_key"] == f"{monitor_id}:warning:2026-07-22:agency:parse"
    assert "신규 공고가 없습니다" not in row["payload_json"]
    for forbidden in ("token=x", "Cookie", "session=secret", "<html>", "private.test"):
        assert forbidden not in row["payload_json"]


def test_no_change_notification_uses_observation_date_in_monitor_timezone(
    connection: sqlite3.Connection,
) -> None:
    spec = make_spec(notify_on_no_change=True)
    observed_at = datetime(2026, 7, 22, 16, 0, tzinfo=UTC)
    batch = ObservationBatch(
        monitor_id="placeholder", items=(), observed_at=observed_at, source_hash="hash"
    )
    runner, monitor_id, _, _, adapter = configured_runner(connection, batch, spec=spec)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id, items=(), observed_at=observed_at, source_hash="hash"
    )

    result = asyncio.run(runner.run(monitor_id))

    assert result.status == "success"
    row = connection.execute("SELECT dedupe_key, payload_json FROM outbox").fetchone()
    assert row["dedupe_key"] == f"{monitor_id}:no-change:2026-07-23"
    assert row["payload_json"] == '{"text":"오늘은 신규 공고가 없습니다."}'


def test_delivery_key_is_stable_for_new_and_changed_events() -> None:
    item = ObservedItem("listing-1", {"price": 900})
    new_match = RuleMatch(RuleKind.NEW_ITEM, None, None, None)
    changed = RuleMatch(RuleKind.NUMERIC_THRESHOLD, "price", 1100, 900)

    assert delivery_key("monitor-1", item, new_match) == "monitor-1:listing-1:new_item"
    first = delivery_key("monitor-1", item, changed)
    assert first == delivery_key("monitor-1", item, changed)
    expected_digest = content_hash({"previous": 1100, "current": 900})
    assert first == f"monitor-1:listing-1:numeric_threshold:price:{expected_digest}"
    assert first != delivery_key(
        "monitor-1", item, RuleMatch(RuleKind.NUMERIC_THRESHOLD, "price", 1200, 900)
    )


def test_fixed_payload_omits_item_content_and_strips_url_query_and_fragment() -> None:
    payload = render_payload(
        make_spec(),
        ObservedItem("<script>cookie=session-secret</script>", {"html": "<b>secret</b>"}),
        RuleMatch(RuleKind.NEW_ITEM, None, None, None),
    )

    text = str(payload["text"])
    assert "https://example.com/list" in text
    for forbidden in ("view=private", "#secret", "script", "cookie", "<b>"):
        assert forbidden not in text


@pytest.mark.parametrize(
    ("error_class", "expected_status", "expected_code"),
    [
        (ErrorClass.AUTHENTICATION, "paused_auth", "authentication_failed"),
        (ErrorClass.STRUCTURE, "needs_review", "structure_changed"),
        (ErrorClass.VALIDATION, "needs_review", "validation_failed"),
        (ErrorClass.POLICY, "needs_review", "policy_rejected"),
        (ErrorClass.INTERNAL, "needs_review", "internal_error"),
    ],
)
def test_closed_failures_finish_run_transition_status_and_release_lease(
    connection: sqlite3.Connection,
    error_class: ErrorClass,
    expected_status: str,
    expected_code: str,
) -> None:
    secret = "GET https://private.test/?token=x Cookie: session=secret <html>private</html>"
    runner, monitor_id, _, _, _ = configured_runner(
        connection, MonitorError(error_class, "fetch", secret)
    )

    result = asyncio.run(runner.run(monitor_id))

    assert result.status == "failed"
    run = connection.execute(
        "SELECT status, stage, error_class, error_detail, finished_at FROM runs"
    ).fetchone()
    assert tuple(run)[:4] == ("failed", "fetch", error_class.value, expected_code)
    assert run["finished_at"] is not None
    monitor = connection.execute(
        "SELECT status, lease_owner, lease_expires_at, next_run_at FROM monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert monitor["status"] == expected_status
    assert monitor["lease_owner"] is None
    assert monitor["lease_expires_at"] is None
    assert datetime.fromisoformat(monitor["next_run_at"]) > NOW
    assert secret not in json.dumps(dict(run))


def test_transient_network_failure_stays_active_and_retries_exactly_five_minutes_later(
    connection: sqlite3.Connection,
) -> None:
    runner, monitor_id, _, _, _ = configured_runner(
        connection,
        MonitorError(ErrorClass.TRANSIENT_NETWORK, "fetch", "offline secret detail"),
    )

    result = asyncio.run(runner.run(monitor_id))

    assert result.status == "failed"
    run = connection.execute("SELECT error_class, error_detail FROM runs").fetchone()
    assert tuple(run) == ("transient_network", "network_error")
    monitor = connection.execute(
        "SELECT status, lease_owner, next_run_at FROM monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    assert tuple(monitor) == ("active", None, "2026-07-22T03:05:00+00:00")


def test_arbitrary_exception_is_persisted_only_as_internal_error(
    connection: sqlite3.Connection,
) -> None:
    secret = "RuntimeError(GET /private?token=x Cookie=session-secret <html>body</html>)"
    runner, monitor_id, _, _, _ = configured_runner(connection, RuntimeError(secret))

    result = asyncio.run(runner.run(monitor_id))

    assert result.status == "failed"
    run = connection.execute(
        "SELECT stage, error_class, error_detail FROM runs"
    ).fetchone()
    assert tuple(run) == ("internal", "internal", "internal_error")
    assert secret not in json.dumps(dict(run))
    monitor = connection.execute(
        "SELECT status, lease_owner FROM monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    assert tuple(monitor) == ("needs_review", None)


def test_mismatched_adapter_batch_fails_validation_before_snapshot_or_outbox_mutation(
    connection: sqlite3.Connection,
) -> None:
    mismatched = ObservationBatch(
        monitor_id="different-monitor",
        items=(ObservedItem("injected", {"price": 1}),),
        observed_at=NOW,
        source_hash="hash",
    )
    runner, monitor_id, _, runtime, _ = configured_runner(connection, mismatched)
    previous = ObservationBatch(
        monitor_id=monitor_id,
        items=(ObservedItem("existing", {"price": 100}),),
        observed_at=NOW,
        source_hash="old",
    )
    runtime.upsert_items(previous)

    result = asyncio.run(runner.run(monitor_id))

    assert result.status == "failed"
    assert runtime.load_items(monitor_id) == list(previous.items)
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    run = connection.execute("SELECT stage, error_detail FROM runs").fetchone()
    assert tuple(run) == ("validate", "validation_failed")
