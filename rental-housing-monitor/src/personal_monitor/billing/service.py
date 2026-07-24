from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final, Literal
from zoneinfo import ZoneInfo

from personal_monitor.observability import read_backup_status

from .models import BillingSnapshot
from .repository import BillingRepository

BillingRunKind = Literal["sync", "summary"]
_SEOUL: Final = ZoneInfo("Asia/Seoul")


def next_billing_run(
    after: datetime,
    timezone: ZoneInfo,
) -> tuple[datetime, BillingRunKind]:
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("after must be timezone-aware")
    local = after.astimezone(timezone)
    candidates: list[tuple[datetime, BillingRunKind]] = []
    for day_offset in range(0, 370):
        day = local.date() + timedelta(days=day_offset)
        for hour, minute, kind in ((12, 10, "sync"), (12, 20, "summary")):
            candidate = datetime.combine(day, time(hour, minute), tzinfo=timezone)
            utc_candidate = candidate.astimezone(UTC)
            round_trip = utc_candidate.astimezone(timezone)
            if (
                round_trip.date() == day
                and round_trip.hour == hour
                and round_trip.minute == minute
                and utc_candidate > after.astimezone(UTC)
            ):
                candidates.append((utc_candidate, kind))
        if candidates:
            return min(candidates, key=lambda item: item[0])
    raise RuntimeError("unable to calculate billing schedule")


class BillingCoordinator:
    def __init__(
        self,
        repository: BillingRepository,
        source: object,
        sender: object,
        *,
        backup_status_path: Path,
        clock: object = lambda: datetime.now(UTC),
    ) -> None:
        fetch = getattr(source, "fetch", None)
        send = getattr(sender, "send", None)
        if (
            type(repository) is not BillingRepository
            or not callable(fetch)
            or not callable(send)
            or not callable(clock)
            or not Path(backup_status_path).is_absolute()
        ):
            raise ValueError("invalid billing coordinator")
        self._repository = repository
        self._source = source
        self._fetch = fetch
        self._sender = sender
        self._send = send
        self._backup_status_path = Path(backup_status_path)
        self._clock = clock

    def __repr__(self) -> str:
        return "<BillingCoordinator redacted>"

    async def sync(self, *, now: datetime) -> None:
        observed_at = _aware_utc(now)
        for grant in self._repository.list_grants():
            aggregate = await self._fetch(start_on=grant.starts_on, now=observed_at)
            snapshot = self._repository.record_aggregate(grant.id, aggregate)
            for key, text in self._alert_messages(snapshot, now=observed_at):
                if self._repository.claim_alert(grant.id, key, now=observed_at):
                    await self._send("billing", {"text": text})

    async def send_summary(self, *, now: datetime) -> None:
        observed_at = _aware_utc(now)
        for grant in self._repository.list_grants():
            snapshot = self._repository.latest_snapshot(grant.id)
            if snapshot is not None:
                await self._send(
                    "billing",
                    {"text": render_billing_status(snapshot, now=observed_at)},
                )

    def render_status(self) -> str:
        now = _aware_utc(self._clock())
        grants = self._repository.list_grants()
        if not grants:
            return "GCP 크레딧 기준값이 아직 등록되지 않았습니다."
        blocks = []
        for grant in grants:
            snapshot = self._repository.latest_snapshot(grant.id)
            if snapshot is not None:
                blocks.append(render_billing_status(snapshot, now=now))
        return "\n\n".join(blocks) if blocks else "GCP 크레딧 스냅샷이 없습니다."

    def _alert_messages(
        self,
        snapshot: BillingSnapshot,
        *,
        now: datetime,
    ) -> tuple[tuple[str, str], ...]:
        messages: list[tuple[str, str]] = []
        for threshold in (30, 10, 5):
            if snapshot.remaining_basis_points <= threshold * 100:
                lines = [
                    f"⚠️ GCP 크레딧 잔액 {threshold}% 이하",
                    _balance_line(snapshot),
                ]
                if threshold <= 10:
                    lines.append("서버 이전 준비가 필요합니다.")
                    lines.extend(self._migration_readiness())
                messages.append((f"remaining:{threshold}", "\n".join(lines)))

        local_day = now.astimezone(_SEOUL).date()
        days_to_expiry = (snapshot.ends_on - local_day).days
        for threshold in (30, 7, 1):
            if 0 <= days_to_expiry <= threshold:
                messages.append(
                    (
                        f"expiry:{threshold}",
                        "⚠️ GCP 무료 크레딧 "
                        f"만료까지 {days_to_expiry}일 남았습니다. "
                        f"종료일은 {snapshot.ends_on.isoformat()}입니다.",
                    )
                )
        projected = snapshot.projected_exhaustion_on
        if projected is not None and 0 <= (projected - local_day).days <= 30:
            messages.append(
                (
                    "runway:30",
                    "⚠️ 최근 사용 속도라면 GCP 크레딧이 "
                    f"30일 안에 소진될 전망입니다. 예상일: {projected.isoformat()}",
                )
            )
        return tuple(messages)

    def _migration_readiness(self) -> tuple[str, ...]:
        backup = read_backup_status(self._backup_status_path)
        if backup.healthy and backup.updated_at is not None:
            backup_line = (
                "✅ 최신 암호화 백업 검증: "
                f"{_kst_time(backup.updated_at)}"
            )
        else:
            backup_line = "❌ 최신 암호화 백업 검증 상태를 확인해야 합니다."
        return (
            "이전 준비 상태:",
            "✅ 앱 컨테이너 실행 중",
            "✅ SQLite 읽기 정상",
            backup_line,
            "재연결 항목: .env · Codex 로그인 · VM IAM · BigQuery · Telegram",
            "사용자 승인 전 자동 이전하지 않습니다.",
        )


def render_billing_status(snapshot: BillingSnapshot, *, now: datetime) -> str:
    observed_at = _aware_utc(now)
    local_day = observed_at.astimezone(_SEOUL).date()
    expiry_days = (snapshot.ends_on - local_day).days
    expiry_label = (
        f"{expiry_days}일 남음" if expiry_days >= 0 else f"{abs(expiry_days)}일 지남"
    )
    source = "BigQuery" if snapshot.source == "bigquery" else "콘솔"
    lines = [
        "☁️ GCP 크레딧",
        _balance_line(snapshot),
        f"사용: {_won(snapshot.used_micros)}",
        f"만료: {snapshot.ends_on.isoformat()} ({expiry_label})",
    ]
    if snapshot.projected_exhaustion_on is None:
        lines.append("예상 소진: 최근 7일 사용 데이터 부족")
    else:
        lines.append(f"예상 소진: {snapshot.projected_exhaustion_on.isoformat()}")
    lines.append(f"기준: {_kst_time(snapshot.observed_at)} ({source})")
    if snapshot.projects:
        lines.append("이번 달 프로젝트:")
        lines.extend(
            f"- {project.project_name}: {_won(project.cost_micros)}"
            for project in snapshot.projects[:20]
        )
    return "\n".join(lines)


def _balance_line(snapshot: BillingSnapshot) -> str:
    percent = Decimal(snapshot.remaining_basis_points) / Decimal(100)
    return (
        f"잔액: {_won(snapshot.remaining_micros)} / {_won(snapshot.original_micros)} "
        f"({percent:.2f}%)"
    )


def _won(micros: int) -> str:
    value = (Decimal(micros) / Decimal(1_000_000)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return f"₩{value:,.2f}"


def _kst_time(value: datetime) -> str:
    return value.astimezone(_SEOUL).strftime("%Y-%m-%d %H:%M KST")


def _aware_utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("billing time must be timezone-aware")
    return value.astimezone(UTC)
