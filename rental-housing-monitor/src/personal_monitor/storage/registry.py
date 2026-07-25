from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4

from personal_monitor.domain.spec import MonitorSpec, MonitorStatus
from personal_monitor.storage.schema import (
    canonical_json,
    parse_timestamp,
    transaction,
    utc_now,
    utc_timestamp,
)

_OWNER_RE = re.compile(r"telegram-user:[1-9][0-9]{0,18}\Z")


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


@dataclass(frozen=True, slots=True, repr=False)
class ControlMonitor:
    id: str
    owner_id: str
    name: str
    status: MonitorStatus
    active_version_id: str
    next_run_at: datetime | None
    last_success_at: datetime | None
    spec: MonitorSpec

    def __repr__(self) -> str:
        return "<ControlMonitor redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class CandidateVersion:
    id: str
    monitor_id: str
    owner_id: str
    expected_active_version_id: str
    created_by: str
    spec: MonitorSpec

    def __repr__(self) -> str:
        return "<CandidateVersion redacted>"


class RegistryRepository:
    __slots__ = ("_connection", "_connection_anchor")

    def __init__(self, connection: sqlite3.Connection) -> None:
        if type(connection) is not sqlite3.Connection:
            raise ValueError("invalid registry storage")
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_connection_anchor", connection)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("RegistryRepository composition is sealed")

    def __repr__(self) -> str:
        return "<RegistryRepository redacted>"

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection_anchor

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

    def get_url_alias(self, owner_id: str, name: str) -> str | None:
        normalized = _normalize_url_alias(owner_id, name)
        row = self.connection.execute(
            "SELECT url FROM url_aliases WHERE owner_id = ? AND normalized_name = ?",
            (owner_id, normalized),
        ).fetchone()
        if row is None:
            return None
        url = row["url"]
        if not _valid_alias_url(url):
            raise ValueError("invalid stored URL alias")
        return url

    def upsert_url_alias(self, owner_id: str, name: str, url: str) -> None:
        normalized = _normalize_url_alias(owner_id, name)
        if not _valid_alias_url(url):
            raise ValueError("invalid URL alias")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO url_aliases(owner_id, normalized_name, url, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(owner_id, normalized_name) DO UPDATE SET "
                "url = excluded.url, updated_at = excluded.updated_at",
                (owner_id, normalized, url, utc_now().isoformat()),
            )

    def create_monitor(
        self,
        spec: MonitorSpec,
        *,
        created_by: str,
        url_alias: str | None = None,
    ) -> str:
        if created_by != spec.owner_id:
            raise ValueError("initial monitor approver must be the monitor owner")
        normalized_alias = (
            None
            if url_alias is None
            else _normalize_url_alias(spec.owner_id, url_alias)
        )
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
            if normalized_alias is not None:
                self.connection.execute(
                    "INSERT INTO url_aliases(owner_id, normalized_name, url, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(owner_id, normalized_name) DO UPDATE SET "
                    "url = excluded.url, updated_at = excluded.updated_at",
                    (
                        spec.owner_id,
                        normalized_alias,
                        spec.target_url,
                        created_at,
                    ),
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
                "SELECT owner_id, active_version_id FROM monitors WHERE id = ?", (monitor_id,)
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
                "created_by, created_at, approved_by, approved_at, parent_version_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    monitor_id,
                    next_version,
                    canonical_json(spec.model_dump(mode="json")),
                    created_by,
                    created_at,
                    created_by if approved else None,
                    created_at if approved else None,
                    monitor["active_version_id"],
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
                "SELECT v.monitor_id, v.approved_at, v.spec_json, m.owner_id, m.status "
                "FROM monitor_versions AS v "
                "JOIN monitors AS m ON m.id = v.monitor_id WHERE v.id = ?",
                (version_id,),
            ).fetchone()
            if version is None or version["monitor_id"] != monitor_id:
                raise ValueError("version does not belong to monitor")
            if version["owner_id"] != owner_id:
                raise ValueError("only the monitor owner may activate a version")
            if version["approved_at"] is None:
                raise ValueError("version must be approved before activation")
            changed_at = utc_now()
            spec = MonitorSpec.model_validate_json(version["spec_json"])
            scheduled_at = (
                _next_run_at(spec, monitor_id, changed_at).isoformat()
                if version["status"] == MonitorStatus.ACTIVE.value
                else None
            )
            cursor = self.connection.execute(
                "UPDATE monitors SET active_version_id = ?, name = ?, next_run_at = ?, "
                "lease_owner = NULL, lease_expires_at = NULL, "
                "lease_generation = lease_generation + 1, updated_at = ? WHERE id = ?",
                (version_id, spec.name, scheduled_at, changed_at.isoformat(), monitor_id),
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

    def require_recovery_active(self, monitor_id: str, *, owner_id: str, version_id: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM monitors WHERE id = ? AND owner_id = ? AND status = ? "
            "AND active_version_id = ?",
            (monitor_id, owner_id, MonitorStatus.ACTIVE.value, version_id),
        ).fetchone()
        if row is None:
            raise ValueError("recovery precondition failed")

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

    def list_control_monitors(self, owner_id: str) -> tuple[ControlMonitor, ...]:
        rows = self.connection.execute(
            "SELECT id FROM monitors WHERE owner_id = ? AND status != ? ORDER BY created_at, id",
            (owner_id, MonitorStatus.DISABLED.value),
        ).fetchall()
        try:
            return tuple(self.get_control_monitor(row["id"], owner_id=owner_id) for row in rows)
        except Exception:
            raise ValueError("control monitor unavailable") from None

    def get_control_monitor(self, monitor_id: str, *, owner_id: str) -> ControlMonitor:
        try:
            row = self.connection.execute(
                "SELECT m.id, m.owner_id, m.name, m.status, m.active_version_id, "
                "m.next_run_at, v.spec_json, "
                "(SELECT r.finished_at FROM runs AS r WHERE r.monitor_id = m.id "
                "AND r.status = 'success' AND r.finished_at IS NOT NULL "
                "ORDER BY r.finished_at DESC, r.id DESC LIMIT 1) AS last_success_at "
                "FROM monitors AS m "
                "JOIN monitor_versions AS v ON v.id = m.active_version_id "
                "WHERE m.id = ? AND m.owner_id = ? AND m.status != ?",
                (monitor_id, owner_id, MonitorStatus.DISABLED.value),
            ).fetchone()
            if row is None:
                raise ValueError
            status = MonitorStatus(row["status"])
            next_run_at = _safe_optional_timestamp(row["next_run_at"])
            last_success_at = _safe_optional_timestamp(row["last_success_at"])
            spec = MonitorSpec.model_validate_json(row["spec_json"])
            if spec.owner_id != owner_id:
                raise ValueError
            return ControlMonitor(
                id=row["id"],
                owner_id=row["owner_id"],
                name=row["name"],
                status=status,
                active_version_id=row["active_version_id"],
                next_run_at=next_run_at,
                last_success_at=last_success_at,
                spec=spec,
            )
        except Exception:
            raise ValueError("control monitor unavailable") from None

    def transition_status_exact(
        self,
        monitor_id: str,
        *,
        owner_id: str,
        expected_status: MonitorStatus,
        expected_active_version_id: str,
        target_status: MonitorStatus,
        changed_at: datetime,
    ) -> None:
        timestamp = utc_timestamp(changed_at, parameter="changed_at")
        allowed = {
            (MonitorStatus.ACTIVE, MonitorStatus.PAUSED_USER),
            (MonitorStatus.PAUSED_USER, MonitorStatus.ACTIVE),
        }
        if (expected_status, target_status) not in allowed:
            raise ValueError("lifecycle precondition failed")
        with transaction(self.connection, immediate=True):
            scheduled_at: str | None = None
            if target_status is MonitorStatus.ACTIVE:
                row = self.connection.execute(
                    "SELECT v.spec_json FROM monitors AS m "
                    "JOIN monitor_versions AS v ON v.id = m.active_version_id "
                    "WHERE m.id = ? AND m.owner_id = ? AND m.status = ? "
                    "AND m.active_version_id = ?",
                    (
                        monitor_id,
                        owner_id,
                        expected_status.value,
                        expected_active_version_id,
                    ),
                ).fetchone()
                if row is None:
                    raise ValueError("lifecycle precondition failed")
                spec = MonitorSpec.model_validate_json(row["spec_json"])
                scheduled_at = _next_run_at(spec, monitor_id, changed_at).isoformat()
            cursor = self.connection.execute(
                "UPDATE monitors SET status = ?, next_run_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, lease_generation = lease_generation + 1, "
                "updated_at = ? "
                "WHERE id = ? AND owner_id = ? AND status = ? AND active_version_id = ?",
                (
                    target_status.value,
                    scheduled_at,
                    timestamp,
                    monitor_id,
                    owner_id,
                    expected_status.value,
                    expected_active_version_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("lifecycle precondition failed")

    def soft_delete_exact(
        self,
        monitor_id: str,
        *,
        owner_id: str,
        expected_status: MonitorStatus,
        expected_active_version_id: str,
        disabled_at: datetime,
    ) -> None:
        timestamp = utc_timestamp(disabled_at, parameter="disabled_at")
        if expected_status is MonitorStatus.DISABLED:
            raise ValueError("lifecycle precondition failed")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE monitors SET status = ?, disabled_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, lease_generation = lease_generation + 1, "
                "next_run_at = NULL, updated_at = ? "
                "WHERE id = ? AND owner_id = ? AND status = ? AND active_version_id = ?",
                (
                    MonitorStatus.DISABLED.value,
                    timestamp,
                    timestamp,
                    monitor_id,
                    owner_id,
                    expected_status.value,
                    expected_active_version_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("lifecycle precondition failed")

    def stage_candidate_action(
        self,
        monitor_id: str,
        *,
        owner_id: str,
        expected_status: MonitorStatus,
        expected_active_version_id: str,
        spec: MonitorSpec,
        action_kind: str,
        actions: object,
        now: datetime,
        reply_factory: Callable[[object, str, str], object],
    ) -> object:
        from personal_monitor.control.actions import PendingActionService
        from personal_monitor.control.messages import ControlReply

        if (
            type(actions) is not PendingActionService
            or actions.connection is not self.connection
            or action_kind not in {"update", "schedule_change"}
            or expected_status not in {MonitorStatus.ACTIVE, MonitorStatus.PAUSED_USER}
            or type(spec) is not MonitorSpec
            or spec.owner_id != owner_id
            or not callable(reply_factory)
        ):
            raise ValueError("lifecycle precondition failed")
        timestamp = utc_timestamp(now, parameter="now")
        fresh = MonitorSpec.model_validate(spec.model_dump(mode="json"))
        canonical = canonical_json(fresh.model_dump(mode="json"))
        spec_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        candidate_id = uuid4().hex
        with transaction(self.connection, immediate=True):
            monitor = self.connection.execute(
                "SELECT name FROM monitors WHERE id = ? AND owner_id = ? AND status = ? "
                "AND active_version_id = ?",
                (
                    monitor_id,
                    owner_id,
                    expected_status.value,
                    expected_active_version_id,
                ),
            ).fetchone()
            if monitor is None:
                raise ValueError("lifecycle precondition failed")
            next_version = self.connection.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM monitor_versions "
                "WHERE monitor_id = ?",
                (monitor_id,),
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO monitor_versions(id, monitor_id, version_number, spec_json, "
                "created_by, created_at, approved_by, approved_at, parent_version_id) "
                "VALUES (?, ?, ?, ?, 'codex-control', ?, NULL, NULL, ?)",
                (
                    candidate_id,
                    monitor_id,
                    next_version,
                    canonical,
                    timestamp,
                    expected_active_version_id,
                ),
            )
            pending = actions.create(
                owner_id,
                action_kind,
                {
                    "owner_id": owner_id,
                    "monitor_id": monitor_id,
                    "monitor_name": monitor["name"],
                    "expected_status": expected_status.value,
                    "expected_active_version_id": expected_active_version_id,
                    "candidate_version_id": candidate_id,
                    "spec_hash": spec_hash,
                    "action_kind": action_kind,
                },
                now=now,
            )
            reply = reply_factory(pending, candidate_id, spec_hash)
            if type(reply) is not ControlReply:
                raise ValueError("lifecycle precondition failed")
            result = ControlReply(reply.text, reply.buttons)
        return result

    def activate_candidate_exact(
        self,
        monitor_id: str,
        candidate_version_id: str,
        *,
        owner_id: str,
        expected_status: MonitorStatus,
        expected_active_version_id: str,
        expected_created_by: str,
        spec_hash: str,
        activated_at: datetime,
        target_status: MonitorStatus | None = None,
    ) -> MonitorSpec:
        if expected_created_by not in {"codex-control", "scrapling-adaptive"}:
            raise ValueError("lifecycle precondition failed")
        timestamp = utc_timestamp(activated_at, parameter="activated_at")
        resulting_status = expected_status if target_status is None else target_status
        if expected_created_by == "scrapling-adaptive" and (
            expected_status is not MonitorStatus.NEEDS_REVIEW
            or resulting_status is not MonitorStatus.ACTIVE
        ):
            raise ValueError("lifecycle precondition failed")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT v.spec_json FROM monitor_versions AS v "
                "JOIN monitors AS m ON m.id = v.monitor_id "
                "WHERE v.id = ? AND v.monitor_id = ? AND v.created_by = ? "
                "AND v.approved_at IS NULL AND v.parent_version_id = ? "
                "AND m.owner_id = ? AND m.status = ? "
                "AND m.active_version_id = ?",
                (
                    candidate_version_id,
                    monitor_id,
                    expected_created_by,
                    expected_active_version_id,
                    owner_id,
                    expected_status.value,
                    expected_active_version_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("lifecycle precondition failed")
            spec = MonitorSpec.model_validate_json(row["spec_json"])
            canonical = canonical_json(spec.model_dump(mode="json"))
            if (
                spec.owner_id != owner_id
                or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != spec_hash
            ):
                raise ValueError("lifecycle precondition failed")
            scheduled_at = (
                _next_run_at(spec, monitor_id, activated_at).isoformat()
                if resulting_status is MonitorStatus.ACTIVE
                else None
            )
            approved = self.connection.execute(
                "UPDATE monitor_versions SET approved_by = ?, approved_at = ? "
                "WHERE id = ? AND monitor_id = ? AND approved_at IS NULL",
                (owner_id, timestamp, candidate_version_id, monitor_id),
            )
            if approved.rowcount != 1:
                raise ValueError("lifecycle precondition failed")
            activated = self.connection.execute(
                "UPDATE monitors SET active_version_id = ?, name = ?, status = ?, "
                "next_run_at = ?, lease_owner = NULL, lease_expires_at = NULL, "
                "lease_generation = lease_generation + 1, updated_at = ? "
                "WHERE id = ? AND owner_id = ? AND status = ? "
                "AND active_version_id = ?",
                (
                    candidate_version_id,
                    spec.name,
                    resulting_status.value,
                    scheduled_at,
                    timestamp,
                    monitor_id,
                    owner_id,
                    expected_status.value,
                    expected_active_version_id,
                ),
            )
            if activated.rowcount != 1:
                raise ValueError("lifecycle precondition failed")
        return spec

    def find_repair_candidate(
        self,
        monitor_id: str,
        *,
        owner_id: str,
    ) -> CandidateVersion | None:
        try:
            row = self.connection.execute(
                "SELECT v.id, v.monitor_id, v.created_by, v.spec_json, "
                "v.parent_version_id, m.active_version_id, m.owner_id "
                "FROM monitor_versions AS v "
                "JOIN monitors AS m ON m.id = v.monitor_id "
                "WHERE m.id = ? AND m.owner_id = ? AND m.status = ? "
                "AND v.created_by = 'scrapling-adaptive' AND v.approved_at IS NULL "
                "AND v.parent_version_id = m.active_version_id "
                "ORDER BY v.version_number DESC, v.id DESC LIMIT 1",
                (monitor_id, owner_id, MonitorStatus.NEEDS_REVIEW.value),
            ).fetchone()
            if row is None:
                return None
            spec = MonitorSpec.model_validate_json(row["spec_json"])
            if spec.owner_id != owner_id:
                raise ValueError
            return CandidateVersion(
                id=row["id"],
                monitor_id=row["monitor_id"],
                owner_id=row["owner_id"],
                expected_active_version_id=row["parent_version_id"],
                created_by=row["created_by"],
                spec=spec,
            )
        except Exception:
            raise ValueError("control monitor unavailable") from None

    def transition_status(
        self,
        monitor_id: str,
        expected: MonitorStatus,
        target: MonitorStatus,
        *,
        owner_id: str,
    ) -> None:
        with transaction(self.connection):
            changed_at = utc_now()
            scheduled_at: str | None = None
            if target is MonitorStatus.ACTIVE:
                row = self.connection.execute(
                    "SELECT v.spec_json FROM monitors AS m "
                    "JOIN monitor_versions AS v ON v.id = m.active_version_id "
                    "WHERE m.id = ? AND m.owner_id = ? AND m.status = ?",
                    (monitor_id, owner_id, expected.value),
                ).fetchone()
                if row is None:
                    raise ValueError("monitor is not in the expected status")
                spec = MonitorSpec.model_validate_json(row["spec_json"])
                scheduled_at = _next_run_at(spec, monitor_id, changed_at).isoformat()
            cursor = self.connection.execute(
                "UPDATE monitors SET status = ?, next_run_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, lease_generation = lease_generation + 1, "
                "updated_at = ? "
                "WHERE id = ? AND owner_id = ? AND status = ?",
                (
                    target.value,
                    scheduled_at,
                    changed_at.isoformat(),
                    monitor_id,
                    owner_id,
                    expected.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("monitor is not in the expected status")

    def soft_delete(self, monitor_id: str, *, owner_id: str, disabled_at: datetime) -> None:
        timestamp = utc_timestamp(disabled_at, parameter="disabled_at")
        with transaction(self.connection):
            cursor = self.connection.execute(
                "UPDATE monitors SET status = ?, disabled_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, lease_generation = lease_generation + 1, "
                "next_run_at = NULL, updated_at = ? WHERE id = ? AND owner_id = ?",
                (MonitorStatus.DISABLED.value, timestamp, timestamp, monitor_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("monitor does not exist for owner")


def _safe_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 64:
        raise ValueError
    parsed = parse_timestamp(value)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed


def _normalize_url_alias(owner_id: str, name: str) -> str:
    if type(owner_id) is not str or _OWNER_RE.fullmatch(owner_id) is None:
        raise ValueError("invalid URL alias owner")
    if (
        type(name) is not str
        or any(unicodedata.category(character).startswith("C") for character in name)
    ):
        raise ValueError("invalid URL alias name")
    normalized = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    if not 1 <= len(normalized) <= 300:
        raise ValueError("invalid URL alias name")
    return normalized


def _valid_alias_url(value: object) -> bool:
    try:
        if (
            type(value) is not str
            or not 1 <= len(value) <= 2_048
            or any(unicodedata.category(character).startswith("C") for character in value)
        ):
            return False
        parsed = urlsplit(value)
        _ = parsed.port
        return (
            parsed.scheme.casefold() in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
    except (TypeError, ValueError):
        return False


def _next_run_at(spec: MonitorSpec, monitor_id: str, after: datetime) -> datetime:
    from personal_monitor.engine.scheduler import next_run_at

    return next_run_at(spec, monitor_id, after)
