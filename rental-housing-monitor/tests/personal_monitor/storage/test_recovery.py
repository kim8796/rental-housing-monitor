from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from personal_monitor.domain.spec import MonitorSpec, MonitorStatus
from personal_monitor.security.encryption import EncryptedBlob
from personal_monitor.storage import RegistryRepository, open_database


def make_spec(owner_id: str = "owner", *, selector: str = ".price") -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": owner_id,
            "name": "가격 감시",
            "target_url": "https://example.com/product/1",
            "source_adapter": "scrapling",
            "schedule": "0 */6 * * *",
            "timezone": "Asia/Seoul",
            "extract": {
                "item_scope": "main",
                "fields": {"price": {"selector": selector, "type": "krw"}},
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
def monitor(connection: sqlite3.Connection) -> tuple[RegistryRepository, str, str]:
    registry = RegistryRepository(connection)
    registry.create_user("owner", 1)
    registry.create_user("other", 2)
    monitor_id = registry.create_monitor(make_spec(), created_by="owner")
    return registry, monitor_id, registry.get_active_monitor(monitor_id).version_id


def recovery_repository(connection: sqlite3.Connection, clock: Callable[[], object]):
    from personal_monitor.storage.recovery import RecoveryRepository

    return RecoveryRepository(connection, clock=clock)


def encrypted_blob() -> EncryptedBlob:
    return EncryptedBlob(nonce=b"n" * 12, ciphertext=b"ciphertext-with-auth-tag")


def test_diagnostic_storage_uses_one_aware_clock_and_returns_redacted_immutable_copies(
    connection: sqlite3.Connection,
    monitor: tuple[RegistryRepository, str, str],
) -> None:
    _, monitor_id, _ = monitor
    created_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return created_at

    repository = recovery_repository(connection, clock)
    source_nonce = bytearray(b"n" * 12)
    source_ciphertext = bytearray(b"private-encrypted-bytes-with-tag")
    blob = EncryptedBlob(source_nonce, source_ciphertext)

    snapshot_id = repository.store_diagnostic(monitor_id, "owner", blob)
    source_nonce[:] = b"x" * len(source_nonce)
    source_ciphertext[:] = b"x" * len(source_ciphertext)
    snapshot = repository.get_diagnostic(snapshot_id, owner_id="owner")

    assert calls == 1
    assert snapshot.blob.nonce == b"n" * 12
    assert snapshot.blob.ciphertext == b"private-encrypted-bytes-with-tag"
    assert snapshot.created_at == created_at
    assert snapshot.expires_at == created_at + timedelta(days=7)
    assert monitor_id not in repr(snapshot)
    assert "private-encrypted" not in repr(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.created_at = created_at + timedelta(days=1)  # type: ignore[misc]


@pytest.mark.parametrize("clock_value", [datetime(2026, 7, 23), "2026-07-23", None])
def test_diagnostic_clock_must_return_an_aware_datetime_before_any_write(
    connection: sqlite3.Connection,
    monitor: tuple[RegistryRepository, str, str],
    clock_value: object,
) -> None:
    _, monitor_id, _ = monitor
    repository = recovery_repository(connection, lambda: clock_value)

    with pytest.raises((TypeError, ValueError), match="clock"):
        repository.store_diagnostic(monitor_id, "owner", encrypted_blob())

    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0


def test_diagnostic_write_and_read_validate_monitor_ownership_without_leaking_ids(
    connection: sqlite3.Connection,
    monitor: tuple[RegistryRepository, str, str],
) -> None:
    _, monitor_id, _ = monitor
    repository = recovery_repository(connection, lambda: datetime(2026, 7, 23, tzinfo=UTC))

    with pytest.raises(ValueError) as wrong_owner:
        repository.store_diagnostic(monitor_id, "other", encrypted_blob())
    with pytest.raises(ValueError) as missing:
        repository.store_diagnostic("missing-monitor-secret", "owner", encrypted_blob())

    snapshot_id = repository.store_diagnostic(monitor_id, "owner", encrypted_blob())
    with pytest.raises(ValueError) as read_wrong_owner:
        repository.get_diagnostic(snapshot_id, owner_id="other")

    messages = " ".join(map(str, (wrong_owner.value, missing.value, read_wrong_owner.value)))
    assert monitor_id not in messages
    assert "missing-monitor-secret" not in messages
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 1


@pytest.mark.parametrize(
    "blob",
    [
        EncryptedBlob(b"short", b"ciphertext-with-auth-tag"),
        EncryptedBlob(b"n" * 12, b"short"),
        "ciphertext:nonce",
    ],
)
def test_diagnostic_write_rejects_malformed_or_untyped_encrypted_blobs_before_writes(
    connection: sqlite3.Connection,
    monitor: tuple[RegistryRepository, str, str],
    blob: object,
) -> None:
    _, monitor_id, _ = monitor
    repository = recovery_repository(connection, lambda: datetime(2026, 7, 23, tzinfo=UTC))

    with pytest.raises((TypeError, ValueError)):
        repository.store_diagnostic(monitor_id, "owner", blob)  # type: ignore[arg-type]

    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0


def test_candidate_commit_is_unapproved_atomic_and_never_changes_active_version(
    connection: sqlite3.Connection,
    monitor: tuple[RegistryRepository, str, str],
) -> None:
    registry, monitor_id, active_version_id = monitor
    repository = recovery_repository(connection, lambda: datetime(2026, 7, 23, tzinfo=UTC))

    candidate_id = repository.store_candidate(
        monitor_id=monitor_id,
        owner_id="owner",
        expected_active_version_id=active_version_id,
        spec=make_spec(selector=".new-price"),
        diagnostic=encrypted_blob(),
    )

    row = connection.execute(
        "SELECT created_by, approved_by, approved_at, parent_version_id "
        "FROM monitor_versions WHERE id = ?",
        (candidate_id,),
    ).fetchone()
    assert tuple(row) == ("scrapling-adaptive", None, None, active_version_id)
    current = registry.get_active_monitor(monitor_id)
    assert current.version_id == active_version_id
    assert registry.list_monitors("owner")[0].status is MonitorStatus.NEEDS_REVIEW
    monitor_state = connection.execute(
        "SELECT next_run_at, lease_owner, lease_expires_at, lease_generation "
        "FROM monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert tuple(monitor_state) == (None, None, None, 1)
    assert (
        connection.execute(
            "SELECT count(*) FROM diagnostic_snapshots WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("failed_stage", ["version", "diagnostic", "status"])
def test_candidate_commit_rolls_back_every_write_stage(
    connection: sqlite3.Connection,
    monitor: tuple[RegistryRepository, str, str],
    failed_stage: str,
) -> None:
    registry, monitor_id, active_version_id = monitor
    trigger_sql = {
        "version": (
            "CREATE TRIGGER fail_stage BEFORE INSERT ON monitor_versions "
            "WHEN NEW.created_by = 'scrapling-adaptive' BEGIN SELECT RAISE(ABORT, 'fail'); END"
        ),
        "diagnostic": (
            "CREATE TRIGGER fail_stage BEFORE INSERT ON diagnostic_snapshots "
            "BEGIN SELECT RAISE(ABORT, 'fail'); END"
        ),
        "status": (
            "CREATE TRIGGER fail_stage BEFORE UPDATE OF status ON monitors "
            "WHEN NEW.status = 'needs_review' BEGIN SELECT RAISE(ABORT, 'fail'); END"
        ),
    }[failed_stage]
    connection.execute(trigger_sql)
    repository = recovery_repository(connection, lambda: datetime(2026, 7, 23, tzinfo=UTC))

    with pytest.raises(sqlite3.IntegrityError):
        repository.store_candidate(
            monitor_id=monitor_id,
            owner_id="owner",
            expected_active_version_id=active_version_id,
            spec=make_spec(selector=".new-price"),
            diagnostic=encrypted_blob(),
        )

    assert (
        connection.execute(
            "SELECT count(*) FROM monitor_versions WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()[0]
        == 1
    )
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0
    assert registry.get_active_monitor(monitor_id).version_id == active_version_id
    assert registry.list_monitors("owner")[0].status is MonitorStatus.ACTIVE
    connection.execute("DROP TRIGGER fail_stage")
    repository.store_candidate(
        monitor_id=monitor_id,
        owner_id="owner",
        expected_active_version_id=active_version_id,
        spec=make_spec(selector=".after-rollback"),
        diagnostic=encrypted_blob(),
    )
    assert [
        row["version_number"]
        for row in connection.execute(
            "SELECT version_number FROM monitor_versions WHERE monitor_id = ? "
            "ORDER BY version_number",
            (monitor_id,),
        )
    ] == [1, 2]


@pytest.mark.parametrize("mismatch", ["owner", "active_version", "status", "spec_owner"])
def test_candidate_commit_rejects_stale_or_cross_owner_inputs_before_writes(
    connection: sqlite3.Connection,
    monitor: tuple[RegistryRepository, str, str],
    mismatch: str,
) -> None:
    registry, monitor_id, active_version_id = monitor
    owner_id = "other" if mismatch == "owner" else "owner"
    expected_version = "stale-secret-version" if mismatch == "active_version" else active_version_id
    spec = make_spec(owner_id="other") if mismatch == "spec_owner" else make_spec(selector=".new")
    if mismatch == "status":
        registry.transition_status(
            monitor_id, MonitorStatus.ACTIVE, MonitorStatus.PAUSED_USER, owner_id="owner"
        )
    repository = recovery_repository(connection, lambda: datetime(2026, 7, 23, tzinfo=UTC))

    with pytest.raises(ValueError) as caught:
        repository.store_candidate(
            monitor_id=monitor_id,
            owner_id=owner_id,
            expected_active_version_id=expected_version,
            spec=spec,
            diagnostic=encrypted_blob(),
        )

    assert monitor_id not in str(caught.value)
    assert "stale-secret-version" not in str(caught.value)
    assert (
        connection.execute(
            "SELECT count(*) FROM monitor_versions WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()[0]
        == 1
    )
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 0


def test_duplicate_candidate_attempt_fails_closed_without_a_second_write(
    connection: sqlite3.Connection,
    monitor: tuple[RegistryRepository, str, str],
) -> None:
    _, monitor_id, active_version_id = monitor
    repository = recovery_repository(connection, lambda: datetime(2026, 7, 23, tzinfo=UTC))
    arguments = {
        "monitor_id": monitor_id,
        "owner_id": "owner",
        "expected_active_version_id": active_version_id,
        "spec": make_spec(selector=".new"),
        "diagnostic": encrypted_blob(),
    }
    repository.store_candidate(**arguments)

    with pytest.raises(ValueError):
        repository.store_candidate(**arguments)

    assert (
        connection.execute(
            "SELECT count(*) FROM monitor_versions WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()[0]
        == 2
    )
    assert connection.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 1
