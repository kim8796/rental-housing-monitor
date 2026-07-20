from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from rental_monitor.models import Announcement, canonical_key


class AnnouncementRepository:
    def __init__(self, path: str | Path) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.initialize()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS announcements (
                announcement_key TEXT PRIMARY KEY,
                source_id TEXT,
                title TEXT NOT NULL,
                agency TEXT NOT NULL,
                region TEXT NOT NULL,
                housing_type TEXT NOT NULL,
                target TEXT NOT NULL,
                announcement_date TEXT NOT NULL,
                application_start_date TEXT,
                application_end_date TEXT,
                url TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                announcement_key TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (announcement_key, chat_id),
                FOREIGN KEY (announcement_key) REFERENCES announcements(announcement_key)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                new_count INTEGER NOT NULL DEFAULT 0,
                agency_status TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        self.connection.commit()

    def upsert_seen(
        self,
        announcements: Iterable[Announcement],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        timestamp = (observed_at or datetime.now(UTC)).isoformat()
        rows = []
        for announcement in announcements:
            rows.append(
                (
                    canonical_key(announcement),
                    announcement.source_id,
                    announcement.title,
                    announcement.agency.value,
                    announcement.region,
                    announcement.housing_type.value,
                    announcement.target,
                    announcement.announcement_date.isoformat(),
                    _date_text(announcement.application_start_date),
                    _date_text(announcement.application_end_date),
                    announcement.url,
                    timestamp,
                    timestamp,
                )
            )
        self.connection.executemany(
            """
            INSERT INTO announcements (
                announcement_key, source_id, title, agency, region, housing_type, target,
                announcement_date, application_start_date, application_end_date, url,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(announcement_key) DO UPDATE SET
                source_id = excluded.source_id,
                title = excluded.title,
                agency = excluded.agency,
                region = excluded.region,
                housing_type = excluded.housing_type,
                target = excluded.target,
                announcement_date = excluded.announcement_date,
                application_start_date = excluded.application_start_date,
                application_end_date = excluded.application_end_date,
                url = excluded.url,
                last_seen_at = excluded.last_seen_at
            """,
            rows,
        )
        self.connection.commit()

    def pending_for_chat(
        self, announcements: Iterable[Announcement], chat_id: str
    ) -> list[Announcement]:
        pending: list[Announcement] = []
        for announcement in announcements:
            delivered = self.connection.execute(
                """
                SELECT 1 FROM deliveries
                WHERE announcement_key = ? AND chat_id = ?
                """,
                (canonical_key(announcement), chat_id),
            ).fetchone()
            if delivered is None:
                pending.append(announcement)
        return pending

    def mark_delivered(
        self,
        announcement: Announcement,
        chat_id: str,
        message_id: int,
        *,
        delivered_at: datetime | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO deliveries (announcement_key, chat_id, delivered_at, message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(announcement_key, chat_id) DO NOTHING
            """,
            (
                canonical_key(announcement),
                chat_id,
                (delivered_at or datetime.now(UTC)).isoformat(),
                message_id,
            ),
        )
        self.connection.commit()

    def start_run(self, started_at: datetime | None = None) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
            ((started_at or datetime.now(UTC)).isoformat(),),
        )
        self.connection.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("failed to create run record")
        return cursor.lastrowid

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        new_count: int,
        agency_status: dict[str, str],
        finished_at: datetime | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, new_count = ?, agency_status = ?
            WHERE id = ?
            """,
            (
                (finished_at or datetime.now(UTC)).isoformat(),
                status,
                new_count,
                json.dumps(agency_status, ensure_ascii=False, sort_keys=True),
                run_id,
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


def _date_text(value: object) -> str | None:
    return value.isoformat() if value is not None else None
