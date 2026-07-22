from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

from personal_monitor.domain.observation import ObservationBatch, ObservedItem, content_hash
from personal_monitor.domain.spec import MonitorSpec, MonitorStatus
from personal_monitor.storage import RegistryRepository, RuntimeRepository, open_database


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

    assert runtime.claim_due(worker_id="worker-1", now=now, lease_seconds=30) == [due]
    assert runtime.claim_due(worker_id="worker-2", now=now, lease_seconds=30) == []
    lease = connection.execute(
        "SELECT lease_owner, lease_expires_at FROM monitors WHERE id = ?", (due,)
    ).fetchone()
    assert lease["lease_owner"] == "worker-1"
    assert lease["lease_expires_at"] == "2026-01-01T00:00:30+00:00"


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

    assert runtime.claim_due(worker_id="worker-2", now=now) == [monitor_id]


def test_release_lease_requires_its_owner_and_normalizes_next_run(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (now.isoformat(), monitor_id)
    )
    runtime.claim_due(worker_id="worker-1", now=now)

    with pytest.raises(ValueError, match="lease owner"):
        runtime.release_lease(
            monitor_id, worker_id="worker-2", next_run_at=now + timedelta(hours=1)
        )

    korea_time = datetime(2026, 1, 1, 10, 0, tzinfo=timezone(timedelta(hours=9)))
    runtime.release_lease(monitor_id, worker_id="worker-1", next_run_at=korea_time)
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
        runtime.release_lease(monitor_id, worker_id="worker-1", next_run_at=datetime(2026, 1, 1))


def test_run_lifecycle_normalizes_start_and_stores_safe_error_code(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    version_id = registry.get_active_monitor(monitor_id).version_id
    started_at = datetime(2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=9)))

    run_id = runtime.start_run(monitor_id, version_id, started_at=started_at)
    runtime.finish_run(
        run_id,
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
        runtime.start_run(monitor_id, version_id, started_at=datetime(2026, 1, 1))


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
    run_id = runtime.start_run(monitor_id, version_id, started_at=datetime(2026, 1, 1, tzinfo=UTC))

    with pytest.raises(ValueError, match="diagnostic code"):
        runtime.finish_run(run_id, status="failed", stage="fetch", error_detail=detail)


def observation_batch(
    monitor_id: str, items: tuple[ObservedItem, ...], observed_at: datetime
) -> ObservationBatch:
    return ObservationBatch(
        monitor_id=monitor_id,
        items=items,
        observed_at=observed_at,
        source_hash="source-hash",
    )


def test_upsert_items_replaces_snapshot_and_preserves_first_seen(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    first_at = datetime(2026, 1, 1, tzinfo=UTC)
    runtime.upsert_items(
        observation_batch(
            monitor_id,
            (
                ObservedItem("a", {"title": "가", "price": 100}),
                ObservedItem("b", {"title": "나", "price": 200}),
            ),
            first_at,
        )
    )
    second_at = datetime(2026, 1, 2, 9, 0, tzinfo=timezone(timedelta(hours=9)))

    runtime.upsert_items(
        observation_batch(
            monitor_id,
            (
                ObservedItem("a", {"price": 90, "title": "가"}),
                ObservedItem("c", {"title": "다", "price": 300}),
            ),
            second_at,
        )
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
    runtime.upsert_items(
        observation_batch(monitor_id, (ObservedItem("a", {"price": 100}),), observed_at)
    )

    runtime.upsert_items(observation_batch(monitor_id, (), observed_at + timedelta(hours=1)))

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
    runtime.upsert_items(valid)

    with pytest.raises(ValueError, match="unique"):
        runtime.upsert_items(
            observation_batch(
                monitor_id,
                (ObservedItem("a", {"price": 90}), ObservedItem("a", {"price": 80})),
                datetime(2026, 1, 2, tzinfo=UTC),
            )
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.upsert_items(observation_batch(monitor_id, (), datetime(2026, 1, 2)))

    assert runtime.load_items(monitor_id) == [ObservedItem("a", {"price": 100})]


def test_delivery_key_is_idempotent_and_payload_json_is_deterministic(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")

    first = runtime.enqueue_delivery(
        dedupe_key="m1:p1:numeric_threshold:price:99000",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "가격이 99,000원입니다", "meta": {"b": 2, "a": 1}},
    )
    second = runtime.enqueue_delivery(
        dedupe_key="m1:p1:numeric_threshold:price:99000",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "ignored duplicate"},
    )

    assert first == second
    row = connection.execute("SELECT payload_json FROM outbox").fetchone()
    assert row["payload_json"] == ('{"meta":{"a":1,"b":2},"text":"가격이 99,000원입니다"}')
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_due_outbox_orders_retries_and_delivery_removes_item_from_due_work(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    later = runtime.enqueue_delivery(
        dedupe_key="later",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "later"},
    )
    first = runtime.enqueue_delivery(
        dedupe_key="first",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "first"},
    )
    base = datetime(2099, 1, 1, tzinfo=UTC)
    runtime.reschedule_outbox(later, available_at=base + timedelta(minutes=1), error="timeout")
    runtime.reschedule_outbox(first, available_at=base, error="timeout")

    rows = runtime.due_outbox(now=base + timedelta(minutes=1), limit=1)

    assert rows[0].id == first
    assert rows[0].target_id == "target-1"
    assert rows[0].payload == {"text": "first"}
    assert rows[0].attempt_count == 1
    runtime.mark_delivered(first, message_id="telegram-42", delivered_at=base)
    runtime.mark_delivered(first, message_id="telegram-42", delivered_at=base)
    assert [row.id for row in runtime.due_outbox(now=base + timedelta(minutes=1))] == [later]
    delivery = connection.execute(
        "SELECT * FROM deliveries WHERE outbox_id = ?", (first,)
    ).fetchone()
    assert (delivery["target_id"], delivery["external_message_id"], delivery["delivered_at"]) == (
        "target-1",
        "telegram-42",
        base.isoformat(),
    )


def test_outbox_retry_updates_attempt_and_rejects_unsafe_or_naive_values(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    outbox_id = runtime.enqueue_delivery(
        dedupe_key="retry",
        monitor_id=monitor_id,
        target_id="target-1",
        payload={"text": "retry"},
    )

    with pytest.raises(ValueError, match="diagnostic code"):
        runtime.reschedule_outbox(
            outbox_id,
            available_at=datetime(2026, 1, 1, tzinfo=UTC),
            error="GET https://example.com/path?secret=1",
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.reschedule_outbox(outbox_id, available_at=datetime(2026, 1, 1), error="timeout")
    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.due_outbox(now=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        runtime.mark_delivered(outbox_id, message_id="message", delivered_at=datetime(2026, 1, 1))

    row = connection.execute(
        "SELECT attempt_count, last_error FROM outbox WHERE id = ?", (outbox_id,)
    ).fetchone()
    assert tuple(row) == (0, None)
