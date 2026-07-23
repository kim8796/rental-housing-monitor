from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from personal_monitor.ports import DeliverySender, OperatorHealthSink
from personal_monitor.storage import RegistryRepository, RuntimeRepository
from personal_monitor.storage.runtime import OutboxRow

_RETRY_DELAYS_SECONDS = (60, 300, 1800, 7200, 21600)


class OutboxWorker:
    """Deliver leased rows at least once.

    A crash after the sender accepts a message but before SQLite commits may retry the send because
    the remote sender and SQLite cannot share a transaction.
    """

    def __init__(
        self,
        *,
        runtime: RuntimeRepository,
        registry: RegistryRepository,
        sender: DeliverySender,
        health_sink: OperatorHealthSink,
        worker_id: str,
    ) -> None:
        self.runtime = runtime
        self.registry = registry
        self.sender = sender
        self.health_sink = health_sink
        self.worker_id = worker_id

    async def drain_once(
        self,
        *,
        now: datetime,
        limit: int = 50,
        monitor_id: str | None = None,
        outbox_ids: Sequence[str] | None = None,
    ) -> int:
        delivered_count = 0
        for row in self.runtime.claim_due_outbox(
            worker_id=self.worker_id,
            now=now,
            limit=limit,
            monitor_id=monitor_id,
            outbox_ids=outbox_ids,
        ):
            try:
                target = self.registry.get_delivery_target(row.target_id)
            except Exception:
                await self._reschedule(row, now)
                continue
            try:
                message_id = await self.sender.send(target.address, row.payload)
            except Exception:
                await self._reschedule(row, now)
                continue
            if not isinstance(message_id, str) or not message_id.strip():
                await self._reschedule(row, now)
                continue
            self.runtime.mark_delivered(
                row.id,
                worker_id=self.worker_id,
                message_id=message_id,
                delivered_at=now,
            )
            delivered_count += 1
        return delivered_count

    async def _reschedule(self, row: OutboxRow, now: datetime) -> None:
        attempt_count = row.attempt_count + 1
        delay_index = min(row.attempt_count, len(_RETRY_DELAYS_SECONDS) - 1)
        self.runtime.reschedule_outbox(
            row.id,
            worker_id=self.worker_id,
            available_at=now + timedelta(seconds=_RETRY_DELAYS_SECONDS[delay_index]),
            error="delivery_failed",
        )
        if attempt_count >= 5:
            window_start = _six_hour_window_start(now)
            await self.health_sink.emit_once(
                f"outbox-stuck:{row.id}:{window_start.isoformat()}",
                {
                    "code": "delivery_failed",
                    "outbox_id": row.id,
                    "attempt_count": attempt_count,
                },
            )


def _six_hour_window_start(value: datetime) -> datetime:
    utc_value = value.astimezone(UTC)
    return utc_value.replace(hour=(utc_value.hour // 6) * 6, minute=0, second=0, microsecond=0)
