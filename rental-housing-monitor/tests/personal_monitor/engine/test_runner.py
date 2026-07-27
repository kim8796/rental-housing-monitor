from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

import personal_monitor.engine.runner as runner_module
from personal_monitor.domain.observation import (
    ObservationBatch,
    ObservedItem,
    SourceWarning,
    content_hash,
)
from personal_monitor.domain.rules import RuleMatch
from personal_monitor.domain.spec import MonitorSpec, MonitorStatus, RuleKind
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.engine.runner import MonitorRunner, delivery_key, render_payload
from personal_monitor.engine.scheduler import Scheduler
from personal_monitor.ports import (
    AdapterRegistry,
    Clock,
    DeliverySender,
    OperatorHealthSink,
    SourceAdapter,
)
from personal_monitor.storage import (
    DeliveryCandidate,
    MonitorLease,
    RegistryRepository,
    RuntimeRepository,
    open_database,
)
from tests.personal_monitor.sql_seed import seed_snapshot

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
) -> tuple[
    MonitorRunner,
    str,
    RegistryRepository,
    RuntimeRepository,
    FakeAdapter,
    MonitorLease,
]:
    registry = RegistryRepository(connection)
    runtime = RuntimeRepository(connection)
    registry.create_user("owner-1", 1)
    registry.create_delivery_target("target-1", "owner-1", "real-chat-address")
    monitor_id = registry.create_monitor(spec or make_spec(), created_by="owner-1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (NOW.isoformat(), monitor_id)
    )
    lease = runtime.claim_due(worker_id="worker-1", now=NOW)[0]
    adapter = FakeAdapter(batch)
    runner = MonitorRunner(
        registry=registry,
        runtime=runtime,
        adapters=FakeAdapters(adapter),
        clock=FixedClock(),
        worker_id="worker-1",
    )
    return runner, monitor_id, registry, runtime, adapter, lease


def test_runtime_ports_expose_the_closed_async_boundary() -> None:
    assert inspect.iscoroutinefunction(SourceAdapter.fetch)
    assert inspect.iscoroutinefunction(DeliverySender.send)
    assert inspect.iscoroutinefunction(OperatorHealthSink.emit_once)
    assert "durable atomic deduplication" in (OperatorHealthSink.emit_once.__doc__ or "")
    assert "processes and restarts" in (OperatorHealthSink.emit_once.__doc__ or "")
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


def test_public_create_tick_runner_path_needs_no_schedule_sql(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("personal_monitor.storage.registry.utc_now", lambda: NOW)
    registry = RegistryRepository(connection)
    runtime = RuntimeRepository(connection)
    registry.create_user("owner-1", 1)
    registry.create_delivery_target("target-1", "owner-1", "chat-1")
    monitor_id = registry.create_monitor(make_spec(), created_by="owner-1")
    adapter = FakeAdapter(
        ObservationBatch(
            monitor_id=monitor_id,
            items=(ObservedItem("listing", {"price": 100}),),
            observed_at=NOW,
            source_hash="hash",
        )
    )
    runner = MonitorRunner(
        registry=registry,
        runtime=runtime,
        adapters=FakeAdapters(adapter),
        clock=FixedClock(),
        worker_id="worker-1",
    )

    claims = Scheduler(runtime, worker_id="worker-1").tick(NOW)
    result = asyncio.run(runner.run(claims[0]))

    assert claims == [MonitorLease(monitor_id, 1)]
    assert result.status == "success"
    assert runtime.load_items(monitor_id) == [ObservedItem("listing", {"price": 100})]


def test_successful_run_queues_only_idempotent_outbox_and_releases_lease(
    connection: sqlite3.Connection,
) -> None:
    batch = ObservationBatch(
        monitor_id="placeholder",
        items=(ObservedItem("listing-1", {"price": 900}),),
        observed_at=NOW,
        source_hash="hash",
    )
    runner, monitor_id, _, runtime, adapter, lease = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id,
        items=batch.items,
        observed_at=batch.observed_at,
        source_hash=batch.source_hash,
    )

    first = asyncio.run(runner.run(lease))

    assert first.status == "success"
    assert (first.matched_count, first.warning_count) == (1, 0)
    assert runtime.load_items(monitor_id) == list(batch.items)
    row = connection.execute(
        "SELECT id, dedupe_key, target_id, payload_json FROM outbox"
    ).fetchone()
    assert first.outbox_ids == (row["id"],)
    assert row["dedupe_key"] == f"{monitor_id}:listing-1:new_item"
    assert row["target_id"] == "target-1"
    assert "real-chat-address" not in row["payload_json"]
    assert "view=private" not in row["payload_json"]
    assert "#secret" not in row["payload_json"]
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
    runner, monitor_id, _, _, adapter, lease = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id,
        items=batch.items,
        observed_at=NOW,
        source_hash="hash",
    )
    asyncio.run(runner.run(lease))
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (NOW.isoformat(), monitor_id)
    )
    lease = RuntimeRepository(connection).claim_due(worker_id="worker-1", now=NOW)[0]

    result = asyncio.run(runner.run(lease))

    assert result.matched_count == 0
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_runner_precomputes_candidates_then_applies_snapshot_and_outbox_once(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = ObservationBatch(
        monitor_id="placeholder",
        items=(ObservedItem("listing-1", {"price": 900}),),
        observed_at=NOW,
        source_hash="hash",
    )
    runner, monitor_id, _, runtime, adapter, lease = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id,
        items=batch.items,
        observed_at=NOW,
        source_hash="hash",
    )
    applied: list[tuple[ObservationBatch, tuple[DeliveryCandidate, ...]]] = []
    original_apply = runtime.apply_snapshot_and_deliveries

    def record_apply(
        batch: ObservationBatch,
        candidates: Sequence[DeliveryCandidate],
        **kwargs: object,
    ) -> list[str]:
        frozen_candidates = tuple(candidates)
        applied.append((batch, frozen_candidates))
        return original_apply(batch, frozen_candidates, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime, "apply_snapshot_and_deliveries", record_apply)

    result = asyncio.run(runner.run(lease))

    assert result.status == "success"
    assert len(applied) == 1
    assert applied[0][0] == adapter.batch
    assert len(applied[0][1]) == 1
    candidate = applied[0][1][0]
    assert candidate.dedupe_key == f"{monitor_id}:listing-1:new_item"
    assert candidate.target_id == "target-1"
    assert runtime.load_items(monitor_id) == list(batch.items)
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_atomic_enqueue_failure_preserves_old_snapshot_so_retry_recreates_notification(
    connection: sqlite3.Connection,
) -> None:
    batch = ObservationBatch(
        monitor_id="placeholder",
        items=(ObservedItem("listing-1", {"price": 900}),),
        observed_at=NOW,
        source_hash="hash",
    )
    runner, monitor_id, registry, runtime, adapter, lease = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id,
        items=batch.items,
        observed_at=NOW,
        source_hash="hash",
    )
    connection.execute(
        "CREATE TRIGGER reject_runner_delivery BEFORE INSERT ON outbox "
        f"WHEN NEW.dedupe_key = '{monitor_id}:listing-1:new_item' "
        "BEGIN SELECT RAISE(ABORT, 'injected enqueue'); END"
    )

    first = asyncio.run(runner.run(lease))

    assert first.status == "failed"
    assert runtime.load_items(monitor_id) == []
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    registry.transition_status(
        monitor_id,
        MonitorStatus.NEEDS_REVIEW,
        MonitorStatus.ACTIVE,
        owner_id="owner-1",
    )
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (NOW.isoformat(), monitor_id)
    )
    lease = runtime.claim_due(worker_id="worker-1", now=NOW)[0]
    connection.execute("DROP TRIGGER reject_runner_delivery")

    retry = asyncio.run(runner.run(lease))

    assert retry.status == "success"
    assert retry.matched_count == 1
    assert runtime.load_items(monitor_id) == list(batch.items)
    outbox = connection.execute("SELECT dedupe_key FROM outbox").fetchone()
    assert outbox["dedupe_key"] == f"{monitor_id}:listing-1:new_item"


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
    runner, monitor_id, _, _, adapter, lease = configured_runner(connection, batch, spec=spec)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id,
        items=(),
        observed_at=NOW,
        source_hash="hash",
        source_status=batch.source_status,
        warnings=batch.warnings,
    )

    result = asyncio.run(runner.run(lease))

    assert result.status == "partial_failure"
    assert (result.matched_count, result.warning_count) == (0, 1)
    row = connection.execute("SELECT dedupe_key, payload_json FROM outbox").fetchone()
    assert row["dedupe_key"] == f"{monitor_id}:warning:2026-07-22:agency:parse"
    assert "신규 공고가 없습니다" not in row["payload_json"]
    for forbidden in ("token=x", "Cookie", "session=secret", "<html>", "private.test"):
        assert forbidden not in row["payload_json"]


def test_invalid_complete_batch_preserves_snapshot_and_transitions_validation_failure(
    connection: sqlite3.Connection,
) -> None:
    spec = make_spec(validators={"min_items": 1, "max_items": 10})
    batch = ObservationBatch(
        monitor_id="placeholder", items=(), observed_at=NOW, source_hash="empty"
    )
    runner, monitor_id, _, runtime, adapter, lease = configured_runner(connection, batch, spec=spec)
    seed_snapshot(
        connection,
        ObservationBatch(
            monitor_id=monitor_id,
            items=(ObservedItem("existing", {"price": 100}),),
            observed_at=NOW - timedelta(hours=1),
            source_hash="old",
        ),
    )
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id, items=(), observed_at=NOW, source_hash="empty"
    )

    result = asyncio.run(runner.run(lease))

    assert result.status == "failed"
    assert runtime.load_items(monitor_id) == [ObservedItem("existing", {"price": 100})]
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert tuple(connection.execute("SELECT status, error_detail FROM runs").fetchone()) == (
        "failed",
        "validation_failed",
    )
    assert (
        connection.execute("SELECT status FROM monitors WHERE id = ?", (monitor_id,)).fetchone()[0]
        == "needs_review"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"price": True},
        {"price": float("inf")},
    ],
)
def test_invalid_required_or_numeric_types_never_mutate(
    connection: sqlite3.Connection, fields: dict[str, object]
) -> None:
    spec = make_spec(validators={"min_items": 1, "max_items": 10})
    batch = ObservationBatch(
        monitor_id="placeholder",
        items=(ObservedItem("invalid", fields),),  # type: ignore[arg-type]
        observed_at=NOW,
        source_hash="invalid",
    )
    runner, monitor_id, _, runtime, adapter, lease = configured_runner(connection, batch, spec=spec)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id,
        items=batch.items,
        observed_at=NOW,
        source_hash="invalid",
    )

    assert asyncio.run(runner.run(lease)).status == "failed"
    assert runtime.load_items(monitor_id) == []
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_warning_partial_batch_preserves_absent_items_and_avoids_realert_flood(
    connection: sqlite3.Connection,
) -> None:
    spec = make_spec(validators={"min_items": 2, "max_items": 10})
    partial = ObservationBatch(
        monitor_id="placeholder",
        items=(ObservedItem("new", {"price": 80}),),
        observed_at=NOW,
        source_hash="partial",
        warnings=(SourceWarning("agency", "fetch", "offline"),),
    )
    runner, monitor_id, _, runtime, adapter, lease = configured_runner(
        connection, partial, spec=spec
    )
    previous = (
        ObservedItem("old-a", {"price": 100}),
        ObservedItem("old-b", {"price": 120}),
    )
    seed_snapshot(
        connection,
        ObservationBatch(monitor_id, previous, NOW - timedelta(hours=1), "old"),
    )
    adapter.batch = ObservationBatch(
        monitor_id,
        partial.items,
        NOW,
        "partial",
        warnings=partial.warnings,
    )

    first = asyncio.run(runner.run(lease))

    assert first.status == "partial_failure"
    assert runtime.load_items(monitor_id) == [
        ObservedItem("new", {"price": 80}),
        *previous,
    ]
    seen_at = {
        row["item_id"]: row["last_seen_at"]
        for row in connection.execute(
            "SELECT item_id, last_seen_at FROM observations WHERE monitor_id = ?",
            (monitor_id,),
        )
    }
    assert seen_at == {
        "new": NOW.isoformat(),
        "old-a": (NOW - timedelta(hours=1)).isoformat(),
        "old-b": (NOW - timedelta(hours=1)).isoformat(),
    }
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2

    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (NOW.isoformat(), monitor_id)
    )
    lease = runtime.claim_due(worker_id="worker-1", now=NOW)[0]
    adapter.batch = ObservationBatch(
        monitor_id,
        (ObservedItem("new", {"price": 80}), *previous),
        NOW + timedelta(hours=1),
        "complete",
    )

    second = asyncio.run(runner.run(lease))

    assert second.matched_count == 0
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2


def test_empty_warning_batch_preserves_snapshot_and_uses_monitor_local_warning_date(
    connection: sqlite3.Connection,
) -> None:
    observed_at = datetime(2026, 7, 22, 16, 0, tzinfo=UTC)
    spec = make_spec(validators={"min_items": 1, "max_items": 10})
    partial = ObservationBatch(
        monitor_id="placeholder",
        items=(),
        observed_at=observed_at,
        source_hash="partial",
        warnings=(SourceWarning("agency", "fetch", "offline"),),
    )
    runner, monitor_id, _, runtime, adapter, lease = configured_runner(
        connection, partial, spec=spec
    )
    previous = (ObservedItem("old", {"price": 100}),)
    seed_snapshot(connection, ObservationBatch(monitor_id, previous, NOW, "old"))
    adapter.batch = ObservationBatch(
        monitor_id,
        (),
        observed_at,
        "partial",
        warnings=partial.warnings,
    )

    assert asyncio.run(runner.run(lease)).status == "partial_failure"
    assert runtime.load_items(monitor_id) == list(previous)
    assert connection.execute("SELECT dedupe_key FROM outbox").fetchone()[0] == (
        f"{monitor_id}:warning:2026-07-23:agency:fetch"
    )


def test_no_change_notification_uses_observation_date_in_monitor_timezone(
    connection: sqlite3.Connection,
) -> None:
    spec = make_spec(notify_on_no_change=True)
    observed_at = datetime(2026, 7, 22, 16, 0, tzinfo=UTC)
    batch = ObservationBatch(
        monitor_id="placeholder", items=(), observed_at=observed_at, source_hash="hash"
    )
    runner, monitor_id, _, _, adapter, lease = configured_runner(connection, batch, spec=spec)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id, items=(), observed_at=observed_at, source_hash="hash"
    )

    result = asyncio.run(runner.run(lease))

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


def test_rental_new_item_payload_uses_localized_details_and_item_url() -> None:
    payload = render_payload(
        make_spec(
            name="서울·경기 임대주택",
            target_url=(
                "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/"
                "selectWrtancList.do"
            ),
            source_adapter="python_plugin",
            adapter_ref="rental_housing",
            validators={
                "min_items": 0,
                "max_items": 10_000,
                "allowed_link_domains": [
                    "apply.lh.or.kr",
                    "apply.gh.or.kr",
                    "www.gh.or.kr",
                    "www.i-sh.co.kr",
                ],
            },
        ),
        ObservedItem(
            "announcement:LH:2015122300020387",
            {
                "source_id": "2015122300020387",
                "title": "오산시 행복주택 입주자격완화 예비입주자 모집(2026.07.27)",
                "agency": "LH",
                "region": "경기도",
                "housing_type": "행복주택",
                "target": "청년·신혼부부 등 행복주택 대상자",
                "announcement_date": "2026-07-27",
                "application_start_date": None,
                "application_end_date": None,
                "url": (
                    "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/"
                    "selectWrtancInfo.do?panId=2015122300020387"
                    "&ccrCnntSysDsCd=03&uppAisTpCd=06&aisTpCd=10&mi=1026"
                ),
            },
        ),
        RuleMatch(RuleKind.NEW_ITEM, None, None, None),
    )

    assert payload == {
        "text": (
            "🏠 신규 임대주택 공고\n"
            "공고 제목: 오산시 행복주택 입주자격완화 예비입주자 모집(2026.07.27)\n"
            "기관: LH\n"
            "지역: 경기도\n"
            "공급유형: 행복주택\n"
            "대상: 청년·신혼부부 등 행복주택 대상자\n"
            "공고일: 2026-07-27\n"
            "접수기간: 정보 없음\n"
            "URL: https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/"
            "selectWrtancInfo.do?panId=2015122300020387"
            "&ccrCnntSysDsCd=03&uppAisTpCd=06&aisTpCd=10&mi=1026"
        )
    }


def test_generic_payload_localizes_kind_but_omits_untrusted_item_content() -> None:
    payload = render_payload(
        make_spec(),
        ObservedItem("<script>cookie=session-secret</script>", {"html": "<b>secret</b>"}),
        RuleMatch(RuleKind.NEW_ITEM, None, None, None),
    )

    text = str(payload["text"])
    assert "종류: 신규 항목" in text
    assert "new_item" not in text
    assert "https://example.com/list" in text
    for forbidden in ("view=private", "#secret", "script", "cookie", "<b>"):
        assert forbidden not in text


def test_malformed_rental_item_falls_back_without_exposing_content() -> None:
    payload = render_payload(
        make_spec(
            source_adapter="python_plugin",
            adapter_ref="rental_housing",
        ),
        ObservedItem(
            "announcement:LH:broken",
            {
                "source_id": "broken",
                "title": "cookie=session-secret",
                "agency": "LH",
                "region": "경기도",
                "housing_type": "행복주택",
                "target": "청년",
                "announcement_date": "2026-07-27",
                "application_start_date": None,
                "application_end_date": None,
                "url": "https://0x7f.0.0.1/private",
            },
        ),
        RuleMatch(RuleKind.NEW_ITEM, None, None, None),
    )

    assert payload == {
        "text": (
            "모니터 조건에 맞는 변경이 감지되었습니다.\n"
            "종류: 신규 항목\n"
            "출처: https://example.com/list"
        )
    }


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
    runner, monitor_id, _, _, _, lease = configured_runner(
        connection, MonitorError(error_class, "fetch", secret)
    )

    result = asyncio.run(runner.run(lease))

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
    runner, monitor_id, _, _, _, lease = configured_runner(
        connection,
        MonitorError(ErrorClass.TRANSIENT_NETWORK, "fetch", "offline secret detail"),
    )

    result = asyncio.run(runner.run(lease))

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
    runner, monitor_id, _, _, _, lease = configured_runner(connection, RuntimeError(secret))

    result = asyncio.run(runner.run(lease))

    assert result.status == "failed"
    run = connection.execute("SELECT stage, error_class, error_detail FROM runs").fetchone()
    assert tuple(run) == ("internal", "internal", "internal_error")
    assert secret not in json.dumps(dict(run))
    monitor = connection.execute(
        "SELECT status, lease_owner FROM monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    assert tuple(monitor) == ("needs_review", None)


def test_mismatched_adapter_batch_fails_validation_before_snapshot_or_outbox_mutation(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mismatched = ObservationBatch(
        monitor_id="different-monitor",
        items=(ObservedItem("injected", {"price": 1}),),
        observed_at=NOW,
        source_hash="hash",
    )
    runner, monitor_id, _, runtime, _, lease = configured_runner(connection, mismatched)
    previous = ObservationBatch(
        monitor_id=monitor_id,
        items=(ObservedItem("existing", {"price": 100}),),
        observed_at=NOW,
        source_hash="old",
    )
    seed_snapshot(connection, previous)

    def forbid_previous_load(*args: object, **kwargs: object) -> list[ObservedItem]:
        raise AssertionError("previous snapshot loaded before ownership validation")

    monkeypatch.setattr(runtime, "load_items", forbid_previous_load)

    result = asyncio.run(runner.run(lease))

    assert result.status == "failed"
    rows = connection.execute(
        "SELECT item_id, fields_json FROM observations WHERE monitor_id = ?", (monitor_id,)
    ).fetchall()
    assert [(row["item_id"], row["fields_json"]) for row in rows] == [("existing", '{"price":100}')]
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    run = connection.execute("SELECT stage, error_detail FROM runs").fetchone()
    assert tuple(run) == ("validate", "validation_failed")


def test_undeclared_raw_html_and_token_are_rejected_without_any_persistence(
    connection: sqlite3.Connection,
) -> None:
    secret = "<html>token=adapter-secret</html>"
    batch = ObservationBatch(
        monitor_id="placeholder",
        items=(ObservedItem("listing-1", {"price": 900, "html": secret}),),
        observed_at=NOW,
        source_hash="hash",
    )
    runner, monitor_id, _, _, adapter, lease = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id,
        items=batch.items,
        observed_at=NOW,
        source_hash="hash",
    )

    result = asyncio.run(runner.run(lease))

    assert result.status == "failed"
    assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert secret not in "\n".join(connection.iterdump())


def test_finish_failure_is_attempted_once_and_cannot_skip_release(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = ObservationBatch(
        monitor_id="placeholder", items=(), observed_at=NOW, source_hash="hash"
    )
    runner, monitor_id, _, runtime, adapter, lease = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id, items=(), observed_at=NOW, source_hash="hash"
    )
    finish_calls = 0
    release_calls = 0
    original_release = runtime.release_lease

    def fail_finish(*args: object, **kwargs: object) -> None:
        nonlocal finish_calls
        finish_calls += 1
        raise RuntimeError("injected finish failure")

    def record_release(*args: object, **kwargs: object) -> None:
        nonlocal release_calls
        release_calls += 1
        original_release(*args, **kwargs)

    monkeypatch.setattr(runtime, "finish_run", fail_finish)
    monkeypatch.setattr(runtime, "release_lease", record_release)

    with pytest.raises(RuntimeError, match="injected finish failure"):
        asyncio.run(runner.run(lease))

    assert (finish_calls, release_calls) == (1, 1)
    run = connection.execute("SELECT status, finished_at FROM runs").fetchone()
    assert tuple(run) == ("running", None)
    monitor = connection.execute(
        "SELECT lease_owner, lease_expires_at FROM monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    assert tuple(monitor) == (None, None)


def test_transition_failure_cannot_skip_finish_or_release(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, monitor_id, _, runtime, _, lease = configured_runner(
        connection,
        MonitorError(ErrorClass.AUTHENTICATION, "fetch", "expired"),
    )
    finish_calls = 0
    release_calls = 0
    original_finish = runtime.finish_run
    original_release = runtime.release_lease

    def fail_transition(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected transition failure")

    def record_finish(*args: object, **kwargs: object) -> None:
        nonlocal finish_calls
        finish_calls += 1
        original_finish(*args, **kwargs)

    def record_release(*args: object, **kwargs: object) -> None:
        nonlocal release_calls
        release_calls += 1
        original_release(*args, **kwargs)

    monkeypatch.setattr(runtime, "transition_monitor_status", fail_transition)
    monkeypatch.setattr(runtime, "finish_run", record_finish)
    monkeypatch.setattr(runtime, "release_lease", record_release)

    with pytest.raises(RuntimeError, match="injected transition failure"):
        asyncio.run(runner.run(lease))

    assert (finish_calls, release_calls) == (1, 1)
    run = connection.execute("SELECT status, error_detail FROM runs").fetchone()
    assert tuple(run) == ("failed", "authentication_failed")
    monitor = connection.execute(
        "SELECT status, lease_owner FROM monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    assert tuple(monitor) == ("active", None)


def test_scheduling_failure_uses_aware_fallback_and_cannot_skip_release(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = ObservationBatch(
        monitor_id="placeholder", items=(), observed_at=NOW, source_hash="hash"
    )
    runner, monitor_id, _, runtime, adapter, lease = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id, items=(), observed_at=NOW, source_hash="hash"
    )
    released_at: list[datetime] = []
    original_release = runtime.release_lease

    def fail_schedule(*args: object, **kwargs: object) -> datetime:
        raise RuntimeError("injected schedule failure")

    def record_release(*args: object, **kwargs: object) -> None:
        released_at.append(kwargs["next_run_at"])
        original_release(*args, **kwargs)

    monkeypatch.setattr(runner_module, "next_run_at", fail_schedule)
    monkeypatch.setattr(runtime, "release_lease", record_release)

    result = asyncio.run(runner.run(lease))

    assert result.status == "success"
    assert released_at == [NOW + timedelta(minutes=5)]
    assert released_at[0].tzinfo is not None
    run = connection.execute("SELECT status FROM runs").fetchone()
    assert run["status"] == "success"


def test_release_failure_does_not_overwrite_success_and_lease_expires_for_recovery(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = ObservationBatch(
        monitor_id="placeholder", items=(), observed_at=NOW, source_hash="hash"
    )
    runner, monitor_id, _, runtime, adapter, lease = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id, items=(), observed_at=NOW, source_hash="hash"
    )
    finish_calls = 0
    release_calls = 0
    original_finish = runtime.finish_run

    def record_finish(*args: object, **kwargs: object) -> None:
        nonlocal finish_calls
        finish_calls += 1
        original_finish(*args, **kwargs)

    def fail_release(*args: object, **kwargs: object) -> None:
        nonlocal release_calls
        release_calls += 1
        raise sqlite3.OperationalError("injected release failure")

    monkeypatch.setattr(runtime, "finish_run", record_finish)
    monkeypatch.setattr(runtime, "release_lease", fail_release)

    with pytest.raises(sqlite3.OperationalError, match="injected release failure"):
        asyncio.run(runner.run(lease))

    assert (finish_calls, release_calls) == (1, 1)
    run = connection.execute("SELECT status, stage, error_detail FROM runs").fetchone()
    assert tuple(run) == ("success", "complete", None)
    lease = connection.execute(
        "SELECT status, lease_owner, lease_expires_at FROM monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    assert tuple(lease) == ("active", "worker-1", "2026-07-22T03:05:00+00:00")
    assert runtime.claim_due(worker_id="worker-2", now=NOW + timedelta(minutes=5)) == [
        MonitorLease(monitor_id, 2)
    ]


def test_cancellation_propagates_after_one_failed_finish_and_one_release(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancellation = asyncio.CancelledError("shutdown")
    runner, monitor_id, _, runtime, _, lease = configured_runner(connection, cancellation)
    finish_calls = 0
    release_calls = 0
    original_finish = runtime.finish_run
    original_release = runtime.release_lease

    def record_finish(*args: object, **kwargs: object) -> None:
        nonlocal finish_calls
        finish_calls += 1
        original_finish(*args, **kwargs)

    def record_release(*args: object, **kwargs: object) -> None:
        nonlocal release_calls
        release_calls += 1
        original_release(*args, **kwargs)

    monkeypatch.setattr(runtime, "finish_run", record_finish)
    monkeypatch.setattr(runtime, "release_lease", record_release)

    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(runner.run(lease))

    assert caught.value is cancellation
    assert (finish_calls, release_calls) == (1, 1)
    run = connection.execute(
        "SELECT status, stage, error_class, error_detail, finished_at FROM runs"
    ).fetchone()
    assert tuple(run)[:4] == ("failed", "cancelled", "internal", "internal_error")
    assert run["finished_at"] is not None
    monitor = connection.execute(
        "SELECT status, lease_owner, lease_expires_at, next_run_at FROM monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert tuple(monitor) == (
        "active",
        None,
        None,
        "2026-07-22T03:05:00+00:00",
    )


@pytest.mark.parametrize("cleanup_failure", ["finish", "release"])
def test_cleanup_failure_never_replaces_original_cancellation(
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    cancellation = asyncio.CancelledError("original shutdown")
    runner, monitor_id, _, runtime, _, lease = configured_runner(connection, cancellation)
    finish_calls = 0
    release_calls = 0
    original_finish = runtime.finish_run
    original_release = runtime.release_lease

    def finish(*args: object, **kwargs: object) -> None:
        nonlocal finish_calls
        finish_calls += 1
        if cleanup_failure == "finish":
            raise RuntimeError("injected finish cleanup failure")
        original_finish(*args, **kwargs)

    def release(*args: object, **kwargs: object) -> None:
        nonlocal release_calls
        release_calls += 1
        if cleanup_failure == "release":
            raise RuntimeError("injected release cleanup failure")
        original_release(*args, **kwargs)

    monkeypatch.setattr(runtime, "finish_run", finish)
    monkeypatch.setattr(runtime, "release_lease", release)

    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(runner.run(lease))

    assert caught.value is cancellation
    assert (finish_calls, release_calls) == (1, 1)


@pytest.mark.parametrize("cleanup_failure", ["finish", "release"])
def test_task_cancellation_delivered_while_shield_waits_wins_over_cleanup_failure(
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    batch = ObservationBatch(
        monitor_id="placeholder", items=(), observed_at=NOW, source_hash="hash"
    )
    runner, monitor_id, _, runtime, adapter, lease = configured_runner(connection, batch)
    adapter.batch = ObservationBatch(
        monitor_id=monitor_id, items=(), observed_at=NOW, source_hash="hash"
    )
    finish_calls = 0
    release_calls = 0
    original_finish = runtime.finish_run
    original_release = runtime.release_lease
    original_complete = runner._complete_started_run
    unhandled_contexts: list[dict[str, object]] = []

    def finish(*args: object, **kwargs: object) -> None:
        nonlocal finish_calls
        finish_calls += 1
        if cleanup_failure == "finish":
            raise RuntimeError("injected timed finish failure")
        original_finish(*args, **kwargs)

    def release(*args: object, **kwargs: object) -> None:
        nonlocal release_calls
        release_calls += 1
        if cleanup_failure == "release":
            raise RuntimeError("injected timed release failure")
        original_release(*args, **kwargs)

    async def scenario() -> None:
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()

        async def paused_cleanup(*args: object, **kwargs: object) -> object:
            cleanup_started.set()
            await allow_cleanup.wait()
            return await original_complete(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(runner, "_complete_started_run", paused_cleanup)
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled_contexts.append(dict(context)))
        task = asyncio.create_task(runner.run(lease))
        try:
            await cleanup_started.wait()
            assert not task.done()
            task.cancel("cancelled while cleanup was shielded")
            await asyncio.sleep(0)
            assert not task.done()
            allow_cleanup.set()
            await task
        finally:
            await asyncio.sleep(0)
            loop.set_exception_handler(previous_handler)

    monkeypatch.setattr(runtime, "finish_run", finish)
    monkeypatch.setattr(runtime, "release_lease", release)

    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(scenario())

    assert caught.value.args == ("cancelled while cleanup was shielded",)
    assert (finish_calls, release_calls) == (1, 1)
    assert unhandled_contexts == []
