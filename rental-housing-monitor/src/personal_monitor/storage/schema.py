from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

_MIGRATION_1 = """
CREATE TABLE users(
  id TEXT PRIMARY KEY, telegram_user_id INTEGER NOT NULL UNIQUE,
  status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE delivery_targets(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
  address TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(owner_id, kind, address), FOREIGN KEY(owner_id) REFERENCES users(id)
);
CREATE TABLE monitors(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
  status TEXT NOT NULL, active_version_id TEXT, next_run_at TEXT,
  lease_owner TEXT, lease_expires_at TEXT, lease_generation INTEGER NOT NULL DEFAULT 0,
  disabled_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY(owner_id) REFERENCES users(id)
);
CREATE TABLE monitor_versions(
  id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL, version_number INTEGER NOT NULL,
  spec_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
  approved_by TEXT, approved_at TEXT, UNIQUE(monitor_id, version_number),
  FOREIGN KEY(monitor_id) REFERENCES monitors(id)
);
CREATE TABLE observations(
  monitor_id TEXT NOT NULL, item_id TEXT NOT NULL, fields_json TEXT NOT NULL,
  content_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  PRIMARY KEY(monitor_id, item_id), FOREIGN KEY(monitor_id) REFERENCES monitors(id)
);
CREATE TABLE runs(
  id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL, version_id TEXT NOT NULL,
  lease_generation INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL,
  fetch_strategy TEXT, status TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, error_class TEXT, error_detail TEXT,
  FOREIGN KEY(monitor_id) REFERENCES monitors(id)
);
CREATE TABLE outbox(
  id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, monitor_id TEXT NOT NULL,
  target_id TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
  last_error TEXT, lease_owner TEXT, lease_expires_at TEXT, created_at TEXT NOT NULL,
  FOREIGN KEY(monitor_id) REFERENCES monitors(id),
  FOREIGN KEY(target_id) REFERENCES delivery_targets(id)
);
CREATE TABLE deliveries(
  outbox_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, external_message_id TEXT NOT NULL,
  delivered_at TEXT NOT NULL, FOREIGN KEY(outbox_id) REFERENCES outbox(id)
);
CREATE TABLE pending_actions(
  token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL, action TEXT NOT NULL,
  payload_json TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT,
  FOREIGN KEY(owner_id) REFERENCES users(id)
);
CREATE TABLE credential_refs(
  id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
  vault_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
  FOREIGN KEY(owner_id) REFERENCES users(id)
);
CREATE INDEX monitors_due_idx ON monitors(status, next_run_at);
CREATE INDEX outbox_due_idx ON outbox(status, available_at);
CREATE INDEX runs_monitor_started_idx ON runs(monitor_id, started_at);
"""

_MIGRATION_2 = """
CREATE TABLE diagnostic_snapshots(
  id TEXT PRIMARY KEY, monitor_id TEXT NOT NULL, ciphertext BLOB NOT NULL,
  nonce BLOB NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  FOREIGN KEY(monitor_id) REFERENCES monitors(id)
);
CREATE INDEX diagnostic_snapshots_expiry_idx ON diagnostic_snapshots(expires_at);
"""

_MIGRATION_3 = """
CREATE TABLE adaptive_features(
  key_hash TEXT PRIMARY KEY, namespace_hash TEXT NOT NULL,
  nonce BLOB NOT NULL, ciphertext BLOB NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE INDEX adaptive_features_namespace_idx ON adaptive_features(namespace_hash);
CREATE INDEX adaptive_features_expiry_idx ON adaptive_features(expires_at);
"""

_MIGRATION_4 = """
ALTER TABLE monitor_versions
ADD COLUMN parent_version_id TEXT REFERENCES monitor_versions(id);
CREATE INDEX monitor_versions_parent_idx ON monitor_versions(parent_version_id);
"""

_MIGRATION_5 = """
CREATE TABLE operator_events(
  id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL, status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX operator_events_due_idx
ON operator_events(status, available_at);
CREATE TABLE health_write_probe(
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  checked_at TEXT NOT NULL
);
"""

_MIGRATION_6 = """
CREATE TABLE rental_shadow_results(
  run_date TEXT PRIMARY KEY, old_hash TEXT NOT NULL, new_hash TEXT NOT NULL,
  matched INTEGER NOT NULL, differences_json TEXT NOT NULL,
  old_status_json TEXT NOT NULL, new_status_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE TABLE rental_duplicate_probe_results(
  monitor_id TEXT PRIMARY KEY, run_date TEXT NOT NULL, current_hash TEXT NOT NULL,
  passed INTEGER NOT NULL, differences_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
  FOREIGN KEY(monitor_id) REFERENCES monitors(id)
);
"""

_MIGRATION_7 = """
CREATE TABLE billing_credit_grants(
  id TEXT PRIMARY KEY, name TEXT NOT NULL,
  original_micros INTEGER NOT NULL, baseline_remaining_micros INTEGER NOT NULL,
  starts_on TEXT NOT NULL, ends_on TEXT NOT NULL, baseline_as_of TEXT NOT NULL,
  baseline_export_consumed_micros INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE billing_snapshots(
  id TEXT PRIMARY KEY, grant_id TEXT NOT NULL,
  observed_at TEXT NOT NULL, source TEXT NOT NULL,
  original_micros INTEGER NOT NULL, remaining_micros INTEGER NOT NULL,
  daily_burn_micros INTEGER NOT NULL, projected_exhaustion_on TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(grant_id) REFERENCES billing_credit_grants(id)
);
CREATE TABLE billing_project_spend(
  snapshot_id TEXT NOT NULL, project_id TEXT NOT NULL,
  project_name TEXT NOT NULL, cost_micros INTEGER NOT NULL,
  PRIMARY KEY(snapshot_id, project_id),
  FOREIGN KEY(snapshot_id) REFERENCES billing_snapshots(id)
);
CREATE TABLE billing_alerts(
  grant_id TEXT NOT NULL, alert_key TEXT NOT NULL, sent_at TEXT NOT NULL,
  PRIMARY KEY(grant_id, alert_key),
  FOREIGN KEY(grant_id) REFERENCES billing_credit_grants(id)
);
CREATE INDEX billing_snapshots_grant_observed_idx
ON billing_snapshots(grant_id, observed_at);
"""

_MIGRATION_8 = """
CREATE TABLE url_aliases(
  owner_id TEXT NOT NULL, normalized_name TEXT NOT NULL,
  url TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(owner_id, normalized_name),
  FOREIGN KEY(owner_id) REFERENCES users(id)
);
"""

_MIGRATIONS = (
    (1, _MIGRATION_1),
    (2, _MIGRATION_2),
    (3, _MIGRATION_3),
    (4, _MIGRATION_4),
    (5, _MIGRATION_5),
    (6, _MIGRATION_6),
    (7, _MIGRATION_7),
    (8, _MIGRATION_8),
)


def open_database(path: str | Path) -> sqlite3.Connection:
    """Open a configured SQLite connection and atomically apply known migrations."""
    database_path = Path(path)
    if str(path) != ":memory:":
        database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, isolation_level=None)
    _configure_connection(connection)
    try:
        _apply_migrations(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def open_existing_database(path: str | Path) -> sqlite3.Connection:
    """Open an initialized database without creating files or applying migrations."""
    database_path = Path(path)
    if str(path) == ":memory:" or not database_path.is_file():
        raise FileNotFoundError("database file does not exist")
    uri = f"file:{quote(str(database_path.resolve()))}?mode=rw"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    _configure_connection(connection)
    try:
        _validate_existing_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")


def _validate_existing_schema(connection: sqlite3.Connection) -> None:
    has_migrations = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if has_migrations is None:
        raise RuntimeError("database is not initialized")
    applied = _validated_migration_history(connection)
    supported = _MIGRATIONS[-1][0]
    if not applied or applied[-1] != supported:
        raise RuntimeError("database schema is incomplete")
    _validate_schema_integrity(connection, supported)


def _apply_migrations(connection: sqlite3.Connection) -> None:
    with transaction(connection, immediate=True):
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = _validated_migration_history(connection)
        _validate_schema_integrity(connection, applied[-1] if applied else 0)
        for version, script in _MIGRATIONS:
            if version in applied:
                continue
            for statement in _statements(script):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, utc_now().isoformat()),
            )
            _validate_schema_integrity(connection, version)


def _validated_migration_history(connection: sqlite3.Connection) -> list[int]:
    try:
        rows = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.Error:
        raise RuntimeError("database migration history is invalid") from None
    supported = _MIGRATIONS[-1][0]
    versions: list[int] = []
    for row in rows:
        version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
        applied_at = row["applied_at"] if isinstance(row, sqlite3.Row) else row[1]
        if type(version) is not int:
            raise RuntimeError("database migration history is invalid")
        if version > supported:
            raise RuntimeError("database migration is newer than supported by this binary")
        if not isinstance(applied_at, str):
            raise RuntimeError("database migration history is invalid")
        try:
            parsed = datetime.fromisoformat(applied_at)
        except ValueError:
            raise RuntimeError("database migration history is invalid") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise RuntimeError("database migration history is invalid")
        versions.append(version)
    known_prefix = [version for version, _ in _MIGRATIONS[: len(versions)]]
    if versions != known_prefix:
        raise RuntimeError("database migration history is invalid")
    return versions


def _validate_schema_integrity(connection: sqlite3.Connection, version: int) -> None:
    if _schema_snapshot(connection) != _expected_schema_snapshot(version):
        raise RuntimeError("database schema integrity check failed")


def _expected_schema_snapshot(version: int) -> tuple[object, ...]:
    expected = sqlite3.connect(":memory:", isolation_level=None)
    expected.row_factory = sqlite3.Row
    expected.execute("PRAGMA foreign_keys = ON")
    try:
        expected.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for migration_version, script in _MIGRATIONS:
            if migration_version > version:
                break
            for statement in _statements(script):
                expected.execute(statement)
        return _schema_snapshot(expected)
    finally:
        expected.close()


def _schema_snapshot(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    object_rows = tuple(tuple(row) for row in objects)
    table_names = [
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in objects
        if (row["type"] if isinstance(row, sqlite3.Row) else row[0]) == "table"
    ]
    tables: list[object] = []
    for table_name in table_names:
        quoted_table = _quoted_identifier(table_name)
        columns = tuple(
            tuple(row) for row in connection.execute(f"PRAGMA table_xinfo({quoted_table})")
        )
        foreign_keys = tuple(
            tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({quoted_table})")
        )
        index_rows = connection.execute(f"PRAGMA index_list({quoted_table})").fetchall()
        indexes: list[object] = []
        for index_row in index_rows:
            index_name = index_row["name"] if isinstance(index_row, sqlite3.Row) else index_row[1]
            indexes.append(
                (
                    tuple(index_row),
                    tuple(
                        tuple(row)
                        for row in connection.execute(
                            f"PRAGMA index_xinfo({_quoted_identifier(index_name)})"
                        )
                    ),
                )
            )
        tables.append(
            (table_name, columns, foreign_keys, tuple(sorted(indexes, key=lambda item: item[0][1])))
        )
    return object_rows, tuple(tables)


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _statements(script: str) -> Iterator[str]:
    for statement in script.split(";"):
        if statement.strip():
            yield statement


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """Run atomically, nesting only non-immediate work inside caller transactions."""
    if connection.in_transaction:
        if immediate:
            raise RuntimeError(
                "immediate transaction cannot start while another transaction is already active"
            )
        savepoint = f"storage_{uuid4().hex}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        return

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_timestamp(value: datetime, *, parameter: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{parameter} must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def parse_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def canonical_json(value: object) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value
