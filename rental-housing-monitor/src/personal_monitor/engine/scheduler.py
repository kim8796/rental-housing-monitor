from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.storage.runtime import RuntimeRepository


def stable_jitter_seconds(monitor_id: str) -> int:
    """Return a stable, per-monitor delay between zero and two minutes."""
    digest = hashlib.sha256(monitor_id.encode()).digest()
    return int.from_bytes(digest[:2], "big") % 121


def next_run_at(spec: MonitorSpec, monitor_id: str, after: datetime) -> datetime:
    """Calculate the next scheduled run in UTC for an aware instant."""
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("after must be timezone-aware")

    zone = ZoneInfo(spec.timezone)
    scheduled = croniter(spec.schedule, after.astimezone(zone)).get_next(datetime)
    is_rental_exact = (
        monitor_id == "rental-housing-seoul-gyeonggi" and spec.schedule == "13 12 * * *"
    )
    jitter = 0 if is_rental_exact else stable_jitter_seconds(monitor_id)
    return (scheduled + timedelta(seconds=jitter)).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Scheduler:
    """Claim due monitors for a worker without executing their workflows."""

    runtime: RuntimeRepository
    worker_id: str

    def tick(self, now: datetime) -> list[str]:
        return self.runtime.claim_due(worker_id=self.worker_id, now=now)
