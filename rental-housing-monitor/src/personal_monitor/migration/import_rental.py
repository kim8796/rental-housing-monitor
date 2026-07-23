from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlsplit

from personal_monitor.adapters.rental_housing import (
    RENTAL_ANNOUNCEMENT_FIELDS,
    RENTAL_ITEM_SCOPE,
)
from personal_monitor.domain.observation import ObservedItem, content_hash
from personal_monitor.domain.rules import RuleMatch
from personal_monitor.domain.spec import (
    ExtractSpec,
    FetchStrategy,
    MonitorSpec,
    MonitorStatus,
    RuleKind,
    RuleSpec,
    SourceAdapterKind,
    ValidatorSpec,
)
from personal_monitor.engine.runner import render_payload
from personal_monitor.engine.scheduler import next_run_at
from personal_monitor.storage import open_database
from personal_monitor.storage.schema import (
    _validate_existing_schema,
    canonical_json,
    transaction,
    utc_now,
)
from rental_monitor.models import Agency, Announcement, HousingType, canonical_key

RENTAL_MONITOR_ID = "rental-housing-seoul-gyeonggi"
RENTAL_VERSION_ID = "rental-housing-seoul-gyeonggi:v1"
IMPORT_MARKER_ID = "migration:rental-housing:v1"
RENTAL_TARGET_URL = "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancList.do"

_OWNER_PATTERN = re.compile(r"telegram-user:([1-9][0-9]{0,18})\Z")
_MAX_ROWS = 100_000
_MAX_JSON_BYTES = 4096
_FIXED_TIME = datetime(2000, 1, 1, tzinfo=UTC)
_EXPECTED_TABLES = frozenset({"announcements", "deliveries", "runs"})
_ALLOWED_RUN_STATUS = frozenset({"running", "success", "partial_failure", "telegram_failure"})
_ALLOWED_SOURCE_STATUS = frozenset({"ok", "failed"})


class RentalImportError(RuntimeError):
    """A deliberately redacted import failure."""


@dataclass(frozen=True, slots=True)
class ImportReport:
    source_announcements: int
    source_deliveries: int
    source_runs: int
    imported_observations: int
    imported_outbox: int
    imported_deliveries: int
    already_present_observations: int
    already_present_outbox: int
    already_present_deliveries: int
    identity_created: bool
    target_created: bool
    monitor_created: bool
    version_created: bool
    dry_run: bool
    import_complete: bool
    status: str

    @property
    def announcements_imported(self) -> int:
        return self.imported_observations

    @property
    def deliveries_imported(self) -> int:
        return self.imported_deliveries

    def with_no_new_rows(self) -> ImportReport:
        return replace(
            self,
            imported_observations=0,
            imported_outbox=0,
            imported_deliveries=0,
            already_present_observations=(
                self.already_present_observations + self.imported_observations
            ),
            already_present_outbox=self.already_present_outbox + self.imported_outbox,
            already_present_deliveries=(self.already_present_deliveries + self.imported_deliveries),
            identity_created=False,
            target_created=False,
            monitor_created=False,
            version_created=False,
        )


@dataclass(frozen=True, slots=True, repr=False)
class _AnnouncementRow:
    key: str
    fields: Mapping[str, str | None]
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True, slots=True, repr=False)
class _DeliveryRow:
    key: str
    chat_id: str
    delivered_at: str
    message_id: str


@dataclass(frozen=True, slots=True, repr=False)
class _SourceSnapshot:
    announcements: tuple[_AnnouncementRow, ...]
    deliveries: tuple[_DeliveryRow, ...]
    run_count: int
    evidence_time: str


@dataclass(slots=True)
class _ImportCounts:
    imported_observations: int = 0
    imported_outbox: int = 0
    imported_deliveries: int = 0
    already_present_observations: int = 0
    already_present_outbox: int = 0
    already_present_deliveries: int = 0
    identity_created: bool = False
    target_created: bool = False
    monitor_created: bool = False
    version_created: bool = False
    import_complete: bool = False


def import_rental_state(
    old_path: str | Path,
    new_path: str | Path,
    owner_id: str,
    target_id: str,
    *,
    dry_run: bool = False,
) -> ImportReport:
    """Validate and atomically import the fixed legacy rental monitor aggregate."""
    try:
        if type(dry_run) is not bool:
            raise RentalImportError("dry-run mode is invalid")
        source_path, target_path = _validated_paths(old_path, new_path)
        telegram_user_id = _telegram_user_id(owner_id)
        _validate_target_id(target_id)
        source = _read_source(source_path)
        target, _target_exists = _open_target(target_path, dry_run=dry_run)
        try:
            counts = _map_target(
                target,
                source,
                owner_id,
                telegram_user_id,
                target_id,
                dry_run=dry_run,
            )
        finally:
            target.close()
        status = (
            "complete" if counts.import_complete else ("validated" if dry_run else "incomplete")
        )
        return ImportReport(
            source_announcements=len(source.announcements),
            source_deliveries=len(source.deliveries),
            source_runs=source.run_count,
            imported_observations=counts.imported_observations,
            imported_outbox=counts.imported_outbox,
            imported_deliveries=counts.imported_deliveries,
            already_present_observations=counts.already_present_observations,
            already_present_outbox=counts.already_present_outbox,
            already_present_deliveries=counts.already_present_deliveries,
            identity_created=counts.identity_created,
            target_created=counts.target_created,
            monitor_created=counts.monitor_created,
            version_created=counts.version_created,
            dry_run=dry_run,
            import_complete=counts.import_complete,
            status=status,
        )
    except RentalImportError:
        raise
    except Exception:
        raise RentalImportError("rental import failed") from None


def _validated_paths(old_path: str | Path, new_path: str | Path) -> tuple[Path, Path]:
    source = _normalized_absolute_path(old_path)
    target = _normalized_absolute_path(new_path)
    source_stat = _regular_file_stat(source, required=True)
    target_stat = _regular_file_stat(target, required=False)
    if source == target:
        raise RentalImportError("source and target must differ")
    if target_stat is not None and (
        source_stat.st_dev == target_stat.st_dev and source_stat.st_ino == target_stat.st_ino
    ):
        raise RentalImportError("source and target must differ")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{source}{suffix}").exists():
            raise RentalImportError("legacy database is not a closed snapshot")
    return source, target


def _normalized_absolute_path(value: str | Path) -> Path:
    if not isinstance(value, str | Path):
        raise RentalImportError("invalid database path")
    raw = os.fspath(value)
    if type(raw) is not str or "\x00" in raw:
        raise RentalImportError("invalid database path")
    path = Path(raw)
    normalized = os.path.normpath(raw)
    if not path.is_absolute() or raw != normalized or path != Path(normalized):
        raise RentalImportError("invalid database path")
    _reject_symlink_components(path)
    return path


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise RentalImportError("database path contains a symlink")


def _regular_file_stat(path: Path, *, required: bool) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise RentalImportError("database file is missing") from None
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RentalImportError("database path is not a regular file")
    return metadata


def _telegram_user_id(owner_id: str) -> int:
    if type(owner_id) is not str:
        raise RentalImportError("owner identity is invalid")
    match = _OWNER_PATTERN.fullmatch(owner_id)
    if match is None:
        raise RentalImportError("owner identity is invalid")
    numeric = int(match.group(1))
    if numeric > 9_223_372_036_854_775_807:
        raise RentalImportError("owner identity is invalid")
    return numeric


def _validate_target_id(target_id: str) -> None:
    if not _valid_opaque_identifier(target_id):
        raise RentalImportError("target identity is invalid")


def _valid_opaque_identifier(value: object) -> bool:
    if type(value) is not str or not value or value != value.strip():
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return 1 <= size <= 128 and not any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(os.fspath(path), safe='/')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _read_source(path: Path) -> _SourceSnapshot:
    connection: sqlite3.Connection | None = None
    try:
        connection = _read_only_connection(path)
        connection.execute("BEGIN")
        _validate_source_schema(connection)
        _validate_source_integrity(connection)
        announcement_rows = _bounded_rows(
            connection,
            "SELECT announcement_key, source_id, title, agency, region, housing_type, "
            "target, announcement_date, application_start_date, application_end_date, "
            "url, first_seen_at, last_seen_at FROM announcements ORDER BY announcement_key",
            "announcements",
        )
        announcements = tuple(_announcement(row) for row in announcement_rows)
        keys = {row.key for row in announcements}
        if len(keys) != len(announcements):
            raise RentalImportError("legacy announcements keys are invalid")
        delivery_rows = _bounded_rows(
            connection,
            "SELECT announcement_key, chat_id, delivered_at, message_id "
            "FROM deliveries ORDER BY announcement_key, chat_id",
            "deliveries",
        )
        deliveries = tuple(_delivery(row, keys) for row in delivery_rows)
        run_rows = _bounded_rows(
            connection,
            "SELECT id, started_at, finished_at, status, new_count, agency_status "
            "FROM runs ORDER BY id",
            "runs",
        )
        evidence = [_validate_run(row) for row in run_rows]
        timestamps = [
            *(row.first_seen_at for row in announcements),
            *(row.last_seen_at for row in announcements),
            *(row.delivered_at for row in deliveries),
            *evidence,
        ]
        evidence_time = max(timestamps, default=_FIXED_TIME.isoformat())
        connection.rollback()
        return _SourceSnapshot(
            announcements=announcements,
            deliveries=deliveries,
            run_count=len(run_rows),
            evidence_time=evidence_time,
        )
    except RentalImportError:
        raise
    except sqlite3.Error:
        raise RentalImportError("legacy database validation failed") from None
    finally:
        if connection is not None:
            connection.close()


def _validate_source_schema(connection: sqlite3.Connection) -> None:
    objects = connection.execute(
        "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if {(row["type"], row["name"]) for row in objects} != {
        ("table", name) for name in _EXPECTED_TABLES
    }:
        raise RentalImportError("legacy database schema is unsupported")
    expected_columns = {
        "announcements": (
            ("announcement_key", "TEXT", 0, None, 1),
            ("source_id", "TEXT", 0, None, 0),
            ("title", "TEXT", 1, None, 0),
            ("agency", "TEXT", 1, None, 0),
            ("region", "TEXT", 1, None, 0),
            ("housing_type", "TEXT", 1, None, 0),
            ("target", "TEXT", 1, None, 0),
            ("announcement_date", "TEXT", 1, None, 0),
            ("application_start_date", "TEXT", 0, None, 0),
            ("application_end_date", "TEXT", 0, None, 0),
            ("url", "TEXT", 1, None, 0),
            ("first_seen_at", "TEXT", 1, None, 0),
            ("last_seen_at", "TEXT", 1, None, 0),
        ),
        "deliveries": (
            ("announcement_key", "TEXT", 1, None, 1),
            ("chat_id", "TEXT", 1, None, 2),
            ("delivered_at", "TEXT", 1, None, 0),
            ("message_id", "INTEGER", 1, None, 0),
        ),
        "runs": (
            ("id", "INTEGER", 0, None, 1),
            ("started_at", "TEXT", 1, None, 0),
            ("finished_at", "TEXT", 0, None, 0),
            ("status", "TEXT", 1, None, 0),
            ("new_count", "INTEGER", 1, "0", 0),
            ("agency_status", "TEXT", 1, "'{}'", 0),
        ),
    }
    for table, expected in expected_columns.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        actual = tuple(
            (
                row["name"],
                row["type"].upper(),
                row["notnull"],
                row["dflt_value"],
                row["pk"],
            )
            for row in rows
        )
        if actual != expected:
            raise RentalImportError(f"legacy {table} schema is unsupported")
    announcement_indexes = connection.execute("PRAGMA index_list(announcements)").fetchall()
    delivery_indexes = connection.execute("PRAGMA index_list(deliveries)").fetchall()
    run_indexes = connection.execute("PRAGMA index_list(runs)").fetchall()
    if (
        len(announcement_indexes) != 1
        or announcement_indexes[0]["origin"] != "pk"
        or len(delivery_indexes) != 1
        or delivery_indexes[0]["origin"] != "pk"
        or run_indexes
    ):
        raise RentalImportError("legacy database indexes are unsupported")
    foreign_keys = connection.execute("PRAGMA foreign_key_list(deliveries)").fetchall()
    if len(foreign_keys) != 1:
        raise RentalImportError("legacy database foreign keys are unsupported")
    foreign_key = foreign_keys[0]
    if (
        foreign_key["table"],
        foreign_key["from"],
        foreign_key["to"],
        foreign_key["on_update"],
        foreign_key["on_delete"],
        foreign_key["match"],
    ) != (
        "announcements",
        "announcement_key",
        "announcement_key",
        "NO ACTION",
        "NO ACTION",
        "NONE",
    ):
        raise RentalImportError("legacy database foreign keys are unsupported")
    for table in ("announcements", "runs"):
        if connection.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            raise RentalImportError("legacy database foreign keys are unsupported")


def _validate_source_integrity(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check(100)").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        raise RentalImportError("legacy database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RentalImportError("legacy database foreign key check failed")


def _bounded_rows(connection: sqlite3.Connection, statement: str, table: str) -> list[sqlite3.Row]:
    cursor = connection.execute(statement)
    rows = cursor.fetchmany(_MAX_ROWS + 1)
    if len(rows) > _MAX_ROWS:
        raise RentalImportError(f"legacy {table} row limit exceeded")
    return rows


def _announcement(row: sqlite3.Row) -> _AnnouncementRow:
    key_for_error = row["announcement_key"]
    try:
        key = _bounded_text(key_for_error, 256, nonempty=True)
        source_id = _optional_text(row["source_id"], 256, nonempty=True)
        title = _bounded_text(row["title"], 1000, nonempty=True)
        agency = Agency(_bounded_text(row["agency"], 2, nonempty=True))
        region = _bounded_text(row["region"], 200, nonempty=True)
        housing_type = HousingType(_bounded_text(row["housing_type"], 100, nonempty=True))
        target = _bounded_text(row["target"], 1000, nonempty=True)
        announcement_date = _canonical_date(row["announcement_date"])
        application_start = _optional_date(row["application_start_date"])
        application_end = _optional_date(row["application_end_date"])
        url = _validated_url(row["url"])
        first_seen_at = _canonical_timestamp(row["first_seen_at"])
        last_seen_at = _canonical_timestamp(row["last_seen_at"])
        if datetime.fromisoformat(first_seen_at) > datetime.fromisoformat(last_seen_at):
            raise ValueError
        announcement = Announcement(
            source_id=source_id,
            title=title,
            agency=agency,
            region=region,
            housing_type=housing_type,
            target=target,
            announcement_date=date.fromisoformat(announcement_date),
            application_start_date=(
                date.fromisoformat(application_start) if application_start is not None else None
            ),
            application_end_date=(
                date.fromisoformat(application_end) if application_end is not None else None
            ),
            url=url,
        )
        if canonical_key(announcement) != key:
            raise ValueError
        fields = {
            "source_id": source_id,
            "title": title,
            "agency": agency.value,
            "region": region,
            "housing_type": housing_type.value,
            "target": target,
            "announcement_date": announcement_date,
            "application_start_date": application_start,
            "application_end_date": application_end,
            "url": url,
        }
        return _AnnouncementRow(
            key=key,
            fields=fields,
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
        )
    except (TypeError, ValueError):
        raise _row_error("announcements", key_for_error) from None


def _delivery(row: sqlite3.Row, announcement_keys: set[str]) -> _DeliveryRow:
    key_for_error = row["announcement_key"]
    try:
        key = _bounded_text(key_for_error, 256, nonempty=True)
        if key not in announcement_keys:
            raise ValueError
        chat_id = _bounded_text(row["chat_id"], 128, nonempty=True)
        if not _valid_opaque_identifier(chat_id):
            raise ValueError
        delivered_at = _canonical_timestamp(row["delivered_at"])
        message_id = row["message_id"]
        if type(message_id) is not int or message_id <= 0 or message_id > 9_223_372_036_854_775_807:
            raise ValueError
        return _DeliveryRow(key, chat_id, delivered_at, str(message_id))
    except (TypeError, ValueError):
        raise _row_error("deliveries", key_for_error) from None


def _validate_run(row: sqlite3.Row) -> str:
    key_for_error = row["id"]
    try:
        identifier = row["id"]
        if type(identifier) is not int or identifier <= 0:
            raise ValueError
        started_at = _canonical_timestamp(row["started_at"])
        finished_raw = row["finished_at"]
        finished_at = None if finished_raw is None else _canonical_timestamp(finished_raw)
        status = _bounded_text(row["status"], 32, nonempty=True)
        if status not in _ALLOWED_RUN_STATUS:
            raise ValueError
        if status == "running":
            if finished_at is not None:
                raise ValueError
        elif finished_at is None or datetime.fromisoformat(finished_at) < datetime.fromisoformat(
            started_at
        ):
            raise ValueError
        new_count = row["new_count"]
        if type(new_count) is not int or not 0 <= new_count <= _MAX_ROWS:
            raise ValueError
        agency_status = _agency_status(row["agency_status"])
        if status != "running" and not agency_status and new_count:
            raise ValueError
        return finished_at or started_at
    except (TypeError, ValueError):
        raise _row_error("runs", key_for_error) from None


def _agency_status(value: object) -> Mapping[str, str]:
    text = _bounded_text(value, _MAX_JSON_BYTES, nonempty=True)
    if len(text.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    parsed = json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
    )
    if type(parsed) is not dict or any(
        type(key) is not str
        or key not in {"LH", "SH", "GH"}
        or type(item) is not str
        or item not in _ALLOWED_SOURCE_STATUS
        for key, item in parsed.items()
    ):
        raise ValueError
    return parsed


def _bounded_text(value: object, max_bytes: int, *, nonempty: bool) -> str:
    if type(value) is not str:
        raise TypeError
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes or "\x00" in value or (nonempty and not value.strip()):
        raise ValueError
    return value


def _optional_text(value: object, max_bytes: int, *, nonempty: bool) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, max_bytes, nonempty=nonempty)


def _canonical_date(value: object) -> str:
    text = _bounded_text(value, 10, nonempty=True)
    parsed = date.fromisoformat(text)
    if not 1900 <= parsed.year <= 2200 or parsed.isoformat() != text:
        raise ValueError
    return text


def _optional_date(value: object) -> str | None:
    return None if value is None else _canonical_date(value)


def _canonical_timestamp(value: object) -> str:
    text = _bounded_text(value, 64, nonempty=True)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC).isoformat()


def _validated_url(value: object) -> str:
    text = _bounded_text(value, 2048, nonempty=True)
    parts = urlsplit(text)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or any(ord(character) < 32 for character in text)
    ):
        raise ValueError
    return text


def _row_error(table: str, key: object) -> RentalImportError:
    code = sha256(repr((type(key).__name__, key)).encode("utf-8")).hexdigest()[:12]
    return RentalImportError(f"legacy {table} row is invalid ({code})")


def _open_target(path: Path, *, dry_run: bool) -> tuple[sqlite3.Connection, bool]:
    exists = path.exists()
    if dry_run:
        if not exists:
            return open_database(":memory:"), False
        connection = _read_only_connection(path)
        try:
            _validate_existing_schema(connection)
        except BaseException:
            connection.close()
            raise
        return connection, True
    deadline = time.monotonic() + 5.0
    while True:
        try:
            return open_database(path), exists
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).casefold() or time.monotonic() >= deadline:
                raise
            time.sleep(0.025)


def _rental_spec(owner_id: str) -> MonitorSpec:
    return MonitorSpec(
        schema_version=1,
        owner_id=owner_id,
        name="서울·경기 임대주택",
        target_url=RENTAL_TARGET_URL,
        source_adapter=SourceAdapterKind.PYTHON_PLUGIN,
        adapter_ref="rental_housing",
        fetch_strategy=FetchStrategy.HTTP,
        schedule="13 12 * * *",
        timezone="Asia/Seoul",
        extract=ExtractSpec(
            item_scope=RENTAL_ITEM_SCOPE,
            fields=RENTAL_ANNOUNCEMENT_FIELDS,
        ),
        validators=ValidatorSpec(
            min_items=0,
            max_items=10_000,
            allowed_link_domains=(
                "apply.lh.or.kr",
                "www.gh.or.kr",
                "www.i-sh.co.kr",
            ),
        ),
        rules=(RuleSpec(kind=RuleKind.NEW_ITEM),),
        notify_on_no_change=True,
    )


def _map_target(
    connection: sqlite3.Connection,
    source: _SourceSnapshot,
    owner_id: str,
    telegram_user_id: int,
    target_id: str,
    *,
    dry_run: bool,
) -> _ImportCounts:
    counts = _ImportCounts()
    context = nullcontext() if dry_run else transaction(connection, immediate=True)
    with context:
        _phase_hook("identity")
        target_address = _target_address(connection, source, owner_id, telegram_user_id, target_id)
        counts.identity_created = _ensure_user(
            connection, owner_id, telegram_user_id, source.evidence_time, dry_run
        )
        counts.target_created = _ensure_target(
            connection,
            target_id,
            owner_id,
            target_address,
            source.evidence_time,
            dry_run,
        )
        spec = _rental_spec(owner_id)
        _phase_hook("version")
        counts.monitor_created, counts.version_created = _ensure_monitor_version(
            connection, owner_id, spec, source.evidence_time, dry_run
        )
        _phase_hook("observation")
        for row in source.announcements:
            created = _ensure_observation(connection, row, dry_run)
            if created:
                counts.imported_observations += 1
            else:
                counts.already_present_observations += 1
        selected_deliveries = tuple(
            row for row in source.deliveries if row.chat_id == target_address
        )
        _phase_hook("outbox")
        outbox_rows: list[tuple[str, _DeliveryRow]] = []
        by_key = {row.key: row for row in source.announcements}
        for row in selected_deliveries:
            outbox_id, created = _ensure_outbox(
                connection, row, by_key[row.key], spec, target_id, dry_run
            )
            outbox_rows.append((outbox_id, row))
            if created:
                counts.imported_outbox += 1
            else:
                counts.already_present_outbox += 1
        _phase_hook("delivery")
        for outbox_id, row in outbox_rows:
            created = _ensure_delivery(connection, outbox_id, target_id, row, dry_run)
            if created:
                counts.imported_deliveries += 1
            else:
                counts.already_present_deliveries += 1
        _phase_hook("marker")
        counts.import_complete = _ensure_marker(connection, owner_id, source.evidence_time, dry_run)
    return counts


def _phase_hook(_phase: str) -> None:
    """Test seam for asserting all mapping phases share one transaction."""


def _target_address(
    connection: sqlite3.Connection,
    source: _SourceSnapshot,
    owner_id: str,
    telegram_user_id: int,
    target_id: str,
) -> str:
    chat_ids = {row.chat_id for row in source.deliveries}
    existing = connection.execute(
        "SELECT owner_id, kind, address FROM delivery_targets WHERE id = ?",
        (target_id,),
    ).fetchone()
    if existing is not None:
        if (
            existing["owner_id"] != owner_id
            or existing["kind"] != "telegram"
            or not _valid_chat_address(existing["address"])
        ):
            raise RentalImportError("target aggregate conflicts")
        address = existing["address"]
        if chat_ids and address not in chat_ids:
            raise RentalImportError("legacy delivery target conflicts")
        return address
    if len(chat_ids) > 1:
        raise RentalImportError("legacy delivery target is ambiguous")
    return next(iter(chat_ids), str(telegram_user_id))


def _valid_chat_address(value: object) -> bool:
    return _valid_opaque_identifier(value)


def _valid_stored_timestamp(value: object) -> bool:
    try:
        return _canonical_timestamp(value) == value
    except (TypeError, ValueError):
        return False


def _ensure_user(
    connection: sqlite3.Connection,
    owner_id: str,
    telegram_user_id: int,
    timestamp: str,
    dry_run: bool,
) -> bool:
    row = connection.execute(
        "SELECT telegram_user_id, status, created_at FROM users WHERE id = ?",
        (owner_id,),
    ).fetchone()
    if row is not None:
        if (
            row["telegram_user_id"] != telegram_user_id
            or row["status"] != "active"
            or not _valid_stored_timestamp(row["created_at"])
        ):
            raise RentalImportError("owner aggregate conflicts")
        return False
    collision = connection.execute(
        "SELECT 1 FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()
    if collision is not None:
        raise RentalImportError("owner aggregate conflicts")
    if not dry_run:
        connection.execute(
            "INSERT INTO users(id, telegram_user_id, status, created_at) "
            "VALUES (?, ?, 'active', ?)",
            (owner_id, telegram_user_id, timestamp),
        )
    return True


def _ensure_target(
    connection: sqlite3.Connection,
    target_id: str,
    owner_id: str,
    address: str,
    timestamp: str,
    dry_run: bool,
) -> bool:
    row = connection.execute(
        "SELECT owner_id, kind, address, created_at FROM delivery_targets WHERE id = ?",
        (target_id,),
    ).fetchone()
    if row is not None:
        if (
            row["owner_id"],
            row["kind"],
            row["address"],
        ) != (owner_id, "telegram", address) or not _valid_stored_timestamp(row["created_at"]):
            raise RentalImportError("target aggregate conflicts")
        return False
    collision = connection.execute(
        "SELECT 1 FROM delivery_targets WHERE owner_id = ? AND kind = 'telegram' AND address = ?",
        (owner_id, address),
    ).fetchone()
    if collision is not None:
        raise RentalImportError("target aggregate conflicts")
    if not dry_run:
        connection.execute(
            "INSERT INTO delivery_targets(id, owner_id, kind, address, created_at) "
            "VALUES (?, ?, 'telegram', ?, ?)",
            (target_id, owner_id, address, timestamp),
        )
    return True


def _ensure_monitor_version(
    connection: sqlite3.Connection,
    owner_id: str,
    spec: MonitorSpec,
    timestamp: str,
    dry_run: bool,
) -> tuple[bool, bool]:
    spec_json = canonical_json(spec.model_dump(mode="json"))
    monitor = connection.execute(
        "SELECT owner_id, name, status, active_version_id, disabled_at, created_at, "
        "updated_at FROM monitors WHERE id = ?",
        (RENTAL_MONITOR_ID,),
    ).fetchone()
    monitor_created = monitor is None
    if monitor is not None and (
        (
            monitor["owner_id"],
            monitor["name"],
            monitor["status"],
            monitor["active_version_id"],
            monitor["disabled_at"],
        )
        != (
            owner_id,
            spec.name,
            MonitorStatus.ACTIVE.value,
            RENTAL_VERSION_ID,
            None,
        )
        or not _valid_stored_timestamp(monitor["created_at"])
        or not _valid_stored_timestamp(monitor["updated_at"])
    ):
        raise RentalImportError("monitor aggregate conflicts")
    version = connection.execute(
        "SELECT monitor_id, version_number, spec_json, created_by, created_at, "
        "approved_by, approved_at, parent_version_id FROM monitor_versions WHERE id = ?",
        (RENTAL_VERSION_ID,),
    ).fetchone()
    version_created = version is None
    if version is not None and (
        (
            version["monitor_id"],
            version["version_number"],
            version["spec_json"],
            version["created_by"],
            version["approved_by"],
            version["parent_version_id"],
        )
        != (
            RENTAL_MONITOR_ID,
            1,
            spec_json,
            owner_id,
            owner_id,
            None,
        )
        or not _valid_stored_timestamp(version["created_at"])
        or not _valid_stored_timestamp(version["approved_at"])
    ):
        raise RentalImportError("monitor version conflicts")
    if monitor_created != version_created:
        raise RentalImportError("monitor aggregate is incomplete")
    if not dry_run and monitor_created:
        scheduled = next_run_at(spec, RENTAL_MONITOR_ID, utc_now()).isoformat()
        connection.execute(
            "INSERT INTO monitors(id, owner_id, name, status, active_version_id, "
            "next_run_at, lease_owner, lease_expires_at, lease_generation, disabled_at, "
            "created_at, updated_at) VALUES (?, ?, ?, 'active', NULL, ?, NULL, NULL, 0, "
            "NULL, ?, ?)",
            (RENTAL_MONITOR_ID, owner_id, spec.name, scheduled, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO monitor_versions(id, monitor_id, version_number, spec_json, "
            "created_by, created_at, approved_by, approved_at, parent_version_id) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?, NULL)",
            (
                RENTAL_VERSION_ID,
                RENTAL_MONITOR_ID,
                spec_json,
                owner_id,
                timestamp,
                owner_id,
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE monitors SET active_version_id = ? WHERE id = ?",
            (RENTAL_VERSION_ID, RENTAL_MONITOR_ID),
        )
    return monitor_created, version_created


def _ensure_observation(
    connection: sqlite3.Connection, row: _AnnouncementRow, dry_run: bool
) -> bool:
    item_id = f"announcement:{row.key}"
    fields_json = canonical_json(row.fields)
    digest = content_hash(row.fields)
    existing = connection.execute(
        "SELECT fields_json, content_hash, first_seen_at, last_seen_at "
        "FROM observations WHERE monitor_id = ? AND item_id = ?",
        (RENTAL_MONITOR_ID, item_id),
    ).fetchone()
    expected = (fields_json, digest, row.first_seen_at, row.last_seen_at)
    if existing is not None:
        if tuple(existing) != expected:
            raise RentalImportError("observation aggregate conflicts")
        return False
    if not dry_run:
        connection.execute(
            "INSERT INTO observations(monitor_id, item_id, fields_json, content_hash, "
            "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            (RENTAL_MONITOR_ID, item_id, *expected),
        )
    return True


def _outbox_id(dedupe_key: str) -> str:
    return f"rental-import:{sha256(dedupe_key.encode('utf-8')).hexdigest()}"


def _ensure_outbox(
    connection: sqlite3.Connection,
    delivery: _DeliveryRow,
    announcement: _AnnouncementRow,
    spec: MonitorSpec,
    target_id: str,
    dry_run: bool,
) -> tuple[str, bool]:
    item_id = f"announcement:{delivery.key}"
    dedupe_key = f"{RENTAL_MONITOR_ID}:{item_id}:new_item"
    outbox_id = _outbox_id(dedupe_key)
    payload = render_payload(
        spec,
        ObservedItem(item_id=item_id, fields=announcement.fields),
        RuleMatch(RuleKind.NEW_ITEM, None, None, None),
    )
    payload_json = canonical_json(payload)
    existing = connection.execute(
        "SELECT id, monitor_id, target_id, payload_json, status, attempt_count, "
        "available_at, last_error, lease_owner, lease_expires_at, created_at "
        "FROM outbox WHERE dedupe_key = ?",
        (dedupe_key,),
    ).fetchone()
    expected = (
        outbox_id,
        RENTAL_MONITOR_ID,
        target_id,
        payload_json,
        "delivered",
        0,
        delivery.delivered_at,
        None,
        None,
        None,
        delivery.delivered_at,
    )
    if existing is not None:
        if tuple(existing) != expected:
            raise RentalImportError("outbox aggregate conflicts")
        return outbox_id, False
    id_collision = connection.execute("SELECT 1 FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
    if id_collision is not None:
        raise RentalImportError("outbox aggregate conflicts")
    if not dry_run:
        connection.execute(
            "INSERT INTO outbox(id, dedupe_key, monitor_id, target_id, payload_json, "
            "status, attempt_count, available_at, last_error, lease_owner, "
            "lease_expires_at, created_at) VALUES (?, ?, ?, ?, ?, 'delivered', 0, ?, "
            "NULL, NULL, NULL, ?)",
            (
                outbox_id,
                dedupe_key,
                RENTAL_MONITOR_ID,
                target_id,
                payload_json,
                delivery.delivered_at,
                delivery.delivered_at,
            ),
        )
    return outbox_id, True


def _ensure_delivery(
    connection: sqlite3.Connection,
    outbox_id: str,
    target_id: str,
    row: _DeliveryRow,
    dry_run: bool,
) -> bool:
    existing = connection.execute(
        "SELECT target_id, external_message_id, delivered_at FROM deliveries WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    expected = (target_id, row.message_id, row.delivered_at)
    if existing is not None:
        if tuple(existing) != expected:
            raise RentalImportError("delivery aggregate conflicts")
        return False
    if not dry_run:
        connection.execute(
            "INSERT INTO deliveries(outbox_id, target_id, external_message_id, "
            "delivered_at) VALUES (?, ?, ?, ?)",
            (outbox_id, *expected),
        )
    return True


def _ensure_marker(
    connection: sqlite3.Connection,
    owner_id: str,
    timestamp: str,
    dry_run: bool,
) -> bool:
    row = connection.execute(
        "SELECT monitor_id, version_id, lease_generation, stage, fetch_strategy, "
        "status, started_at, finished_at, error_class, error_detail FROM runs WHERE id = ?",
        (IMPORT_MARKER_ID,),
    ).fetchone()
    expected = (
        RENTAL_MONITOR_ID,
        RENTAL_VERSION_ID,
        0,
        "migration_import",
        None,
        "success",
        timestamp,
        timestamp,
        None,
        None,
    )
    if row is not None:
        if tuple(row) != expected:
            raise RentalImportError("import marker conflicts")
        return True
    if not dry_run:
        connection.execute(
            "INSERT INTO runs(id, monitor_id, version_id, lease_generation, stage, "
            "fetch_strategy, status, started_at, finished_at, error_class, error_detail) "
            "VALUES (?, ?, ?, 0, 'migration_import', NULL, 'success', ?, ?, NULL, NULL)",
            (
                IMPORT_MARKER_ID,
                RENTAL_MONITOR_ID,
                RENTAL_VERSION_ID,
                timestamp,
                timestamp,
            ),
        )
        return True
    return False
