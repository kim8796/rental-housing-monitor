from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from personal_monitor.domain.spec import MonitorSpec, MonitorStatus
from personal_monitor.storage.schema import (
    canonical_json,
    parse_timestamp,
    transaction,
    utc_now,
    utc_timestamp,
)


@dataclass(frozen=True, slots=True)
class ActiveMonitor:
    id: str
    owner_id: str
    version_id: str
    spec: MonitorSpec


@dataclass(frozen=True, slots=True)
class DeliveryTargetRow:
    id: str
    owner_id: str
    kind: str
    address: str


@dataclass(frozen=True, slots=True)
class MonitorRow:
    id: str
    owner_id: str
    name: str
    status: MonitorStatus
    next_run_at: datetime | None


class RegistryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_user(self, user_id: str, telegram_user_id: int) -> None:
        with transaction(self.connection):
            self.connection.execute(
                "INSERT INTO users(id, telegram_user_id, status, created_at) "
                "VALUES (?, ?, 'active', ?)",
                (user_id, telegram_user_id, utc_now().isoformat()),
            )

    def create_delivery_target(self, target_id: str, owner_id: str, address: str) -> None:
        with transaction(self.connection):
            self.connection.execute(
                "INSERT INTO delivery_targets(id, owner_id, kind, address, created_at) "
                "VALUES (?, ?, 'telegram', ?, ?)",
                (target_id, owner_id, address, utc_now().isoformat()),
            )

    def create_monitor(self, spec: MonitorSpec, *, created_by: str) -> str:
        if created_by != spec.owner_id:
            raise ValueError("initial monitor approver must be the monitor owner")
        monitor_id = uuid4().hex
        version_id = uuid4().hex
        created_at = utc_now().isoformat()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO monitors(id, owner_id, name, status, active_version_id, "
                "next_run_at, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    monitor_id,
                    spec.owner_id,
                    spec.name,
                    MonitorStatus.ACTIVE.value,
                    created_at,
                    created_at,
                    created_at,
                ),
            )
            self.connection.execute(
                "INSERT INTO monitor_versions(id, monitor_id, version_number, spec_json, "
                "created_by, created_at, approved_by, approved_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    monitor_id,
                    canonical_json(spec.model_dump(mode="json")),
                    created_by,
                    created_at,
                    created_by,
                    created_at,
                ),
            )
            self.connection.execute(
                "UPDATE monitors SET active_version_id = ? WHERE id = ?",
                (version_id, monitor_id),
            )
        return monitor_id

    def add_version(
        self,
        monitor_id: str,
        spec: MonitorSpec,
        *,
        created_by: str,
        approved: bool,
    ) -> str:
        version_id = uuid4().hex
        created_at = utc_now().isoformat()
        with transaction(self.connection, immediate=True):
            monitor = self.connection.execute(
                "SELECT owner_id FROM monitors WHERE id = ?", (monitor_id,)
            ).fetchone()
            if monitor is None:
                raise ValueError("monitor does not exist")
            if monitor["owner_id"] != spec.owner_id:
                raise ValueError("monitor version owner must match monitor owner")
            if approved and created_by != monitor["owner_id"]:
                raise ValueError("approved monitor version must be created by the owner")
            next_version = self.connection.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM monitor_versions "
                "WHERE monitor_id = ?",
                (monitor_id,),
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO monitor_versions(id, monitor_id, version_number, spec_json, "
                "created_by, created_at, approved_by, approved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    monitor_id,
                    next_version,
                    canonical_json(spec.model_dump(mode="json")),
                    created_by,
                    created_at,
                    created_by if approved else None,
                    created_at if approved else None,
                ),
            )
        return version_id

    def approve_version(self, version_id: str, *, approved_by: str) -> None:
        with transaction(self.connection):
            version = self.connection.execute(
                "SELECT m.owner_id, v.approved_at FROM monitor_versions AS v "
                "JOIN monitors AS m ON m.id = v.monitor_id WHERE v.id = ?",
                (version_id,),
            ).fetchone()
            if version is None:
                raise ValueError("monitor version does not exist")
            if version["owner_id"] != approved_by:
                raise ValueError("only the monitor owner may approve a version")
            if version["approved_at"] is not None:
                return
            cursor = self.connection.execute(
                "UPDATE monitor_versions SET approved_by = ?, approved_at = ? "
                "WHERE id = ? AND approved_at IS NULL",
                (approved_by, utc_now().isoformat(), version_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("monitor version approval changed concurrently")

    def activate_version(self, monitor_id: str, version_id: str, *, owner_id: str) -> None:
        with transaction(self.connection, immediate=True):
            version = self.connection.execute(
                "SELECT v.monitor_id, v.approved_at, m.owner_id FROM monitor_versions AS v "
                "JOIN monitors AS m ON m.id = v.monitor_id WHERE v.id = ?",
                (version_id,),
            ).fetchone()
            if version is None or version["monitor_id"] != monitor_id:
                raise ValueError("version does not belong to monitor")
            if version["owner_id"] != owner_id:
                raise ValueError("only the monitor owner may activate a version")
            if version["approved_at"] is None:
                raise ValueError("version must be approved before activation")
            cursor = self.connection.execute(
                "UPDATE monitors SET active_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, utc_now().isoformat(), monitor_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("monitor does not exist")

    def get_active_spec(self, monitor_id: str) -> MonitorSpec:
        return self.get_active_monitor(monitor_id).spec

    def get_active_monitor(self, monitor_id: str) -> ActiveMonitor:
        row = self.connection.execute(
            "SELECT m.id, m.owner_id, v.id AS version_id, v.spec_json "
            "FROM monitors AS m JOIN monitor_versions AS v ON v.id = m.active_version_id "
            "WHERE m.id = ?",
            (monitor_id,),
        ).fetchone()
        if row is None:
            raise ValueError("monitor has no active version")
        return ActiveMonitor(
            id=row["id"],
            owner_id=row["owner_id"],
            version_id=row["version_id"],
            spec=MonitorSpec.model_validate_json(row["spec_json"]),
        )

    def get_active_monitor_for_recovery(self, monitor_id: str, *, owner_id: str) -> ActiveMonitor:
        row = self.connection.execute(
            "SELECT m.id, m.owner_id, v.id AS version_id, v.spec_json "
            "FROM monitors AS m JOIN monitor_versions AS v ON v.id = m.active_version_id "
            "WHERE m.id = ? AND m.owner_id = ? AND m.status = ?",
            (monitor_id, owner_id, MonitorStatus.ACTIVE.value),
        ).fetchone()
        if row is None:
            raise ValueError("monitor is not eligible for adaptive recovery")
        return ActiveMonitor(
            id=row["id"],
            owner_id=row["owner_id"],
            version_id=row["version_id"],
            spec=MonitorSpec.model_validate_json(row["spec_json"]),
        )

    def get_primary_target(self, owner_id: str) -> DeliveryTargetRow:
        row = self.connection.execute(
            "SELECT id, owner_id, kind, address FROM delivery_targets "
            "WHERE owner_id = ? ORDER BY created_at, id LIMIT 1",
            (owner_id,),
        ).fetchone()
        if row is None:
            raise ValueError("owner has no delivery target")
        return DeliveryTargetRow(
            id=row["id"], owner_id=row["owner_id"], kind=row["kind"], address=row["address"]
        )

    def get_delivery_target(self, target_id: str) -> DeliveryTargetRow:
        row = self.connection.execute(
            "SELECT id, owner_id, kind, address FROM delivery_targets WHERE id = ?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise ValueError("delivery target does not exist")
        return DeliveryTargetRow(
            id=row["id"], owner_id=row["owner_id"], kind=row["kind"], address=row["address"]
        )

    def list_monitors(self, owner_id: str, *, include_disabled: bool = False) -> list[MonitorRow]:
        where = "owner_id = ?" if include_disabled else "owner_id = ? AND status != ?"
        parameters: tuple[object, ...] = (
            (owner_id,) if include_disabled else (owner_id, MonitorStatus.DISABLED.value)
        )
        rows = self.connection.execute(
            f"SELECT id, owner_id, name, status, next_run_at FROM monitors WHERE {where} "
            "ORDER BY created_at, id",
            parameters,
        )
        return [
            MonitorRow(
                id=row["id"],
                owner_id=row["owner_id"],
                name=row["name"],
                status=MonitorStatus(row["status"]),
                next_run_at=parse_timestamp(row["next_run_at"]),
            )
            for row in rows
        ]

    def transition_status(
        self,
        monitor_id: str,
        expected: MonitorStatus,
        target: MonitorStatus,
        *,
        owner_id: str,
    ) -> None:
        with transaction(self.connection):
            cursor = self.connection.execute(
                "UPDATE monitors SET status = ?, updated_at = ? "
                "WHERE id = ? AND owner_id = ? AND status = ?",
                (target.value, utc_now().isoformat(), monitor_id, owner_id, expected.value),
            )
            if cursor.rowcount != 1:
                raise ValueError("monitor is not in the expected status")

    def soft_delete(self, monitor_id: str, *, owner_id: str, disabled_at: datetime) -> None:
        timestamp = utc_timestamp(disabled_at, parameter="disabled_at")
        with transaction(self.connection):
            cursor = self.connection.execute(
                "UPDATE monitors SET status = ?, disabled_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE id = ? AND owner_id = ?",
                (MonitorStatus.DISABLED.value, timestamp, timestamp, monitor_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("monitor does not exist for owner")
