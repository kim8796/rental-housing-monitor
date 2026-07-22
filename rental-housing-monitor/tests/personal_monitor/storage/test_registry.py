from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

from personal_monitor.domain.spec import MonitorSpec, MonitorStatus
from personal_monitor.storage import RegistryRepository, open_database, schema


def make_spec(owner_id: str = "telegram-user:1", name: str = "가격 감시") -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": owner_id,
            "name": name,
            "target_url": "https://example.com/product/1",
            "source_adapter": "scrapling",
            "schedule": "0 */6 * * *",
            "timezone": "Asia/Seoul",
            "extract": {
                "item_scope": "main",
                "fields": {"price": {"selector": ".price", "type": "krw"}},
            },
            "validators": {"min_items": 1, "max_items": 1},
            "rules": [{"kind": "new_item"}],
        }
    )


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    value = open_database(":memory:")
    yield value
    value.close()


@pytest.fixture
def registry(connection: sqlite3.Connection) -> RegistryRepository:
    value = RegistryRepository(connection)
    value.create_user("telegram-user:1", 1)
    value.create_user("telegram-user:2", 2)
    return value


def test_schema_migration_is_idempotent(tmp_path) -> None:
    database_path = tmp_path / "state" / "monitor.db"

    first = open_database(database_path)
    first.close()
    second = open_database(database_path)

    assert database_path.exists()
    assert second.row_factory is not None
    assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert second.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert second.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    versions = second.execute("SELECT version FROM schema_migrations")
    assert [row["version"] for row in versions] == [1, 2, 3]
    columns = {
        row["name"]: row for row in second.execute("PRAGMA table_info(diagnostic_snapshots)")
    }
    assert set(columns) == {"id", "monitor_id", "ciphertext", "nonce", "created_at", "expires_at"}
    assert columns["ciphertext"]["type"] == "BLOB"
    assert columns["nonce"]["type"] == "BLOB"
    assert second.execute("PRAGMA foreign_key_list(diagnostic_snapshots)").fetchone()["table"] == (
        "monitors"
    )
    indexes = {row["name"] for row in second.execute("PRAGMA index_list(diagnostic_snapshots)")}
    assert "diagnostic_snapshots_expiry_idx" in indexes
    adaptive_columns = {
        row["name"]: row for row in second.execute("PRAGMA table_info(adaptive_features)")
    }
    assert set(adaptive_columns) == {
        "key_hash",
        "namespace_hash",
        "nonce",
        "ciphertext",
        "created_at",
        "updated_at",
        "expires_at",
    }
    assert adaptive_columns["nonce"]["type"] == "BLOB"
    assert adaptive_columns["ciphertext"]["type"] == "BLOB"
    adaptive_indexes = {
        row["name"] for row in second.execute("PRAGMA index_list(adaptive_features)")
    }
    assert {
        "adaptive_features_namespace_idx",
        "adaptive_features_expiry_idx",
    } <= adaptive_indexes
    second.close()


def test_existing_v1_database_migrates_to_v2_atomically_and_reruns(tmp_path) -> None:
    database_path = tmp_path / "existing-v1.db"
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for statement in schema._statements(schema._MIGRATION_1):
        connection.execute(statement)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
        (datetime.now(UTC).isoformat(),),
    )
    connection.commit()
    connection.close()

    migrated = open_database(database_path)
    migrated_versions = migrated.execute("SELECT version FROM schema_migrations")
    assert [row["version"] for row in migrated_versions] == [1, 2, 3]
    assert migrated.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_snapshots'"
    ).fetchone()
    migrated.close()

    rerun = open_database(database_path)
    migration_count = rerun.execute(
        "SELECT count(*) FROM schema_migrations WHERE version = 2"
    ).fetchone()[0]
    assert migration_count == 1
    rerun.close()


def test_schema_rejects_a_migration_newer_than_the_binary(tmp_path) -> None:
    database_path = tmp_path / "future.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (4, ?)",
        (datetime.now(UTC).isoformat(),),
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        open_database(database_path)


def test_open_existing_rejects_an_incomplete_v1_schema(tmp_path) -> None:
    database_path = tmp_path / "incomplete.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
        (datetime.now(UTC).isoformat(),),
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="incomplete"):
        schema.open_existing_database(database_path)


@pytest.mark.parametrize(
    "damage",
    (
        "DROP TABLE adaptive_features",
        "ALTER TABLE adaptive_features RENAME COLUMN expires_at TO expires_broken",
        "DROP INDEX adaptive_features_expiry_idx",
        "DROP TABLE diagnostic_snapshots",
    ),
)
def test_applied_migrations_do_not_mask_concrete_schema_corruption(tmp_path, damage: str) -> None:
    database_path = tmp_path / "corrupt-schema.db"
    connection = open_database(database_path)
    connection.execute(damage)
    connection.close()

    with pytest.raises(RuntimeError, match="schema integrity"):
        open_database(database_path)
    with pytest.raises(RuntimeError, match="schema integrity"):
        schema.open_existing_database(database_path)


def test_migration_history_must_be_an_exact_contiguous_known_prefix(tmp_path) -> None:
    database_path = tmp_path / "migration-gap.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version in (1, 3):
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="migration history"):
        open_database(database_path)


@pytest.mark.parametrize("applied_at", ("not-a-time", "2026-07-23T00:00:00"))
def test_migration_timestamps_must_be_parseable_and_timezone_aware(
    tmp_path, applied_at: str
) -> None:
    database_path = tmp_path / "bad-migration-time.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
        (applied_at,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="migration history"):
        open_database(database_path)


def test_schema_migration_rolls_back_every_statement_on_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = sqlite3.connect(tmp_path / "broken.db", isolation_level=None)
    monkeypatch.setattr(
        schema,
        "_MIGRATIONS",
        ((1, schema._MIGRATION_1 + "\nCREATE TABLE must_rollback(id INTEGER);\nINVALID SQL;"),),
    )

    with pytest.raises(sqlite3.OperationalError):
        schema._apply_migrations(connection)

    remaining_tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    assert remaining_tables == []
    connection.close()


def test_repository_does_not_commit_a_caller_owned_transaction(
    connection: sqlite3.Connection,
) -> None:
    registry = RegistryRepository(connection)
    connection.execute("BEGIN")

    registry.create_user("temporary", 99)
    connection.rollback()

    assert connection.execute("SELECT 1 FROM users WHERE id = 'temporary'").fetchone() is None


def test_get_delivery_target_resolves_internal_id_and_returns_existing_row_type(
    registry: RegistryRepository,
) -> None:
    registry.create_delivery_target("target-1", "telegram-user:1", "chat-address")

    target = registry.get_delivery_target("target-1")

    assert (target.id, target.owner_id, target.kind, target.address) == (
        "target-1",
        "telegram-user:1",
        "telegram",
        "chat-address",
    )
    with pytest.raises(ValueError, match="delivery target does not exist"):
        registry.get_delivery_target("chat-address")


def test_immediate_operation_rejects_a_caller_owned_transaction_before_work(
    registry: RegistryRepository, connection: sqlite3.Connection
) -> None:
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    original_count = connection.execute(
        "SELECT count(*) FROM monitor_versions WHERE monitor_id = ?", (monitor_id,)
    ).fetchone()[0]
    connection.execute("BEGIN")

    with pytest.raises(RuntimeError, match="immediate transaction.*already active"):
        registry.add_version(
            monitor_id,
            make_spec(name="must not be stored"),
            created_by="codex",
            approved=False,
        )

    assert connection.in_transaction
    assert (
        connection.execute(
            "SELECT count(*) FROM monitor_versions WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()[0]
        == original_count
    )
    connection.rollback()


def test_create_monitor_owns_an_approved_active_version(
    registry: RegistryRepository, connection: sqlite3.Connection
) -> None:
    spec = make_spec()

    monitor_id = registry.create_monitor(spec, created_by="telegram-user:1")

    monitor = registry.get_active_monitor(monitor_id)
    version = connection.execute(
        "SELECT version_number, spec_json, approved_by, approved_at "
        "FROM monitor_versions WHERE id = ?",
        (monitor.version_id,),
    ).fetchone()
    assert monitor.owner_id == spec.owner_id
    assert monitor.spec == spec
    assert version["version_number"] == 1
    assert json.loads(version["spec_json"]) == spec.model_dump(mode="json")
    assert version["spec_json"] == json.dumps(
        spec.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert version["approved_by"] == "telegram-user:1"
    assert version["approved_at"] is not None
    created = registry.list_monitors("telegram-user:1")[0]
    assert created.next_run_at is not None


def test_unapproved_version_cannot_become_active(registry: RegistryRepository) -> None:
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    candidate = registry.add_version(
        monitor_id, make_spec(name="새 가격 감시"), created_by="codex", approved=False
    )

    with pytest.raises(ValueError, match="approved"):
        registry.activate_version(monitor_id, candidate, owner_id="telegram-user:1")


def test_version_cannot_be_activated_for_a_different_monitor(
    registry: RegistryRepository,
) -> None:
    first = registry.create_monitor(make_spec(name="첫 번째"), created_by="telegram-user:1")
    second = registry.create_monitor(make_spec(name="두 번째"), created_by="telegram-user:1")
    candidate = registry.add_version(
        first, make_spec(name="첫 번째 수정"), created_by="telegram-user:1", approved=True
    )

    with pytest.raises(ValueError, match="belong"):
        registry.activate_version(second, candidate, owner_id="telegram-user:1")


def test_monitor_version_must_keep_the_monitor_owner(registry: RegistryRepository) -> None:
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")

    with pytest.raises(ValueError, match="owner"):
        registry.add_version(
            monitor_id, make_spec(owner_id="telegram-user:2"), created_by="codex", approved=False
        )


def test_approved_candidate_can_become_active(registry: RegistryRepository) -> None:
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    updated = make_spec(name="승인된 수정")
    candidate = registry.add_version(monitor_id, updated, created_by="codex", approved=False)

    registry.approve_version(candidate, approved_by="telegram-user:1")
    registry.activate_version(monitor_id, candidate, owner_id="telegram-user:1")

    assert registry.get_active_spec(monitor_id) == updated


def test_only_monitor_owner_can_approve_or_activate_a_version(
    registry: RegistryRepository,
) -> None:
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    candidate = registry.add_version(
        monitor_id, make_spec(name="owner only"), created_by="codex", approved=False
    )

    with pytest.raises(ValueError, match="owner"):
        registry.approve_version(candidate, approved_by="telegram-user:2")
    registry.approve_version(candidate, approved_by="telegram-user:1")
    with pytest.raises(ValueError, match="owner"):
        registry.activate_version(monitor_id, candidate, owner_id="telegram-user:2")


def test_owner_scoped_lists_never_return_another_users_monitor(
    registry: RegistryRepository,
) -> None:
    first = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    registry.create_monitor(
        make_spec(owner_id="telegram-user:2", name="다른 소유자"),
        created_by="telegram-user:2",
    )

    assert [row.id for row in registry.list_monitors("telegram-user:1")] == [first]


def test_delivery_target_lookup_is_owner_scoped(registry: RegistryRepository) -> None:
    registry.create_delivery_target("target-2", "telegram-user:2", "chat-2")
    registry.create_delivery_target("target-1", "telegram-user:1", "chat-1")

    target = registry.get_primary_target("telegram-user:1")

    assert (target.id, target.owner_id, target.kind, target.address) == (
        "target-1",
        "telegram-user:1",
        "telegram",
        "chat-1",
    )


def test_status_transition_uses_compare_and_swap(registry: RegistryRepository) -> None:
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")

    registry.transition_status(
        monitor_id,
        MonitorStatus.ACTIVE,
        MonitorStatus.PAUSED_USER,
        owner_id="telegram-user:1",
    )
    with pytest.raises(ValueError, match="expected status"):
        registry.transition_status(
            monitor_id,
            MonitorStatus.ACTIVE,
            MonitorStatus.NEEDS_REVIEW,
            owner_id="telegram-user:1",
        )

    assert registry.list_monitors("telegram-user:1")[0].status is MonitorStatus.PAUSED_USER


def test_soft_delete_requires_aware_time_and_is_hidden_by_default(
    registry: RegistryRepository, connection: sqlite3.Connection
) -> None:
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")
    with pytest.raises(ValueError, match="timezone-aware"):
        registry.soft_delete(
            monitor_id, owner_id="telegram-user:1", disabled_at=datetime(2026, 1, 1)
        )

    disabled_at = datetime(2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    registry.soft_delete(monitor_id, owner_id="telegram-user:1", disabled_at=disabled_at)

    assert registry.list_monitors("telegram-user:1") == []
    assert registry.list_monitors("telegram-user:1", include_disabled=True)[0].status is (
        MonitorStatus.DISABLED
    )
    stored = connection.execute(
        "SELECT disabled_at FROM monitors WHERE id = ?", (monitor_id,)
    ).fetchone()[0]
    assert stored == "2026-01-01T00:00:00+00:00"


def test_wrong_owner_cannot_pause_monitor(registry: RegistryRepository) -> None:
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")

    with pytest.raises(ValueError):
        registry.transition_status(
            monitor_id,
            MonitorStatus.ACTIVE,
            MonitorStatus.PAUSED_USER,
            owner_id="telegram-user:2",
        )

    assert registry.list_monitors("telegram-user:1")[0].status is MonitorStatus.ACTIVE


def test_wrong_owner_cannot_soft_delete_monitor(registry: RegistryRepository) -> None:
    monitor_id = registry.create_monitor(make_spec(), created_by="telegram-user:1")

    with pytest.raises(ValueError):
        registry.soft_delete(
            monitor_id,
            owner_id="telegram-user:2",
            disabled_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    assert registry.list_monitors("telegram-user:1")[0].status is MonitorStatus.ACTIVE
