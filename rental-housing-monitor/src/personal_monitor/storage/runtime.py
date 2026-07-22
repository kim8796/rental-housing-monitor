from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from personal_monitor.domain.observation import (
    ObservationBatch,
    ObservedItem,
    content_hash,
)
from personal_monitor.storage.schema import (
    canonical_json,
    transaction,
    utc_now,
    utc_timestamp,
)

_ERROR_DETAIL_LIMIT = 500
_URL_QUERY = re.compile(r"https?://\S+\?\S+", re.IGNORECASE)
_SENSITIVE_RAW_DETAIL = re.compile(
    r"\b(?:cookies?|set-cookie)\b|\bresponse\.(?:text|content)\b|\bresponse\s+body\b",
    re.IGNORECASE,
)
_EXCEPTION_REPR = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\(")


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: str
    target_id: str
    payload: dict[str, object]
    attempt_count: int


class RuntimeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def claim_due(self, *, worker_id: str, now: datetime, lease_seconds: int = 300) -> list[str]:
        now_timestamp = utc_timestamp(now, parameter="now")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        lease_expires_at = utc_timestamp(
            now + timedelta(seconds=lease_seconds), parameter="lease_expires_at"
        )
        with transaction(self.connection, immediate=True):
            rows = self.connection.execute(
                "SELECT id FROM monitors WHERE status = 'active' "
                "AND next_run_at IS NOT NULL AND next_run_at <= ? "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                "ORDER BY next_run_at, id",
                (now_timestamp, now_timestamp),
            ).fetchall()
            monitor_ids = [row["id"] for row in rows]
            for monitor_id in monitor_ids:
                self.connection.execute(
                    "UPDATE monitors SET lease_owner = ?, lease_expires_at = ? WHERE id = ?",
                    (worker_id, lease_expires_at, monitor_id),
                )
        return monitor_ids

    def release_lease(self, monitor_id: str, *, worker_id: str, next_run_at: datetime) -> None:
        next_timestamp = utc_timestamp(next_run_at, parameter="next_run_at")
        with transaction(self.connection):
            cursor = self.connection.execute(
                "UPDATE monitors SET lease_owner = NULL, lease_expires_at = NULL, "
                "next_run_at = ?, updated_at = ? WHERE id = ? AND lease_owner = ?",
                (next_timestamp, next_timestamp, monitor_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("worker is not the monitor lease owner")

    def start_run(self, monitor_id: str, version_id: str, *, started_at: datetime) -> str:
        started_timestamp = utc_timestamp(started_at, parameter="started_at")
        run_id = uuid4().hex
        with transaction(self.connection):
            self.connection.execute(
                "INSERT INTO runs(id, monitor_id, version_id, stage, status, started_at) "
                "VALUES (?, ?, ?, 'fetch', 'running', ?)",
                (run_id, monitor_id, version_id, started_timestamp),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        stage: str,
        error_class: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        _validate_error_detail(error_detail)
        if error_class is not None and len(error_class) > 120:
            raise ValueError("error_class must be at most 120 characters")
        with transaction(self.connection):
            cursor = self.connection.execute(
                "UPDATE runs SET status = ?, stage = ?, finished_at = ?, error_class = ?, "
                "error_detail = ? WHERE id = ?",
                (status, stage, utc_now().isoformat(), error_class, error_detail, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("run does not exist")

    def load_items(self, monitor_id: str) -> list[ObservedItem]:
        rows = self.connection.execute(
            "SELECT item_id, fields_json FROM observations WHERE monitor_id = ? ORDER BY item_id",
            (monitor_id,),
        )
        return [
            ObservedItem(item_id=row["item_id"], fields=json.loads(row["fields_json"]))
            for row in rows
        ]

    def upsert_items(self, batch: ObservationBatch) -> None:
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
        with transaction(self.connection):
            self.connection.executemany(
                "INSERT INTO observations(monitor_id, item_id, fields_json, content_hash, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(monitor_id, item_id) DO UPDATE SET "
                "fields_json = excluded.fields_json, content_hash = excluded.content_hash, "
                "last_seen_at = excluded.last_seen_at",
                values,
            )
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

    def enqueue_delivery(
        self,
        *,
        dedupe_key: str,
        monitor_id: str,
        target_id: str,
        payload: dict[str, object],
    ) -> str:
        payload_json = canonical_json(payload)
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT id FROM outbox WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if existing is not None:
                return existing["id"]
            outbox_id = uuid4().hex
            created_at = utc_now().isoformat()
            self.connection.execute(
                "INSERT INTO outbox(id, dedupe_key, monitor_id, target_id, payload_json, "
                "status, available_at, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    outbox_id,
                    dedupe_key,
                    monitor_id,
                    target_id,
                    payload_json,
                    created_at,
                    created_at,
                ),
            )
        return outbox_id

    def due_outbox(self, *, now: datetime, limit: int = 50) -> list[OutboxRow]:
        now_timestamp = utc_timestamp(now, parameter="now")
        if limit <= 0:
            raise ValueError("limit must be positive")
        rows = self.connection.execute(
            "SELECT id, target_id, payload_json, attempt_count FROM outbox "
            "WHERE status = 'pending' AND available_at <= ? "
            "ORDER BY available_at, created_at, id LIMIT ?",
            (now_timestamp, limit),
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

    def mark_delivered(self, outbox_id: str, *, message_id: str, delivered_at: datetime) -> None:
        delivered_timestamp = utc_timestamp(delivered_at, parameter="delivered_at")
        with transaction(self.connection, immediate=True):
            outbox = self.connection.execute(
                "SELECT target_id FROM outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            if outbox is None:
                raise ValueError("outbox item does not exist")
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
                "UPDATE outbox SET status = 'delivered' WHERE id = ?", (outbox_id,)
            )

    def reschedule_outbox(self, outbox_id: str, *, available_at: datetime, error: str) -> None:
        available_timestamp = utc_timestamp(available_at, parameter="available_at")
        _validate_error_detail(error)
        with transaction(self.connection):
            cursor = self.connection.execute(
                "UPDATE outbox SET status = 'pending', attempt_count = attempt_count + 1, "
                "available_at = ?, last_error = ? WHERE id = ? AND status = 'pending'",
                (available_timestamp, error, outbox_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("pending outbox item does not exist")


def _validate_error_detail(detail: str | None) -> None:
    if detail is None:
        return
    lowered = detail.casefold()
    unsafe = (
        len(detail) > _ERROR_DETAIL_LIMIT
        or _URL_QUERY.search(detail) is not None
        or _SENSITIVE_RAW_DETAIL.search(lowered) is not None
        or _EXCEPTION_REPR.search(detail) is not None
    )
    if unsafe:
        raise ValueError("safe error detail must be bounded and exclude sensitive raw data")
