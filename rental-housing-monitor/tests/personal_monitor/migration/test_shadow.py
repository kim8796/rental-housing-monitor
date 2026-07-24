from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from personal_monitor.domain.observation import (
    ObservationBatch,
    ObservedItem,
    SourceWarning,
    content_hash,
)
from personal_monitor.migration.import_rental import _rental_spec, import_rental_state
from personal_monitor.migration.shadow import (
    DuplicateProbeResult,
    ShadowComparator,
    ShadowItem,
    ShadowRepository,
    ShadowResult,
    ShadowSnapshot,
    load_legacy_shadow_snapshot,
    run_duplicate_probe,
    run_shadow_fetch,
)
from personal_monitor.storage import open_database, open_existing_database
from personal_monitor.storage.schema import canonical_json

NOW = datetime(2026, 7, 30, tzinfo=UTC)
SEOUL_TODAY = date(2026, 7, 30)
STATUSES = {"LH": "ok", "SH": "ok", "GH": "ok"}


def test_rental_spec_allows_current_official_gh_application_host() -> None:
    spec = _rental_spec("telegram-user:1")

    assert "apply.gh.or.kr" in spec.validators.allowed_link_domains


def snapshot(
    *item_ids: str,
    statuses: dict[str, str] | None = None,
) -> ShadowSnapshot:
    return ShadowSnapshot(
        items=tuple(
            ShadowItem(
                agency=item_id.removeprefix("announcement:").split(":", 1)[0],
                item_id=item_id,
            )
            for item_id in item_ids
        ),
        source_status=STATUSES if statuses is None else statuses,
    )


def opaque_item_id(raw_item_id: str) -> str:
    agency = raw_item_id.removeprefix("announcement:").split(":", 1)[0]
    digest = sha256(raw_item_id.encode("utf-8")).hexdigest()
    return f"announcement:{agency}:sha256-{digest}"


def result_for(
    run_date: date,
    *,
    matched: bool = True,
    recorded_at: datetime = NOW,
) -> ShadowResult:
    old = snapshot("announcement:LH:one")
    new = old if matched else snapshot("announcement:SH:two")
    return ShadowComparator(clock=lambda: recorded_at).compare(old, new, run_date)


def seed_exact_import_marker(connection: sqlite3.Connection) -> None:
    timestamp = "2026-07-01T00:00:00+00:00"
    spec = _rental_spec("telegram-user:1")
    connection.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) "
        "VALUES ('telegram-user:1', 1, 'active', ?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO monitors(id, owner_id, name, status, active_version_id, next_run_at, "
        "created_at, updated_at) VALUES "
        "('rental-housing-seoul-gyeonggi', 'telegram-user:1', ?, 'active', "
        "'rental-housing-seoul-gyeonggi:v1', NULL, ?, ?)",
        (spec.name, timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO monitor_versions(id, monitor_id, version_number, spec_json, created_by, "
        "created_at, approved_by, approved_at) VALUES "
        "('rental-housing-seoul-gyeonggi:v1', 'rental-housing-seoul-gyeonggi', 1, ?, "
        "'telegram-user:1', ?, 'telegram-user:1', ?)",
        (canonical_json(spec.model_dump(mode="json")), timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO delivery_targets(id, owner_id, kind, address, created_at) "
        "VALUES ('target-1', 'telegram-user:1', 'telegram', '1', ?)",
        (timestamp,),
    )
    connection.execute(
        "INSERT INTO runs(id, monitor_id, version_id, lease_generation, stage, fetch_strategy, "
        "status, started_at, finished_at, error_class, error_detail) VALUES "
        "('migration:rental-housing:v1', 'rental-housing-seoul-gyeonggi', "
        "'rental-housing-seoul-gyeonggi:v1', 0, 'migration_import', NULL, 'success', "
        "?, ?, NULL, NULL)",
        (timestamp, timestamp),
    )


def seed_passing_probe(repository: ShadowRepository, run_date: date) -> None:
    repository.record_duplicate_probe(
        DuplicateProbeResult(
            monitor_id="rental-housing-seoul-gyeonggi",
            run_date=run_date,
            current_hash="a" * 64,
            passed=True,
            missing_ids=(),
            conflicting_ids=(),
            recorded_at=NOW,
        )
    )


def create_legacy(path: Path) -> sqlite3.Connection:
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


def insert_announcement(
    connection: sqlite3.Connection,
    key: str,
    *,
    last_seen_at: str,
) -> None:
    agency, source_id = key.split(":", 1)
    connection.execute(
        "INSERT INTO announcements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            source_id,
            f"sensitive title {source_id}",
            agency,
            "서울",
            "행복주택",
            "청년",
            "2026-07-29",
            None,
            None,
            f"https://apply.lh.or.kr/item?secret={source_id}",
            "2026-07-01T00:00:00+00:00",
            last_seen_at,
        ),
    )


def insert_run(
    connection: sqlite3.Connection,
    *,
    started_at: str,
    finished_at: str | None,
    status: str = "success",
    agency_status: str = '{"GH":"ok","LH":"ok","SH":"ok"}',
) -> None:
    connection.execute(
        "INSERT INTO runs(started_at, finished_at, status, new_count, agency_status) "
        "VALUES (?, ?, ?, 0, ?)",
        (started_at, finished_at, status, agency_status),
    )


def rental_item(item_id: str = "announcement:LH:one") -> ObservedItem:
    agency, source_id = item_id.removeprefix("announcement:").split(":", 1)
    return ObservedItem(
        item_id=item_id,
        fields={
            "source_id": source_id,
            "title": f"sensitive title {source_id}",
            "agency": agency,
            "region": "서울",
            "housing_type": "행복주택",
            "target": "청년",
            "announcement_date": "2026-07-29",
            "application_start_date": None,
            "application_end_date": None,
            "url": f"https://apply.lh.or.kr/item?secret={source_id}",
        },
    )


class FakeAdapter:
    def __init__(self, outcome: ObservationBatch | BaseException) -> None:
        self.outcome = outcome
        self.calls = 0

    async def fetch(self, monitor_id, spec) -> ObservationBatch:
        self.calls += 1
        assert monitor_id == "rental-housing-seoul-gyeonggi"
        assert spec == _rental_spec("telegram-user:1")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class FailingSender:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, _address: str, _payload: dict[str, object]) -> str:
        self.calls += 1
        raise AssertionError("shadow sender must never be called")


def runtime_snapshot(connection: sqlite3.Connection) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = (
        "users",
        "delivery_targets",
        "monitors",
        "monitor_versions",
        "observations",
        "runs",
        "outbox",
        "deliveries",
        "pending_actions",
        "credential_refs",
        "diagnostic_snapshots",
        "adaptive_features",
        "operator_events",
        "health_write_probe",
    )
    return {
        table: tuple(
            tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
        )
        for table in tables
    }


def make_batch(
    *,
    items: tuple[ObservedItem, ...] = (),
    statuses: dict[str, str] | None = None,
    warnings: tuple[SourceWarning, ...] = (),
    observed_at: datetime = datetime(2026, 7, 29, 3, tzinfo=UTC),
) -> ObservationBatch:
    return ObservationBatch(
        monitor_id="rental-housing-seoul-gyeonggi",
        items=items,
        observed_at=observed_at,
        source_hash="b" * 64,
        source_status=STATUSES if statuses is None else statuses,
        warnings=warnings,
    )


def seed_observation(connection: sqlite3.Connection, item: ObservedItem) -> None:
    timestamp = "2026-07-01T00:00:00+00:00"
    connection.execute(
        "INSERT INTO observations(monitor_id, item_id, fields_json, content_hash, "
        "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "rental-housing-seoul-gyeonggi",
            item.item_id,
            canonical_json(item.fields),
            content_hash(item.fields),
            timestamp,
            timestamp,
        ),
    )


def create_shadow_source(path: Path, *keys: str) -> None:
    connection = create_legacy(path)
    for key in keys:
        insert_announcement(
            connection,
            key.removeprefix("announcement:"),
            last_seen_at="2026-07-29T12:00:00+09:00",
        )
    insert_run(
        connection,
        started_at="2026-07-29T12:00:00+09:00",
        finished_at="2026-07-29T12:30:00+09:00",
    )
    connection.commit()
    connection.close()


def imported_delivered_repository(
    tmp_path: Path,
) -> tuple[ShadowRepository, ObservedItem]:
    source = tmp_path / "legacy-import.db"
    database = tmp_path / "personal-import.db"
    connection = create_legacy(source)
    insert_announcement(
        connection,
        "LH:one",
        last_seen_at="2026-07-29T12:00:00+09:00",
    )
    insert_run(
        connection,
        started_at="2026-07-29T12:00:00+09:00",
        finished_at="2026-07-29T12:30:00+09:00",
    )
    connection.execute(
        "INSERT INTO deliveries(announcement_key, chat_id, delivered_at, message_id) "
        "VALUES ('LH:one', '12345', '2026-07-29T12:15:00+09:00', 77)"
    )
    connection.commit()
    connection.close()
    import_rental_state(
        source,
        database,
        "telegram-user:1",
        "target-1",
    )
    return (
        ShadowRepository(open_existing_database(database), clock=lambda: NOW),
        rental_item(),
    )


@pytest.fixture
def shadow_repo() -> ShadowRepository:
    connection = open_database(":memory:")
    seed_exact_import_marker(connection)
    repository = ShadowRepository(connection, clock=lambda: NOW)
    yield repository
    connection.close()


def test_comparator_hashes_only_safe_normalized_identifiers_and_status() -> None:
    old = snapshot(
        "announcement:SH:two",
        "announcement:LH:one",
        statuses={"SH": "ok", "GH": "failed", "LH": "ok"},
    )
    new = snapshot(
        "announcement:GH:three",
        "announcement:LH:one",
        statuses={"LH": "ok", "SH": "ok", "GH": "failed"},
    )

    result = ShadowComparator(clock=lambda: NOW).compare(old, new, date(2026, 7, 29))

    assert result.old_hash == content_hash(
        {
            "items": [
                ["LH", "announcement:LH:one", "included"],
                ["SH", "announcement:SH:two", "included"],
            ],
            "source_status": {"GH": "failed", "LH": "ok", "SH": "ok"},
        }
    )
    assert result.new_hash == content_hash(
        {
            "items": [
                ["GH", "announcement:GH:three", "included"],
                ["LH", "announcement:LH:one", "included"],
            ],
            "source_status": {"GH": "failed", "LH": "ok", "SH": "ok"},
        }
    )
    assert result.matched is False
    assert [
        (difference.agency, difference.missing_ids, difference.extra_ids)
        for difference in result.differences
    ] == [
        ("GH", (), (opaque_item_id("announcement:GH:three"),)),
        ("SH", (opaque_item_id("announcement:SH:two"),), ()),
    ]
    rendered = repr((old, new, result, *result.differences))
    assert "announcement:" not in rendered
    assert "one" not in rendered


def test_status_mismatch_is_unmatched_without_item_differences() -> None:
    old = snapshot("announcement:LH:one")
    new = snapshot(
        "announcement:LH:one",
        statuses={"LH": "ok", "SH": "failed", "GH": "ok"},
    )

    result = ShadowComparator(clock=lambda: NOW).compare(old, new, date(2026, 7, 29))

    assert result.matched is False
    assert result.differences == ()
    assert dict(result.old_status) == STATUSES
    assert dict(result.new_status) == {"LH": "ok", "SH": "failed", "GH": "ok"}


@pytest.mark.parametrize(
    ("agency", "item_id"),
    (
        ("XX", "announcement:XX:one"),
        ("LH", "announcement:SH:one"),
        ("LH", "https://secret.example/item?q=credential"),
        ("LH", "announcement:LH:"),
    ),
)
def test_shadow_item_rejects_unknown_conflicting_or_unsafe_identity_redacted(
    agency: str,
    item_id: str,
) -> None:
    with pytest.raises(ValueError) as caught:
        ShadowItem(agency=agency, item_id=item_id)

    assert item_id not in str(caught.value)
    assert item_id not in repr(caught.value)


@pytest.mark.parametrize(
    "raw_item_id",
    (
        "announcement:LH:credential-MARKER-plain",
        "announcement:LH:https://user:password@secret.example/path?token=MARKER",
        "announcement:LH:notice%3Ftoken%3DMARKER",
        "announcement:LH:비밀-MARKER",
        "announcement:LH:line\nMARKER",
    ),
)
def test_differences_always_use_opaque_aliases_and_never_reveal_raw_identity(
    shadow_repo: ShadowRepository,
    raw_item_id: str,
) -> None:
    result = ShadowComparator(clock=lambda: NOW).compare(
        snapshot(raw_item_id),
        snapshot(),
        SEOUL_TODAY,
    )

    shadow_repo.record(result)

    expected = opaque_item_id(raw_item_id)
    assert result.differences[0].missing_ids == (expected,)
    stored = shadow_repo.connection.execute(
        "SELECT differences_json FROM rental_shadow_results"
    ).fetchone()[0]
    rendered = canonical_json(
        {
            "differences": tuple(
                {
                    "agency": difference.agency,
                    "missing_ids": difference.missing_ids,
                    "extra_ids": difference.extra_ids,
                }
                for difference in result.differences
            )
        }
    )
    for output in (stored, rendered, repr(result), repr(result.differences[0])):
        assert expected in output or output.startswith("<")
        assert "MARKER" not in output
        assert "secret.example" not in output
        assert "password" not in output


def test_oversized_raw_identity_is_rejected_without_revealing_marker() -> None:
    raw_item_id = "announcement:LH:" + ("MARKER" * 100)

    with pytest.raises(ValueError) as caught:
        ShadowItem("LH", raw_item_id)

    assert "MARKER" not in str(caught.value)
    assert "MARKER" not in repr(caught.value)


def test_snapshot_rejects_conflicting_duplicate_and_nonexact_status_map() -> None:
    with pytest.raises(ValueError):
        ShadowSnapshot(
            items=(
                ShadowItem("LH", "announcement:LH:one"),
                ShadowItem("LH", "announcement:LH:one"),
            ),
            source_status=STATUSES,
        )
    with pytest.raises(ValueError):
        snapshot("announcement:LH:one", statuses={"LH": "ok", "SH": "ok"})
    with pytest.raises(ValueError):
        snapshot(
            "announcement:LH:one",
            statuses={"LH": "ok", "SH": "ok", "GH": "unknown"},
        )


def test_seven_consecutive_matches_ending_today_are_ready(
    shadow_repo: ShadowRepository,
) -> None:
    start = SEOUL_TODAY - timedelta(days=6)
    for offset in range(7):
        shadow_repo.record(result_for(start + timedelta(days=offset)))
    seed_passing_probe(shadow_repo, SEOUL_TODAY)

    status = shadow_repo.status(SEOUL_TODAY)

    assert status.consecutive_matches == 7
    assert status.last_match_date == SEOUL_TODAY
    assert status.unresolved_differences == 0
    assert status.state_imported is True
    assert status.duplicate_probe_passed is True
    assert status.cutover_ready is True
    assert shadow_repo.cutover_ready(SEOUL_TODAY) is True


def test_seven_consecutive_matches_ending_yesterday_are_ready(
    shadow_repo: ShadowRepository,
) -> None:
    yesterday = SEOUL_TODAY - timedelta(days=1)
    start = yesterday - timedelta(days=6)
    for offset in range(7):
        shadow_repo.record(result_for(start + timedelta(days=offset)))
    seed_passing_probe(shadow_repo, yesterday)

    assert shadow_repo.cutover_ready(SEOUL_TODAY) is True


def test_status_uses_one_snapshot_across_concurrent_marker_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import personal_monitor.migration.shadow as shadow_module

    database = tmp_path / "status-snapshot.db"
    connection = open_database(database)
    seed_exact_import_marker(connection)
    repository = ShadowRepository(connection, clock=lambda: NOW)
    start = SEOUL_TODAY - timedelta(days=6)
    for offset in range(7):
        repository.record(result_for(start + timedelta(days=offset)))
    seed_passing_probe(repository, SEOUL_TODAY)
    replacement = open_existing_database(database)
    replacement.execute("PRAGMA busy_timeout = 0")
    original = shadow_module._state_imported

    def replace_then_read(
        active: sqlite3.Connection,
        evidence_time: datetime,
    ) -> bool:
        replacement.execute("DELETE FROM runs WHERE id = 'migration:rental-housing:v1'")
        return original(active, evidence_time)

    monkeypatch.setattr(shadow_module, "_state_imported", replace_then_read)
    try:
        status = repository.status(SEOUL_TODAY)

        assert status.state_imported is True
        assert status.cutover_ready is True
    finally:
        replacement.close()
        connection.close()


@pytest.mark.parametrize("fault", ("gap", "mismatch", "stale", "stale_probe"))
def test_gap_mismatch_or_stale_evidence_is_not_ready(
    shadow_repo: ShadowRepository,
    fault: str,
) -> None:
    end = SEOUL_TODAY - (timedelta(days=2) if fault == "stale" else timedelta())
    start = end - timedelta(days=6)
    for offset in range(7):
        if fault == "gap" and offset == 3:
            continue
        shadow_repo.record(
            result_for(
                start + timedelta(days=offset),
                matched=not (fault == "mismatch" and offset == 3),
            )
        )
    probe_date = end - timedelta(days=1) if fault == "stale_probe" else end
    seed_passing_probe(shadow_repo, probe_date)

    assert shadow_repo.cutover_ready(SEOUL_TODAY) is False


def test_rerun_replaces_date_and_later_clean_streak_resolves_old_mismatch(
    shadow_repo: ShadowRepository,
) -> None:
    old_day = SEOUL_TODAY - timedelta(days=10)
    shadow_repo.record(result_for(old_day, matched=False))
    start = SEOUL_TODAY - timedelta(days=6)
    for offset in range(7):
        shadow_repo.record(result_for(start + timedelta(days=offset)))
    replacement = result_for(SEOUL_TODAY, matched=True)
    shadow_repo.record(replacement)
    seed_passing_probe(shadow_repo, SEOUL_TODAY)

    status = shadow_repo.status(SEOUL_TODAY)
    stored = shadow_repo.connection.execute(
        "SELECT matched, recorded_at FROM rental_shadow_results WHERE run_date = ?",
        (SEOUL_TODAY.isoformat(),),
    ).fetchone()

    assert tuple(stored) == (1, replacement.recorded_at.isoformat())
    assert status.consecutive_matches == 7
    assert status.unresolved_differences == 0
    assert status.cutover_ready is True


def test_repository_serializes_only_safe_canonical_json(
    shadow_repo: ShadowRepository,
) -> None:
    result = ShadowComparator(clock=lambda: NOW).compare(
        snapshot("announcement:LH:old-secret"),
        snapshot("announcement:LH:new-secret"),
        SEOUL_TODAY,
    )

    shadow_repo.record(result)

    row = shadow_repo.connection.execute(
        "SELECT differences_json, old_status_json, new_status_json "
        "FROM rental_shadow_results WHERE run_date = ?",
        (SEOUL_TODAY.isoformat(),),
    ).fetchone()
    assert row["differences_json"] == canonical_json(
        [
            {
                "agency": "LH",
                "extra_ids": [opaque_item_id("announcement:LH:new-secret")],
                "missing_ids": [opaque_item_id("announcement:LH:old-secret")],
            }
        ]
    )
    assert row["old_status_json"] == '{"GH":"ok","LH":"ok","SH":"ok"}'
    assert row["new_status_json"] == '{"GH":"ok","LH":"ok","SH":"ok"}'
    assert "title" not in "".join(row)
    assert "url" not in "".join(row)


def test_record_is_atomic_when_upsert_fails_after_validation(
    shadow_repo: ShadowRepository,
) -> None:
    original = result_for(SEOUL_TODAY, matched=False)
    shadow_repo.record(original)
    shadow_repo.connection.execute(
        "CREATE TRIGGER reject_shadow_update BEFORE UPDATE ON rental_shadow_results "
        "BEGIN SELECT RAISE(ABORT, 'sensitive database detail'); END"
    )

    with pytest.raises(RuntimeError) as caught:
        shadow_repo.record(result_for(SEOUL_TODAY, matched=True))

    assert str(caught.value) == "shadow result storage failed"
    assert "sensitive database detail" not in repr(caught.value)
    stored = shadow_repo.connection.execute(
        "SELECT old_hash, new_hash, matched FROM rental_shadow_results WHERE run_date = ?",
        (SEOUL_TODAY.isoformat(),),
    ).fetchone()
    assert tuple(stored) == (original.old_hash, original.new_hash, 0)


def test_duplicate_probe_record_is_atomic_and_redacts_storage_failure(
    shadow_repo: ShadowRepository,
) -> None:
    original = DuplicateProbeResult(
        monitor_id="rental-housing-seoul-gyeonggi",
        run_date=SEOUL_TODAY,
        current_hash="a" * 64,
        passed=False,
        missing_ids=(opaque_item_id("announcement:LH:one"),),
        conflicting_ids=(),
        recorded_at=NOW,
    )
    shadow_repo.record_duplicate_probe(original)
    shadow_repo.connection.execute(
        "CREATE TRIGGER reject_probe_update BEFORE UPDATE "
        "ON rental_duplicate_probe_results "
        "BEGIN SELECT RAISE(ABORT, 'sensitive probe detail'); END"
    )
    replacement = DuplicateProbeResult(
        monitor_id="rental-housing-seoul-gyeonggi",
        run_date=SEOUL_TODAY,
        current_hash="b" * 64,
        passed=True,
        missing_ids=(),
        conflicting_ids=(),
        recorded_at=NOW,
    )

    with pytest.raises(RuntimeError) as caught:
        shadow_repo.record_duplicate_probe(replacement)

    assert str(caught.value) == "duplicate probe storage failed"
    assert "sensitive probe detail" not in repr(caught.value)
    stored = shadow_repo.connection.execute(
        "SELECT current_hash, passed, differences_json FROM rental_duplicate_probe_results"
    ).fetchone()
    assert tuple(stored) == (
        original.current_hash,
        0,
        canonical_json(
            {
                "conflicting_ids": [],
                "missing_ids": [opaque_item_id("announcement:LH:one")],
            }
        ),
    )


def test_status_rejects_future_date_and_corrupt_storage(
    shadow_repo: ShadowRepository,
) -> None:
    with pytest.raises(ValueError):
        shadow_repo.status(SEOUL_TODAY + timedelta(days=1))
    shadow_repo.record(result_for(SEOUL_TODAY))
    shadow_repo.connection.execute("UPDATE rental_shadow_results SET old_hash = 'NOT-A-HASH'")
    with pytest.raises(RuntimeError) as caught:
        shadow_repo.status(SEOUL_TODAY)
    assert "NOT-A-HASH" not in str(caught.value)


def test_status_rejects_noncanonical_stored_timestamp(
    shadow_repo: ShadowRepository,
) -> None:
    shadow_repo.record(result_for(SEOUL_TODAY))
    shadow_repo.connection.execute(
        "UPDATE rental_shadow_results SET recorded_at = '2026-07-30T09:00:00+09:00'"
    )

    with pytest.raises(RuntimeError, match="shadow storage"):
        shadow_repo.status(SEOUL_TODAY)


def test_status_rejects_future_probe_evidence(
    shadow_repo: ShadowRepository,
) -> None:
    shadow_repo.record(result_for(SEOUL_TODAY))
    seed_passing_probe(shadow_repo, SEOUL_TODAY)
    shadow_repo.connection.execute(
        "UPDATE rental_duplicate_probe_results SET run_date = '2026-07-31'"
    )

    with pytest.raises(RuntimeError, match="probe storage"):
        shadow_repo.status(SEOUL_TODAY)


@pytest.mark.parametrize(
    ("evidence", "error"),
    (
        ("shadow", "shadow storage"),
        ("probe", "probe storage"),
        ("marker", "import marker"),
    ),
)
def test_status_rejects_future_recorded_evidence_relative_to_injected_clock(
    shadow_repo: ShadowRepository,
    evidence: str,
    error: str,
) -> None:
    shadow_repo.record(result_for(SEOUL_TODAY))
    seed_passing_probe(shadow_repo, SEOUL_TODAY)
    future = (NOW + timedelta(seconds=1)).isoformat()
    if evidence == "shadow":
        shadow_repo.connection.execute(
            "UPDATE rental_shadow_results SET recorded_at = ?",
            (future,),
        )
    elif evidence == "probe":
        shadow_repo.connection.execute(
            "UPDATE rental_duplicate_probe_results SET recorded_at = ?",
            (future,),
        )
    else:
        shadow_repo.connection.execute(
            "UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?",
            (future, future, "migration:rental-housing:v1"),
        )

    with pytest.raises(RuntimeError, match=error):
        shadow_repo.status(SEOUL_TODAY)


def test_public_results_reject_bool_counts_and_hide_all_values() -> None:
    with pytest.raises(ValueError):
        DuplicateProbeResult(
            monitor_id="rental-housing-seoul-gyeonggi",
            run_date=SEOUL_TODAY,
            current_hash="a" * 64,
            passed=True,
            missing_ids=(),
            conflicting_ids=(),
            recorded_at=datetime(2026, 7, 30),
        )
    probe = DuplicateProbeResult(
        monitor_id="rental-housing-seoul-gyeonggi",
        run_date=SEOUL_TODAY,
        current_hash="a" * 64,
        passed=False,
        missing_ids=(opaque_item_id("announcement:LH:private-value"),),
        conflicting_ids=(),
        recorded_at=NOW,
    )
    assert repr(probe) == "<DuplicateProbeResult redacted>"
    assert json.loads(json.dumps({"passed": probe.passed})) == {"passed": False}


def test_legacy_loader_selects_latest_finished_run_and_inclusive_window(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.db"
    connection = create_legacy(source)
    insert_announcement(
        connection,
        "LH:older",
        last_seen_at="2026-07-29T01:30:00+09:00",
    )
    insert_announcement(
        connection,
        "SH:start",
        last_seen_at="2026-07-29T12:00:00+09:00",
    )
    insert_announcement(
        connection,
        "GH:end",
        last_seen_at="2026-07-29T12:30:00+09:00",
    )
    insert_announcement(
        connection,
        "LH:after",
        last_seen_at="2026-07-29T12:30:01+09:00",
    )
    insert_run(
        connection,
        started_at="2026-07-29T01:00:00+09:00",
        finished_at="2026-07-29T02:00:00+09:00",
    )
    insert_run(
        connection,
        started_at="2026-07-29T12:00:00+09:00",
        finished_at="2026-07-29T12:30:00+09:00",
        status="partial_failure",
        agency_status='{"LH":"ok","SH":"failed","GH":"ok"}',
    )
    connection.commit()
    connection.close()

    loaded = load_legacy_shadow_snapshot(
        source,
        date(2026, 7, 29),
        now=datetime(2026, 7, 30, tzinfo=UTC),
    )

    assert [(item.agency, item.item_id) for item in loaded.items] == [
        ("GH", "announcement:GH:end"),
        ("SH", "announcement:SH:start"),
    ]
    assert dict(loaded.source_status) == {"LH": "ok", "SH": "failed", "GH": "ok"}
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()
    assert not Path(f"{source}-journal").exists()


def test_legacy_loader_uses_later_success_after_an_earlier_failed_run(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.db"
    connection = create_legacy(source)
    insert_announcement(
        connection,
        "LH:later",
        last_seen_at="2026-07-29T12:00:00+09:00",
    )
    insert_run(
        connection,
        started_at="2026-07-29T01:00:00+09:00",
        finished_at="2026-07-29T01:30:00+09:00",
        status="telegram_failure",
    )
    insert_run(
        connection,
        started_at="2026-07-29T12:00:00+09:00",
        finished_at="2026-07-29T12:30:00+09:00",
    )
    connection.commit()
    connection.close()

    loaded = load_legacy_shadow_snapshot(source, date(2026, 7, 29), now=NOW)

    assert [item.item_id for item in loaded.items] == ["announcement:LH:later"]


def test_legacy_loader_rejects_a_run_that_finishes_in_the_future(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.db"
    connection = create_legacy(source)
    insert_run(
        connection,
        started_at="2026-07-30T12:00:00+09:00",
        finished_at="2026-07-30T12:30:00+09:00",
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError):
        load_legacy_shadow_snapshot(source, date(2026, 7, 30), now=NOW)


@pytest.mark.parametrize(
    "mutation",
    (
        "no-run",
        "running",
        "telegram-failure",
        "missing-status",
        "duplicate-latest",
    ),
)
def test_legacy_loader_rejects_missing_ambiguous_or_failed_run_redacted(
    tmp_path: Path,
    mutation: str,
) -> None:
    source = tmp_path / f"sensitive-{mutation}.db"
    connection = create_legacy(source)
    if mutation != "no-run":
        insert_run(
            connection,
            started_at="2026-07-29T12:00:00+09:00",
            finished_at=None if mutation == "running" else "2026-07-29T12:30:00+09:00",
            status=(
                "running"
                if mutation == "running"
                else "telegram_failure"
                if mutation == "telegram-failure"
                else "success"
            ),
            agency_status=(
                '{"LH":"ok","SH":"ok"}'
                if mutation == "missing-status"
                else '{"LH":"ok","SH":"ok","GH":"ok"}'
            ),
        )
    if mutation == "duplicate-latest":
        insert_run(
            connection,
            started_at="2026-07-29T12:00:00+09:00",
            finished_at="2026-07-29T12:45:00+09:00",
        )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError) as caught:
        load_legacy_shadow_snapshot(
            source,
            date(2026, 7, 29),
            now=datetime(2026, 7, 30, tzinfo=UTC),
        )

    assert str(source) not in str(caught.value)
    assert mutation not in str(caught.value)


def test_legacy_loader_rejects_future_old_relative_and_live_snapshot_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.db"
    connection = create_legacy(source)
    insert_run(
        connection,
        started_at="2026-07-29T12:00:00+09:00",
        finished_at="2026-07-29T12:30:00+09:00",
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError):
        load_legacy_shadow_snapshot(
            source,
            date(2026, 7, 31),
            now=datetime(2026, 7, 30, tzinfo=UTC),
        )
    with pytest.raises(RuntimeError):
        load_legacy_shadow_snapshot(
            source,
            date(2026, 5, 1),
            now=datetime(2026, 7, 30, tzinfo=UTC),
        )
    with pytest.raises(RuntimeError):
        load_legacy_shadow_snapshot(
            Path("relative.db"),
            date(2026, 7, 29),
            now=datetime(2026, 7, 30, tzinfo=UTC),
        )
    Path(f"{source}-wal").touch()
    with pytest.raises(RuntimeError):
        load_legacy_shadow_snapshot(
            source,
            date(2026, 7, 29),
            now=datetime(2026, 7, 30, tzinfo=UTC),
        )


def test_shadow_fetch_records_comparison_without_any_runtime_or_sender_change(
    tmp_path: Path,
    shadow_repo: ShadowRepository,
) -> None:
    source = tmp_path / "legacy.db"
    item = rental_item()
    create_shadow_source(source, item.item_id)
    adapter = FakeAdapter(make_batch(items=(item,)))
    sender = FailingSender()
    before = runtime_snapshot(shadow_repo.connection)

    result = asyncio.run(
        run_shadow_fetch(
            source,
            shadow_repo,
            date(2026, 7, 29),
            adapter=adapter,
            sender=sender,
            now=NOW,
        )
    )

    assert result.matched is True
    assert adapter.calls == 1
    assert sender.calls == 0
    assert runtime_snapshot(shadow_repo.connection) == before
    assert (
        shadow_repo.connection.execute("SELECT count(*) FROM rental_shadow_results").fetchone()[0]
        == 1
    )


def test_shadow_fetch_records_partial_status_as_unmatched_without_persistence(
    tmp_path: Path,
    shadow_repo: ShadowRepository,
) -> None:
    source = tmp_path / "legacy.db"
    create_shadow_source(source)
    adapter = FakeAdapter(
        make_batch(
            statuses={"LH": "ok", "SH": "failed", "GH": "ok"},
            warnings=(SourceWarning("SH", "collection", "sensitive response body"),),
        )
    )
    sender = FailingSender()
    before = runtime_snapshot(shadow_repo.connection)

    result = asyncio.run(
        run_shadow_fetch(
            source,
            shadow_repo,
            date(2026, 7, 29),
            adapter=adapter,
            sender=sender,
            now=NOW,
        )
    )

    assert result.matched is False
    assert dict(result.new_status)["SH"] == "failed"
    assert sender.calls == 0
    assert runtime_snapshot(shadow_repo.connection) == before
    stored = shadow_repo.connection.execute(
        "SELECT differences_json, new_status_json FROM rental_shadow_results"
    ).fetchone()
    assert "response body" not in "".join(stored)


@pytest.mark.parametrize(
    "outcome",
    (
        ValueError("sensitive URL https://secret.example/?key=credential"),
        make_batch(observed_at=datetime(2026, 7, 28, 3, tzinfo=UTC)),
        make_batch(statuses={"LH": "ok", "SH": "ok"}),
    ),
)
def test_shadow_fetch_ordinary_failure_is_redacted_and_leaves_all_rows_unchanged(
    tmp_path: Path,
    shadow_repo: ShadowRepository,
    outcome: ObservationBatch | BaseException,
) -> None:
    source = tmp_path / "legacy.db"
    create_shadow_source(source)
    adapter = FakeAdapter(outcome)
    sender = FailingSender()
    before = runtime_snapshot(shadow_repo.connection)

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(
            run_shadow_fetch(
                source,
                shadow_repo,
                date(2026, 7, 29),
                adapter=adapter,
                sender=sender,
                now=NOW,
            )
        )

    assert str(caught.value) == "rental shadow failed"
    assert sender.calls == 0
    assert runtime_snapshot(shadow_repo.connection) == before
    assert (
        shadow_repo.connection.execute("SELECT count(*) FROM rental_shadow_results").fetchone()[0]
        == 0
    )


class FatalShadowFailure(BaseException):
    pass


@pytest.mark.parametrize("failure", (FatalShadowFailure(),))
def test_shadow_fetch_fatal_error_outranks_redaction_and_writes_nothing(
    tmp_path: Path,
    shadow_repo: ShadowRepository,
    failure: BaseException,
) -> None:
    source = tmp_path / "legacy.db"
    create_shadow_source(source)
    sender = FailingSender()
    before = runtime_snapshot(shadow_repo.connection)

    with pytest.raises(FatalShadowFailure):
        asyncio.run(
            run_shadow_fetch(
                source,
                shadow_repo,
                date(2026, 7, 29),
                adapter=FakeAdapter(failure),
                sender=sender,
                now=NOW,
            )
        )

    assert sender.calls == 0
    assert runtime_snapshot(shadow_repo.connection) == before


def test_duplicate_probe_passes_without_runtime_persistence_or_sender(
    shadow_repo: ShadowRepository,
) -> None:
    item = rental_item()
    seed_observation(shadow_repo.connection, item)
    sender = FailingSender()
    adapter = FakeAdapter(make_batch(items=(item,)))
    before = runtime_snapshot(shadow_repo.connection)

    result = asyncio.run(
        run_duplicate_probe(
            shadow_repo,
            "rental-housing-seoul-gyeonggi",
            adapter=adapter,
            sender=sender,
            now=NOW,
        )
    )

    assert result.passed is True
    assert result.missing_ids == ()
    assert result.conflicting_ids == ()
    assert result.current_hash == content_hash(
        {
            "items": [["LH", "announcement:LH:one", "included"]],
            "source_status": {"GH": "ok", "LH": "ok", "SH": "ok"},
        }
    )
    assert adapter.calls == 1
    assert sender.calls == 0
    assert runtime_snapshot(shadow_repo.connection) == before
    assert (
        shadow_repo.connection.execute(
            "SELECT passed FROM rental_duplicate_probe_results"
        ).fetchone()[0]
        == 1
    )


def test_duplicate_probe_passes_with_real_task2_delivered_aggregate(
    tmp_path: Path,
) -> None:
    repository, item = imported_delivered_repository(tmp_path)
    sender = FailingSender()
    before = runtime_snapshot(repository.connection)
    try:
        result = asyncio.run(
            run_duplicate_probe(
                repository,
                "rental-housing-seoul-gyeonggi",
                adapter=FakeAdapter(make_batch(items=(item,))),
                sender=sender,
                now=NOW,
            )
        )

        assert result.passed is True
        assert sender.calls == 0
        assert runtime_snapshot(repository.connection) == before
    finally:
        repository.connection.close()


def test_duplicate_probe_passes_empty_task2_import_with_fixed_marker(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty-legacy-import.db"
    database = tmp_path / "empty-personal-import.db"
    legacy = create_legacy(source)
    legacy.commit()
    legacy.close()
    import_rental_state(source, database, "telegram-user:1", "target-1")
    repository = ShadowRepository(open_existing_database(database), clock=lambda: NOW)
    try:
        result = asyncio.run(
            run_duplicate_probe(
                repository,
                "rental-housing-seoul-gyeonggi",
                adapter=FakeAdapter(make_batch()),
                sender=FailingSender(),
                now=NOW,
            )
        )

        assert result.passed is True
        assert (
            repository.connection.execute(
                "SELECT started_at FROM runs WHERE id = 'migration:rental-housing:v1'"
            ).fetchone()[0]
            == "2000-01-01T00:00:00+00:00"
        )
    finally:
        repository.connection.close()


def test_duplicate_probe_validation_and_record_share_one_immediate_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import personal_monitor.migration.shadow as shadow_module

    repository, item = imported_delivered_repository(tmp_path)
    original = shadow_module._probe_imported_state

    def require_transaction(connection, batch, spec):
        assert connection.in_transaction
        return original(connection, batch, spec)

    monkeypatch.setattr(
        shadow_module,
        "_probe_imported_state",
        require_transaction,
    )
    try:
        result = asyncio.run(
            run_duplicate_probe(
                repository,
                "rental-housing-seoul-gyeonggi",
                adapter=FakeAdapter(make_batch(items=(item,))),
                sender=FailingSender(),
                now=NOW,
            )
        )

        assert result.passed is True
    finally:
        repository.connection.close()


def test_duplicate_probe_revalidates_active_version_after_fetch(
    tmp_path: Path,
) -> None:
    repository, item = imported_delivered_repository(tmp_path)
    database = tmp_path / "personal-import.db"
    replacement = open_existing_database(database)

    class ReplacingAdapter(FakeAdapter):
        async def fetch(self, monitor_id, spec) -> ObservationBatch:
            batch = await super().fetch(monitor_id, spec)
            replacement.execute(
                "UPDATE monitors SET active_version_id = NULL "
                "WHERE id = 'rental-housing-seoul-gyeonggi'"
            )
            return batch

    try:
        with pytest.raises(RuntimeError, match="duplicate probe failed"):
            asyncio.run(
                run_duplicate_probe(
                    repository,
                    "rental-housing-seoul-gyeonggi",
                    adapter=ReplacingAdapter(make_batch(items=(item,))),
                    sender=FailingSender(),
                    now=NOW,
                )
            )

        assert (
            repository.connection.execute(
                "SELECT count(*) FROM rental_duplicate_probe_results"
            ).fetchone()[0]
            == 0
        )
    finally:
        replacement.close()
        repository.connection.close()


@pytest.mark.parametrize(
    "mutation",
    (
        "observation-order",
        "observation-time-format",
        "outbox-time-format",
        "message-id-format",
        "target-kind",
        "target-address",
        "target-created-at",
        "missing-owner",
        "orphan-delivery",
        "missing-delivery",
        "wrong-outbox-prefix",
        "extra-orphan-rental-delivery",
        "future-observation",
        "future-delivery",
    ),
)
def test_duplicate_probe_fails_malformed_task2_aggregate(
    tmp_path: Path,
    mutation: str,
) -> None:
    repository, item = imported_delivered_repository(tmp_path)
    connection = repository.connection
    if mutation == "observation-order":
        connection.execute("UPDATE observations SET first_seen_at = '2026-07-30T00:00:00+00:00'")
    elif mutation == "observation-time-format":
        connection.execute("UPDATE observations SET last_seen_at = '2026-07-29T12:00:00+09:00'")
    elif mutation == "outbox-time-format":
        timestamp = "2026-07-29T12:15:00+09:00"
        connection.execute(
            "UPDATE outbox SET available_at = ?, created_at = ?",
            (timestamp, timestamp),
        )
        connection.execute("UPDATE deliveries SET delivered_at = ?", (timestamp,))
    elif mutation == "message-id-format":
        connection.execute("UPDATE deliveries SET external_message_id = '01'")
    elif mutation == "target-kind":
        connection.execute("UPDATE delivery_targets SET kind = 'email'")
    elif mutation == "target-address":
        connection.execute("UPDATE delivery_targets SET address = ' bad address '")
    elif mutation == "missing-owner":
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM users WHERE id = 'telegram-user:1'")
        connection.execute("PRAGMA foreign_keys = ON")
    elif mutation == "orphan-delivery":
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM outbox")
        connection.execute("PRAGMA foreign_keys = ON")
    elif mutation == "missing-delivery":
        connection.execute("DELETE FROM deliveries")
    elif mutation == "wrong-outbox-prefix":
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("UPDATE outbox SET id = 'wrong-prefix'")
        connection.execute("UPDATE deliveries SET outbox_id = 'wrong-prefix'")
        connection.execute("PRAGMA foreign_keys = ON")
    elif mutation == "extra-orphan-rental-delivery":
        marker_time = connection.execute(
            "SELECT started_at FROM runs WHERE id = 'migration:rental-housing:v1'"
        ).fetchone()[0]
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO deliveries(outbox_id, target_id, external_message_id, delivered_at) "
            "VALUES (?, 'target-1', '99', ?)",
            ("rental-import:" + ("f" * 64), marker_time),
        )
        connection.execute("PRAGMA foreign_keys = ON")
    elif mutation in {"future-observation", "future-delivery"}:
        marker_time = connection.execute(
            "SELECT started_at FROM runs WHERE id = 'migration:rental-housing:v1'"
        ).fetchone()[0]
        future = (datetime.fromisoformat(marker_time) + timedelta(hours=1)).isoformat()
        if mutation == "future-observation":
            connection.execute(
                "UPDATE observations SET last_seen_at = ?",
                (future,),
            )
        else:
            connection.execute(
                "UPDATE outbox SET available_at = ?, created_at = ?",
                (future, future),
            )
            connection.execute(
                "UPDATE deliveries SET delivered_at = ?",
                (future,),
            )
    else:
        connection.execute("UPDATE delivery_targets SET created_at = '2026-07-29T12:30:00+09:00'")
    before = runtime_snapshot(connection)
    sender = FailingSender()
    try:
        result = asyncio.run(
            run_duplicate_probe(
                repository,
                "rental-housing-seoul-gyeonggi",
                adapter=FakeAdapter(make_batch(items=(item,))),
                sender=sender,
                now=NOW,
            )
        )

        assert result.passed is False
        assert sender.calls == 0
        assert runtime_snapshot(connection) == before
        assert (
            connection.execute("SELECT passed FROM rental_duplicate_probe_results").fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_duplicate_probe_ignores_unrelated_orphan_delivery(
    tmp_path: Path,
) -> None:
    repository, item = imported_delivered_repository(tmp_path)
    connection = repository.connection
    marker_time = connection.execute(
        "SELECT started_at FROM runs WHERE id = 'migration:rental-housing:v1'"
    ).fetchone()[0]
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO deliveries(outbox_id, target_id, external_message_id, delivered_at) "
        "VALUES ('unrelated-monitor:delivery', 'target-1', '99', ?)",
        (marker_time,),
    )
    connection.execute("PRAGMA foreign_keys = ON")
    before = runtime_snapshot(connection)
    try:
        result = asyncio.run(
            run_duplicate_probe(
                repository,
                "rental-housing-seoul-gyeonggi",
                adapter=FakeAdapter(make_batch(items=(item,))),
                sender=FailingSender(),
                now=NOW,
            )
        )

        assert result.passed is True
        assert runtime_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.parametrize(
    "fault",
    ("missing", "conflict", "missing-marker", "forged-marker", "partial"),
)
def test_duplicate_probe_records_safe_failure_without_runtime_changes(
    shadow_repo: ShadowRepository,
    fault: str,
) -> None:
    item = rental_item()
    if fault != "missing":
        seed_observation(shadow_repo.connection, item)
    if fault == "conflict":
        shadow_repo.connection.execute(
            "UPDATE observations SET fields_json = '{}', content_hash = ?",
            ("c" * 64,),
        )
    if fault == "missing-marker":
        shadow_repo.connection.execute("DELETE FROM runs WHERE id = 'migration:rental-housing:v1'")
    if fault == "forged-marker":
        shadow_repo.connection.execute(
            "UPDATE runs SET stage = 'normal' WHERE id = 'migration:rental-housing:v1'"
        )
    statuses = {"LH": "ok", "SH": "failed", "GH": "ok"} if fault == "partial" else STATUSES
    warnings = (SourceWarning("SH", "collection", "secret response"),) if fault == "partial" else ()
    sender = FailingSender()
    before = runtime_snapshot(shadow_repo.connection)

    result = asyncio.run(
        run_duplicate_probe(
            shadow_repo,
            "rental-housing-seoul-gyeonggi",
            adapter=FakeAdapter(make_batch(items=(item,), statuses=statuses, warnings=warnings)),
            sender=sender,
            now=NOW,
        )
    )

    assert result.passed is False
    assert result.missing_ids == (
        (opaque_item_id("announcement:LH:one"),) if fault == "missing" else ()
    )
    assert result.conflicting_ids == (
        (opaque_item_id("announcement:LH:one"),) if fault == "conflict" else ()
    )
    assert sender.calls == 0
    assert runtime_snapshot(shadow_repo.connection) == before
    stored = shadow_repo.connection.execute(
        "SELECT passed, differences_json FROM rental_duplicate_probe_results"
    ).fetchone()
    assert stored["passed"] == 0
    assert "secret response" not in stored["differences_json"]


def test_duplicate_probe_fails_inconsistent_delivered_aggregate_safely(
    shadow_repo: ShadowRepository,
) -> None:
    item = rental_item()
    seed_observation(shadow_repo.connection, item)
    timestamp = "2026-07-01T00:00:00+00:00"
    shadow_repo.connection.execute(
        "INSERT INTO outbox(id, dedupe_key, monitor_id, target_id, payload_json, status, "
        "available_at, created_at) VALUES "
        "('rental-import:broken', ?, 'rental-housing-seoul-gyeonggi', 'target-1', '{}', "
        "'delivered', ?, ?)",
        (
            "rental-housing-seoul-gyeonggi:announcement:LH:one:new_item",
            timestamp,
            timestamp,
        ),
    )
    before = runtime_snapshot(shadow_repo.connection)

    result = asyncio.run(
        run_duplicate_probe(
            shadow_repo,
            "rental-housing-seoul-gyeonggi",
            adapter=FakeAdapter(make_batch(items=(item,))),
            sender=FailingSender(),
            now=NOW,
        )
    )

    assert result.passed is False
    assert runtime_snapshot(shadow_repo.connection) == before


def test_duplicate_probe_wrong_monitor_is_fixed_and_records_nothing(
    shadow_repo: ShadowRepository,
) -> None:
    sender = FailingSender()
    before = runtime_snapshot(shadow_repo.connection)

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(
            run_duplicate_probe(
                shadow_repo,
                "sensitive-other-monitor",
                adapter=FakeAdapter(make_batch()),
                sender=sender,
                now=NOW,
            )
        )

    assert str(caught.value) == "rental duplicate probe failed"
    assert "sensitive" not in repr(caught.value)
    assert sender.calls == 0
    assert runtime_snapshot(shadow_repo.connection) == before
    assert (
        shadow_repo.connection.execute(
            "SELECT count(*) FROM rental_duplicate_probe_results"
        ).fetchone()[0]
        == 0
    )


def test_duplicate_probe_propagates_real_cancellation_without_any_write(
    shadow_repo: ShadowRepository,
) -> None:
    sender = FailingSender()
    before = runtime_snapshot(shadow_repo.connection)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_duplicate_probe(
                shadow_repo,
                "rental-housing-seoul-gyeonggi",
                adapter=FakeAdapter(asyncio.CancelledError()),
                sender=sender,
                now=NOW,
            )
        )

    assert sender.calls == 0
    assert runtime_snapshot(shadow_repo.connection) == before
    assert (
        shadow_repo.connection.execute(
            "SELECT count(*) FROM rental_duplicate_probe_results"
        ).fetchone()[0]
        == 0
    )


def test_duplicate_probe_redacts_ordinary_failure_without_any_write(
    shadow_repo: ShadowRepository,
) -> None:
    sender = FailingSender()
    before = runtime_snapshot(shadow_repo.connection)

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(
            run_duplicate_probe(
                shadow_repo,
                "rental-housing-seoul-gyeonggi",
                adapter=FakeAdapter(ValueError("secret adapter response")),
                sender=sender,
                now=NOW,
            )
        )

    assert str(caught.value) == "rental duplicate probe failed"
    assert "secret" not in repr(caught.value)
    assert sender.calls == 0
    assert runtime_snapshot(shadow_repo.connection) == before
    assert (
        shadow_repo.connection.execute(
            "SELECT count(*) FROM rental_duplicate_probe_results"
        ).fetchone()[0]
        == 0
    )


def test_duplicate_probe_propagates_fatal_failure_without_any_write(
    shadow_repo: ShadowRepository,
) -> None:
    sender = FailingSender()
    before = runtime_snapshot(shadow_repo.connection)

    with pytest.raises(FatalShadowFailure):
        asyncio.run(
            run_duplicate_probe(
                shadow_repo,
                "rental-housing-seoul-gyeonggi",
                adapter=FakeAdapter(FatalShadowFailure()),
                sender=sender,
                now=NOW,
            )
        )

    assert sender.calls == 0
    assert runtime_snapshot(shadow_repo.connection) == before
    assert (
        shadow_repo.connection.execute(
            "SELECT count(*) FROM rental_duplicate_probe_results"
        ).fetchone()[0]
        == 0
    )
