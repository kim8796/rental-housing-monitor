from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from rental_monitor.collectors.base import Collector, CollectorError
from rental_monitor.models import Agency, Announcement, canonical_key
from rental_monitor.repository import AnnouncementRepository
from rental_monitor.telegram import format_announcement


class TelegramSender(Protocol):
    def send(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class RunResult:
    status: str
    new_count: int
    agency_status: dict[str, str]


class MonitorRunner:
    def __init__(
        self,
        collectors: Sequence[Collector],
        repository: AnnouncementRepository,
        telegram: TelegramSender,
        *,
        chat_id: str,
    ) -> None:
        self.collectors = collectors
        self.repository = repository
        self.telegram = telegram
        self.chat_id = chat_id

    def run(self) -> RunResult:
        run_id = self.repository.start_run()
        all_notices: dict[str, Announcement] = {}
        failures: list[tuple[Agency, str, str]] = []
        agency_status: dict[str, str] = {}
        for collector in self.collectors:
            try:
                for announcement in collector.collect():
                    all_notices[canonical_key(announcement)] = announcement
                agency_status[collector.agency.value] = "ok"
            except CollectorError as error:
                agency_status[collector.agency.value] = "failed"
                failures.append((collector.agency, error.stage, error.detail))
            except Exception as error:  # institution isolation requires a final boundary
                agency_status[collector.agency.value] = "failed"
                failures.append((collector.agency, "수집", f"{type(error).__name__}: {error}"))

        notices = list(all_notices.values())
        self.repository.upsert_seen(notices)
        pending = self.repository.pending_for_chat(notices, self.chat_id)
        delivered_count = 0
        try:
            for announcement in pending:
                message_id = self.telegram.send(format_announcement(announcement))
                self.repository.mark_delivered(announcement, self.chat_id, message_id)
                delivered_count += 1

            if failures:
                self.telegram.send(_format_failures(failures, agency_status))
                status = "partial_failure"
            else:
                status = "success"
                if delivered_count == 0:
                    self.telegram.send("오늘은 신규 공고가 없습니다.")
        except Exception:
            self.repository.finish_run(
                run_id,
                status="telegram_failure",
                new_count=delivered_count,
                agency_status=agency_status,
            )
            raise

        self.repository.finish_run(
            run_id,
            status=status,
            new_count=delivered_count,
            agency_status=agency_status,
        )
        return RunResult(status=status, new_count=delivered_count, agency_status=agency_status)


def _format_failures(
    failures: list[tuple[Agency, str, str]], agency_status: dict[str, str]
) -> str:
    blocks = ["⚠️ 임대주택 모니터 수집 오류"]
    for agency, stage, detail in failures:
        blocks.extend((f"기관: {agency.value}", f"단계: {stage}", f"원인: {detail}"))
    healthy = [name for name, status in agency_status.items() if status == "ok"]
    blocks.append(f"정상 처리: {', '.join(healthy) if healthy else '없음'}")
    return "\n".join(blocks)
