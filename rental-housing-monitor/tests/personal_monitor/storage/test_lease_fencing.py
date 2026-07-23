from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from personal_monitor.domain.observation import ObservationBatch, ObservedItem
from personal_monitor.domain.spec import MonitorSpec, MonitorStatus
from personal_monitor.engine.scheduler import next_run_at
from personal_monitor.storage import (
    DeliveryCandidate,
    MonitorLease,
    RegistryRepository,
    RuntimeRepository,
    open_database,
)
from tests.personal_monitor.sql_seed import seed_snapshot

NOW = datetime(2026, 7, 22, 3, 0, tzinfo=UTC)


def make_spec() -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": "owner-1",
            "name": "lease monitor",
            "target_url": "https://example.com/items",
            "source_adapter": "scrapling",
            "extract": {
                "item_scope": "main",
                "fields": {"price": {"selector": ".price", "type": "krw"}},
            },
            "validators": {"min_items": 0, "max_items": 10},
            "rules": [{"kind": "new_item"}],
        }
    )


def test_reclaimed_worker_cannot_commit_or_cleanup_newer_worker_state() -> None:
    connection = open_database(":memory:")
    registry = RegistryRepository(connection)
    runtime = RuntimeRepository(connection)
    registry.create_user("owner-1", 1)
    registry.create_delivery_target("target-1", "owner-1", "chat-1")
    monitor_id = registry.create_monitor(make_spec(), created_by="owner-1")
    version_id = registry.get_active_monitor(monitor_id).version_id
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (NOW.isoformat(), monitor_id)
    )

    lease_a = runtime.claim_due(worker_id="worker-a", now=NOW, lease_seconds=30)[0]
    assert lease_a == MonitorLease(monitor_id=monitor_id, generation=1)
    run_a = runtime.start_run(
        lease_a,
        version_id,
        worker_id="worker-a",
        fetch_strategy="auto",
        started_at=NOW,
    )
    lease_b = runtime.claim_due(
        worker_id="worker-b", now=NOW + timedelta(seconds=30), lease_seconds=30
    )[0]
    assert lease_b == MonitorLease(monitor_id=monitor_id, generation=2)
    run_b = runtime.start_run(
        lease_b,
        version_id,
        worker_id="worker-b",
        fetch_strategy="auto",
        started_at=NOW + timedelta(seconds=30),
    )
    batch_b = ObservationBatch(
        monitor_id=monitor_id,
        items=(ObservedItem("b", {"price": 80}),),
        observed_at=NOW + timedelta(seconds=31),
        source_hash="b",
    )
    runtime.apply_snapshot_and_deliveries(
        batch_b,
        (DeliveryCandidate("b:new", "target-1", {"text": "worker b"}),),
        lease=lease_b,
        worker_id="worker-b",
    )
    runtime.finish_run(
        run_b,
        lease=lease_b,
        worker_id="worker-b",
        status="success",
        stage="complete",
    )

    stale_batch = ObservationBatch(
        monitor_id=monitor_id,
        items=(ObservedItem("a", {"price": 100}),),
        observed_at=NOW + timedelta(seconds=32),
        source_hash="a",
    )
    with pytest.raises(ValueError, match="lease generation"):
        runtime.apply_snapshot_and_deliveries(
            stale_batch,
            (DeliveryCandidate("a:new", "target-1", {"text": "worker a"}),),
            lease=lease_a,
            worker_id="worker-a",
        )
    with pytest.raises(ValueError, match="lease generation"):
        runtime.transition_monitor_status(
            lease_a,
            worker_id="worker-a",
            expected=MonitorStatus.ACTIVE,
            target=MonitorStatus.NEEDS_REVIEW,
        )
    with pytest.raises(ValueError, match="lease generation"):
        runtime.finish_run(
            run_a,
            lease=lease_a,
            worker_id="worker-a",
            status="failed",
            stage="complete",
            error_class="internal",
            error_detail="internal_error",
        )
    with pytest.raises(ValueError, match="lease generation"):
        runtime.release_lease(
            lease_a,
            worker_id="worker-a",
            next_run_at=NOW + timedelta(hours=1),
        )

    assert runtime.load_items(monitor_id) == [ObservedItem("b", {"price": 80})]
    assert [
        tuple(row)
        for row in connection.execute(
            "SELECT dedupe_key, payload_json FROM outbox ORDER BY dedupe_key"
        )
    ] == [("b:new", '{"text":"worker b"}')]
    monitor = connection.execute(
        "SELECT status, lease_owner, lease_generation, next_run_at FROM monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert tuple(monitor) == ("active", "worker-b", 2, NOW.isoformat())

    runtime.release_lease(
        lease_b,
        worker_id="worker-b",
        next_run_at=NOW + timedelta(hours=2),
    )
    connection.close()


def test_atomic_enqueue_rejects_cross_owner_target_and_rolls_back_snapshot() -> None:
    connection = open_database(":memory:")
    registry = RegistryRepository(connection)
    runtime = RuntimeRepository(connection)
    registry.create_user("owner-1", 1)
    registry.create_user("owner-2", 2)
    registry.create_delivery_target("target-1", "owner-1", "chat-1")
    registry.create_delivery_target("target-2", "owner-2", "chat-2")
    monitor_id = registry.create_monitor(make_spec(), created_by="owner-1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (NOW.isoformat(), monitor_id)
    )
    lease = runtime.claim_due(worker_id="worker-1", now=NOW)[0]
    old = ObservationBatch(
        monitor_id=monitor_id,
        items=(ObservedItem("old", {"price": 100}),),
        observed_at=NOW,
        source_hash="old",
    )
    seed_snapshot(connection, old)
    new = ObservationBatch(
        monitor_id=monitor_id,
        items=(ObservedItem("new", {"price": 90}),),
        observed_at=NOW + timedelta(minutes=1),
        source_hash="new",
    )

    with pytest.raises(ValueError, match="target owner"):
        runtime.apply_snapshot_and_deliveries(
            new,
            (DeliveryCandidate("cross-owner", "target-2", {"text": "unsafe"}),),
            lease=lease,
            worker_id="worker-1",
        )

    assert runtime.load_items(monitor_id) == list(old.items)
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    connection.close()


def test_owner_pause_fences_old_worker_and_resume_recomputes_next_run() -> None:
    connection = open_database(":memory:")
    registry = RegistryRepository(connection)
    runtime = RuntimeRepository(connection)
    registry.create_user("owner-1", 1)
    registry.create_delivery_target("target-1", "owner-1", "chat-1")
    monitor_id = registry.create_monitor(make_spec(), created_by="owner-1")
    active = registry.get_active_monitor(monitor_id)
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (NOW.isoformat(), monitor_id)
    )
    lease = runtime.claim_due(worker_id="worker-a", now=NOW)[0]

    changed_at = NOW + timedelta(minutes=1)
    registry.transition_status_exact(
        monitor_id,
        owner_id="owner-1",
        expected_status=MonitorStatus.ACTIVE,
        expected_active_version_id=active.version_id,
        target_status=MonitorStatus.PAUSED_USER,
        changed_at=changed_at,
    )

    stale = ObservationBatch(
        monitor_id=monitor_id,
        items=(ObservedItem("stale", {"price": 10}),),
        observed_at=changed_at,
        source_hash="stale",
    )
    with pytest.raises(ValueError, match="lease generation"):
        runtime.apply_snapshot_and_deliveries(
            stale,
            (DeliveryCandidate("stale:new", "target-1", {"text": "stale"}),),
            lease=lease,
            worker_id="worker-a",
        )
    paused = connection.execute(
        "SELECT lease_owner, lease_expires_at, lease_generation, next_run_at "
        "FROM monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert tuple(paused) == (None, None, lease.generation + 1, None)
    assert connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0

    resumed_at = NOW + timedelta(minutes=2)
    registry.transition_status_exact(
        monitor_id,
        owner_id="owner-1",
        expected_status=MonitorStatus.PAUSED_USER,
        expected_active_version_id=active.version_id,
        target_status=MonitorStatus.ACTIVE,
        changed_at=resumed_at,
    )
    resumed = connection.execute(
        "SELECT lease_owner, lease_expires_at, lease_generation, next_run_at "
        "FROM monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert tuple(resumed) == (
        None,
        None,
        lease.generation + 2,
        next_run_at(active.spec, monitor_id, resumed_at).isoformat(),
    )
    connection.close()
