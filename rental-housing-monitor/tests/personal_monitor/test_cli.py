from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from personal_monitor.cli import build_parser, main
from personal_monitor.migration.shadow import (
    DuplicateProbeResult,
    MigrationStatus,
    ShadowComparator,
    ShadowSnapshot,
)
from personal_monitor.storage import open_database as initialize_database


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


def test_billing_register_credit_seeds_exact_console_baseline_idempotently(
    tmp_path: Path,
    capsys,
) -> None:
    database_path = tmp_path / "monitor.sqlite3"
    assert main(["database", "init", "--path", str(database_path)]) == 0
    command = [
        "billing",
        "register-credit",
        "--database",
        str(database_path),
        "--id",
        "free-trial",
        "--name",
        "Free Trial",
        "--original-won",
        "460418.00",
        "--remaining-won",
        "455463.26",
        "--starts-on",
        "2026-07-08",
        "--ends-on",
        "2026-10-08",
        "--as-of",
        "2026-07-24T03:10:00Z",
    ]

    assert main(command) == 0
    assert main(command) == 0

    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT original_micros, baseline_remaining_micros "
            "FROM billing_credit_grants WHERE id = 'free-trial'"
        ).fetchone() == (460_418_000_000, 455_463_260_000)
        assert connection.execute("SELECT count(*) FROM billing_snapshots").fetchone() == (1,)
    finally:
        connection.close()
    captured = capsys.readouterr()
    assert captured.out == "billing credit registered\nbilling credit registered\n"


def test_billing_register_credit_rejects_non_exact_money_without_echo(
    tmp_path: Path,
    capsys,
) -> None:
    database_path = tmp_path / "monitor.sqlite3"
    assert main(["database", "init", "--path", str(database_path)]) == 0
    secret = "NaN-private"

    assert (
        main(
            [
                "billing",
                "register-credit",
                "--database",
                str(database_path),
                "--id",
                "free-trial",
                "--name",
                "Free Trial",
                "--original-won",
                secret,
                "--remaining-won",
                "455463.26",
                "--starts-on",
                "2026-07-08",
                "--ends-on",
                "2026-10-08",
                "--as-of",
                "2026-07-24T03:10:00Z",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "billing credit registration failed\n"
    assert secret not in captured.err


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


@pytest.mark.parametrize(
    ("arguments", "boundary", "expected"),
    [
        (["serve"], "_serve_command", "personal monitor service failed\n"),
        (
            ["run-once", "--database", "/tmp/db", "--monitor", "monitor-1"],
            "_run_once_command",
            "run-once failed\n",
        ),
    ],
)
def test_service_cli_boundaries_redact_raw_sqlite_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    arguments: list[str],
    boundary: str,
    expected: str,
) -> None:
    import personal_monitor.cli as cli_module

    def fail(*_args) -> int:
        raise sqlite3.OperationalError("token=private query=select private")

    monkeypatch.setattr(cli_module, boundary, fail)

    assert main(arguments) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected
    assert "private" not in captured.err


def test_migration_parser_exposes_only_fixed_shadow_probe_and_status_fields() -> None:
    parser = build_parser()

    shadow = parser.parse_args(
        [
            "migration",
            "shadow-run",
            "--source",
            "/tmp/legacy.db",
            "--database",
            "/tmp/personal.db",
            "--run-date",
            "2026-07-29",
        ]
    )
    probe = parser.parse_args(
        [
            "migration",
            "duplicate-probe",
            "--database",
            "/tmp/personal.db",
            "--monitor",
            "rental-housing-seoul-gyeonggi",
        ]
    )
    status = parser.parse_args(["migration", "status", "--database", "/tmp/personal.db"])

    assert (shadow.migration_action, shadow.run_date) == ("shadow-run", "2026-07-29")
    assert (probe.migration_action, probe.monitor) == (
        "duplicate-probe",
        "rental-housing-seoul-gyeonggi",
    )
    assert (status.migration_action, status.as_of) == ("status", None)


def test_migration_status_prints_exact_canonical_json_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    database = tmp_path / "personal.db"
    initialize_database(database).close()
    expected = MigrationStatus(
        consecutive_matches=7,
        last_match_date=date(2026, 7, 29),
        unresolved_differences=0,
        state_imported=True,
        duplicate_probe_passed=True,
        cutover_ready=True,
    )
    monkeypatch.setattr(
        "personal_monitor.migration.shadow.ShadowRepository.status",
        lambda _self, as_of: expected if as_of == date(2026, 7, 30) else None,
    )
    monkeypatch.delenv("PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY", raising=False)
    monkeypatch.delenv("PERSONAL_MONITOR_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert (
        main(
            [
                "migration",
                "status",
                "--database",
                str(database),
                "--as-of",
                "2026-07-30",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        '{"consecutive_matches":7,"cutover_ready":true,'
        '"duplicate_probe_passed":true,"last_match_date":"2026-07-29",'
        '"state_imported":true,"unresolved_differences":0}\n'
    )


def test_shadow_cli_uses_only_data_key_null_sender_and_safe_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    database = tmp_path / "personal.db"
    source = tmp_path / "legacy.db"
    initialize_database(database).close()
    source.touch()
    adapter = object()
    key = "sensitive-data-go-kr-key"
    monkeypatch.setenv("PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY", key)
    monkeypatch.delenv("PERSONAL_MONITOR_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "personal_monitor.adapters.rental_housing.production_rental_housing_adapter",
        lambda value: adapter if value == key else None,
    )

    async def fake_run(source_path, repository, run_date, *, adapter: object, sender: object):
        from personal_monitor.service import NullDeliverySender

        assert source_path == source
        assert repository.connection is not None
        assert run_date == date(2026, 7, 29)
        assert adapter is not None
        assert type(sender) is NullDeliverySender
        empty = ShadowSnapshot(items=(), source_status={"LH": "ok", "SH": "ok", "GH": "ok"})
        return ShadowComparator(clock=lambda: datetime(2026, 7, 29, tzinfo=UTC)).compare(
            empty, empty, run_date
        )

    monkeypatch.setattr(
        "personal_monitor.migration.shadow.run_shadow_fetch",
        fake_run,
    )

    assert (
        main(
            [
                "migration",
                "shadow-run",
                "--source",
                str(source),
                "--database",
                str(database),
                "--run-date",
                "2026-07-29",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["matched"] is True
    assert key not in captured.out


def test_duplicate_probe_cli_uses_null_sender_and_fixed_monitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    database = tmp_path / "personal.db"
    initialize_database(database).close()
    adapter = object()
    monkeypatch.setenv("PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY", "data-key")
    monkeypatch.setattr(
        "personal_monitor.adapters.rental_housing.production_rental_housing_adapter",
        lambda _value: adapter,
    )

    async def fake_probe(repository, monitor_id, *, adapter: object, sender: object):
        from personal_monitor.service import NullDeliverySender

        assert repository.connection is not None
        assert monitor_id == "rental-housing-seoul-gyeonggi"
        assert adapter is not None
        assert type(sender) is NullDeliverySender
        return DuplicateProbeResult(
            monitor_id=monitor_id,
            run_date=date(2026, 7, 30),
            current_hash="d" * 64,
            passed=True,
            missing_ids=(),
            conflicting_ids=(),
            recorded_at=datetime(2026, 7, 30, tzinfo=UTC),
        )

    monkeypatch.setattr(
        "personal_monitor.migration.shadow.run_duplicate_probe",
        fake_probe,
    )

    assert (
        main(
            [
                "migration",
                "duplicate-probe",
                "--database",
                str(database),
                "--monitor",
                "rental-housing-seoul-gyeonggi",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "conflicting_ids": [],
        "current_hash": "d" * 64,
        "missing_ids": [],
        "monitor_id": "rental-housing-seoul-gyeonggi",
        "passed": True,
        "run_date": "2026-07-30",
    }


@pytest.mark.parametrize(
    ("action", "expected"),
    (
        ("shadow-run", "rental shadow failed\n"),
        ("duplicate-probe", "rental duplicate probe failed\n"),
    ),
)
def test_fetch_migration_commands_require_data_key_with_fixed_redacted_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    action: str,
    expected: str,
) -> None:
    database = tmp_path / "personal.db"
    source = tmp_path / "secret-source.db"
    initialize_database(database).close()
    source.touch()
    monkeypatch.delenv("PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY", raising=False)
    arguments = ["migration", action, "--database", str(database)]
    if action == "shadow-run":
        arguments += ["--source", str(source), "--run-date", "2026-07-29"]
    else:
        arguments += ["--monitor", "rental-housing-seoul-gyeonggi"]

    assert main(arguments) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected
    assert "secret-source" not in captured.err
