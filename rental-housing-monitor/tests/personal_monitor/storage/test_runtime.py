from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

from personal_monitor.domain.observation import (
    ObservationBatch,
    ObservedItem,
    content_hash,
    diff_items,
)
from personal_monitor.domain.spec import MonitorSpec, MonitorStatus
from personal_monitor.storage import (
    DeliveryCandidate,
    MonitorLease,
    RegistryRepository,
    RuntimeRepository,
    open_database,
)
from tests.personal_monitor.sql_seed import seed_outbox, seed_snapshot

OUTBOX_AT = datetime(2099, 1, 1, tzinfo=UTC)


def claim_monitor(runtime: RuntimeRepository, monitor_id: str, now: datetime) -> MonitorLease:
    runtime.connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (now.isoformat(), monitor_id)
    )
    return runtime.claim_due(worker_id="worker-1", now=now)[0]


def make_spec(name: str = "가격 감시") -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": "telegram-user:1",
            "name": name,
            "target_url": "https://example.com/product/1",
            "source_adapter": "scrapling",
            "extract": {
                "item_scope": "main",
                "fields": {"price": {"selector": ".price", "type": "krw"}},
            },
            "validators": {"min_items": 0, "max_items": 10},
            "rules": [{"kind": "new_item"}],
        }
    )


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    value = open_database(":memory:")
    yield value
    value.close()


@pytest.fixture
def repositories(
    connection: sqlite3.Connection,
) -> tuple[RegistryRepository, RuntimeRepository]:
    registry = RegistryRepository(connection)
    registry.create_user("telegram-user:1", 1)
    registry.create_delivery_target("target-1", "telegram-user:1", "chat-1")
    return registry, RuntimeRepository(connection)


def test_claim_due_leases_only_active_due_monitors(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    now = datetime(2026, 1, 1, tzinfo=UTC)
    due = registry.create_monitor(make_spec("due"), created_by="telegram-user:1")
    future = registry.create_monitor(make_spec("future"), created_by="telegram-user:1")
    paused = registry.create_monitor(make_spec("paused"), created_by="telegram-user:1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?",
        ((now - timedelta(seconds=1)).isoformat(), due),
    )
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?",
        ((now + timedelta(seconds=1)).isoformat(), future),
    )
    connection.execute(
        "UPDATE monitors SET next_run_at = ?, status = ? WHERE id = ?",
        ((now - timedelta(seconds=1)).isoformat(), MonitorStatus.PAUSED_USER.value, paused),
    )

    assert runtime.claim_due(worker_id="worker-1", now=now, lease_seconds=30) == [
        MonitorLease(due, 1)
    ]
    assert runtime.claim_due(worker_id="worker-2", now=now, lease_seconds=30) == []
    lease = connection.execute(
        "SELECT lease_owner, lease_expires_at FROM monitors WHERE id = ?", (due,)
    ).fetchone()
    assert lease["lease_owner"] == "worker-1"
    assert lease["lease_expires_at"] == "2026-01-01T00:00:30+00:00"


def test_claim_monitor_for_operator_run_ignores_schedule_but_not_live_lease(
    repositories: tuple[RegistryRepository, RuntimeRepository],
    connection: sqlite3.Connection,
) -> None:
    registry, runtime = repositories
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?",
        ((now + timedelta(days=30)).isoformat(), monitor_id),
    )

    assert runtime.claim_monitor(monitor_id, worker_id="operator", now=now) == MonitorLease(
        monitor_id, 1
    )
    with pytest.raises(ValueError, match="monitor is not available"):
        runtime.claim_monitor(monitor_id, worker_id="second", now=now)


def test_runtime_exposes_no_unfenced_snapshot_or_outbox_mutation() -> None:
    assert not hasattr(RuntimeRepository, "upsert_items")
    assert not hasattr(RuntimeRepository, "enqueue_delivery")


def test_expired_lease_can_be_reclaimed(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ?, lease_owner = ?, lease_expires_at = ? WHERE id = ?",
        ((now - timedelta(minutes=1)).isoformat(), "dead-worker", now.isoformat(), monitor_id),
    )

    assert runtime.claim_due(worker_id="worker-2", now=now) == [MonitorLease(monitor_id, 1)]


def test_release_lease_requires_its_owner_and_normalizes_next_run(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (now.isoformat(), monitor_id)
    )
    lease = runtime.claim_due(worker_id="worker-1", now=now)[0]

    with pytest.raises(ValueError, match="lease generation"):
        runtime.release_lease(lease, worker_id="worker-2", next_run_at=now + timedelta(hours=1))

    korea_time = datetime(2026, 1, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
    runtime.release_lease(lease, worker_id="worker-1", next_run_at=korea_time)
    row = connection.execute(
        "SELECT next_run_at, lease_owner, lease_expires_at FROM monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert tuple(row) == ("2026-01-01T01:00:00+00:00", None, None)


def test_lease_datetimes_must_be_aware(
    repositories: tuple[RegistryRepository, RuntimeRepository],
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")

    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.claim_due(worker_id="worker-1", now=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.release_lease(
            MonitorLease(monitor_id, 1),
            worker_id="worker-1",
            next_run_at=datetime(2026, 1, 1),
        )


def test_run_lifecycle_normalizes_start_and_stores_safe_error_code(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    version_id = registry.get_active_monitor(monitor_id).version_id
    started_at = datetime(2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?",
        (started_at.astimezone(UTC).isoformat(), monitor_id),
    )
    lease = runtime.claim_due(worker_id="worker-1", now=started_at)[0]

    run_id = runtime.start_run(
        lease,
        version_id,
        worker_id="worker-1",
        fetch_strategy="auto",
        started_at=started_at,
    )
    runtime.finish_run(
        run_id,
        lease=lease,
        worker_id="worker-1",
        status="failed",
        stage="validate",
        error_class="ValidationError",
        error_detail="required_field_missing",
    )

    row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["monitor_id"] == monitor_id
    assert row["version_id"] == version_id
    assert row["started_at"] == "2026-01-01T00:00:00+00:00"
    assert row["status"] == "failed"
    assert row["stage"] == "validate"
    assert row["fetch_strategy"] == "auto"
    assert row["finished_at"] is not None
    assert row["error_class"] == "ValidationError"
    assert row["error_detail"] == "required_field_missing"


def test_run_start_requires_an_aware_datetime(
    repositories: tuple[RegistryRepository, RuntimeRepository],
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    version_id = registry.get_active_monitor(monitor_id).version_id

    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.start_run(
            MonitorLease(monitor_id, 1),
            version_id,
            worker_id="worker-1",
            fetch_strategy="auto",
            started_at=datetime(2026, 1, 1),
        )


@pytest.mark.parametrize(
    "detail",
    [
        "x" * 501,
        "GET https://example.com/private?token=secret failed",
        "Cookie: session=secret",
        "cookies={'session': 'secret'}",
        "response body: private document",
        "request failed: response.text='private document'",
        "request failed: HTTPStatusError('500 Server Error')",
        "FetchFailure('GET /private?token=secret')",
        "<html><body>private document</body></html>",
        "sessionid=secret",
        "ordinary prose is not a stable code",
    ],
)
def test_finish_run_rejects_non_code_error_detail(
    repositories: tuple[RegistryRepository, RuntimeRepository], detail: str
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    version_id = registry.get_active_monitor(monitor_id).version_id
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    runtime.connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?",
        (started_at.isoformat(), monitor_id),
    )
    lease = runtime.claim_due(worker_id="worker-1", now=started_at)[0]
    run_id = runtime.start_run(
        lease,
        version_id,
        worker_id="worker-1",
        fetch_strategy="auto",
        started_at=started_at,
    )

    with pytest.raises(ValueError, match="diagnostic code"):
        runtime.finish_run(
            run_id,
            lease=lease,
            worker_id="worker-1",
            status="failed",
            stage="fetch",
            error_detail=detail,
        )


def observation_batch(
    monitor_id: str, items: tuple[ObservedItem, ...], observed_at: datetime
) -> ObservationBatch:
    return ObservationBatch(
        monitor_id=monitor_id,
        items=items,
        observed_at=observed_at,
        source_hash="source-hash",
    )


def test_fenced_unit_replaces_snapshot_and_preserves_first_seen(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    first_at = datetime(2026, 1, 1, tzinfo=UTC)
    first_batch = observation_batch(
        monitor_id,
        (
            ObservedItem("a", {"title": "가", "price": 100}),
            ObservedItem("b", {"title": "나", "price": 200}),
        ),
        first_at,
    )
    lease = claim_monitor(runtime, monitor_id, first_at)
    runtime.apply_snapshot_and_deliveries(first_batch, (), lease=lease, worker_id="worker-1")
    second_at = datetime(2026, 1, 2, 9, 0, tzinfo=timezone(timedelta(hours=9)))

    runtime.apply_snapshot_and_deliveries(
        observation_batch(
            monitor_id,
            (
                ObservedItem("a", {"price": 90, "title": "가"}),
                ObservedItem("c", {"title": "다", "price": 300}),
            ),
            second_at,
        ),
        (),
        lease=lease,
        worker_id="worker-1",
    )

    assert runtime.load_items(monitor_id) == [
        ObservedItem("a", {"price": 90, "title": "가"}),
        ObservedItem("c", {"price": 300, "title": "다"}),
    ]
    rows = connection.execute(
        "SELECT * FROM observations WHERE monitor_id = ? ORDER BY item_id", (monitor_id,)
    ).fetchall()
    assert rows[0]["fields_json"] == '{"price":90,"title":"가"}'
    assert rows[0]["content_hash"] == content_hash({"price": 90, "title": "가"})
    assert rows[0]["first_seen_at"] == first_at.isoformat()
    assert rows[0]["last_seen_at"] == "2026-01-02T00:00:00+00:00"
    assert rows[1]["first_seen_at"] == "2026-01-02T00:00:00+00:00"


def test_empty_successful_batch_clears_the_snapshot(
    repositories: tuple[RegistryRepository, RuntimeRepository],
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    lease = claim_monitor(runtime, monitor_id, observed_at)
    runtime.apply_snapshot_and_deliveries(
        observation_batch(monitor_id, (ObservedItem("a", {"price": 100}),), observed_at),
        (),
        lease=lease,
        worker_id="worker-1",
    )

    runtime.apply_snapshot_and_deliveries(
        observation_batch(monitor_id, (), observed_at + timedelta(hours=1)),
        (),
        lease=lease,
        worker_id="worker-1",
    )

    assert runtime.load_items(monitor_id) == []


def test_invalid_observation_batch_does_not_replace_existing_snapshot(
    repositories: tuple[RegistryRepository, RuntimeRepository],
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    valid = observation_batch(
        monitor_id,
        (ObservedItem("a", {"price": 100}),),
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    lease = claim_monitor(runtime, monitor_id, valid.observed_at)
    runtime.apply_snapshot_and_deliveries(valid, (), lease=lease, worker_id="worker-1")

    with pytest.raises(ValueError, match="unique"):
        runtime.apply_snapshot_and_deliveries(
            observation_batch(
                monitor_id,
                (ObservedItem("a", {"price": 90}), ObservedItem("a", {"price": 80})),
                datetime(2026, 1, 2, tzinfo=UTC),
            ),
            (),
            lease=lease,
            worker_id="worker-1",
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.apply_snapshot_and_deliveries(
            observation_batch(monitor_id, (), datetime(2026, 1, 2)),
            (),
            lease=lease,
            worker_id="worker-1",
        )

    assert runtime.load_items(monitor_id) == [ObservedItem("a", {"price": 100})]


def test_delivery_candidate_owns_an_immutable_payload_copy() -> None:
    original = {"text": "before", "meta": {"count": 1}}

    candidate = DeliveryCandidate("key", "target-1", original)
    original["text"] = "after"
    original["meta"]["count"] = 2  # type: ignore[index]

    assert candidate.payload["text"] == "before"
    assert candidate.payload["meta"]["count"] == 1  # type: ignore[index]
    with pytest.raises(TypeError):
        candidate.payload["text"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        candidate.payload["meta"]["count"] = 3  # type: ignore[index]


def test_snapshot_and_all_delivery_candidates_commit_or_rollback_as_one_unit(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    old_batch = observation_batch(
        monitor_id,
        (ObservedItem("listing", {"price": 100}),),
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    new_batch = observation_batch(
        monitor_id,
        (ObservedItem("listing", {"price": 90}),),
        datetime(2026, 1, 2, tzinfo=UTC),
    )
    seed_snapshot(connection, old_batch)
    candidates = (
        DeliveryCandidate("first", "target-1", {"text": "first"}),
        DeliveryCandidate("fail", "target-1", {"text": "second"}),
    )
    connection.execute(
        "CREATE TRIGGER reject_injected_delivery BEFORE INSERT ON outbox "
        "WHEN NEW.dedupe_key = 'fail' BEGIN SELECT RAISE(ABORT, 'injected enqueue'); END"
    )
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?",
        (new_batch.observed_at.isoformat(), monitor_id),
    )
    lease = runtime.claim_due(worker_id="worker-1", now=new_batch.observed_at)[0]

    with pytest.raises(sqlite3.IntegrityError, match="injected enqueue"):
        runtime.apply_snapshot_and_deliveries(
            new_batch, candidates, lease=lease, worker_id="worker-1"
        )

    assert runtime.load_items(monitor_id) == list(old_batch.items)
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    retry_changes = diff_items(runtime.load_items(monitor_id), list(new_batch.items))
    assert retry_changes[0].changed_fields["price"] == (100, 90)

    connection.execute("DROP TRIGGER reject_injected_delivery")
    outbox_ids = runtime.apply_snapshot_and_deliveries(
        new_batch, candidates, lease=lease, worker_id="worker-1"
    )

    assert len(outbox_ids) == 2
    assert runtime.load_items(monitor_id) == list(new_batch.items)
    assert [
        row["dedupe_key"]
        for row in connection.execute("SELECT dedupe_key FROM outbox ORDER BY rowid")
    ] == ["first", "fail"]


def test_delivery_key_is_idempotent_and_payload_json_is_deterministic(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")

    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    lease = claim_monitor(runtime, monitor_id, observed_at)
    candidate = DeliveryCandidate(
        "m1:p1:numeric_threshold:price:99000",
        "target-1",
        {"text": "가격이 99,000원입니다", "meta": {"b": 2, "a": 1}},
    )
    batch = observation_batch(monitor_id, (), observed_at)
    first = runtime.apply_snapshot_and_deliveries(
        batch, (candidate,), lease=lease, worker_id="worker-1"
    )[0]
    second = runtime.apply_snapshot_and_deliveries(
        batch,
        (DeliveryCandidate(candidate.dedupe_key, "target-1", {"text": "ignored duplicate"}),),
        lease=lease,
        worker_id="worker-1",
    )[0]

    assert first == second
    row = connection.execute("SELECT payload_json FROM outbox").fetchone()
    assert row["payload_json"] == ('{"meta":{"a":1,"b":2},"text":"가격이 99,000원입니다"}')
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_claim_due_outbox_orders_retries_and_delivery_removes_item_from_due_work(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    later = seed_outbox(
        connection,
        dedupe_key="later",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "later"},
        available_at=OUTBOX_AT,
    )
    first = seed_outbox(
        connection,
        dedupe_key="first",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "first"},
        available_at=OUTBOX_AT,
    )
    base = datetime(2099, 1, 1, tzinfo=UTC)
    connection.execute(
        "UPDATE outbox SET available_at = ?, attempt_count = 1 WHERE id = ?",
        ((base + timedelta(minutes=1)).isoformat(), later),
    )
    connection.execute(
        "UPDATE outbox SET available_at = ?, attempt_count = 1 WHERE id = ?",
        (base.isoformat(), first),
    )

    rows = runtime.claim_due_outbox(worker_id="worker-1", now=base + timedelta(minutes=1), limit=1)

    assert rows[0].id == first
    assert rows[0].target_id == "target-1"
    assert rows[0].payload == {"text": "first"}
    assert rows[0].attempt_count == 1
    runtime.mark_delivered(first, worker_id="worker-1", message_id="telegram-42", delivered_at=base)
    assert [
        row.id
        for row in runtime.claim_due_outbox(worker_id="worker-1", now=base + timedelta(minutes=1))
    ] == [later]
    delivery = connection.execute(
        "SELECT * FROM deliveries WHERE outbox_id = ?", (first,)
    ).fetchone()
    assert (delivery["target_id"], delivery["external_message_id"], delivery["delivered_at"]) == (
        "target-1",
        "telegram-42",
        base.isoformat(),
    )


def test_claim_due_outbox_can_be_scoped_to_one_operator_selected_monitor(
    repositories: tuple[RegistryRepository, RuntimeRepository],
    connection: sqlite3.Connection,
) -> None:
    registry, runtime = repositories
    selected = registry.create_monitor(make_spec("selected"), created_by="telegram-user:1")
    other = registry.create_monitor(make_spec("other"), created_by="telegram-user:1")
    selected_outbox = seed_outbox(
        connection,
        dedupe_key="selected",
        monitor_id=selected,
        target_id="target-1",
        payload={"text": "selected"},
        available_at=OUTBOX_AT,
    )
    seed_outbox(
        connection,
        dedupe_key="other",
        monitor_id=other,
        target_id="target-1",
        payload={"text": "other"},
        available_at=OUTBOX_AT,
    )

    rows = runtime.claim_due_outbox(
        worker_id="operator",
        now=OUTBOX_AT,
        monitor_id=selected,
    )

    assert [row.id for row in rows] == [selected_outbox]
    assert (
        connection.execute(
            "SELECT lease_owner FROM outbox WHERE monitor_id = ?",
            (other,),
        ).fetchone()[0]
        is None
    )


def test_claim_due_outbox_exact_ids_excludes_older_same_monitor_rows(
    repositories: tuple[RegistryRepository, RuntimeRepository],
    connection: sqlite3.Connection,
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    older = seed_outbox(
        connection,
        dedupe_key="older",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "older"},
        available_at=OUTBOX_AT,
    )
    produced = seed_outbox(
        connection,
        dedupe_key="produced",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "produced"},
        available_at=OUTBOX_AT,
    )

    rows = runtime.claim_due_outbox(
        worker_id="operator",
        now=OUTBOX_AT,
        outbox_ids=(produced,),
    )

    assert [row.id for row in rows] == [produced]
    assert (
        connection.execute(
            "SELECT lease_owner FROM outbox WHERE id = ?",
            (older,),
        ).fetchone()[0]
        is None
    )
    assert (
        runtime.claim_due_outbox(
            worker_id="empty",
            now=OUTBOX_AT,
            outbox_ids=(),
        )
        == []
    )


def test_two_workers_cannot_claim_the_same_unexpired_outbox_lease(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    outbox_id = seed_outbox(
        connection,
        dedupe_key="exclusive",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "exclusive"},
        available_at=OUTBOX_AT,
    )
    now = datetime(2099, 1, 1, tzinfo=UTC)
    connection.execute("UPDATE outbox SET available_at = ?", (now.isoformat(),))

    assert [
        row.id for row in runtime.claim_due_outbox(worker_id="worker-1", now=now, lease_seconds=30)
    ] == [outbox_id]
    assert runtime.claim_due_outbox(worker_id="worker-2", now=now, lease_seconds=30) == []
    row = connection.execute(
        "SELECT lease_owner, lease_expires_at FROM outbox WHERE id = ?", (outbox_id,)
    ).fetchone()
    assert tuple(row) == ("worker-1", "2099-01-01T00:00:30+00:00")


def test_expired_outbox_lease_is_recoverable_by_another_worker(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    outbox_id = seed_outbox(
        connection,
        dedupe_key="recoverable",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "recoverable"},
        available_at=OUTBOX_AT,
    )
    now = datetime(2099, 1, 1, tzinfo=UTC)
    connection.execute("UPDATE outbox SET available_at = ?", (now.isoformat(),))
    runtime.claim_due_outbox(worker_id="worker-1", now=now, lease_seconds=30)

    assert (
        runtime.claim_due_outbox(
            worker_id="worker-2", now=now + timedelta(seconds=29), lease_seconds=30
        )
        == []
    )
    recovered = runtime.claim_due_outbox(
        worker_id="worker-2", now=now + timedelta(seconds=30), lease_seconds=30
    )

    assert [row.id for row in recovered] == [outbox_id]
    lease = connection.execute(
        "SELECT lease_owner, lease_expires_at FROM outbox WHERE id = ?", (outbox_id,)
    ).fetchone()
    assert tuple(lease) == ("worker-2", "2099-01-01T00:01:00+00:00")


def test_outbox_completion_and_retry_require_claiming_worker(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    outbox_id = seed_outbox(
        connection,
        dedupe_key="owned",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "owned"},
        available_at=OUTBOX_AT,
    )
    now = datetime(2099, 1, 1, tzinfo=UTC)
    connection.execute("UPDATE outbox SET available_at = ?", (now.isoformat(),))
    runtime.claim_due_outbox(worker_id="worker-1", now=now)

    with pytest.raises(ValueError, match="claiming worker"):
        runtime.mark_delivered(
            outbox_id, worker_id="worker-2", message_id="message", delivered_at=now
        )
    with pytest.raises(ValueError, match="claiming worker"):
        runtime.reschedule_outbox(
            outbox_id,
            worker_id="worker-2",
            available_at=now + timedelta(minutes=1),
            error="timeout",
        )

    row = connection.execute(
        "SELECT status, attempt_count, lease_owner FROM outbox WHERE id = ?", (outbox_id,)
    ).fetchone()
    assert tuple(row) == ("pending", 0, "worker-1")


def test_outbox_retry_updates_attempt_and_rejects_unsafe_or_naive_values(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    outbox_id = seed_outbox(
        connection,
        dedupe_key="retry",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "retry"},
        available_at=OUTBOX_AT,
    )
    claim_at = datetime(2099, 1, 1, tzinfo=UTC)
    connection.execute("UPDATE outbox SET available_at = ?", (claim_at.isoformat(),))
    runtime.claim_due_outbox(worker_id="worker-1", now=claim_at)

    with pytest.raises(ValueError, match="diagnostic code"):
        runtime.reschedule_outbox(
            outbox_id,
            worker_id="worker-1",
            available_at=datetime(2026, 1, 1, tzinfo=UTC),
            error="GET https://example.com/path?secret=1",
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.reschedule_outbox(
            outbox_id,
            worker_id="worker-1",
            available_at=datetime(2026, 1, 1),
            error="timeout",
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.claim_due_outbox(worker_id="worker-1", now=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.mark_delivered(
            outbox_id,
            worker_id="worker-1",
            message_id="message",
            delivered_at=datetime(2026, 1, 1),
        )

    row = connection.execute(
        "SELECT attempt_count, last_error FROM outbox WHERE id = ?", (outbox_id,)
    ).fetchone()
    assert tuple(row) == (0, None)
