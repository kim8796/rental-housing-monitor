from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from personal_monitor.domain.observation import (
    ObservationBatch,
    ObservedItem,
    SourceWarning,
    content_hash,
    diff_items,
)
from personal_monitor.domain.rules import RuleMatch, evaluate_rules
from personal_monitor.domain.spec import MonitorSpec, MonitorStatus, RuleKind, SourceAdapterKind
from personal_monitor.domain.validator import BatchValidationError, validate_batch
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.engine.scheduler import next_run_at
from personal_monitor.ports import AdapterRegistry, Clock
from personal_monitor.security.credential_names import is_sensitive_credential_name
from personal_monitor.security.url_policy import (
    ALLOWED_PORTS,
    canonicalize_hostname,
    has_unsafe_url_characters,
)
from personal_monitor.storage import (
    DeliveryCandidate,
    MonitorLease,
    RegistryRepository,
    RuntimeRepository,
)
from rental_monitor.models import Agency, Announcement, HousingType
from rental_monitor.telegram import format_announcement

_DIAGNOSTIC_CODES = {
    ErrorClass.TRANSIENT_NETWORK: "network_error",
    ErrorClass.AUTHENTICATION: "authentication_failed",
    ErrorClass.STRUCTURE: "structure_changed",
    ErrorClass.VALIDATION: "validation_failed",
    ErrorClass.POLICY: "policy_rejected",
    ErrorClass.DELIVERY: "delivery_failed",
    ErrorClass.INTERNAL: "internal_error",
}
_RULE_KIND_LABELS = {
    RuleKind.NEW_ITEM: "신규 항목",
    RuleKind.FIELD_CHANGED: "값 변경",
    RuleKind.NUMERIC_THRESHOLD: "기준값 도달",
    RuleKind.STATUS_EQUALS: "상태 일치",
    RuleKind.KEYWORD_MATCH: "키워드 일치",
}


@dataclass(frozen=True, slots=True)
class RunResult:
    status: Literal["success", "partial_failure", "failed"]
    matched_count: int
    warning_count: int
    outbox_ids: tuple[str, ...] = ()


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
        current_items = _current_items(batch, previous)
        changes = diff_items(previous, current_items)
        previous_by_id = {item.item_id: item for item in previous}
        current_by_id = {item.item_id: item for item in current_items}
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
        outbox_ids = self.runtime.apply_snapshot_and_deliveries(
            batch,
            candidates,
            lease=lease,
            worker_id=self.worker_id,
            return_new_only=True,
        )
        return RunResult(
            status=run_status,
            matched_count=matched_count,
            warning_count=len(batch.warnings),
            outbox_ids=tuple(outbox_ids),
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


def _current_items(batch: ObservationBatch, previous: list[ObservedItem]) -> list[ObservedItem]:
    if not batch.warnings:
        return list(batch.items)
    merged = {item.item_id: item for item in previous}
    merged.update((item.item_id, item) for item in batch.items)
    return [merged[item_id] for item_id in sorted(merged)]


def render_payload(spec: MonitorSpec, item: ObservedItem, match: RuleMatch) -> dict[str, object]:
    rental_payload = _rental_payload(spec, item, match)
    if rental_payload is not None:
        return rental_payload
    return {
        "text": (
            "모니터 조건에 맞는 변경이 감지되었습니다.\n"
            f"종류: {_RULE_KIND_LABELS[match.kind]}\n"
            f"출처: {_public_url(spec.target_url)}"
        )
    }


def _rental_payload(
    spec: MonitorSpec,
    item: ObservedItem,
    match: RuleMatch,
) -> dict[str, object] | None:
    if (
        spec.source_adapter is not SourceAdapterKind.PYTHON_PLUGIN
        or spec.adapter_ref != "rental_housing"
        or match.kind is not RuleKind.NEW_ITEM
    ):
        return None
    try:
        fields = item.fields
        source_id = fields.get("source_id")
        if source_id is not None:
            source_id = _rental_text(source_id, limit=500, allow_blank=True)
        announcement = Announcement(
            source_id=source_id,
            title=_rental_text(fields["title"], limit=1_000),
            agency=Agency(_rental_text(fields["agency"], limit=20)),
            region=_rental_text(fields["region"], limit=200),
            housing_type=HousingType(_rental_text(fields["housing_type"], limit=100)),
            target=_rental_text(fields["target"], limit=1_000),
            announcement_date=_rental_date(fields["announcement_date"]),
            application_start_date=_optional_rental_date(
                fields["application_start_date"]
            ),
            application_end_date=_optional_rental_date(fields["application_end_date"]),
            url=_rental_url(fields["url"], spec),
        )
        return {"text": format_announcement(announcement)}
    except (KeyError, TypeError, ValueError, MonitorError):
        return None


def _rental_text(value: object, *, limit: int, allow_blank: bool = False) -> str:
    if type(value) is not str or not 0 <= len(value) <= limit:
        raise ValueError
    normalized = value.strip()
    if (not allow_blank and not normalized) or any(
        unicodedata.category(character).startswith("C") for character in normalized
    ):
        raise ValueError
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError from None
    return normalized


def _rental_date(value: object) -> date:
    text = _rental_text(value, limit=10)
    parsed = date.fromisoformat(text)
    if parsed.isoformat() != text:
        raise ValueError
    return parsed


def _optional_rental_date(value: object) -> date | None:
    return None if value is None else _rental_date(value)


def _rental_url(value: object, spec: MonitorSpec) -> str:
    url = _rental_text(value, limit=2_048)
    if has_unsafe_url_characters(url):
        raise ValueError
    parts = urlsplit(url)
    hostname = parts.hostname
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or hostname is None
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError
    canonical_hostname = canonicalize_hostname(hostname)
    if canonical_hostname not in spec.validators.allowed_link_domains:
        raise ValueError
    try:
        port = parts.port
    except ValueError:
        raise ValueError from None
    if port is not None and port not in ALLOWED_PORTS:
        raise ValueError
    if any(
        is_sensitive_credential_name(name)
        for name, _ in parse_qsl(parts.query, keep_blank_values=True)
    ):
        raise ValueError
    return urlunsplit((parts.scheme.casefold(), parts.netloc, parts.path or "/", parts.query, ""))


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
