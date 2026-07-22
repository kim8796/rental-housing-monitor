from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.engine.outbox import OutboxWorker
from personal_monitor.storage import RegistryRepository, RuntimeRepository, open_database
from tests.personal_monitor.sql_seed import seed_outbox

NOW = datetime(2026, 7, 22, 3, 15, tzinfo=UTC)


def make_spec() -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": "owner-1",
            "name": "가격 감시",
            "target_url": "https://example.com/list",
            "source_adapter": "scrapling",
            "extract": {
                "item_scope": "main",
                "fields": {"price": {"selector": ".price", "type": "krw"}},
            },
            "validators": {"min_items": 0, "max_items": 10},
            "rules": [{"kind": "new_item"}],
        }
    )


class RecordingSender:
    def __init__(self, result: str | BaseException = "message-1") -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def send(self, address: str, payload: dict[str, object]) -> str:
        self.calls.append((address, payload))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class DeduplicatingHealthSink:
    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.events: list[tuple[str, dict[str, object]]] = []
        self.calls: list[str] = []

    async def emit_once(self, dedupe_key: str, payload: dict[str, object]) -> None:
        self.calls.append(dedupe_key)
        if dedupe_key in self.keys:
            return
        self.keys.add(dedupe_key)
        self.events.append((dedupe_key, payload))


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    value = open_database(":memory:")
    yield value
    value.close()


def configured_worker(
    connection: sqlite3.Connection,
    sender: RecordingSender,
    health: DeduplicatingHealthSink | None = None,
) -> tuple[OutboxWorker, str, RegistryRepository, RuntimeRepository, DeduplicatingHealthSink]:
    registry = RegistryRepository(connection)
    runtime = RuntimeRepository(connection)
    registry.create_user("owner-1", 1)
    registry.create_delivery_target("opaque-target-id", "owner-1", "chat-address-42")
    monitor_id = registry.create_monitor(make_spec(), created_by="owner-1")
    outbox_id = seed_outbox(
        connection,
        dedupe_key="delivery-1",
        monitor_id=monitor_id,
        target_id="opaque-target-id",
        payload={"text": "hello"},
        available_at=NOW,
    )
    connection.execute(
        "UPDATE outbox SET available_at = ? WHERE id = ?", (NOW.isoformat(), outbox_id)
    )
    sink = health or DeduplicatingHealthSink()
    return (
        OutboxWorker(
            runtime=runtime,
            registry=registry,
            sender=sender,
            health_sink=sink,
            worker_id="outbox-worker-1",
        ),
        outbox_id,
        registry,
        runtime,
        sink,
    )


def test_success_resolves_target_address_then_marks_delivery(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingSender("external-message-7")
    worker, outbox_id, _, _, health = configured_worker(connection, sender)

    delivered = asyncio.run(worker.drain_once(now=NOW))

    assert delivered == 1
    assert sender.calls == [("chat-address-42", {"text": "hello"})]
    assert sender.calls[0][0] != "opaque-target-id"
    row = connection.execute(
        "SELECT status, attempt_count, last_error, lease_owner, lease_expires_at "
        "FROM outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert tuple(row) == ("delivered", 0, None, None, None)
    delivery = connection.execute(
        "SELECT external_message_id, delivered_at FROM deliveries WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    assert tuple(delivery) == ("external-message-7", NOW.isoformat())
    assert health.events == []


@pytest.mark.parametrize(
    ("existing_attempts", "delay_seconds"),
    [(0, 60), (1, 300), (2, 1800), (3, 7200), (4, 21600), (5, 21600)],
)
def test_send_failure_keeps_pending_with_closed_code_and_exact_backoff(
    connection: sqlite3.Connection, existing_attempts: int, delay_seconds: int
) -> None:
    secret = "offline GET /private?token=x Cookie=session-secret <html>body</html>"
    sender = RecordingSender(RuntimeError(secret))
    worker, outbox_id, _, _, _ = configured_worker(connection, sender)
    connection.execute(
        "UPDATE outbox SET attempt_count = ? WHERE id = ?", (existing_attempts, outbox_id)
    )

    delivered = asyncio.run(worker.drain_once(now=NOW))

    assert delivered == 0
    row = connection.execute(
        "SELECT status, attempt_count, available_at, last_error FROM outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert tuple(row) == (
        "pending",
        existing_attempts + 1,
        (NOW + timedelta(seconds=delay_seconds)).isoformat(),
        "delivery_failed",
    )
    assert secret not in row["last_error"]
    assert connection.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0


def test_attempt_five_and_later_emit_through_idempotent_health_sink_by_utc_window(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingSender(RuntimeError("offline"))
    health = DeduplicatingHealthSink()
    worker, outbox_id, _, _, _ = configured_worker(connection, sender, health)
    connection.execute("UPDATE outbox SET attempt_count = 4 WHERE id = ?", (outbox_id,))
    korea_now = NOW.astimezone(timezone(timedelta(hours=9)))

    asyncio.run(worker.drain_once(now=korea_now))
    first_key = f"outbox-stuck:{outbox_id}:2026-07-22T00:00:00+00:00"
    assert health.events == [
        (
            first_key,
            {"code": "delivery_failed", "outbox_id": outbox_id, "attempt_count": 5},
        )
    ]
    connection.execute(
        "UPDATE outbox SET available_at = ? WHERE id = ?", (NOW.isoformat(), outbox_id)
    )
    asyncio.run(worker.drain_once(now=NOW + timedelta(hours=2)))

    assert health.calls == [first_key, first_key]
    assert len(health.events) == 1
    row = connection.execute(
        "SELECT status, attempt_count, available_at FROM outbox WHERE id = ?", (outbox_id,)
    ).fetchone()
    assert tuple(row) == (
        "pending",
        6,
        (NOW + timedelta(hours=8)).isoformat(),
    )


def test_missing_target_is_rescheduled_without_sending(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingSender()
    worker, outbox_id, _, _, _ = configured_worker(connection, sender)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM delivery_targets WHERE id = 'opaque-target-id'")
    connection.execute("PRAGMA foreign_keys = ON")

    assert asyncio.run(worker.drain_once(now=NOW)) == 0

    assert sender.calls == []
    row = connection.execute(
        "SELECT status, attempt_count, last_error FROM outbox WHERE id = ?", (outbox_id,)
    ).fetchone()
    assert tuple(row) == ("pending", 1, "delivery_failed")


@pytest.mark.parametrize("message_id", ["", " ", "\t\n"])
def test_empty_or_whitespace_message_id_is_retried_without_marking_delivery(
    connection: sqlite3.Connection, message_id: str
) -> None:
    sender = RecordingSender(message_id)
    worker, outbox_id, _, _, _ = configured_worker(connection, sender)

    assert asyncio.run(worker.drain_once(now=NOW)) == 0

    row = connection.execute(
        "SELECT status, attempt_count, available_at, last_error, lease_owner "
        "FROM outbox WHERE id = ?",
        (outbox_id,),
    ).fetchone()
    assert tuple(row) == (
        "pending",
        1,
        (NOW + timedelta(seconds=60)).isoformat(),
        "delivery_failed",
        None,
    )
    assert connection.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
