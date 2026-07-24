from __future__ import annotations

import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from personal_monitor.cli import main
from personal_monitor.domain.observation import content_hash
from personal_monitor.migration.import_rental import (
    IMPORT_MARKER_ID,
    RENTAL_MONITOR_ID,
    import_rental_state,
)
from personal_monitor.storage import open_database
from personal_monitor.storage.schema import canonical_json

OWNER = "telegram-user:12345"
TARGET = "rental-private"
CHAT = "12345"


def _create_legacy(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE announcements (
            announcement_key TEXT PRIMARY KEY,
            source_id TEXT,
            title TEXT NOT NULL,
            agency TEXT NOT NULL,
            region TEXT NOT NULL,
            housing_type TEXT NOT NULL,
            target TEXT NOT NULL,
            announcement_date TEXT NOT NULL,
            application_start_date TEXT,
            application_end_date TEXT,
            url TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE deliveries (
            announcement_key TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY (announcement_key, chat_id),
            FOREIGN KEY (announcement_key) REFERENCES announcements(announcement_key)
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            new_count INTEGER NOT NULL DEFAULT 0,
            agency_status TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    return connection


def _seed_three_agencies(connection: sqlite3.Connection) -> None:
    first = datetime(2026, 7, 20, 3, tzinfo=UTC).isoformat()
    last = datetime(2026, 7, 21, 3, tzinfo=UTC).isoformat()
    rows = (
        (
            "LH:lh-1",
            "lh-1",
            "LH title",
            "LH",
            "서울",
            "행복주택",
            "청년",
            "2026-07-20",
            "2026-07-27",
            "2026-07-29",
            "https://apply.lh.or.kr/notice/1?view=full",
            first,
            last,
        ),
        (
            "SH:sh-1",
            "sh-1",
            "SH title",
            "SH",
            "서울",
            "국민임대",
            "무주택자",
            "2026-07-20",
            None,
            None,
            "https://www.i-sh.co.kr/notice/1",
            first,
            last,
        ),
        (
            "GH:gh-1",
            "gh-1",
            "GH title",
            "GH",
            "경기",
            "신혼부부 매입임대",
            "신혼부부",
            "2026-07-20",
            "2026-07-28",
            None,
            "https://www.gh.or.kr/notice/1",
            first,
            last,
        ),
    )
    connection.executemany(
        "INSERT INTO announcements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.execute(
        "INSERT INTO deliveries VALUES (?, ?, ?, ?)",
        ("LH:lh-1", CHAT, datetime(2026, 7, 21, 4, tzinfo=UTC).isoformat(), 77),
    )
    connection.execute(
        "INSERT INTO runs(started_at, finished_at, status, new_count, agency_status) "
        "VALUES (?, ?, 'success', 1, ?)",
        (
            first,
            last,
            json.dumps({"LH": "ok", "SH": "ok", "GH": "ok"}),
        ),
    )
    connection.commit()


def test_import_maps_exact_fields_times_hash_delivery_and_marker(tmp_path: Path) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.close()

    report = import_rental_state(old_path, new_path, OWNER, TARGET)

    assert report.source_announcements == 3
    assert report.source_deliveries == 1
    assert report.imported_observations == 3
    assert report.imported_deliveries == 1
    assert report.import_complete
    with sqlite3.connect(new_path) as target:
        target.row_factory = sqlite3.Row
        rows = target.execute(
            "SELECT item_id, fields_json, content_hash, first_seen_at, last_seen_at "
            "FROM observations ORDER BY item_id"
        ).fetchall()
        assert [row["item_id"] for row in rows] == [
            "announcement:GH:gh-1",
            "announcement:LH:lh-1",
            "announcement:SH:sh-1",
        ]
        lh = next(row for row in rows if row["item_id"] == "announcement:LH:lh-1")
        fields = json.loads(lh["fields_json"])
        assert fields == {
            "agency": "LH",
            "announcement_date": date(2026, 7, 20).isoformat(),
            "application_end_date": date(2026, 7, 29).isoformat(),
            "application_start_date": date(2026, 7, 27).isoformat(),
            "housing_type": "행복주택",
            "region": "서울",
            "source_id": "lh-1",
            "target": "청년",
            "title": "LH title",
            "url": "https://apply.lh.or.kr/notice/1?view=full",
        }
        assert lh["content_hash"] == content_hash(fields)
        assert lh["first_seen_at"] == "2026-07-20T03:00:00+00:00"
        assert lh["last_seen_at"] == "2026-07-21T03:00:00+00:00"
        outbox = target.execute(
            "SELECT status, lease_owner, lease_expires_at, last_error, payload_json FROM outbox"
        ).fetchone()
        assert tuple(outbox)[:4] == ("delivered", None, None, None)
        assert "view=full" not in outbox["payload_json"]
        delivery = target.execute(
            "SELECT external_message_id, delivered_at FROM deliveries"
        ).fetchone()
        assert tuple(delivery) == ("77", "2026-07-21T04:00:00+00:00")
        marker = target.execute(
            "SELECT stage, status FROM runs WHERE id = ?", (IMPORT_MARKER_ID,)
        ).fetchone()
        assert tuple(marker) == ("migration_import", "success")
        monitor = target.execute(
            "SELECT owner_id, name, status, active_version_id, next_run_at "
            "FROM monitors WHERE id = ?",
            (RENTAL_MONITOR_ID,),
        ).fetchone()
        assert monitor["owner_id"] == OWNER
        assert monitor["name"] == "서울·경기 임대주택"
        assert monitor["status"] == "active"
        assert monitor["active_version_id"]
        assert datetime.fromisoformat(monitor["next_run_at"]).tzinfo is not None


def test_empty_import_marks_complete_and_second_import_is_idempotent(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()

    first = import_rental_state(old_path, new_path, OWNER, TARGET)
    second = import_rental_state(old_path, new_path, OWNER, TARGET)

    assert second == first.with_no_new_rows()
    assert first.announcements_imported == 0
    assert first.deliveries_imported == 0
    assert first.import_complete
    assert first.identity_created
    assert first.target_created
    assert first.monitor_created
    assert first.version_created
    assert not second.identity_created
    assert not second.target_created
    assert not second.monitor_created
    assert not second.version_created
    assert second.imported_observations == 0
    assert second.imported_outbox == 0
    assert second.imported_deliveries == 0
    assert second.import_complete
    with sqlite3.connect(new_path) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM runs WHERE id = ?", (IMPORT_MARKER_ID,)
            ).fetchone()[0]
            == 1
        )


def test_second_populated_import_reports_only_already_present_rows(tmp_path: Path) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.close()

    import_rental_state(old_path, new_path, OWNER, TARGET)
    report = import_rental_state(old_path, new_path, OWNER, TARGET)

    assert report.imported_observations == 0
    assert report.imported_outbox == 0
    assert report.imported_deliveries == 0
    assert report.already_present_observations == 3
    assert report.already_present_outbox == 1
    assert report.already_present_deliveries == 1


def test_dry_run_absent_target_computes_would_import_without_creating_files(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "missing" / "personal.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.close()
    before = {child.name for child in tmp_path.iterdir()}

    report = import_rental_state(old_path, new_path, OWNER, TARGET, dry_run=True)

    assert report.dry_run
    assert report.imported_observations == 3
    assert report.imported_deliveries == 1
    assert not report.import_complete
    assert {child.name for child in tmp_path.iterdir()} == before
    assert not new_path.exists()
    assert not new_path.parent.exists()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{new_path}{suffix}").exists()


def test_dry_run_existing_target_does_not_change_bytes_aux_files_or_rows(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.close()
    import_rental_state(old_path, new_path, OWNER, TARGET)
    before_bytes = new_path.read_bytes()
    with sqlite3.connect(new_path) as connection:
        before_counts = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("users", "monitors", "observations", "outbox", "deliveries", "runs")
        )
    before_aux = {
        suffix: Path(f"{new_path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")
    }

    report = import_rental_state(old_path, new_path, OWNER, TARGET, dry_run=True)

    assert report.import_complete
    assert report.imported_observations == 0
    assert new_path.read_bytes() == before_bytes
    assert {
        suffix: Path(f"{new_path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")
    } == before_aux
    with sqlite3.connect(f"file:{new_path}?mode=ro", uri=True) as connection:
        assert (
            tuple(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("users", "monitors", "observations", "outbox", "deliveries", "runs")
            )
            == before_counts
        )


@pytest.mark.parametrize(
    "phase", ("identity", "version", "observation", "outbox", "delivery", "marker")
)
def test_failure_at_each_mapping_phase_rolls_back_all_domain_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import personal_monitor.migration.import_rental as module

    old_path = tmp_path / f"legacy-{phase}.db"
    new_path = tmp_path / f"personal-{phase}.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.close()

    def fail(selected: str) -> None:
        if selected == phase:
            raise RuntimeError("private path / token / chat")

    monkeypatch.setattr(module, "_phase_hook", fail)
    with pytest.raises(RuntimeError):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    if new_path.exists():
        with sqlite3.connect(new_path) as connection:
            assert all(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
                for table in (
                    "users",
                    "delivery_targets",
                    "monitors",
                    "monitor_versions",
                    "observations",
                    "outbox",
                    "deliveries",
                    "runs",
                )
            )


def test_rejects_extra_legacy_schema_without_mutating_target(tmp_path: Path) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    legacy.execute("ALTER TABLE announcements ADD COLUMN unexpected TEXT")
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError, match="schema"):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert not new_path.exists()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("agency", "XX"),
        ("announcement_date", "20-07-2026"),
        ("first_seen_at", "2026-07-20T03:00:00"),
        ("url", "file:///private"),
    ],
)
def test_rejects_malformed_announcement_rows_redacted(
    tmp_path: Path, column: str, value: str
) -> None:
    old_path = tmp_path / f"legacy-{column}.db"
    new_path = tmp_path / f"personal-{column}.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.execute(f"UPDATE announcements SET {column} = ? WHERE agency = 'LH'", (value,))
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError) as caught:
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert value not in repr(caught.value)
    assert str(old_path) not in repr(caught.value)
    assert not new_path.exists()


@pytest.mark.parametrize(
    "agency_status",
    [
        '{"LH":"ok","LH":"failed"}',
        '{"LH":NaN}',
        '{"LH":"unknown"}',
        '{"XX":"ok"}',
        '["LH","ok"]',
    ],
)
def test_rejects_malformed_agency_status_json(tmp_path: Path, agency_status: str) -> None:
    old_path = tmp_path / f"legacy-{sha256(agency_status.encode()).hexdigest()[:8]}.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    now = datetime(2026, 7, 21, 5, tzinfo=UTC).isoformat()
    legacy.execute(
        "INSERT INTO runs(started_at, finished_at, status, agency_status) "
        "VALUES (?, ?, 'success', ?)",
        (now, now, agency_status),
    )
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert not new_path.exists()


def test_multiple_legacy_chats_fail_closed_without_an_existing_exact_target(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.execute(
        "INSERT INTO deliveries VALUES ('SH:sh-1', '-98765', ?, 88)",
        (datetime(2026, 7, 21, 5, tzinfo=UTC).isoformat(),),
    )
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError, match="ambiguous"):
        import_rental_state(old_path, new_path, OWNER, TARGET)


def test_existing_owned_target_disambiguates_multiple_legacy_chats(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.execute(
        "INSERT INTO deliveries VALUES ('SH:sh-1', '-98765', ?, 88)",
        (datetime(2026, 7, 21, 5, tzinfo=UTC).isoformat(),),
    )
    legacy.commit()
    legacy.close()
    target = open_database(new_path)
    now = datetime(2026, 7, 21, 5, tzinfo=UTC).isoformat()
    target.execute("INSERT INTO users VALUES (?, ?, 'active', ?)", (OWNER, 12345, now))
    target.execute(
        "INSERT INTO delivery_targets VALUES (?, ?, 'telegram', ?, ?)",
        (TARGET, OWNER, CHAT, now),
    )
    target.close()

    report = import_rental_state(old_path, new_path, OWNER, TARGET)

    assert report.source_deliveries == 2
    assert report.imported_deliveries == 1


def test_legacy_default_alias_requires_and_uses_explicit_target_address(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.execute("UPDATE deliveries SET chat_id = 'telegram-default'")
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError, match="target"):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    report = import_rental_state(
        old_path,
        new_path,
        OWNER,
        TARGET,
        target_address="-10012345",
    )

    assert report.imported_deliveries == 1
    with sqlite3.connect(new_path) as target:
        address = target.execute(
            "SELECT address FROM delivery_targets WHERE id = ?",
            (TARGET,),
        ).fetchone()[0]
        assert address == "-10012345"
        assert (
            target.execute(
                "SELECT COUNT(*) FROM delivery_targets WHERE address = 'telegram-default'"
            ).fetchone()[0]
            == 0
        )


def test_rejects_symlink_hardlink_and_same_path_aliases(tmp_path: Path) -> None:
    old_path = tmp_path / "legacy.db"
    _create_legacy(old_path).close()
    symlink = tmp_path / "source-link.db"
    symlink.symlink_to(old_path)
    hardlink = tmp_path / "target-hard.db"
    hardlink.hardlink_to(old_path)

    for source, target in (
        (symlink, tmp_path / "target.db"),
        (old_path, hardlink),
        (old_path, old_path),
    ):
        with pytest.raises(RuntimeError):
            import_rental_state(source, target, OWNER, TARGET)


def test_read_only_legacy_database_is_never_modified_or_given_sidecars(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    before = old_path.read_bytes()
    old_path.chmod(0o400)

    import_rental_state(old_path, new_path, OWNER, TARGET)

    assert old_path.read_bytes() == before
    for suffix in ("-wal", "-shm", "-journal", ".ready"):
        assert not Path(f"{old_path}{suffix}").exists()


def test_concurrent_imports_converge_to_one_success_and_one_idempotent_result(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(
            pool.map(
                lambda _: import_rental_state(old_path, new_path, OWNER, TARGET),
                range(2),
            )
        )

    assert sorted(report.imported_observations for report in reports) == [0, 3]
    assert all(report.import_complete for report in reports)


def test_cli_prints_one_canonical_json_report(tmp_path: Path, capsys) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()

    result = main(
        [
            "migration",
            "import-rental",
            "--source",
            str(old_path),
            "--database",
            str(new_path),
            "--owner",
            OWNER,
            "--target",
            TARGET,
            "--target-address",
            CHAT,
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert captured.out == canonical_json(payload) + "\n"
    assert payload["identity_created"] is True
    assert payload["monitor_created"] is True
    assert payload["import_complete"] is True


def test_cli_failure_is_fixed_and_redacts_all_arguments(tmp_path: Path, capsys) -> None:
    private_source = tmp_path / "private-token-source.db"
    private_target = tmp_path / "private-chat-target.db"

    result = main(
        [
            "migration",
            "import-rental",
            "--source",
            str(private_source),
            "--database",
            str(private_target),
            "--owner",
            "telegram-user:998877",
            "--target",
            "private-target",
            "--target-address",
            "private-address",
            "--dry-run",
        ]
    )

    captured = capsys.readouterr()
    assert result != 0
    assert captured.out == ""
    assert captured.err == "rental import failed\n"
    assert "private" not in captured.err
    assert "998877" not in captured.err


def test_target_absence_race_cannot_turn_source_hardlink_into_write_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import personal_monitor.migration.import_rental as module

    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    source_before = old_path.read_bytes()
    original_open_target = module._open_target

    def raced_open_target(path: Path, *, source_identity, dry_run: bool):
        new_path.hardlink_to(old_path)
        return original_open_target(
            path,
            source_identity=source_identity,
            dry_run=dry_run,
        )

    monkeypatch.setattr(module, "_open_target", raced_open_target)

    with pytest.raises(RuntimeError):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert old_path.read_bytes() == source_before
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{old_path}{suffix}").exists()


def test_source_path_swap_after_validation_is_rejected_by_read_fd_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import personal_monitor.migration.import_rental as module

    old_path = tmp_path / "legacy.db"
    original_anchor = tmp_path / "legacy-original.db"
    replacement = tmp_path / "legacy-replacement.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    original_anchor.hardlink_to(old_path)
    _create_legacy(replacement).close()
    source_before = original_anchor.read_bytes()
    original_read_source = module._read_source

    def raced_read_source(path: Path, expected_identity):
        old_path.unlink()
        old_path.hardlink_to(replacement)
        return original_read_source(path, expected_identity)

    monkeypatch.setattr(module, "_read_source", raced_read_source)

    with pytest.raises(RuntimeError):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert original_anchor.read_bytes() == source_before
    assert not new_path.exists()


def test_existing_target_swap_to_source_hardlink_is_rejected_before_schema_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import personal_monitor.migration.import_rental as module

    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    import_rental_state(old_path, new_path, OWNER, TARGET)
    source_before = old_path.read_bytes()
    original_open_target = module._open_target

    def raced_open_target(path: Path, *, source_identity, dry_run: bool):
        new_path.unlink()
        new_path.hardlink_to(old_path)
        return original_open_target(
            path,
            source_identity=source_identity,
            dry_run=dry_run,
        )

    monkeypatch.setattr(module, "_open_target", raced_open_target)

    with pytest.raises(RuntimeError):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert old_path.read_bytes() == source_before
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{old_path}{suffix}").exists()


def test_existing_target_swap_after_fd_proof_is_rejected_before_sidecar_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import personal_monitor.migration.import_rental as module

    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    import_rental_state(old_path, new_path, OWNER, TARGET)
    source_before = old_path.read_bytes()

    def swap_after_proof(path: Path) -> None:
        assert path == new_path
        new_path.unlink()
        new_path.hardlink_to(old_path)

    monkeypatch.setattr(module, "_before_target_schema_write", swap_after_proof)

    with pytest.raises(RuntimeError):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert old_path.read_bytes() == source_before
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{old_path}{suffix}").exists()


def test_dry_run_reads_committed_target_wal_without_creating_sidecars(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    import_rental_state(old_path, new_path, OWNER, TARGET)
    writer = open_database(new_path)
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute(
        "UPDATE delivery_targets SET address = 'committed-wal-conflict' WHERE id = ?",
        (TARGET,),
    )
    wal_path = Path(f"{new_path}-wal")
    shm_path = Path(f"{new_path}-shm")
    assert wal_path.exists()
    assert shm_path.exists()
    before_database = new_path.read_bytes()
    before_presence = (wal_path.exists(), shm_path.exists())
    before_sidecars = (wal_path.read_bytes(), shm_path.read_bytes())
    try:
        with pytest.raises(RuntimeError, match="conflict"):
            import_rental_state(old_path, new_path, OWNER, TARGET, dry_run=True)
        assert new_path.read_bytes() == before_database
        assert (wal_path.exists(), shm_path.exists()) == before_presence
        assert (wal_path.read_bytes(), shm_path.read_bytes()) == before_sidecars
    finally:
        writer.close()


def test_dry_run_snapshot_is_private_and_unstable_copy_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import personal_monitor.migration.import_rental as module

    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    import_rental_state(old_path, new_path, OWNER, TARGET)
    identity = module._identity(new_path.lstat())
    workspace, snapshot = module._copy_target_snapshot(
        new_path,
        expected_identity=identity,
    )
    temporary_path = workspace.path
    try:
        assert stat.S_IMODE(temporary_path.lstat().st_mode) == 0o700
        assert stat.S_IMODE(snapshot.lstat().st_mode) == 0o600
    finally:
        workspace.cleanup()
    assert not temporary_path.exists()
    target_before = new_path.read_bytes()
    monkeypatch.setattr(
        module,
        "_target_snapshot_is_stable",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(RuntimeError):
        import_rental_state(old_path, new_path, OWNER, TARGET, dry_run=True)

    assert new_path.read_bytes() == target_before


@pytest.mark.parametrize("replaced_name", ("target.sqlite3", "target.sqlite3-wal"))
def test_private_workspace_cleanup_never_unlinks_replaced_owned_inode(
    tmp_path: Path,
    replaced_name: str,
) -> None:
    import personal_monitor.migration.import_rental as module

    workspace = module._PrivateWorkspace.create(
        parent=tmp_path,
        prefix=".cleanup-proof-",
    )
    owned = workspace.path / replaced_name
    descriptor = os.open(owned, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    workspace.record(owned)
    owned.unlink()
    owned.write_bytes(b"replacement")

    with pytest.raises(RuntimeError, match="cleanup identity"):
        workspace.cleanup()

    assert owned.read_bytes() == b"replacement"
    owned.unlink()
    workspace.path.rmdir()


def test_dry_run_rejects_live_rollback_journal_without_mutation(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    import_rental_state(old_path, new_path, OWNER, TARGET)
    writer = sqlite3.connect(new_path, isolation_level=None)
    assert writer.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "UPDATE delivery_targets SET address = 'rollback-pending' WHERE id = ?",
        (TARGET,),
    )
    journal = Path(f"{new_path}-journal")
    assert journal.exists()
    before = (new_path.read_bytes(), journal.read_bytes())
    try:
        with pytest.raises(RuntimeError, match="rollback journal"):
            import_rental_state(old_path, new_path, OWNER, TARGET, dry_run=True)
        assert (new_path.read_bytes(), journal.read_bytes()) == before
    finally:
        writer.rollback()
        writer.close()


def test_dry_run_rejects_closed_non_wal_target(tmp_path: Path) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    import_rental_state(old_path, new_path, OWNER, TARGET)
    with sqlite3.connect(new_path, isolation_level=None) as connection:
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    before = new_path.read_bytes()

    with pytest.raises(RuntimeError, match="WAL mode"):
        import_rental_state(old_path, new_path, OWNER, TARGET, dry_run=True)

    assert new_path.read_bytes() == before


def test_dry_run_fails_closed_when_rollback_journal_appears_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import personal_monitor.migration.import_rental as module

    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    import_rental_state(old_path, new_path, OWNER, TARGET)
    before = new_path.read_bytes()
    journal = Path(f"{new_path}-journal")
    original_stable = module._target_snapshot_is_stable

    def create_journal_then_check(*args, **kwargs) -> bool:
        journal.write_bytes(b"appeared")
        return original_stable(*args, **kwargs)

    monkeypatch.setattr(module, "_target_snapshot_is_stable", create_journal_then_check)

    with pytest.raises(RuntimeError, match="rollback journal"):
        import_rental_state(old_path, new_path, OWNER, TARGET, dry_run=True)

    assert new_path.read_bytes() == before
    assert journal.read_bytes() == b"appeared"


def test_generated_legacy_column_is_rejected_even_when_table_info_hides_it(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    legacy.execute(
        "ALTER TABLE announcements ADD COLUMN hidden_title TEXT GENERATED ALWAYS AS (title) VIRTUAL"
    )
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError, match="schema"):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert not new_path.exists()


def test_runs_table_without_autoincrement_is_rejected(tmp_path: Path) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    legacy.execute("ALTER TABLE runs RENAME TO runs_old")
    legacy.execute(
        "CREATE TABLE runs("
        "id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, "
        "status TEXT NOT NULL, new_count INTEGER NOT NULL DEFAULT 0, "
        "agency_status TEXT NOT NULL DEFAULT '{}')"
    )
    legacy.execute("DROP TABLE runs_old")
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError, match="schema"):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert not new_path.exists()


def test_empty_source_requires_existing_target_to_use_owner_private_chat(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    _create_legacy(old_path).close()
    target = open_database(new_path)
    evidence = "2000-01-01T00:00:00+00:00"
    target.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) "
        "VALUES (?, 12345, 'active', ?)",
        (OWNER, evidence),
    )
    target.execute(
        "INSERT INTO delivery_targets(id, owner_id, kind, address, created_at) "
        "VALUES (?, ?, 'telegram', 'not-the-private-chat', ?)",
        (TARGET, OWNER, evidence),
    )
    target.close()

    with pytest.raises(RuntimeError, match="conflict"):
        import_rental_state(old_path, new_path, OWNER, TARGET)


@pytest.mark.parametrize(
    ("table", "column", "where"),
    [
        ("users", "created_at", "id = 'telegram-user:12345'"),
        ("delivery_targets", "created_at", "id = 'rental-private'"),
        ("monitors", "created_at", "id = 'rental-housing-seoul-gyeonggi'"),
        (
            "monitor_versions",
            "created_at",
            "id = 'rental-housing-seoul-gyeonggi:v1'",
        ),
        (
            "monitor_versions",
            "approved_at",
            "id = 'rental-housing-seoul-gyeonggi:v1'",
        ),
    ],
)
def test_existing_aggregate_creation_and_approval_times_match_source_evidence(
    tmp_path: Path, table: str, column: str, where: str
) -> None:
    old_path = tmp_path / f"legacy-{table}-{column}.db"
    new_path = tmp_path / f"personal-{table}-{column}.db"
    _create_legacy(old_path).close()
    import_rental_state(old_path, new_path, OWNER, TARGET)
    target = open_database(new_path)
    target.execute(f"UPDATE {table} SET {column} = '2001-01-01T00:00:00+00:00' WHERE {where}")
    target.close()

    with pytest.raises(RuntimeError, match="conflict"):
        import_rental_state(old_path, new_path, OWNER, TARGET)


@pytest.mark.parametrize("message_id", (0, -1, "not-an-integer"))
def test_invalid_legacy_message_ids_abort_without_target_mutation(
    tmp_path: Path, message_id: int | str
) -> None:
    old_path = tmp_path / f"legacy-{sha256(str(message_id).encode()).hexdigest()[:8]}.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.execute("UPDATE deliveries SET message_id = ?", (message_id,))
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert not new_path.exists()


def test_pre_run_zero_message_id_imports_as_legacy_baseline(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    seen_at = datetime(2026, 7, 19, 3, tzinfo=UTC).isoformat()
    delivered_at = datetime(2026, 7, 19, 4, tzinfo=UTC).isoformat()
    legacy.execute(
        "UPDATE announcements SET first_seen_at = ?, last_seen_at = ?",
        (seen_at, seen_at),
    )
    legacy.execute(
        "UPDATE deliveries SET delivered_at = ?, message_id = 0",
        (delivered_at,),
    )
    legacy.commit()
    legacy.close()

    report = import_rental_state(old_path, new_path, OWNER, TARGET)

    assert report.imported_deliveries == 1
    with sqlite3.connect(new_path) as target:
        assert (
            target.execute("SELECT external_message_id FROM deliveries").fetchone()[0]
            == "legacy-baseline"
        )


def test_legacy_foreign_key_violation_aborts_without_target_mutation(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "legacy.db"
    new_path = tmp_path / "personal.db"
    legacy = _create_legacy(old_path)
    legacy.execute("PRAGMA foreign_keys = OFF")
    legacy.execute(
        "INSERT INTO deliveries VALUES ('LH:missing', '12345', ?, 10)",
        (datetime(2026, 7, 21, 5, tzinfo=UTC).isoformat(),),
    )
    legacy.commit()
    legacy.close()

    with pytest.raises(RuntimeError, match="foreign key"):
        import_rental_state(old_path, new_path, OWNER, TARGET)

    assert not new_path.exists()


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("observations", "content_hash", "conflicting-hash"),
        ("outbox", "payload_json", '{"text":"conflicting"}'),
        ("deliveries", "external_message_id", "999"),
    ],
)
def test_existing_observation_outbox_and_delivery_conflicts_abort_atomically(
    tmp_path: Path, table: str, column: str, value: str
) -> None:
    old_path = tmp_path / f"legacy-{table}.db"
    new_path = tmp_path / f"personal-{table}.db"
    legacy = _create_legacy(old_path)
    _seed_three_agencies(legacy)
    legacy.close()
    import_rental_state(old_path, new_path, OWNER, TARGET)
    target = open_database(new_path)
    target.execute(f"UPDATE {table} SET {column} = ?", (value,))
    target.close()

    with pytest.raises(RuntimeError, match="conflict"):
        import_rental_state(old_path, new_path, OWNER, TARGET)
