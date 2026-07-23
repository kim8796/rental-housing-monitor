from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from personal_monitor.domain.spec import MonitorSpec, MonitorStatus
from personal_monitor.security.encryption import EncryptedBlob
from personal_monitor.storage.schema import (
    canonical_json,
    parse_timestamp,
    transaction,
    utc_now,
    utc_timestamp,
)

_DIAGNOSTIC_RETENTION = timedelta(days=7)
_GCM_NONCE_LENGTH = 12
_GCM_TAG_LENGTH = 16


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    id: str = field(repr=False)
    monitor_id: str = field(repr=False)
    blob: EncryptedBlob = field(repr=False)
    created_at: datetime = field(repr=False)
    expires_at: datetime = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "blob",
            EncryptedBlob(nonce=self.blob.nonce, ciphertext=self.blob.ciphertext),
        )


class RecoveryRepository:
    """Persist encrypted diagnostics and adaptive candidates under one SQLite fence."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be SQLite")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.connection = connection
        self._clock = clock

    def store_diagnostic(
        self,
        monitor_id: str,
        owner_id: str,
        blob: EncryptedBlob,
    ) -> str:
        _validate_blob(blob)
        created_at = self._read_clock()
        snapshot_id = uuid4().hex
        with transaction(self.connection, immediate=True):
            self._require_owned_monitor(monitor_id, owner_id)
            self._insert_diagnostic(snapshot_id, monitor_id, blob, created_at)
        return snapshot_id

    def store_diagnostic_if_active(
        self,
        monitor_id: str,
        owner_id: str,
        expected_active_version_id: str,
        blob: EncryptedBlob,
    ) -> str:
        _validate_blob(blob)
        created_at = self._read_clock()
        snapshot_id = uuid4().hex
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT 1 FROM monitors WHERE id = ? AND owner_id = ? AND status = ? "
                "AND active_version_id = ?",
                (
                    monitor_id,
                    owner_id,
                    MonitorStatus.ACTIVE.value,
                    expected_active_version_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("recovery precondition failed")
            self._insert_diagnostic(snapshot_id, monitor_id, blob, created_at)
        return snapshot_id

    def get_diagnostic(self, snapshot_id: str, *, owner_id: str) -> DiagnosticSnapshot:
        row = self.connection.execute(
            "SELECT d.id, d.monitor_id, d.nonce, d.ciphertext, d.created_at, d.expires_at "
            "FROM diagnostic_snapshots AS d JOIN monitors AS m ON m.id = d.monitor_id "
            "WHERE d.id = ? AND m.owner_id = ?",
            (snapshot_id, owner_id),
        ).fetchone()
        if row is None:
            raise ValueError("diagnostic access denied")
        created_at = parse_timestamp(row["created_at"])
        expires_at = parse_timestamp(row["expires_at"])
        if created_at is None or expires_at is None:
            raise ValueError("diagnostic metadata is invalid")
        return DiagnosticSnapshot(
            id=row["id"],
            monitor_id=row["monitor_id"],
            blob=EncryptedBlob(nonce=bytes(row["nonce"]), ciphertext=bytes(row["ciphertext"])),
            created_at=created_at,
            expires_at=expires_at,
        )

    def store_candidate(
        self,
        *,
        monitor_id: str,
        owner_id: str,
        expected_active_version_id: str,
        spec: MonitorSpec,
        diagnostic: EncryptedBlob,
    ) -> str:
        if not isinstance(spec, MonitorSpec):
            raise TypeError("spec must be a MonitorSpec")
        _validate_blob(diagnostic)
        created_at = self._read_clock()
        candidate_id = uuid4().hex
        snapshot_id = uuid4().hex
        timestamp = utc_timestamp(created_at, parameter="created_at")
        with transaction(self.connection, immediate=True):
            monitor = self.connection.execute(
                "SELECT owner_id, status, active_version_id FROM monitors WHERE id = ?",
                (monitor_id,),
            ).fetchone()
            if (
                monitor is None
                or monitor["owner_id"] != owner_id
                or monitor["owner_id"] != spec.owner_id
                or monitor["status"] != MonitorStatus.ACTIVE.value
                or monitor["active_version_id"] != expected_active_version_id
            ):
                raise ValueError("recovery precondition failed")
            active = self.connection.execute(
                "SELECT 1 FROM monitor_versions WHERE id = ? AND monitor_id = ?",
                (expected_active_version_id, monitor_id),
            ).fetchone()
            if active is None:
                raise ValueError("recovery precondition failed")
            next_version = self.connection.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM monitor_versions "
                "WHERE monitor_id = ?",
                (monitor_id,),
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO monitor_versions(id, monitor_id, version_number, spec_json, "
                "created_by, created_at, approved_by, approved_at, parent_version_id) "
                "VALUES (?, ?, ?, ?, 'scrapling-adaptive', ?, NULL, NULL, ?)",
                (
                    candidate_id,
                    monitor_id,
                    next_version,
                    canonical_json(spec.model_dump(mode="json")),
                    timestamp,
                    expected_active_version_id,
                ),
            )
            self._insert_diagnostic(snapshot_id, monitor_id, diagnostic, created_at)
            cursor = self.connection.execute(
                "UPDATE monitors SET status = ?, next_run_at = NULL, "
                "lease_owner = NULL, lease_expires_at = NULL, "
                "lease_generation = lease_generation + 1, updated_at = ? "
                "WHERE id = ? AND owner_id = ? AND status = ? AND active_version_id = ?",
                (
                    MonitorStatus.NEEDS_REVIEW.value,
                    timestamp,
                    monitor_id,
                    owner_id,
                    MonitorStatus.ACTIVE.value,
                    expected_active_version_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("recovery precondition failed")
            current = self.connection.execute(
                "SELECT active_version_id FROM monitors WHERE id = ?", (monitor_id,)
            ).fetchone()
            if current is None or current["active_version_id"] != expected_active_version_id:
                raise ValueError("recovery precondition failed")
        return candidate_id

    def _insert_diagnostic(
        self,
        snapshot_id: str,
        monitor_id: str,
        blob: EncryptedBlob,
        created_at: datetime,
    ) -> None:
        created_timestamp = utc_timestamp(created_at, parameter="created_at")
        expires_timestamp = utc_timestamp(
            created_at + _DIAGNOSTIC_RETENTION,
            parameter="expires_at",
        )
        self.connection.execute(
            "INSERT INTO diagnostic_snapshots("
            "id, monitor_id, ciphertext, nonce, created_at, expires_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                monitor_id,
                sqlite3.Binary(bytes(blob.ciphertext)),
                sqlite3.Binary(bytes(blob.nonce)),
                created_timestamp,
                expires_timestamp,
            ),
        )

    def _require_owned_monitor(self, monitor_id: str, owner_id: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM monitors WHERE id = ? AND owner_id = ?", (monitor_id, owner_id)
        ).fetchone()
        if row is None:
            raise ValueError("diagnostic access denied")

    def _read_clock(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _validate_blob(blob: object) -> None:
    if not isinstance(blob, EncryptedBlob):
        raise TypeError("diagnostic must be an EncryptedBlob")
    if len(blob.nonce) != _GCM_NONCE_LENGTH or len(blob.ciphertext) < _GCM_TAG_LENGTH:
        raise ValueError("diagnostic encrypted blob is malformed")
