import sqlite3
from datetime import UTC, date, datetime, timedelta

from rental_monitor.models import Agency, Announcement, HousingType
from rental_monitor.repository import AnnouncementRepository


def notice(title: str = "서울 행복주택 입주자 모집") -> Announcement:
    return Announcement(
        source_id="notice-1",
        title=title,
        agency=Agency.LH,
        region="서울특별시",
        housing_type=HousingType.HAPPY,
        target="청년, 신혼부부",
        announcement_date=date(2026, 7, 20),
        application_start_date=date(2026, 7, 27),
        application_end_date=date(2026, 7, 29),
        url="https://apply.lh.or.kr/notice/1",
    )


def test_seen_but_undelivered_notice_remains_pending(tmp_path) -> None:
    repository = AnnouncementRepository(tmp_path / "announcements.db")
    item = notice()

    repository.upsert_seen([item])

    assert repository.pending_for_chat([item], "42") == [item]


def test_successful_delivery_is_not_pending_again(tmp_path) -> None:
    repository = AnnouncementRepository(tmp_path / "announcements.db")
    item = notice()
    repository.upsert_seen([item])

    repository.mark_delivered(item, "42", 123)

    assert repository.pending_for_chat([item], "42") == []


def test_upsert_updates_content_without_changing_first_seen(tmp_path) -> None:
    repository = AnnouncementRepository(tmp_path / "announcements.db")
    first = datetime(2026, 7, 20, 3, tzinfo=UTC)
    later = datetime(2026, 7, 21, 3, tzinfo=UTC)
    repository.upsert_seen([notice()], observed_at=first)

    repository.upsert_seen([notice(title="[정정] 서울 행복주택 입주자 모집")], observed_at=later)

    row = repository.connection.execute(
        "SELECT title, first_seen_at, last_seen_at FROM announcements"
    ).fetchone()
    assert tuple(row) == (
        "[정정] 서울 행복주택 입주자 모집",
        first.isoformat(),
        later.isoformat(),
    )


def test_run_status_is_recorded(tmp_path) -> None:
    repository = AnnouncementRepository(tmp_path / "announcements.db")
    run_id = repository.start_run(datetime(2026, 7, 20, 3, tzinfo=UTC))

    repository.finish_run(
        run_id, status="partial_failure", new_count=2, agency_status={"SH": "failed"}
    )

    row = repository.connection.execute(
        "SELECT status, new_count, agency_status FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row[0:2] == ("partial_failure", 2)
    assert '"SH": "failed"' in row[2]


def test_compact_removes_only_runs_older_than_retention(tmp_path) -> None:
    database_path = tmp_path / "announcements.db"
    repository = AnnouncementRepository(database_path)
    now = datetime(2026, 7, 20, 3, tzinfo=UTC)
    old_run = repository.start_run(now - timedelta(days=91))
    recent_run = repository.start_run(now - timedelta(days=89))
    item = notice()
    repository.upsert_seen([item], observed_at=now - timedelta(days=120))
    repository.mark_delivered(item, "telegram-default", 123, delivered_at=now)

    repository.compact(now=now)

    run_ids = [row[0] for row in repository.connection.execute("SELECT id FROM runs")]
    assert run_ids == [recent_run]
    assert old_run not in run_ids
    assert repository.connection.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 1
    assert repository.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
    assert repository.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_close_can_compact_and_leave_reopenable_database(tmp_path) -> None:
    database_path = tmp_path / "announcements.db"
    repository = AnnouncementRepository(database_path)
    now = datetime(2026, 7, 20, 3, tzinfo=UTC)
    repository.start_run(now - timedelta(days=120))

    repository.close(compact=True, now=now)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
