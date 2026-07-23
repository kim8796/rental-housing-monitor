from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from personal_monitor.observability import (
    HeartbeatMonitor,
    JsonLogFormatter,
    OperatorEventRepository,
    SafeContextFilter,
    configure_json_logging,
    read_backup_status,
)
from personal_monitor.storage import open_database


def test_json_formatter_emits_only_exact_allowlisted_keys() -> None:
    record = logging.LogRecord(
        "personal_monitor.test",
        logging.ERROR,
        __file__,
        1,
        "raw message token=private",
        (),
        None,
    )
    record.event = "scheduler_iteration_failed"
    record.context = {
        "monitor_id": "monitor-1",
        "duration_ms": 12,
        "token": "private",
        "nested": {"authorization": "private"},
        "unknown": "drop",
    }
    record.api_token = "private"
    record.error = RuntimeError("private query")

    assert SafeContextFilter().filter(record)
    assert not hasattr(record, "api_token")
    assert record.msg == ""
    value = json.loads(JsonLogFormatter().format(record))

    assert set(value) == {
        "timestamp",
        "level",
        "logger",
        "event",
        "monitor_id",
        "duration_ms",
        "error_class",
    }
    assert value["event"] == "scheduler_iteration_failed"
    assert value["error_class"] == "RuntimeError"
    assert "private" not in json.dumps(value)


def test_repeated_logger_setup_has_one_rotating_handler(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "monitor.jsonl"

    first = configure_json_logging(path, logger_name="personal_monitor.test.setup")
    second = configure_json_logging(path, logger_name="personal_monitor.test.setup")

    assert first is second
    handlers = [
        handler for handler in first.handlers if getattr(handler, "baseFilename", None) == str(path)
    ]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 10 * 1024 * 1024
    assert handlers[0].backupCount == 5


def test_operator_event_enqueue_is_durable_and_deduplicated(tmp_path: Path) -> None:
    connection = open_database(tmp_path / "monitor.db")
    repository = OperatorEventRepository(connection)
    try:
        assert repository.enqueue_once(
            "codex_login:2026-07-23T00:00:00+00:00",
            {"code": "codex_login_unhealthy"},
            now=datetime(2026, 7, 23, tzinfo=UTC),
        )
        assert not repository.enqueue_once(
            "codex_login:2026-07-23T00:00:00+00:00",
            {"code": "codex_login_unhealthy"},
            now=datetime(2026, 7, 23, 1, tzinfo=UTC),
        )
        assert connection.execute("SELECT COUNT(*) FROM operator_events").fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[]",
        '{"status":"failed","detail":"private"}',
        '{"status":"ok","failed":false,"extra":1}',
    ],
)
def test_backup_status_is_bounded_and_fail_closed(tmp_path: Path, content: str) -> None:
    path = tmp_path / "backup.json"
    path.write_text(content, encoding="utf-8")

    status = read_backup_status(path)

    assert not status.healthy
    assert status.code == "backup_status_invalid"


def test_heartbeat_records_each_probe_independently_and_enqueues_failures(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "monitor.db"
    connection = open_database(database_path)
    backup_path = tmp_path / "backup.json"
    backup_path.write_text(
        '{"schema_version":1,"status":"ok","updated_at":"2026-07-23T00:00:00Z"}',
        encoding="utf-8",
    )

    class HealthyAuth:
        async def check(self) -> None:
            return None

    now = datetime(2026, 7, 23, 1, tzinfo=UTC)
    monitor = HeartbeatMonitor(
        connection=connection,
        database_path=database_path,
        backup_status_path=backup_path,
        auth_guard=HealthyAuth(),
        operator_events=OperatorEventRepository(connection),
        scheduler_last_loop=lambda: None,
        telegram_last_poll=lambda: now,
    )
    try:
        snapshot = asyncio.run(monitor.collect(now=now))

        assert snapshot.db_write.healthy
        assert not snapshot.scheduler_loop.healthy
        assert snapshot.disk_free.healthy
        assert snapshot.telegram_poll.healthy
        assert snapshot.telegram_update.code == "no_updates"
        assert snapshot.outbox_backlog.value == 0
        assert snapshot.codex_login.healthy
        assert snapshot.backup.healthy
        assert connection.execute("SELECT COUNT(*) FROM operator_events").fetchone()[0] == 1
    finally:
        connection.close()
