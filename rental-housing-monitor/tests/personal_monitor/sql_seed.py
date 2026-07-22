from __future__ import annotations

import sqlite3
from datetime import datetime
from uuid import uuid4

from personal_monitor.domain.observation import ObservationBatch, content_hash
from personal_monitor.storage.schema import canonical_json, utc_timestamp


def seed_snapshot(connection: sqlite3.Connection, batch: ObservationBatch) -> None:
    """Seed prerequisite snapshot state without exposing a production write bypass."""
    observed_at = utc_timestamp(batch.observed_at, parameter="observed_at")
    connection.execute("DELETE FROM observations WHERE monitor_id = ?", (batch.monitor_id,))
    connection.executemany(
        "INSERT INTO observations(monitor_id, item_id, fields_json, content_hash, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            (
                batch.monitor_id,
                item.item_id,
                canonical_json(item.fields),
                content_hash(item.fields),
                observed_at,
                observed_at,
            )
            for item in batch.items
        ),
    )


def seed_outbox(
    connection: sqlite3.Connection,
    *,
    monitor_id: str,
    target_id: str,
    dedupe_key: str,
    payload: dict[str, object],
    available_at: datetime,
) -> str:
    """Seed prerequisite outbox state without exposing a production enqueue bypass."""
    outbox_id = uuid4().hex
    timestamp = utc_timestamp(available_at, parameter="available_at")
    connection.execute(
        "INSERT INTO outbox(id, dedupe_key, monitor_id, target_id, payload_json, status, "
        "available_at, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        (
            outbox_id,
            dedupe_key,
            monitor_id,
            target_id,
            canonical_json(payload),
            timestamp,
            timestamp,
        ),
    )
    return outbox_id
