from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from personal_monitor.maintenance import Maintenance
from personal_monitor.storage import open_database


@pytest.fixture
def connection() -> sqlite3.Connection:
    value = open_database(":memory:")
    value.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) VALUES "
        "('owner', 1, 'active', '2026-01-01T00:00:00+00:00')"
    )
    value.execute(
        "INSERT INTO delivery_targets(id, owner_id, kind, address, created_at) VALUES "
        "('target', 'owner', 'telegram', 'chat', '2026-01-01T00:00:00+00:00')"
    )
    yield value
    value.close()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 1, tzinfo=UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _add_monitor(
    connection: sqlite3.Connection,
    identifier: str,
    *,
    status: str = "active",
    disabled_at: datetime | None = None,
) -> None:
    timestamp = "2026-01-01T00:00:00+00:00"
    connection.execute(
        "INSERT INTO monitors(id, owner_id, name, status, disabled_at, created_at, updated_at) "
        "VALUES (?, 'owner', ?, ?, ?, ?, ?)",
        (
            identifier,
            identifier,
            status,
            _timestamp(disabled_at) if disabled_at else None,
            timestamp,
            timestamp,
        ),
    )


def _add_outbox(
    connection: sqlite3.Connection,
    identifier: str,
    monitor_id: str,
    *,
    delivered_at: datetime | None = None,
) -> None:
    timestamp = "2026-01-01T00:00:00+00:00"
    status = "delivered" if delivered_at else "pending"
    connection.execute(
        "INSERT INTO outbox(id, dedupe_key, monitor_id, target_id, payload_json, status, "
        "available_at, created_at) VALUES (?, ?, ?, 'target', '{}', ?, ?, ?)",
        (identifier, identifier, monitor_id, status, timestamp, timestamp),
    )
    if delivered_at:
        connection.execute(
            "INSERT INTO deliveries(outbox_id, target_id, external_message_id, delivered_at) "
            "VALUES (?, 'target', ?, ?)",
            (identifier, identifier, _timestamp(delivered_at)),
        )


def _exists(
    connection: sqlite3.Connection, table: str, identifier: str, column: str = "id"
) -> bool:
    return (
        connection.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (identifier,)).fetchone()
        is not None
    )


def test_removes_runs_by_finish_or_fallback_start_strictly_older_than_ninety_days(
    connection: sqlite3.Connection, now: datetime
) -> None:
    _add_monitor(connection, "monitor")
    cutoff = now - timedelta(days=90)
    connection.executemany(
        "INSERT INTO runs(id, monitor_id, version_id, stage, status, started_at, finished_at) "
        "VALUES (?, 'monitor', 'version', 'done', ?, ?, ?)",
        [
            (
                "old",
                "succeeded",
                _timestamp(cutoff - timedelta(microseconds=1)),
                _timestamp(cutoff - timedelta(microseconds=1)),
            ),
            ("boundary", "succeeded", _timestamp(cutoff), _timestamp(cutoff)),
            ("unfinished", "running", _timestamp(cutoff - timedelta(days=1)), None),
        ],
    )

    Maintenance(connection).run(now=now)

    assert not _exists(connection, "runs", "old")
    assert _exists(connection, "runs", "boundary")
    assert not _exists(connection, "runs", "unfinished")


def test_removes_delivery_before_its_delivered_outbox_strictly_after_window(
    connection: sqlite3.Connection, now: datetime
) -> None:
    _add_monitor(connection, "monitor")
    cutoff = now - timedelta(days=180)
    _add_outbox(connection, "old", "monitor", delivered_at=cutoff - timedelta(microseconds=1))
    _add_outbox(connection, "boundary", "monitor", delivered_at=cutoff)

    Maintenance(connection).run(now=now)

    assert not _exists(connection, "deliveries", "old", "outbox_id")
    assert not _exists(connection, "outbox", "old")
    assert _exists(connection, "deliveries", "boundary", "outbox_id")
    assert _exists(connection, "outbox", "boundary")


def test_removes_only_pending_actions_strictly_older_than_one_day(
    connection: sqlite3.Connection, now: datetime
) -> None:
    cutoff = now - timedelta(days=1)
    connection.executemany(
        "INSERT INTO pending_actions("
        "token_hash, owner_id, action, payload_json, expires_at, consumed_at"
        ") "
        "VALUES (?, 'owner', 'approve', '{}', ?, ?)",
        [
            (
                "consumed-old",
                _timestamp(now + timedelta(days=2)),
                _timestamp(cutoff - timedelta(microseconds=1)),
            ),
            ("consumed-boundary", _timestamp(now + timedelta(days=2)), _timestamp(cutoff)),
            ("expired-old", _timestamp(cutoff - timedelta(microseconds=1)), None),
            ("expired-boundary", _timestamp(cutoff), None),
        ],
    )

    Maintenance(connection).run(now=now)

    assert not _exists(connection, "pending_actions", "consumed-old", "token_hash")
    assert _exists(connection, "pending_actions", "consumed-boundary", "token_hash")
    assert not _exists(connection, "pending_actions", "expired-old", "token_hash")
    assert _exists(connection, "pending_actions", "expired-boundary", "token_hash")


def test_removes_old_diagnostic_snapshots_only_when_the_optional_table_exists(
    connection: sqlite3.Connection, now: datetime
) -> None:
    Maintenance(connection).run(now=now)
    connection.execute(
        "CREATE TABLE diagnostic_snapshots(id TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
    )
    cutoff = now - timedelta(days=7)
    connection.executemany(
        "INSERT INTO diagnostic_snapshots(id, created_at) VALUES (?, ?)",
        [("old", _timestamp(cutoff - timedelta(microseconds=1))), ("boundary", _timestamp(cutoff))],
    )

    Maintenance(connection).run(now=now)

    assert not _exists(connection, "diagnostic_snapshots", "old")
    assert _exists(connection, "diagnostic_snapshots", "boundary")


def test_removes_old_disabled_monitor_dependencies_without_removing_shared_owner_data(
    connection: sqlite3.Connection, now: datetime
) -> None:
    cutoff = now - timedelta(days=30)
    _add_monitor(
        connection, "old", status="disabled", disabled_at=cutoff - timedelta(microseconds=1)
    )
    _add_monitor(connection, "boundary", status="disabled", disabled_at=cutoff)
    connection.execute(
        "INSERT INTO monitor_versions("
        "id, monitor_id, version_number, spec_json, created_by, created_at"
        ") "
        "VALUES ('old-version', 'old', 1, '{}', 'owner', '2026-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO observations("
        "monitor_id, item_id, fields_json, content_hash, first_seen_at, last_seen_at"
        ") VALUES ("
        "'old', 'item', '{}', 'hash', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00'"
        ")"
    )
    connection.execute(
        "INSERT INTO runs(id, monitor_id, version_id, stage, status, started_at) "
        "VALUES ('old-run', 'old', 'old-version', 'fetch', 'running', '2026-01-01T00:00:00+00:00')"
    )
    _add_outbox(connection, "old-outbox", "old", delivered_at=now)
    _add_outbox(connection, "boundary-outbox", "boundary", delivered_at=now)

    Maintenance(connection).run(now=now)

    assert not _exists(connection, "monitors", "old")
    assert not _exists(connection, "monitor_versions", "old-version")
    assert (
        connection.execute("SELECT 1 FROM observations WHERE monitor_id = 'old'").fetchone() is None
    )
    assert not _exists(connection, "runs", "old-run")
    assert not _exists(connection, "outbox", "old-outbox")
    assert not _exists(connection, "deliveries", "old-outbox", "outbox_id")
    assert _exists(connection, "monitors", "boundary")
    assert _exists(connection, "outbox", "boundary-outbox")
    assert _exists(connection, "users", "owner")
    assert _exists(connection, "delivery_targets", "target")


def test_removes_diagnostic_snapshots_of_an_old_disabled_monitor(
    connection: sqlite3.Connection, now: datetime
) -> None:
    connection.execute(
        "CREATE TABLE diagnostic_snapshots("
        "id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL, created_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, FOREIGN KEY(monitor_id) REFERENCES monitors(id)"
        ")"
    )
    _add_monitor(
        connection,
        "old",
        status="disabled",
        disabled_at=now - timedelta(days=30, microseconds=1),
    )
    connection.execute(
        "INSERT INTO diagnostic_snapshots(id, monitor_id, created_at, expires_at) "
        "VALUES ('snapshot', 'old', ?, ?)",
        (_timestamp(now), _timestamp(now + timedelta(days=7))),
    )

    Maintenance(connection).run(now=now)

    assert not _exists(connection, "diagnostic_snapshots", "snapshot")
    assert not _exists(connection, "monitors", "old")


def test_runs_optimize_after_the_retention_transaction(
    connection: sqlite3.Connection, now: datetime
) -> None:
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    Maintenance(connection).run(now=now)

    assert any(statement == "PRAGMA optimize" for statement in statements)
    assert max(index for index, statement in enumerate(statements) if statement == "COMMIT") < min(
        index for index, statement in enumerate(statements) if statement == "PRAGMA optimize"
    )
