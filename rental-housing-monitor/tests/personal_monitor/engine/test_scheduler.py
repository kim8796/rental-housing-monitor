from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.engine.scheduler import Scheduler, next_run_at, stable_jitter_seconds
from personal_monitor.storage import (
    MonitorLease,
    RegistryRepository,
    RuntimeRepository,
    open_database,
)


def make_spec(*, name: str = "가격 감시", schedule: str = "0 12 * * *") -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": "telegram-user:1",
            "name": name,
            "target_url": "https://example.com/product/1",
            "source_adapter": "scrapling",
            "schedule": schedule,
            "timezone": "Asia/Seoul",
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


def test_stable_jitter_is_deterministic_and_bounded() -> None:
    jitter = stable_jitter_seconds("monitor-7")

    assert stable_jitter_seconds("monitor-7") == jitter
    assert 0 <= jitter <= 120


def test_next_run_uses_monitor_timezone_and_stable_jitter() -> None:
    result = next_run_at(
        make_spec(),
        "monitor-7",
        datetime(2026, 7, 22, 0, 1, tzinfo=UTC),
    )

    assert result == datetime(2026, 7, 22, 3, 0, tzinfo=UTC) + timedelta(
        seconds=stable_jitter_seconds("monitor-7")
    )


def test_next_run_keeps_rental_monitor_at_its_exact_cron_time() -> None:
    result = next_run_at(
        make_spec(name="renamed rental", schedule="13 12 * * *"),
        "rental-housing-seoul-gyeonggi",
        datetime(2026, 7, 22, 0, 1, tzinfo=UTC),
    )

    assert result == datetime(2026, 7, 22, 3, 13, tzinfo=UTC)


def test_next_run_jitters_same_named_ordinary_monitor() -> None:
    monitor_id = "ordinary-monitor"
    result = next_run_at(
        make_spec(name="서울·경기 임대주택", schedule="13 12 * * *"),
        monitor_id,
        datetime(2026, 7, 22, 0, 1, tzinfo=UTC),
    )

    assert result == datetime(2026, 7, 22, 3, 13, tzinfo=UTC) + timedelta(
        seconds=stable_jitter_seconds(monitor_id)
    )


def test_next_run_rejects_naive_after() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_run_at(make_spec(), "monitor-7", datetime(2026, 7, 22, 0, 1))


def test_tick_claims_due_monitor_once(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    now = datetime(2026, 7, 22, tzinfo=UTC)
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (now.isoformat(), monitor_id)
    )
    scheduler = Scheduler(runtime, worker_id="worker-a")

    assert scheduler.tick(now) == [MonitorLease(monitor_id, 1)]
    assert scheduler.tick(now) == []


def test_tick_excludes_monitor_with_unexpired_lease(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    now = datetime(2026, 7, 22, tzinfo=UTC)
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ?, lease_owner = ?, lease_expires_at = ? WHERE id = ?",
        (now.isoformat(), "worker-a", (now + timedelta(seconds=300)).isoformat(), monitor_id),
    )

    assert Scheduler(runtime, worker_id="worker-b").tick(now) == []


def test_tick_reclaims_monitor_after_lease_expiry(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    now = datetime(2026, 7, 22, tzinfo=UTC)
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ?, lease_owner = ?, lease_expires_at = ? WHERE id = ?",
        (now.isoformat(), "worker-a", now.isoformat(), monitor_id),
    )

    assert Scheduler(runtime, worker_id="worker-b").tick(now) == [MonitorLease(monitor_id, 1)]


def test_release_lease_rejects_a_different_worker(
    repositories: tuple[RegistryRepository, RuntimeRepository], connection: sqlite3.Connection
) -> None:
    registry, runtime = repositories
    now = datetime(2026, 7, 22, tzinfo=UTC)
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    connection.execute(
        "UPDATE monitors SET next_run_at = ? WHERE id = ?", (now.isoformat(), monitor_id)
    )
    lease = Scheduler(runtime, worker_id="worker-a").tick(now)[0]

    with pytest.raises(ValueError, match="lease generation"):
        runtime.release_lease(lease, worker_id="worker-b", next_run_at=now + timedelta(hours=1))
