from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.maintenance import Maintenance
from personal_monitor.storage import open_database
from personal_monitor.storage.schema import canonical_json, utc_now


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personal-monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-spec")
    validate.add_argument("path", type=Path)
    database = sub.add_parser("database")
    database.add_argument("action", choices=("init", "integrity-check", "vacuum"))
    database.add_argument("--path", type=Path, required=True)
    maintenance = sub.add_parser("maintenance")
    maintenance.add_argument("action", choices=("run",))
    maintenance.add_argument("--database", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "validate-spec":
        return _validate_spec(arguments.path)
    if arguments.command == "database":
        return _database(arguments.action, arguments.path)
    if arguments.command == "maintenance":
        return _maintenance(arguments.database)
    return 2


def _validate_spec(path: Path) -> int:
    try:
        spec = MonitorSpec.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("invalid monitor specification", file=sys.stderr)
        return 2
    print(canonical_json(spec.model_dump(mode="json")))
    return 0


def _database(action: str, path: Path) -> int:
    connection: sqlite3.Connection | None = None
    try:
        connection = open_database(path)
        if action == "init":
            return 0
        if action == "integrity-check":
            rows = list(connection.execute("PRAGMA integrity_check"))
            if len(rows) == 1 and rows[0][0] == "ok":
                return 0
            print("database integrity check failed", file=sys.stderr)
            return 1
        connection.execute("VACUUM")
        return 0
    except (OSError, sqlite3.Error, ValueError):
        print("database command failed", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


def _maintenance(path: Path) -> int:
    connection: sqlite3.Connection | None = None
    try:
        connection = open_database(path)
        Maintenance(connection).run(now=utc_now())
        return 0
    except (OSError, sqlite3.Error, ValueError):
        print("maintenance command failed", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
