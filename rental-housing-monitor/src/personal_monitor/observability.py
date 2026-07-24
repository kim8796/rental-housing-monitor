from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final
from uuid import uuid4

from personal_monitor.storage.schema import canonical_json, transaction, utc_timestamp

_OPTIONAL_FIELDS: Final = (
    "monitor_id",
    "run_id",
    "stage",
    "fetch_strategy",
    "duration_ms",
    "retry_count",
    "error_class",
)
_SENSITIVE_KEY_PARTS: Final = (
    "token",
    "cookie",
    "authorization",
    "secret",
    "query",
    "html",
    "message_text",
)
_TRUSTED_EVENTS: Final = frozenset(
    {
        "billing_iteration_failed",
        "heartbeat_iteration_failed",
        "maintenance_iteration_failed",
        "monitor_run_failed",
        "outbox_iteration_failed",
        "scheduler_iteration_failed",
        "telegram_iteration_failed",
        "telegram_update_failed",
    }
)
_SAFE_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
_MAX_BACKUP_STATUS_BYTES: Final = 16 * 1024
_SCHEDULER_MAX_AGE = timedelta(seconds=45)
_TELEGRAM_POLL_MAX_AGE = timedelta(seconds=90)
_MAX_CLOCK_SKEW = timedelta(seconds=5)


def _normalized_key(value: object) -> str:
    try:
        return re.sub(r"[^a-z0-9]", "", str(value).casefold())
    except BaseException:
        return ""


def _sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    return not normalized or any(
        re.sub(r"[^a-z0-9]", "", part) in normalized for part in _SENSITIVE_KEY_PARTS
    )


def _safe_context(value: object, *, depth: int = 0) -> dict[str, object]:
    if depth > 4 or not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    try:
        items = list(value.items())
    except BaseException:
        return {}
    if len(items) > 64:
        return {}
    for raw_key, raw_value in items:
        if type(raw_key) is not str or _sensitive_key(raw_key):
            continue
        if isinstance(raw_value, Mapping):
            _safe_context(raw_value, depth=depth + 1)
            continue
        if raw_key not in _OPTIONAL_FIELDS:
            continue
        normalized = _safe_log_value(raw_key, raw_value)
        if normalized is not None:
            result[raw_key] = normalized
    return result


def _safe_log_value(key: str, value: object) -> str | int | float | None:
    if key in {"duration_ms", "retry_count"}:
        if type(value) is int and 0 <= value <= 2**63 - 1:
            return value
        if key == "duration_ms" and type(value) is float and 0 <= value <= 10**15:
            return value
        return None
    if type(value) is not str or _SAFE_VALUE.fullmatch(value) is None:
        return None
    return value


class SafeContextFilter(logging.Filter):
    """Reduce arbitrary logging extras to the small public diagnostic vocabulary."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            context = _safe_context(getattr(record, "context", {}))
            error = getattr(record, "error", None)
            if isinstance(error, BaseException):
                error_class = _safe_log_value("error_class", type(error).__name__)
                context["error_class"] = error_class or "Error"
            elif "error_class" not in context:
                direct = _safe_log_value("error_class", getattr(record, "error_class", None))
                if direct is not None:
                    context["error_class"] = direct
            for key in tuple(vars(record)):
                if _sensitive_key(key):
                    with suppress(BaseException):
                        delattr(record, key)
            record.context = context
            record.msg = ""
            record.args = ()
            with suppress(BaseException):
                delattr(record, "error")
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        except BaseException:
            try:
                record.context = {}
                record.exc_info = None
                record.exc_text = None
                record.stack_info = None
            except BaseException:
                pass
        return True


class JsonLogFormatter(logging.Formatter):
    """Format only constant event codes and explicitly allowlisted diagnostics."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            raw_event = getattr(record, "event", None)
            event = (
                raw_event
                if type(raw_event) is str and raw_event in _TRUSTED_EVENTS
                else "log_event"
            )
            value: dict[str, object] = {
                "timestamp": datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "level": logging.getLevelName(record.levelno),
                "logger": _safe_logger_name(record.name),
                "event": event,
            }
            context = _safe_context(getattr(record, "context", {}))
            error = getattr(record, "error", None)
            if isinstance(error, BaseException):
                error_class = _safe_log_value("error_class", type(error).__name__)
                context["error_class"] = error_class or "Error"
            for key in _OPTIONAL_FIELDS:
                if key in context:
                    value[key] = context[key]
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except BaseException:
            return (
                '{"timestamp":"1970-01-01T00:00:00.000Z","level":"ERROR",'
                '"logger":"personal_monitor","event":"logging_failure"}'
            )


def _safe_logger_name(value: object) -> str:
    if type(value) is str and _SAFE_VALUE.fullmatch(value) is not None:
        return value
    return "personal_monitor"


def configure_json_logging(
    path: str | Path,
    *,
    logger_name: str = "personal_monitor",
    level: int = logging.INFO,
) -> logging.Logger:
    log_path = Path(path)
    if not log_path.is_absolute():
        raise ValueError("log path must be absolute")
    _ensure_log_parent(log_path.parent)
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = False
    resolved = str(log_path)
    for handler in tuple(logger.handlers):
        if (
            isinstance(handler, RotatingFileHandler)
            and getattr(handler, "_personal_monitor_json_path", None) == resolved
        ):
            return logger
    handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
        delay=True,
    )
    handler.addFilter(SafeContextFilter())
    handler.setFormatter(JsonLogFormatter())
    handler._personal_monitor_json_path = resolved
    logger.addHandler(handler)
    return logger


def _ensure_log_parent(parent: Path) -> None:
    if not parent.is_absolute() or parent != Path(os.path.normpath(parent)):
        raise ValueError("log parent is not a directory")
    ancestor = parent.parent
    try:
        ancestor_metadata = ancestor.lstat()
        if (
            not stat.S_ISDIR(ancestor_metadata.st_mode)
            or ancestor.is_symlink()
            or ancestor_metadata.st_mode & 0o022
            or ancestor_metadata.st_uid not in {0, os.geteuid()}
        ):
            raise ValueError
        with suppress(FileExistsError):
            os.mkdir(parent, 0o700)
        metadata = parent.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or parent.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ValueError
    except (OSError, ValueError):
        raise ValueError("log parent is not a private owned directory") from None


@dataclass(frozen=True, slots=True)
class BackupStatus:
    healthy: bool
    code: str
    updated_at: datetime | None


def read_backup_status(path: str | Path) -> BackupStatus:
    status_path = Path(path)
    try:
        descriptor = os.open(
            status_path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > _MAX_BACKUP_STATUS_BYTES
            ):
                raise ValueError
            data = os.read(descriptor, _MAX_BACKUP_STATUS_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(data) != metadata.st_size:
            raise ValueError
        value = json.loads(
            data.decode("utf-8", "strict"),
            object_pairs_hook=_unique_status_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if type(value) is not dict or set(value) != {
            "schema_version",
            "status",
            "updated_at",
        }:
            raise ValueError
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or type(value["status"]) is not str
            or value["status"] not in {"ok", "failed"}
        ):
            raise ValueError
        if type(value["updated_at"]) is not str or len(value["updated_at"]) > 64:
            raise ValueError
        updated_at = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError
        if value["status"] == "failed":
            return BackupStatus(False, "backup_failed", updated_at.astimezone(UTC))
        return BackupStatus(True, "ok", updated_at.astimezone(UTC))
    except Exception:
        return BackupStatus(False, "backup_status_invalid", None)


def _unique_status_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


class OperatorEventRepository:
    """Durably enqueue operator health events without fake monitor foreign keys."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        if type(connection) is not sqlite3.Connection:
            raise TypeError("connection must be SQLite")
        self.connection = connection

    def enqueue_once(
        self,
        dedupe_key: str,
        payload: Mapping[str, object],
        *,
        now: datetime,
    ) -> bool:
        if (
            type(dedupe_key) is not str
            or not 1 <= len(dedupe_key) <= 512
            or any(ord(character) < 32 for character in dedupe_key)
        ):
            raise ValueError("invalid operator event key")
        timestamp = utc_timestamp(now, parameter="now")
        payload_json = canonical_json(payload)
        if len(payload_json.encode("utf-8")) > 4096:
            raise ValueError("operator event payload is too large")
        with transaction(self.connection, immediate=True):
            if (
                self.connection.execute(
                    "SELECT 1 FROM operator_events WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                is not None
            ):
                return False
            self.connection.execute(
                "INSERT INTO operator_events("
                "id, dedupe_key, payload_json, status, available_at, created_at"
                ") VALUES (?, ?, ?, 'pending', ?, ?)",
                (uuid4().hex, dedupe_key, payload_json, timestamp, timestamp),
            )
        return True

    async def emit_once(self, dedupe_key: str, payload: dict[str, object]) -> None:
        self.enqueue_once(dedupe_key, payload, now=datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ProbeResult:
    healthy: bool
    code: str
    value: int | datetime | None = None


@dataclass(frozen=True, slots=True)
class HeartbeatSnapshot:
    checked_at: datetime
    db_write: ProbeResult
    scheduler_loop: ProbeResult
    disk_free: ProbeResult
    telegram_poll: ProbeResult
    telegram_update: ProbeResult
    outbox_backlog: ProbeResult
    codex_login: ProbeResult
    backup: ProbeResult


class HeartbeatMonitor:
    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        database_path: Path,
        backup_status_path: Path,
        auth_guard: object,
        operator_events: OperatorEventRepository,
        scheduler_last_loop: object,
        telegram_last_poll: object,
        telegram_last_update: object = None,
    ) -> None:
        self.connection = connection
        self.database_path = Path(database_path)
        self.backup_status_path = Path(backup_status_path)
        self.auth_guard = auth_guard
        self.operator_events = operator_events
        self.scheduler_last_loop = scheduler_last_loop
        self.telegram_last_poll = telegram_last_poll
        self.telegram_last_update = telegram_last_update
        self.last_snapshot: HeartbeatSnapshot | None = None

    async def beat(self, *, now: datetime) -> None:
        self.last_snapshot = await self.collect(now=now)

    async def collect(self, *, now: datetime) -> HeartbeatSnapshot:
        checked_at = now.astimezone(UTC)
        db_write = self._db_probe(checked_at)
        scheduler = self._time_probe(
            self.scheduler_last_loop,
            checked_at=checked_at,
            missing_code="scheduler_loop_missing",
            stale_code="scheduler_loop_stale",
            max_age=_SCHEDULER_MAX_AGE,
        )
        disk = self._disk_probe()
        telegram = self._time_probe(
            self.telegram_last_poll,
            checked_at=checked_at,
            missing_code="telegram_poll_missing",
            stale_code="telegram_poll_stale",
            max_age=_TELEGRAM_POLL_MAX_AGE,
        )
        telegram_update = self._optional_time_probe(self.telegram_last_update)
        backlog = self._backlog_probe()
        codex = await self._codex_probe()
        backup_status = read_backup_status(self.backup_status_path)
        backup = ProbeResult(
            backup_status.healthy,
            backup_status.code,
            backup_status.updated_at,
        )
        snapshot = HeartbeatSnapshot(
            checked_at,
            db_write,
            scheduler,
            disk,
            telegram,
            telegram_update,
            backlog,
            codex,
            backup,
        )
        window = checked_at.replace(
            hour=(checked_at.hour // 6) * 6,
            minute=0,
            second=0,
            microsecond=0,
        )
        for name, probe in (
            ("db_write", db_write),
            ("scheduler_loop", scheduler),
            ("disk_free", disk),
            ("telegram_poll", telegram),
            ("telegram_update", telegram_update),
            ("outbox_backlog", backlog),
            ("codex_login", codex),
            ("backup", backup),
        ):
            if not probe.healthy:
                try:
                    self.operator_events.enqueue_once(
                        f"health:{name}:{window.isoformat()}",
                        {"code": probe.code},
                        now=checked_at,
                    )
                except Exception:
                    continue
        return snapshot

    def _db_probe(self, now: datetime) -> ProbeResult:
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO health_write_probe(singleton, checked_at) VALUES (1, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET checked_at = excluded.checked_at",
                    (now.isoformat(),),
                )
            return ProbeResult(True, "ok", now)
        except Exception:
            return ProbeResult(False, "db_write_failed")

    def _time_probe(
        self,
        source: object,
        *,
        checked_at: datetime,
        missing_code: str,
        stale_code: str,
        max_age: timedelta,
    ) -> ProbeResult:
        try:
            value = source() if callable(source) else source
            if value is None:
                return ProbeResult(False, missing_code)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError
            observed_at = value.astimezone(UTC)
            age = checked_at - observed_at
            if age < -_MAX_CLOCK_SKEW:
                return ProbeResult(False, missing_code)
            if age > max_age:
                return ProbeResult(False, stale_code, observed_at)
            return ProbeResult(True, "ok", observed_at)
        except Exception:
            return ProbeResult(False, missing_code)

    def _optional_time_probe(self, source: object) -> ProbeResult:
        try:
            value = source() if callable(source) else source
            if value is None:
                return ProbeResult(True, "no_updates")
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError
            return ProbeResult(True, "ok", value.astimezone(UTC))
        except Exception:
            return ProbeResult(False, "telegram_update_invalid")

    def _disk_probe(self) -> ProbeResult:
        try:
            free = shutil.disk_usage(self.database_path.parent).free
            if type(free) is not int or free < 0:
                raise ValueError
            return ProbeResult(free > 0, "ok" if free > 0 else "disk_full", free)
        except Exception:
            return ProbeResult(False, "disk_probe_failed")

    def _backlog_probe(self) -> ProbeResult:
        try:
            count = self.connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE status = 'pending'"
            ).fetchone()[0]
            if type(count) is not int or count < 0:
                raise ValueError
            return ProbeResult(True, "ok", count)
        except Exception:
            return ProbeResult(False, "outbox_probe_failed")

    async def _codex_probe(self) -> ProbeResult:
        try:
            await asyncio.wait_for(self.auth_guard.check(), timeout=12)
            return ProbeResult(True, "ok")
        except asyncio.CancelledError:
            raise
        except Exception:
            return ProbeResult(False, "codex_login_unhealthy")
