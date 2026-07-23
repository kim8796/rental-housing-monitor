from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from personal_monitor.cli import build_parser, main


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
    monkeypatch.setattr("personal_monitor.cli.open_existing_database", lambda path: connection)

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
        "personal_monitor.cli.open_existing_database",
        lambda path: EmptyIntegrityConnection(),
    )

    assert main(["database", "integrity-check", "--path", str(tmp_path / "db")]) != 0

    assert "database integrity check failed" in capsys.readouterr().err


def test_database_vacuum_uses_a_database_connection(tmp_path: Path) -> None:
    database_path = tmp_path / "monitor.sqlite3"
    assert main(["database", "init", "--path", str(database_path)]) == 0

    assert main(["database", "vacuum", "--path", str(database_path)]) == 0


def test_maintenance_command_runs_once_and_closes_its_database(tmp_path: Path) -> None:
    database_path = tmp_path / "monitor.sqlite3"
    assert main(["database", "init", "--path", str(database_path)]) == 0
    assert main(["maintenance", "run", "--database", str(database_path)]) == 0

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "arguments",
    [
        ["database", "integrity-check", "--path"],
        ["database", "vacuum", "--path"],
        ["maintenance", "run", "--database"],
    ],
)
def test_non_init_commands_reject_a_misspelled_path_without_creating_it(
    tmp_path: Path, arguments: list[str]
) -> None:
    database_path = tmp_path / "misspelled-parent" / "monitor.sqlite3"

    assert main([*arguments, str(database_path)]) != 0

    assert not database_path.exists()
    assert not database_path.parent.exists()


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
    assert "profile" in result.stdout


def test_profile_bootstrap_parser_accepts_only_the_documented_fields() -> None:
    arguments = build_parser().parse_args(
        [
            "profile",
            "bootstrap",
            "--id",
            "shopping",
            "--url",
            "https://example.com/login?state=opaque",
            "--profiles-root",
            "/srv/personal-monitor/profiles",
        ]
    )

    assert arguments.command == "profile"
    assert arguments.action == "bootstrap"
    assert vars(arguments) == {
        "command": "profile",
        "action": "bootstrap",
        "profile_id": "shopping",
        "url": "https://example.com/login?state=opaque",
        "profiles_root": Path("/srv/personal-monitor/profiles"),
    }


@pytest.mark.parametrize(
    "forbidden",
    ["--username", "--password", "--otp", "--cookie", "--token", "--browser-args"],
)
def test_profile_bootstrap_rejects_credentials_and_arbitrary_browser_arguments_safely(
    forbidden: str, capsys
) -> None:
    secret = "operator-private-value"

    result = main(
        [
            "profile",
            "bootstrap",
            "--id",
            "shopping",
            "--url",
            "https://example.com/login?token=private-query",
            "--profiles-root",
            "/srv/personal-monitor/profiles",
            forbidden,
            secret,
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err.endswith("invalid command arguments\n")
    assert secret not in captured.err
    assert "private-query" not in captured.err
    assert "shopping" not in captured.err


def test_profile_bootstrap_delegates_without_printing_the_url_or_identifier(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    import personal_monitor.cli as cli_module

    calls: list[tuple[str, str, Path]] = []

    def command(profile_id: str, url: str, profiles_root: Path) -> None:
        calls.append((profile_id, url, profiles_root))

    monkeypatch.setattr(cli_module, "_profile_bootstrap_command", command)
    url = "https://example.com/login?token=private-query"

    assert (
        main(
            [
                "profile",
                "bootstrap",
                "--id",
                "shopping",
                "--url",
                url,
                "--profiles-root",
                str(tmp_path),
            ]
        )
        == 0
    )

    assert calls == [("shopping", url, tmp_path)]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_profile_bootstrap_has_one_fixed_redacted_failure_boundary(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    import personal_monitor.cli as cli_module

    def fail(_profile_id: str, _url: str, _profiles_root: Path) -> None:
        raise RuntimeError("shopping private-query password=private")

    monkeypatch.setattr(cli_module, "_profile_bootstrap_command", fail)

    assert (
        main(
            [
                "profile",
                "bootstrap",
                "--id",
                "shopping",
                "--url",
                "https://example.com/login?token=private-query",
                "--profiles-root",
                str(tmp_path),
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "profile bootstrap failed\n"


def test_run_once_defaults_to_disabled_delivery() -> None:
    arguments = build_parser().parse_args(
        ["run-once", "--database", "/srv/monitor.db", "--monitor", "monitor-1"]
    )

    assert arguments.delivery == "disabled"


def test_ai_worker_refuses_service_and_api_environments_before_start(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    import personal_monitor.cli as cli_module

    started = False

    def should_not_start(_socket: Path) -> int:
        nonlocal started
        started = True
        return 0

    monkeypatch.setattr(cli_module, "_ai_worker_command", should_not_start)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "private")

    result = main(["ai-worker", "--socket", str(tmp_path / "worker.sock")])

    assert result == 1
    assert not started
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ai worker configuration refused\n"
    assert "private" not in captured.err


def test_run_once_passes_only_explicit_delivery_mode_to_safe_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import personal_monitor.cli as cli_module

    calls: list[tuple[Path, str, str]] = []
    monkeypatch.setattr(
        cli_module,
        "_run_once_command",
        lambda database, monitor, delivery: calls.append((database, monitor, delivery)) or 0,
    )

    assert (
        main(
            [
                "run-once",
                "--database",
                str(tmp_path / "db"),
                "--monitor",
                "monitor-private",
                "--delivery",
                "enabled",
            ]
        )
        == 0
    )
    assert calls == [(tmp_path / "db", "monitor-private", "enabled")]
