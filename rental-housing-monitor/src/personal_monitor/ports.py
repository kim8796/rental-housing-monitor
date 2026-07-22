from __future__ import annotations

from datetime import datetime
from typing import Protocol

from personal_monitor.domain.observation import ObservationBatch
from personal_monitor.domain.spec import MonitorSpec, SourceAdapterKind


class SourceAdapter(Protocol):
    async def fetch(self, monitor_id: str, spec: MonitorSpec) -> ObservationBatch: ...


class AdapterRegistry(Protocol):
    def resolve(self, kind: SourceAdapterKind, adapter_ref: str | None) -> SourceAdapter: ...


class DeliverySender(Protocol):
    async def send(self, address: str, payload: dict[str, object]) -> str: ...


class OperatorHealthSink(Protocol):
    async def emit_once(self, dedupe_key: str, payload: dict[str, object]) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
