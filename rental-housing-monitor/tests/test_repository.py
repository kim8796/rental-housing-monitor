from datetime import UTC, date, datetime

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

    repository.finish_run(run_id, status="partial_failure", new_count=2, agency_status={"SH": "failed"})

    row = repository.connection.execute(
        "SELECT status, new_count, agency_status FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row[0:2] == ("partial_failure", 2)
    assert '"SH": "failed"' in row[2]
