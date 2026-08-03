from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from personal_monitor.billing import (
    BillingAggregate,
    BillingRepository,
    CreditGrant,
    ProjectSpend,
)
from personal_monitor.billing.service import BillingCoordinator, next_billing_run
from personal_monitor.storage import open_database

NOW = datetime(2026, 7, 24, 3, 10, tzinfo=UTC)
SEOUL = ZoneInfo("Asia/Seoul")


class Source:
    def __init__(self, aggregate: BillingAggregate) -> None:
        self.aggregate = aggregate
        self.calls: list[tuple[date, datetime, datetime]] = []

    async def fetch(
        self,
        *,
        start_on: date,
        baseline_as_of: datetime,
        now: datetime,
    ) -> BillingAggregate:
        self.calls.append((start_on, baseline_as_of, now))
        return self.aggregate


class Sender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, address: str, payload: dict[str, object]) -> str:
        assert address == "billing"
        self.messages.append(str(payload["text"]))
        return f"message-{len(self.messages)}"


def _grant(*, remaining: int = 455_463_260_000, ends_on: date | None = None) -> CreditGrant:
    return CreditGrant(
        id="free-trial",
        name="Free Trial",
        original_micros=460_418_000_000,
        baseline_remaining_micros=remaining,
        starts_on=date(2026, 7, 8),
        ends_on=ends_on or date(2026, 10, 8),
        baseline_as_of=NOW,
    )


def _aggregate(*, recent: int = 0) -> BillingAggregate:
    return BillingAggregate(
        observed_at=NOW,
        promotion_consumed_micros=4_954_740_000,
        baseline_promotion_consumed_micros=4_954_740_000,
        recent_7d_consumed_micros=recent,
        projects=(ProjectSpend("project-a", "주 프로젝트", 4_500_125_000),),
    )


def _backup(path: Path) -> None:
    path.write_text(
        '{"schema_version":1,"status":"ok","updated_at":"2026-07-24T02:00:00Z"}\n',
        encoding="utf-8",
    )


def test_next_billing_run_selects_1210_sync_then_1220_summary() -> None:
    sync_at, sync_kind = next_billing_run(
        datetime(2026, 7, 24, 3, 9, 59, tzinfo=UTC),
        SEOUL,
    )
    summary_at, summary_kind = next_billing_run(sync_at, SEOUL)
    tomorrow, tomorrow_kind = next_billing_run(
        datetime(2026, 7, 24, 3, 20, tzinfo=UTC),
        SEOUL,
    )

    assert (sync_at, sync_kind) == (datetime(2026, 7, 24, 3, 10, tzinfo=UTC), "sync")
    assert (summary_at, summary_kind) == (
        datetime(2026, 7, 24, 3, 20, tzinfo=UTC),
        "summary",
    )
    assert (tomorrow, tomorrow_kind) == (
        datetime(2026, 7, 25, 3, 10, tzinfo=UTC),
        "sync",
    )


def test_status_and_daily_summary_show_credit_project_and_snapshot_time(tmp_path: Path) -> None:
    repository = BillingRepository(open_database(":memory:"))
    repository.register_grant(_grant())
    sender = Sender()
    backup = tmp_path / "backup-status.json"
    _backup(backup)
    coordinator = BillingCoordinator(
        repository,
        Source(_aggregate()),
        sender,
        backup_status_path=backup,
        clock=lambda: NOW,
    )

    status = coordinator.render_status()
    asyncio.run(coordinator.sync(now=NOW))
    asyncio.run(coordinator.send_summary(now=NOW))

    assert "GCP 크레딧" in status
    assert "₩455,463.26 / ₩460,418.00 (98.92%)" in status
    assert "기준: 2026-07-24 12:10 KST (콘솔)" in status
    assert len(sender.messages) == 1
    assert "주 프로젝트: ₩4,500.13" in sender.messages[0]
    assert "BigQuery" in sender.messages[0]


def test_daily_summary_warns_when_latest_usage_sync_is_over_24_hours_old(
    tmp_path: Path,
) -> None:
    repository = BillingRepository(open_database(":memory:"))
    repository.register_grant(_grant())
    sender = Sender()
    backup = tmp_path / "backup-status.json"
    _backup(backup)
    coordinator = BillingCoordinator(
        repository,
        Source(_aggregate()),
        sender,
        backup_status_path=backup,
        clock=lambda: NOW,
    )

    asyncio.run(coordinator.send_summary(now=NOW + timedelta(days=1, minutes=1)))

    assert len(sender.messages) == 1
    assert "⚠️ 사용량 동기화가 24시간 이상 지연되었습니다." in sender.messages[0]
    assert "기준: 2026-07-24 12:10 KST (콘솔)" in sender.messages[0]


def test_ten_percent_alert_includes_migration_readiness_and_is_not_duplicated(
    tmp_path: Path,
) -> None:
    repository = BillingRepository(open_database(":memory:"))
    repository.register_grant(_grant(remaining=40_000_000_000))
    sender = Sender()
    source = Source(_aggregate())
    backup = tmp_path / "backup-status.json"
    _backup(backup)
    coordinator = BillingCoordinator(
        repository,
        source,
        sender,
        backup_status_path=backup,
        clock=lambda: NOW,
    )

    asyncio.run(coordinator.sync(now=NOW))
    first_count = len(sender.messages)
    asyncio.run(coordinator.sync(now=NOW))

    assert first_count == 2
    assert len(sender.messages) == first_count
    assert any("30% 이하" in message for message in sender.messages)
    migration = next(message for message in sender.messages if "10% 이하" in message)
    assert "서버 이전 준비가 필요합니다" in migration
    assert "앱 컨테이너 실행 중" in migration
    assert "SQLite 읽기 정상" in migration
    assert "최신 암호화 백업 검증: 2026-07-24 11:00 KST" in migration
    assert ".env · Codex 로그인 · VM IAM · BigQuery · Telegram" in migration
    assert "사용자 승인 전 자동 이전하지 않습니다" in migration


def test_expiry_and_projected_exhaustion_alerts_are_deduplicated(tmp_path: Path) -> None:
    repository = BillingRepository(open_database(":memory:"))
    repository.register_grant(_grant(ends_on=date(2026, 8, 20)))
    sender = Sender()
    backup = tmp_path / "missing.json"
    coordinator = BillingCoordinator(
        repository,
        Source(_aggregate(recent=140_000_000_000)),
        sender,
        backup_status_path=backup,
        clock=lambda: NOW,
    )

    asyncio.run(coordinator.sync(now=NOW))
    asyncio.run(coordinator.sync(now=NOW))

    assert sum("만료까지 27일" in message for message in sender.messages) == 1
    assert sum("30일 안에 소진될 전망" in message for message in sender.messages) == 1
