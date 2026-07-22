from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from personal_monitor.storage.schema import transaction, utc_timestamp


class Maintenance:
    """Apply the runtime's bounded retention policy to one SQLite database."""

    def __init__(self, repository: object) -> None:
        connection = getattr(repository, "connection", repository)
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("repository must provide a SQLite connection")
        self.connection = connection

    def run(self, *, now: datetime) -> None:
        """Delete rows past strict retention cutoffs, then optimize the database."""
        cutoffs = _Cutoffs.from_now(now)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "DELETE FROM runs WHERE COALESCE(finished_at, started_at) < ?",
                (cutoffs.runs,),
            )
            self._delete_old_deliveries(cutoffs.deliveries)
            self.connection.execute(
                "DELETE FROM pending_actions WHERE "
                "(consumed_at IS NOT NULL AND consumed_at < ?) OR expires_at < ?",
                (cutoffs.actions, cutoffs.actions),
            )
            snapshot_columns = self._table_columns("diagnostic_snapshots")
            if snapshot_columns:
                self._delete_old_diagnostic_snapshots(snapshot_columns, cutoffs)
            self._delete_old_disabled_monitors(cutoffs.disabled_monitors)
        self.connection.execute("PRAGMA optimize")

    def _delete_old_deliveries(self, cutoff: str) -> None:
        outbox_ids = [
            row["outbox_id"]
            for row in self.connection.execute(
                "SELECT deliveries.outbox_id FROM deliveries "
                "JOIN outbox ON outbox.id = deliveries.outbox_id "
                "WHERE outbox.status = 'delivered' AND deliveries.delivered_at < ?",
                (cutoff,),
            )
        ]
        self._delete_outbox_ids(outbox_ids)

    def _delete_old_disabled_monitors(self, cutoff: str) -> None:
        monitor_ids = [
            row["id"]
            for row in self.connection.execute(
                "SELECT id FROM monitors WHERE status = 'disabled' AND disabled_at < ?", (cutoff,)
            )
        ]
        if not monitor_ids:
            return
        placeholders = _placeholders(monitor_ids)
        if "monitor_id" in self._table_columns("diagnostic_snapshots"):
            self.connection.execute(
                f"DELETE FROM diagnostic_snapshots WHERE monitor_id IN ({placeholders})",
                monitor_ids,
            )
        outbox_ids = [
            row["id"]
            for row in self.connection.execute(
                f"SELECT id FROM outbox WHERE monitor_id IN ({placeholders})", monitor_ids
            )
        ]
        self._delete_outbox_ids(outbox_ids)
        self.connection.execute(
            f"DELETE FROM observations WHERE monitor_id IN ({placeholders})", monitor_ids
        )
        self.connection.execute(
            f"DELETE FROM runs WHERE monitor_id IN ({placeholders})", monitor_ids
        )
        self.connection.execute(
            f"DELETE FROM monitor_versions WHERE monitor_id IN ({placeholders})", monitor_ids
        )
        self.connection.execute(f"DELETE FROM monitors WHERE id IN ({placeholders})", monitor_ids)

    def _delete_outbox_ids(self, outbox_ids: list[str]) -> None:
        if not outbox_ids:
            return
        placeholders = _placeholders(outbox_ids)
        self.connection.execute(
            f"DELETE FROM deliveries WHERE outbox_id IN ({placeholders})", outbox_ids
        )
        self.connection.execute(f"DELETE FROM outbox WHERE id IN ({placeholders})", outbox_ids)

    def _delete_old_diagnostic_snapshots(self, columns: frozenset[str], cutoffs: _Cutoffs) -> None:
        if "expires_at" in columns:
            self.connection.execute(
                "DELETE FROM diagnostic_snapshots WHERE created_at < ? OR expires_at < ?",
                (cutoffs.snapshots, cutoffs.now),
            )
            return
        self.connection.execute(
            "DELETE FROM diagnostic_snapshots WHERE created_at < ?", (cutoffs.snapshots,)
        )

    def _table_columns(self, name: str) -> frozenset[str]:
        if (
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is None
        ):
            return frozenset()
        return frozenset(
            row["name"] for row in self.connection.execute(f"PRAGMA table_info({name})")
        )


class _Cutoffs:
    def __init__(
        self,
        *,
        runs: str,
        deliveries: str,
        actions: str,
        snapshots: str,
        disabled_monitors: str,
        now: str,
    ) -> None:
        self.runs = runs
        self.deliveries = deliveries
        self.actions = actions
        self.snapshots = snapshots
        self.disabled_monitors = disabled_monitors
        self.now = now

    @classmethod
    def from_now(cls, now: datetime) -> _Cutoffs:
        return cls(
            runs=utc_timestamp(now - timedelta(days=90), parameter="runs_cutoff"),
            deliveries=utc_timestamp(now - timedelta(days=180), parameter="deliveries_cutoff"),
            actions=utc_timestamp(now - timedelta(days=1), parameter="actions_cutoff"),
            snapshots=utc_timestamp(now - timedelta(days=7), parameter="snapshots_cutoff"),
            disabled_monitors=utc_timestamp(
                now - timedelta(days=30), parameter="disabled_monitors_cutoff"
            ),
            now=utc_timestamp(now, parameter="now"),
        )


def _placeholders(values: list[str]) -> str:
    return ", ".join("?" for _ in values)
