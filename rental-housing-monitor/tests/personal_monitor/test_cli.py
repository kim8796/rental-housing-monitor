from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from personal_monitor.cli import main


def valid_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "owner_id": "telegram-user:1",
        "name": "가격 감시",
        "target_url": "https://example.com/product/1",
        "source_adapter": "scrapling",
        "extract": {
            "item_scope": "main",
            "fields": {"price": {"selector": ".price", "type": "krw"}},
        },
        "validators": {"min_items": 0, "max_items": 10},
        "rules": [{"kind": "new_item"}],
    }


def test_validate_spec_prints_canonical_utf8_json(tmp_path: Path, capsys) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(valid_spec()), encoding="utf-8")

    assert main(["validate-spec", str(spec_path)]) == 0

    output = capsys.readouterr().out
    assert json.loads(output)["schema_version"] == 1
    assert (
        output
        == json.dumps(json.loads(output), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def test_validate_spec_returns_safe_nonzero_error_for_invalid_input(tmp_path: Path, capsys) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text('{"target_url":"https://example.com/?token=secret"}', encoding="utf-8")

    assert main(["validate-spec", str(spec_path)]) != 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid monitor specification" in captured.err
    assert "secret" not in captured.err


def test_database_init_applies_migrations_and_closes_connection(tmp_path: Path) -> None:
    database_path = tmp_path / "monitor.sqlite3"

    assert main(["database", "init", "--path", str(database_path)]) == 0

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT version FROM schema_migrations").fetchone() == (1,)
    finally:
        connection.close()


def test_database_integrity_check_returns_safe_nonzero_for_failure(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    class CorruptConnection:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, statement: str) -> list[tuple[str]]:
            assert statement == "PRAGMA integrity_check"
            return [("corruption details",)]

        def close(self) -> None:
            self.closed = True

    connection = CorruptConnection()
    monkeypatch.setattr("personal_monitor.cli.open_database", lambda path: connection)

    assert main(["database", "integrity-check", "--path", str(tmp_path / "db")]) != 0

    captured = capsys.readouterr()
    assert "database integrity check failed" in captured.err
    assert "corruption details" not in captured.err
    assert connection.closed


def test_database_integrity_check_requires_an_ok_result(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    class EmptyIntegrityConnection:
        def execute(self, statement: str) -> list[tuple[str]]:
            assert statement == "PRAGMA integrity_check"
            return []

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "personal_monitor.cli.open_database", lambda path: EmptyIntegrityConnection()
    )

    assert main(["database", "integrity-check", "--path", str(tmp_path / "db")]) != 0

    assert "database integrity check failed" in capsys.readouterr().err


def test_database_vacuum_uses_a_database_connection(tmp_path: Path) -> None:
    database_path = tmp_path / "monitor.sqlite3"
    assert main(["database", "init", "--path", str(database_path)]) == 0

    assert main(["database", "vacuum", "--path", str(database_path)]) == 0


def test_maintenance_command_runs_once_and_closes_its_database(tmp_path: Path) -> None:
    database_path = tmp_path / "monitor.sqlite3"
    assert main(["maintenance", "run", "--database", str(database_path)]) == 0

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


def test_module_entry_point_lists_operator_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "personal_monitor", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "validate-spec" in result.stdout
    assert "database" in result.stdout
    assert "maintenance" in result.stdout
