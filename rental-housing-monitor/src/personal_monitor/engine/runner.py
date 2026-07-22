from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from personal_monitor.domain.observation import (
    ObservedItem,
    SourceWarning,
    content_hash,
    diff_items,
)
from personal_monitor.domain.rules import RuleMatch, evaluate_rules
from personal_monitor.domain.spec import MonitorSpec, MonitorStatus, RuleKind
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.engine.scheduler import next_run_at
from personal_monitor.ports import AdapterRegistry, Clock
from personal_monitor.storage import RegistryRepository, RuntimeRepository

_DIAGNOSTIC_CODES = {
    ErrorClass.TRANSIENT_NETWORK: "network_error",
    ErrorClass.AUTHENTICATION: "authentication_failed",
    ErrorClass.STRUCTURE: "structure_changed",
    ErrorClass.VALIDATION: "validation_failed",
    ErrorClass.POLICY: "policy_rejected",
    ErrorClass.DELIVERY: "delivery_failed",
    ErrorClass.INTERNAL: "internal_error",
}


@dataclass(frozen=True, slots=True)
class RunResult:
    status: Literal["success", "partial_failure", "failed"]
    matched_count: int
    warning_count: int


class MonitorRunner:
    def __init__(
        self,
        *,
        registry: RegistryRepository,
        runtime: RuntimeRepository,
        adapters: AdapterRegistry,
        clock: Clock,
        worker_id: str,
    ) -> None:
        self.registry = registry
        self.runtime = runtime
        self.adapters = adapters
        self.clock = clock
        self.worker_id = worker_id

    async def run(self, monitor_id: str) -> RunResult:
        active = self.registry.get_active_monitor(monitor_id)
        spec = active.spec
        target = self.registry.get_primary_target(active.owner_id)
        run_id = self.runtime.start_run(monitor_id, active.version_id, started_at=self.clock.now())
        try:
            return await self._run_started(monitor_id, spec, target.id, run_id)
        except Exception as caught:
            error = (
                caught
                if isinstance(caught, MonitorError)
                else MonitorError(ErrorClass.INTERNAL, "internal", "internal failure")
            )
            self._finish_failed(monitor_id, spec, run_id, error)
            return RunResult(status="failed", matched_count=0, warning_count=0)

    async def _run_started(
        self, monitor_id: str, spec: MonitorSpec, target_id: str, run_id: str
    ) -> RunResult:
        batch = await self.adapters.resolve(spec.source_adapter, spec.adapter_ref).fetch(
            monitor_id, spec
        )
        if batch.monitor_id != monitor_id:
            raise MonitorError(ErrorClass.VALIDATION, "validate", "batch monitor mismatch")
        previous = self.runtime.load_items(monitor_id)
        changes = diff_items(previous, list(batch.items))
        self.runtime.upsert_items(batch)
        previous_by_id = {item.item_id: item for item in previous}
        current_by_id = {item.item_id: item for item in batch.items}
        matched_count = 0
        for change in changes:
            current = current_by_id.get(change.item_id)
            if current is None:
                continue
            matches = evaluate_rules(
                spec.rules,
                previous=previous_by_id.get(change.item_id),
                current=current,
                is_new=change.is_new,
            )
            for match in matches:
                matched_count += 1
                self.runtime.enqueue_delivery(
                    dedupe_key=delivery_key(monitor_id, current, match),
                    monitor_id=monitor_id,
                    target_id=target_id,
                    payload=render_payload(spec, current, match),
                )
        if batch.warnings:
            for warning in batch.warnings:
                self.runtime.enqueue_delivery(
                    dedupe_key=(
                        f"{monitor_id}:warning:{batch.observed_at.date()}:"
                        f"{warning.source}:{warning.stage}"
                    ),
                    monitor_id=monitor_id,
                    target_id=target_id,
                    payload=render_warning(spec, warning, batch.source_status),
                )
            run_status: Literal["success", "partial_failure"] = "partial_failure"
        elif matched_count == 0 and spec.notify_on_no_change:
            local_date = batch.observed_at.astimezone(ZoneInfo(spec.timezone)).date()
            self.runtime.enqueue_delivery(
                dedupe_key=f"{monitor_id}:no-change:{local_date}",
                monitor_id=monitor_id,
                target_id=target_id,
                payload={"text": "오늘은 신규 공고가 없습니다."},
            )
            run_status = "success"
        else:
            run_status = "success"
        self.runtime.finish_run(run_id, status=run_status, stage="complete")
        self.runtime.release_lease(
            monitor_id,
            worker_id=self.worker_id,
            next_run_at=next_run_at(spec, monitor_id, self.clock.now()),
        )
        return RunResult(
            status=run_status,
            matched_count=matched_count,
            warning_count=len(batch.warnings),
        )

    def _finish_failed(
        self, monitor_id: str, spec: MonitorSpec, run_id: str, error: MonitorError
    ) -> None:
        self.runtime.finish_run(
            run_id,
            status="failed",
            stage=error.stage,
            error_class=error.error_class.value,
            error_detail=_DIAGNOSTIC_CODES[error.error_class],
        )
        now = self.clock.now()
        if error.error_class is ErrorClass.TRANSIENT_NETWORK:
            retry_at = now + timedelta(minutes=5)
        else:
            target_status = (
                MonitorStatus.PAUSED_AUTH
                if error.error_class is ErrorClass.AUTHENTICATION
                else MonitorStatus.NEEDS_REVIEW
            )
            self.registry.transition_status(monitor_id, MonitorStatus.ACTIVE, target_status)
            retry_at = next_run_at(spec, monitor_id, now)
        self.runtime.release_lease(
            monitor_id,
            worker_id=self.worker_id,
            next_run_at=retry_at,
        )


def render_payload(
    spec: MonitorSpec, item: ObservedItem, match: RuleMatch
) -> dict[str, object]:
    del item
    return {
        "text": (
            "모니터 조건에 맞는 변경이 감지되었습니다.\n"
            f"종류: {match.kind.value}\n"
            f"출처: {_public_url(spec.target_url)}"
        )
    }


def render_warning(
    spec: MonitorSpec, warning: SourceWarning, source_status: object
) -> dict[str, object]:
    del spec, warning, source_status
    return {"text": "일부 소스 처리에 실패해 결과가 불완전할 수 있습니다."}


def delivery_key(monitor_id: str, item: ObservedItem, match: RuleMatch) -> str:
    if match.kind is RuleKind.NEW_ITEM:
        return f"{monitor_id}:{item.item_id}:new_item"
    digest = content_hash({"previous": match.previous, "current": match.current})
    return f"{monitor_id}:{item.item_id}:{match.kind.value}:{match.field}:{digest}"


def _public_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
