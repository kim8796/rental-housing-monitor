from datetime import date

import pytest

from rental_monitor.collectors.base import ParserStructureError
from rental_monitor.models import Agency, Announcement, HousingType
from rental_monitor.repository import AnnouncementRepository
from rental_monitor.runner import MonitorRunner


def notice() -> Announcement:
    return Announcement(
        source_id="801",
        title="다산 국민임대주택 예비입주자 모집공고",
        agency=Agency.GH,
        region="경기도",
        housing_type=HousingType.NATIONAL,
        target="국민임대 입주자격 충족자",
        announcement_date=date(2026, 7, 10),
        application_start_date=None,
        application_end_date=date(2026, 7, 20),
        url="https://apply.gh.or.kr/official/801",
    )


class FakeCollector:
    def __init__(self, agency: Agency, results: list[Announcement] | Exception) -> None:
        self.agency = agency
        self.results = results

    def collect(self) -> list[Announcement]:
        if isinstance(self.results, Exception):
            raise self.results
        return self.results


class FakeTelegram:
    def __init__(self, error: Exception | None = None) -> None:
        self.messages: list[str] = []
        self.error = error

    def send(self, text: str) -> int:
        self.messages.append(text)
        if self.error:
            raise self.error
        return len(self.messages)


def collectors(results: list[Announcement] | None = None) -> list[FakeCollector]:
    return [
        FakeCollector(Agency.LH, results or []),
        FakeCollector(Agency.SH, []),
        FakeCollector(Agency.GH, []),
    ]


def test_runner_marks_delivery_only_after_telegram_success(tmp_path) -> None:
    repository = AnnouncementRepository(tmp_path / "db.sqlite")
    telegram = FakeTelegram()
    runner = MonitorRunner(collectors([notice()]), repository, telegram, chat_id="42")

    result = runner.run()

    assert result.new_count == 1
    assert repository.pending_for_chat([notice()], "42") == []


def test_failed_telegram_send_leaves_notice_pending(tmp_path) -> None:
    repository = AnnouncementRepository(tmp_path / "db.sqlite")
    runner = MonitorRunner(
        collectors([notice()]), repository, FakeTelegram(RuntimeError("offline")), chat_id="42"
    )

    with pytest.raises(RuntimeError, match="offline"):
        runner.run()

    assert repository.pending_for_chat([notice()], "42") == [notice()]


def test_no_new_notice_is_sent_when_all_collectors_succeed(tmp_path) -> None:
    telegram = FakeTelegram()
    runner = MonitorRunner(
        collectors(), AnnouncementRepository(tmp_path / "db.sqlite"), telegram, chat_id="42"
    )

    runner.run()

    assert telegram.messages == ["오늘은 신규 공고가 없습니다."]


def test_partial_failure_names_agency_instead_of_claiming_no_new(tmp_path) -> None:
    telegram = FakeTelegram()
    failing = [
        FakeCollector(Agency.LH, []),
        FakeCollector(
            Agency.SH,
            ParserStructureError(Agency.SH, "목록 파싱", "공고 행 선택자를 찾지 못했습니다"),
        ),
        FakeCollector(Agency.GH, []),
    ]
    runner = MonitorRunner(
        failing, AnnouncementRepository(tmp_path / "db.sqlite"), telegram, chat_id="42"
    )

    result = runner.run()

    assert result.status == "partial_failure"
    assert "기관: SH" in telegram.messages[0]
    assert "오늘은 신규 공고가 없습니다" not in telegram.messages[0]
    assert "정상 처리: LH, GH" in telegram.messages[0]


def test_second_run_does_not_send_same_notice_again(tmp_path) -> None:
    repository = AnnouncementRepository(tmp_path / "db.sqlite")
    telegram = FakeTelegram()
    first = MonitorRunner(collectors([notice()]), repository, telegram, chat_id="42")
    second = MonitorRunner(collectors([notice()]), repository, telegram, chat_id="42")

    first.run()
    second.run()

    assert len([message for message in telegram.messages if "공고 제목:" in message]) == 1
    assert telegram.messages[-1] == "오늘은 신규 공고가 없습니다."
