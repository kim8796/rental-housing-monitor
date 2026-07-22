from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from personal_monitor.domain.observation import (
    ObservationBatch,
    ObservedItem,
    SourceWarning,
    content_hash,
    diff_items,
)
from personal_monitor.domain.rules import RuleMatch, evaluate_rules
from personal_monitor.domain.spec import MonitorSpec, MonitorStatus, RuleKind
from personal_monitor.domain.validator import BatchValidationError, validate_batch
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.engine.scheduler import next_run_at
from personal_monitor.ports import AdapterRegistry, Clock
from personal_monitor.storage import (
    DeliveryCandidate,
    MonitorLease,
    RegistryRepository,
    RuntimeRepository,
)

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


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    result: RunResult
    stage: str
    error_class: ErrorClass | None = None
    error_detail: str | None = None
    transition_to: MonitorStatus | None = None
    use_fallback_schedule: bool = False


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

    async def run(self, lease: MonitorLease) -> RunResult:
        monitor_id = lease.monitor_id
        active = self.registry.get_active_monitor(monitor_id)
        spec = active.spec
        target = self.registry.get_primary_target(active.owner_id)
        started_at = self.clock.now()
        run_id = self.runtime.start_run(
            lease,
            active.version_id,
            worker_id=self.worker_id,
            fetch_strategy=spec.fetch_strategy.value,
            started_at=started_at,
        )
        outcome = _cancellation_outcome()
        pending_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        completed_result = outcome.result
        try:
            try:
                result = await self._run_started(lease, spec, target.id)
            except asyncio.CancelledError as caught:
                pending_error = caught
            except Exception as caught:
                error = (
                    caught
                    if isinstance(caught, MonitorError)
                    else MonitorError(ErrorClass.INTERNAL, "internal", "internal failure")
                )
                outcome = _failure_outcome(error)
            except BaseException as caught:
                pending_error = caught
            else:
                outcome = _RunOutcome(result=result, stage="complete")
        finally:
            try:
                completed_result = await _shield_cleanup(
                    self._complete_started_run(lease, spec, run_id, started_at, outcome)
                )
            except BaseException as caught:
                cleanup_error = caught

        if pending_error is not None:
            raise pending_error
        if cleanup_error is not None:
            raise cleanup_error
        return completed_result

    async def _run_started(
        self, lease: MonitorLease, spec: MonitorSpec, target_id: str
    ) -> RunResult:
        monitor_id = lease.monitor_id
        batch = await self.adapters.resolve(spec.source_adapter, spec.adapter_ref).fetch(
            monitor_id, spec
        )
        if batch.monitor_id != monitor_id:
            raise MonitorError(ErrorClass.VALIDATION, "validate", "batch monitor mismatch")
        try:
            validate_batch(spec, batch)
        except BatchValidationError as error:
            raise MonitorError(
                ErrorClass.VALIDATION, "validate", "adapter batch validation failed"
            ) from error
        previous = self.runtime.load_items(monitor_id)
        snapshot_batch = _snapshot_batch(batch, previous)
        changes = diff_items(previous, list(snapshot_batch.items))
        previous_by_id = {item.item_id: item for item in previous}
        current_by_id = {item.item_id: item for item in snapshot_batch.items}
        candidates: list[DeliveryCandidate] = []
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
                candidates.append(
                    DeliveryCandidate(
                        dedupe_key=delivery_key(monitor_id, current, match),
                        target_id=target_id,
                        payload=render_payload(spec, current, match),
                    )
                )
        if batch.warnings:
            local_date = batch.observed_at.astimezone(ZoneInfo(spec.timezone)).date()
            for warning in batch.warnings:
                candidates.append(
                    DeliveryCandidate(
                        dedupe_key=(
                            f"{monitor_id}:warning:{local_date}:{warning.source}:{warning.stage}"
                        ),
                        target_id=target_id,
                        payload=render_warning(spec, warning, batch.source_status),
                    )
                )
            run_status: Literal["success", "partial_failure"] = "partial_failure"
        elif matched_count == 0 and spec.notify_on_no_change:
            local_date = batch.observed_at.astimezone(ZoneInfo(spec.timezone)).date()
            candidates.append(
                DeliveryCandidate(
                    dedupe_key=f"{monitor_id}:no-change:{local_date}",
                    target_id=target_id,
                    payload={"text": "오늘은 신규 공고가 없습니다."},
                )
            )
            run_status = "success"
        else:
            run_status = "success"
        self.runtime.apply_snapshot_and_deliveries(
            snapshot_batch, candidates, lease=lease, worker_id=self.worker_id
        )
        return RunResult(
            status=run_status,
            matched_count=matched_count,
            warning_count=len(batch.warnings),
        )

    async def _complete_started_run(
        self,
        lease: MonitorLease,
        spec: MonitorSpec,
        run_id: str,
        started_at: datetime,
        outcome: _RunOutcome,
    ) -> RunResult:
        monitor_id = lease.monitor_id
        cleanup_error: Exception | None = None
        schedule_base = started_at
        try:
            if outcome.transition_to is not None:
                try:
                    self.runtime.transition_monitor_status(
                        lease,
                        worker_id=self.worker_id,
                        expected=MonitorStatus.ACTIVE,
                        target=outcome.transition_to,
                    )
                except Exception as caught:
                    cleanup_error = caught

            try:
                schedule_base = self.clock.now()
                if outcome.use_fallback_schedule:
                    scheduled_at = _aware_fallback(schedule_base)
                elif outcome.error_class is ErrorClass.TRANSIENT_NETWORK:
                    scheduled_at = schedule_base + timedelta(minutes=5)
                else:
                    scheduled_at = next_run_at(spec, monitor_id, schedule_base)
            except Exception:
                scheduled_at = _aware_fallback(schedule_base)

            try:
                self.runtime.finish_run(
                    run_id,
                    lease=lease,
                    worker_id=self.worker_id,
                    status=outcome.result.status,
                    stage=outcome.stage,
                    error_class=(
                        outcome.error_class.value if outcome.error_class is not None else None
                    ),
                    error_detail=outcome.error_detail,
                )
            except Exception as caught:
                if cleanup_error is None:
                    cleanup_error = caught
        finally:
            self.runtime.release_lease(
                lease,
                worker_id=self.worker_id,
                next_run_at=scheduled_at,
            )

        if cleanup_error is not None:
            raise cleanup_error
        return outcome.result


def _failure_outcome(error: MonitorError) -> _RunOutcome:
    if error.error_class is ErrorClass.TRANSIENT_NETWORK:
        transition_to = None
    elif error.error_class is ErrorClass.AUTHENTICATION:
        transition_to = MonitorStatus.PAUSED_AUTH
    else:
        transition_to = MonitorStatus.NEEDS_REVIEW
    return _RunOutcome(
        result=RunResult(status="failed", matched_count=0, warning_count=0),
        stage=error.stage,
        error_class=error.error_class,
        error_detail=_DIAGNOSTIC_CODES[error.error_class],
        transition_to=transition_to,
    )


def _cancellation_outcome() -> _RunOutcome:
    return _RunOutcome(
        result=RunResult(status="failed", matched_count=0, warning_count=0),
        stage="cancelled",
        error_class=ErrorClass.INTERNAL,
        error_detail=_DIAGNOSTIC_CODES[ErrorClass.INTERNAL],
        use_fallback_schedule=True,
    )


async def _shield_cleanup(
    cleanup: Coroutine[Any, Any, RunResult],
) -> RunResult:
    cleanup_task = asyncio.create_task(cleanup)
    interrupted: asyncio.CancelledError | None = None
    cleanup_error: BaseException | None = None
    while not cleanup_task.done():
        try:
            # Waiting does not forward caller cancellation into the retained task.
            await asyncio.wait({cleanup_task})
        except asyncio.CancelledError as caught:
            if interrupted is None:
                interrupted = caught
    try:
        result = cleanup_task.result()
    except BaseException as caught:
        cleanup_error = caught
    if interrupted is not None:
        raise interrupted
    if cleanup_error is not None:
        raise cleanup_error
    return result


def _aware_fallback(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = datetime.now(UTC)
    return value.astimezone(UTC) + timedelta(minutes=5)


def _snapshot_batch(batch: ObservationBatch, previous: list[ObservedItem]) -> ObservationBatch:
    if not batch.warnings:
        return batch
    merged = {item.item_id: item for item in previous}
    merged.update((item.item_id, item) for item in batch.items)
    return ObservationBatch(
        monitor_id=batch.monitor_id,
        items=tuple(merged[item_id] for item_id in sorted(merged)),
        observed_at=batch.observed_at,
        source_hash=batch.source_hash,
        source_status=batch.source_status,
        warnings=batch.warnings,
    )


def render_payload(spec: MonitorSpec, item: ObservedItem, match: RuleMatch) -> dict[str, object]:
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
