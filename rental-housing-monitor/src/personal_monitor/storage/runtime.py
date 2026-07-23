from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from uuid import uuid4

from personal_monitor.domain.observation import (
    ObservationBatch,
    ObservedItem,
    content_hash,
)
from personal_monitor.domain.spec import MonitorStatus
from personal_monitor.storage.schema import (
    canonical_json,
    transaction,
    utc_now,
    utc_timestamp,
)

SAFE_DIAGNOSTIC_CODES = frozenset(
    {
        "authentication_failed",
        "connection_timeout",
        "delivery_failed",
        "internal_error",
        "network_error",
        "offline",
        "policy_rejected",
        "required_field_missing",
        "structure_changed",
        "timeout",
        "validation_failed",
    }
)


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: str
    target_id: str
    payload: dict[str, object]
    attempt_count: int


@dataclass(frozen=True, slots=True)
class MonitorLease:
    monitor_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class DeliveryCandidate:
    dedupe_key: str
    target_id: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_json(self.payload))


class RuntimeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def claim_due(
        self, *, worker_id: str, now: datetime, lease_seconds: int = 300
    ) -> list[MonitorLease]:
        now_timestamp = utc_timestamp(now, parameter="now")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        lease_expires_at = utc_timestamp(
            now + timedelta(seconds=lease_seconds), parameter="lease_expires_at"
        )
        with transaction(self.connection, immediate=True):
            rows = self.connection.execute(
                "SELECT id, lease_generation FROM monitors WHERE status = 'active' "
                "AND next_run_at IS NOT NULL AND next_run_at <= ? "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                "ORDER BY next_run_at, id",
                (now_timestamp, now_timestamp),
            ).fetchall()
            leases: list[MonitorLease] = []
            for row in rows:
                generation = row["lease_generation"] + 1
                cursor = self.connection.execute(
                    "UPDATE monitors SET lease_owner = ?, lease_expires_at = ?, "
                    "lease_generation = ? WHERE id = ? AND lease_generation = ?",
                    (
                        worker_id,
                        lease_expires_at,
                        generation,
                        row["id"],
                        row["lease_generation"],
                    ),
                )
                if cursor.rowcount == 1:
                    leases.append(MonitorLease(row["id"], generation))
        return leases

    def claim_monitor(
        self,
        monitor_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int = 300,
    ) -> MonitorLease:
        """Claim one explicitly selected active monitor for an operator one-shot run."""
        now_timestamp = utc_timestamp(now, parameter="now")
        if (
            type(monitor_id) is not str
            or not monitor_id
            or type(worker_id) is not str
            or not worker_id
            or lease_seconds <= 0
        ):
            raise ValueError("monitor is not available")
        lease_expires_at = utc_timestamp(
            now + timedelta(seconds=lease_seconds),
            parameter="lease_expires_at",
        )
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT lease_generation FROM monitors WHERE id = ? AND status = 'active' "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
                (monitor_id, now_timestamp),
            ).fetchone()
            if row is None:
                raise ValueError("monitor is not available")
            generation = row["lease_generation"] + 1
            cursor = self.connection.execute(
                "UPDATE monitors SET lease_owner = ?, lease_expires_at = ?, "
                "lease_generation = ? WHERE id = ? AND lease_generation = ? "
                "AND status = 'active' AND "
                "(lease_expires_at IS NULL OR lease_expires_at <= ?)",
                (
                    worker_id,
                    lease_expires_at,
                    generation,
                    monitor_id,
                    row["lease_generation"],
                    now_timestamp,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("monitor is not available")
        return MonitorLease(monitor_id, generation)

    def release_lease(self, lease: MonitorLease, *, worker_id: str, next_run_at: datetime) -> None:
        next_timestamp = utc_timestamp(next_run_at, parameter="next_run_at")
        with transaction(self.connection):
            cursor = self.connection.execute(
                "UPDATE monitors SET lease_owner = NULL, lease_expires_at = NULL, "
                "next_run_at = ?, updated_at = ? WHERE id = ? AND lease_owner = ? "
                "AND lease_generation = ?",
                (
                    next_timestamp,
                    next_timestamp,
                    lease.monitor_id,
                    worker_id,
                    lease.generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("worker does not own this monitor lease generation")

    def start_run(
        self,
        lease: MonitorLease,
        version_id: str,
        *,
        worker_id: str,
        fetch_strategy: str,
        started_at: datetime,
    ) -> str:
        started_timestamp = utc_timestamp(started_at, parameter="started_at")
        run_id = uuid4().hex
        with transaction(self.connection):
            self._assert_monitor_lease(lease, worker_id)
            self.connection.execute(
                "INSERT INTO runs(id, monitor_id, version_id, lease_generation, stage, "
                "fetch_strategy, status, started_at) "
                "VALUES (?, ?, ?, ?, 'fetch', ?, 'running', ?)",
                (
                    run_id,
                    lease.monitor_id,
                    version_id,
                    lease.generation,
                    fetch_strategy,
                    started_timestamp,
                ),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        lease: MonitorLease,
        worker_id: str,
        status: str,
        stage: str,
        error_class: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        _validate_diagnostic_code(error_detail)
        if error_class is not None and len(error_class) > 120:
            raise ValueError("error_class must be at most 120 characters")
        with transaction(self.connection):
            self._assert_monitor_lease(lease, worker_id)
            cursor = self.connection.execute(
                "UPDATE runs SET status = ?, stage = ?, finished_at = ?, error_class = ?, "
                "error_detail = ? WHERE id = ? AND monitor_id = ? AND lease_generation = ?",
                (
                    status,
                    stage,
                    utc_now().isoformat(),
                    error_class,
                    error_detail,
                    run_id,
                    lease.monitor_id,
                    lease.generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("run does not belong to this monitor lease generation")

    def transition_monitor_status(
        self,
        lease: MonitorLease,
        *,
        worker_id: str,
        expected: MonitorStatus,
        target: MonitorStatus,
    ) -> None:
        with transaction(self.connection):
            cursor = self.connection.execute(
                "UPDATE monitors SET status = ?, updated_at = ? WHERE id = ? AND status = ? "
                "AND lease_owner = ? AND lease_generation = ?",
                (
                    target.value,
                    utc_now().isoformat(),
                    lease.monitor_id,
                    expected.value,
                    worker_id,
                    lease.generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("worker does not own this monitor lease generation")

    def load_items(self, monitor_id: str) -> list[ObservedItem]:
        rows = self.connection.execute(
            "SELECT item_id, fields_json FROM observations WHERE monitor_id = ? ORDER BY item_id",
            (monitor_id,),
        )
        return [
            ObservedItem(item_id=row["item_id"], fields=json.loads(row["fields_json"]))
            for row in rows
        ]

    def _upsert_items(self, batch: ObservationBatch) -> None:
        observed_at = utc_timestamp(batch.observed_at, parameter="observed_at")
        item_ids = [item.item_id for item in batch.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("observation item IDs must be unique")
        values = [
            (
                batch.monitor_id,
                item.item_id,
                canonical_json(item.fields),
                content_hash(item.fields),
                observed_at,
                observed_at,
            )
            for item in batch.items
        ]
        self.connection.executemany(
            "INSERT INTO observations(monitor_id, item_id, fields_json, content_hash, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(monitor_id, item_id) DO UPDATE SET "
            "fields_json = excluded.fields_json, content_hash = excluded.content_hash, "
            "last_seen_at = excluded.last_seen_at",
            values,
        )
        if batch.warnings:
            return
        if item_ids:
            placeholders = ", ".join("?" for _ in item_ids)
            self.connection.execute(
                f"DELETE FROM observations WHERE monitor_id = ? "
                f"AND item_id NOT IN ({placeholders})",
                (batch.monitor_id, *item_ids),
            )
        else:
            self.connection.execute(
                "DELETE FROM observations WHERE monitor_id = ?", (batch.monitor_id,)
            )

    def apply_snapshot_and_deliveries(
        self,
        batch: ObservationBatch,
        candidates: Sequence[DeliveryCandidate],
        *,
        lease: MonitorLease,
        worker_id: str,
        return_new_only: bool = False,
    ) -> list[str]:
        with transaction(self.connection, immediate=True):
            if batch.monitor_id != lease.monitor_id:
                raise ValueError("batch monitor does not match lease")
            self._assert_monitor_lease(lease, worker_id)
            self._upsert_items(batch)
            results = [
                self._enqueue_delivery(batch.monitor_id, candidate) for candidate in candidates
            ]
            return [outbox_id for outbox_id, created in results if created or not return_new_only]

    def _enqueue_delivery(self, monitor_id: str, candidate: DeliveryCandidate) -> tuple[str, bool]:
        ownership = self.connection.execute(
            "SELECT m.owner_id AS monitor_owner, t.owner_id AS target_owner "
            "FROM monitors AS m JOIN delivery_targets AS t ON t.id = ? WHERE m.id = ?",
            (candidate.target_id, monitor_id),
        ).fetchone()
        if ownership is None:
            raise ValueError("monitor or delivery target does not exist")
        if ownership["monitor_owner"] != ownership["target_owner"]:
            raise ValueError("delivery target owner must match monitor owner")
        existing = self.connection.execute(
            "SELECT id, monitor_id, target_id FROM outbox WHERE dedupe_key = ?",
            (candidate.dedupe_key,),
        ).fetchone()
        if existing is not None:
            if existing["monitor_id"] != monitor_id or existing["target_id"] != candidate.target_id:
                raise ValueError("delivery dedupe key belongs to another aggregate")
            return existing["id"], False
        outbox_id = uuid4().hex
        created_at = utc_now().isoformat()
        self.connection.execute(
            "INSERT INTO outbox(id, dedupe_key, monitor_id, target_id, payload_json, "
            "status, available_at, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                outbox_id,
                candidate.dedupe_key,
                monitor_id,
                candidate.target_id,
                canonical_json(candidate.payload),
                created_at,
                created_at,
            ),
        )
        return outbox_id, True

    def _assert_monitor_lease(self, lease: MonitorLease, worker_id: str) -> None:
        owned = self.connection.execute(
            "SELECT 1 FROM monitors WHERE id = ? AND lease_owner = ? AND lease_generation = ?",
            (lease.monitor_id, worker_id, lease.generation),
        ).fetchone()
        if owned is None:
            raise ValueError("worker does not own this monitor lease generation")

    def claim_due_outbox(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int = 300,
        limit: int = 50,
        monitor_id: str | None = None,
        outbox_ids: Sequence[str] | None = None,
    ) -> list[OutboxRow]:
        now_timestamp = utc_timestamp(now, parameter="now")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if limit <= 0:
            raise ValueError("limit must be positive")
        if monitor_id is not None and (type(monitor_id) is not str or not monitor_id):
            raise ValueError("monitor_id must be nonempty")
        exact_ids: tuple[str, ...] | None = None
        if outbox_ids is not None:
            exact_ids = tuple(outbox_ids)
            if not exact_ids:
                return []
            if (
                len(exact_ids) > 100
                or len(set(exact_ids)) != len(exact_ids)
                or any(type(value) is not str or not 1 <= len(value) <= 128 for value in exact_ids)
            ):
                raise ValueError("outbox_ids are invalid")
        lease_expires_at = utc_timestamp(
            now + timedelta(seconds=lease_seconds), parameter="lease_expires_at"
        )
        monitor_clause = "" if monitor_id is None else "AND monitor_id = ? "
        exact_clause = (
            "" if exact_ids is None else f"AND id IN ({', '.join('?' for _ in exact_ids)}) "
        )
        parameters: tuple[object, ...] = (
            now_timestamp,
            now_timestamp,
            *((monitor_id,) if monitor_id is not None else ()),
            *(exact_ids or ()),
            limit,
        )
        with transaction(self.connection, immediate=True):
            rows = self.connection.execute(
                "SELECT id, target_id, payload_json, attempt_count FROM outbox "
                "WHERE status = 'pending' AND available_at <= ? "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                f"{monitor_clause}"
                f"{exact_clause}"
                "ORDER BY available_at, created_at, id LIMIT ?",
                parameters,
            ).fetchall()
            self.connection.executemany(
                "UPDATE outbox SET lease_owner = ?, lease_expires_at = ? WHERE id = ?",
                ((worker_id, lease_expires_at, row["id"]) for row in rows),
            )
        return [
            OutboxRow(
                id=row["id"],
                target_id=row["target_id"],
                payload=json.loads(row["payload_json"]),
                attempt_count=row["attempt_count"],
            )
            for row in rows
        ]

    def mark_delivered(
        self, outbox_id: str, *, worker_id: str, message_id: str, delivered_at: datetime
    ) -> None:
        delivered_timestamp = utc_timestamp(delivered_at, parameter="delivered_at")
        with transaction(self.connection, immediate=True):
            outbox = self.connection.execute(
                "SELECT target_id FROM outbox "
                "WHERE id = ? AND status = 'pending' AND lease_owner = ?",
                (outbox_id, worker_id),
            ).fetchone()
            if outbox is None:
                raise ValueError("outbox item is not owned by the claiming worker")
            delivery = self.connection.execute(
                "SELECT external_message_id, delivered_at FROM deliveries WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if delivery is None:
                self.connection.execute(
                    "INSERT INTO deliveries(outbox_id, target_id, external_message_id, "
                    "delivered_at) VALUES (?, ?, ?, ?)",
                    (outbox_id, outbox["target_id"], message_id, delivered_timestamp),
                )
            elif (
                delivery["external_message_id"] != message_id
                or delivery["delivered_at"] != delivered_timestamp
            ):
                raise ValueError("outbox item already has a different delivery")
            self.connection.execute(
                "UPDATE outbox SET status = 'delivered', lease_owner = NULL, "
                "lease_expires_at = NULL WHERE id = ? AND lease_owner = ?",
                (outbox_id, worker_id),
            )

    def reschedule_outbox(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        available_at: datetime,
        error: str,
    ) -> None:
        available_timestamp = utc_timestamp(available_at, parameter="available_at")
        _validate_diagnostic_code(error)
        with transaction(self.connection):
            cursor = self.connection.execute(
                "UPDATE outbox SET status = 'pending', attempt_count = attempt_count + 1, "
                "available_at = ?, last_error = ?, lease_owner = NULL, lease_expires_at = NULL "
                "WHERE id = ? AND status = 'pending' AND lease_owner = ?",
                (available_timestamp, error, outbox_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("outbox item is not owned by the claiming worker")


def _validate_diagnostic_code(detail: str | None) -> None:
    if detail is not None and detail not in SAFE_DIAGNOSTIC_CODES:
        raise ValueError("error detail must be a safe diagnostic code")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value
