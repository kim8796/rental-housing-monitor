from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from personal_monitor.storage.schema import parse_timestamp, transaction, utc_timestamp

from .models import BillingAggregate, BillingSnapshot, CreditGrant, ProjectSpend

_SEOUL = ZoneInfo("Asia/Seoul")


class BillingRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("invalid billing repository connection")
        self.connection = connection

    def register_grant(self, grant: CreditGrant) -> BillingSnapshot:
        if type(grant) is not CreditGrant:
            raise TypeError("invalid billing credit grant")
        baseline_at = utc_timestamp(grant.baseline_as_of, parameter="baseline_as_of")
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT name, original_micros, baseline_remaining_micros, starts_on, "
                "ends_on, baseline_as_of FROM billing_credit_grants WHERE id = ?",
                (grant.id,),
            ).fetchone()
            expected = (
                grant.name,
                grant.original_micros,
                grant.baseline_remaining_micros,
                grant.starts_on.isoformat(),
                grant.ends_on.isoformat(),
                baseline_at,
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise ValueError("billing credit grant already exists")
                snapshot = self.latest_snapshot(grant.id)
                if snapshot is None:
                    raise RuntimeError("billing credit snapshot is missing")
                return snapshot
            self.connection.execute(
                "INSERT INTO billing_credit_grants("
                "id, name, original_micros, baseline_remaining_micros, starts_on, ends_on, "
                "baseline_as_of, baseline_export_consumed_micros, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (grant.id, *expected, baseline_at, baseline_at),
            )
            self._insert_snapshot(
                grant_id=grant.id,
                observed_at=grant.baseline_as_of.astimezone(UTC),
                source="console",
                original_micros=grant.original_micros,
                remaining_micros=grant.baseline_remaining_micros,
                daily_burn_micros=0,
                projected_exhaustion_on=None,
                projects=(),
            )
        snapshot = self.latest_snapshot(grant.id)
        if snapshot is None:
            raise RuntimeError("billing credit snapshot is missing")
        return snapshot

    def list_grants(self) -> tuple[CreditGrant, ...]:
        rows = self.connection.execute(
            "SELECT id, name, original_micros, baseline_remaining_micros, "
            "starts_on, ends_on, baseline_as_of FROM billing_credit_grants ORDER BY id"
        ).fetchall()
        return tuple(
            CreditGrant(
                id=row["id"],
                name=row["name"],
                original_micros=row["original_micros"],
                baseline_remaining_micros=row["baseline_remaining_micros"],
                starts_on=date.fromisoformat(row["starts_on"]),
                ends_on=date.fromisoformat(row["ends_on"]),
                baseline_as_of=_required_timestamp(row["baseline_as_of"]),
            )
            for row in rows
        )

    def record_aggregate(
        self,
        grant_id: str,
        aggregate: BillingAggregate,
    ) -> BillingSnapshot:
        if type(grant_id) is not str or type(aggregate) is not BillingAggregate:
            raise TypeError("invalid billing aggregate")
        observed_at = aggregate.observed_at.astimezone(UTC)
        observed_text = utc_timestamp(observed_at, parameter="observed_at")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT original_micros, baseline_remaining_micros, ends_on "
                "FROM billing_credit_grants WHERE id = ?",
                (grant_id,),
            ).fetchone()
            if row is None:
                raise ValueError("billing credit grant is unavailable")
            baseline_export = aggregate.baseline_promotion_consumed_micros
            self.connection.execute(
                "UPDATE billing_credit_grants SET "
                "baseline_export_consumed_micros = ?, updated_at = ? WHERE id = ?",
                (baseline_export, observed_text, grant_id),
            )
            delta = aggregate.promotion_consumed_micros - baseline_export
            remaining = max(
                0,
                min(
                    row["original_micros"],
                    row["baseline_remaining_micros"] - delta,
                ),
            )
            daily_burn = aggregate.recent_7d_consumed_micros // 7
            projected = None
            if daily_burn > 0 and remaining > 0:
                days = (remaining + daily_burn - 1) // daily_burn
                projected = observed_at.astimezone(_SEOUL).date() + timedelta(days=days)
            self._insert_snapshot(
                grant_id=grant_id,
                observed_at=observed_at,
                source="bigquery",
                original_micros=row["original_micros"],
                remaining_micros=remaining,
                daily_burn_micros=daily_burn,
                projected_exhaustion_on=projected,
                projects=tuple(
                    sorted(
                        aggregate.projects,
                        key=lambda item: (-item.cost_micros, item.project_id),
                    )
                ),
            )
        snapshot = self.latest_snapshot(grant_id)
        if snapshot is None:
            raise RuntimeError("billing credit snapshot is missing")
        return snapshot

    def latest_snapshot(self, grant_id: str) -> BillingSnapshot | None:
        row = self.connection.execute(
            "SELECT id, grant_id, observed_at, source, original_micros, remaining_micros, "
            "daily_burn_micros, projected_exhaustion_on "
            "FROM billing_snapshots WHERE grant_id = ? "
            "ORDER BY observed_at DESC, rowid DESC LIMIT 1",
            (grant_id,),
        ).fetchone()
        if row is None:
            return None
        grant = self.connection.execute(
            "SELECT ends_on FROM billing_credit_grants WHERE id = ?",
            (grant_id,),
        ).fetchone()
        if grant is None:
            raise RuntimeError("billing credit grant is unavailable")
        projects = self.connection.execute(
            "SELECT project_id, project_name, cost_micros "
            "FROM billing_project_spend WHERE snapshot_id = ? "
            "ORDER BY cost_micros DESC, project_id",
            (row["id"],),
        ).fetchall()
        original = row["original_micros"]
        remaining = row["remaining_micros"]
        return BillingSnapshot(
            grant_id=row["grant_id"],
            observed_at=_required_timestamp(row["observed_at"]),
            source=row["source"],
            original_micros=original,
            remaining_micros=remaining,
            used_micros=original - remaining,
            remaining_basis_points=_basis_points(remaining, original),
            daily_burn_micros=row["daily_burn_micros"],
            projected_exhaustion_on=(
                date.fromisoformat(row["projected_exhaustion_on"])
                if row["projected_exhaustion_on"] is not None
                else None
            ),
            ends_on=date.fromisoformat(grant["ends_on"]),
            projects=tuple(
                ProjectSpend(
                    project_id=item["project_id"],
                    project_name=item["project_name"],
                    cost_micros=item["cost_micros"],
                )
                for item in projects
            ),
        )

    def claim_alert(self, grant_id: str, alert_key: str, *, now: datetime) -> bool:
        if (
            type(grant_id) is not str
            or type(alert_key) is not str
            or not 1 <= len(alert_key) <= 100
        ):
            raise ValueError("invalid billing alert")
        sent_at = utc_timestamp(now, parameter="now")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO billing_alerts(grant_id, alert_key, sent_at) "
                "VALUES (?, ?, ?) ON CONFLICT(grant_id, alert_key) DO NOTHING",
                (grant_id, alert_key, sent_at),
            )
            return cursor.rowcount == 1

    def _insert_snapshot(
        self,
        *,
        grant_id: str,
        observed_at: datetime,
        source: str,
        original_micros: int,
        remaining_micros: int,
        daily_burn_micros: int,
        projected_exhaustion_on: date | None,
        projects: tuple[ProjectSpend, ...],
    ) -> None:
        snapshot_id = uuid4().hex
        observed_text = utc_timestamp(observed_at, parameter="observed_at")
        self.connection.execute(
            "INSERT INTO billing_snapshots("
            "id, grant_id, observed_at, source, original_micros, remaining_micros, "
            "daily_burn_micros, projected_exhaustion_on, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                grant_id,
                observed_text,
                source,
                original_micros,
                remaining_micros,
                daily_burn_micros,
                projected_exhaustion_on.isoformat()
                if projected_exhaustion_on is not None
                else None,
                observed_text,
            ),
        )
        self.connection.executemany(
            "INSERT INTO billing_project_spend("
            "snapshot_id, project_id, project_name, cost_micros"
            ") VALUES (?, ?, ?, ?)",
            (
                (snapshot_id, item.project_id, item.project_name, item.cost_micros)
                for item in projects
            ),
        )


def _basis_points(remaining: int, original: int) -> int:
    return min(10_000, max(0, (remaining * 10_000 + original // 2) // original))


def _required_timestamp(value: str) -> datetime:
    parsed = parse_timestamp(value)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("billing timestamp is invalid")
    return parsed.astimezone(UTC)
