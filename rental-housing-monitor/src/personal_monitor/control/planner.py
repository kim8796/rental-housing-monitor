from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import math
import re
import secrets
import unicodedata
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final, NamedTuple
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from personal_monitor.ai.contracts import IntentKind, IntentResult, PlanRequest, PlanResult
from personal_monitor.ai.worker import CodexWorkerError
from personal_monitor.control.actions import ConsumedAction, PendingAction, PendingActionService
from personal_monitor.domain.observation import ObservedItem, Scalar, stable_item_id
from personal_monitor.domain.spec import (
    SENSITIVE_QUERY_PARAMETER_NAMES,
    FetchStrategy,
    FieldType,
    MonitorSpec,
    SourceAdapterKind,
)
from personal_monitor.engine.errors import MonitorError
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.extractor import DeclarativeExtractor
from personal_monitor.scraping.validator import ObservationValidator
from personal_monitor.security.robots import RobotsDecision
from personal_monitor.security.sanitize import sanitize_for_ai
from personal_monitor.security.secret_text import contains_sensitive_text, redact_sensitive_text
from personal_monitor.security.url_policy import (
    ResolvedTarget,
    canonicalize_hostname,
    is_public_address,
)
from personal_monitor.storage.schema import canonical_json, transaction
from personal_monitor.telegram.gateway import ControlRequest

_ATTEMPTS: Final = (
    ("gpt-5.6-terra", "medium"),
    ("gpt-5.6-terra", "medium"),
    ("gpt-5.6-sol", "high"),
)
_OWNER_RE: Final = re.compile(r"telegram-user:[1-9][0-9]{0,18}\Z")
_OPAQUE_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{32}\Z")
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_PROFILE_RE: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_WARNING_RE: Final = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_MAX_DOCUMENT_BYTES: Final = 10 * 1024 * 1024
_MAX_HEADERS: Final = 100
_MAX_REDIRECTS: Final = 5
_MAX_FIELDS: Final = 50
_ID_SOURCE: Final = secrets.token_hex
_SAFE_EXPLANATION: Final = "검증된 모니터 제안입니다."
_FEEDBACK_SCHEMA: Final = "candidate_schema_invalid"
_FEEDBACK_BINDING: Final = "candidate_binding_invalid"
_FEEDBACK_EXTRACT: Final = "candidate_extract_invalid"
_FEEDBACK_WORKER: Final = "worker_unavailable"
_CREDENTIAL_QUERY_NAMES: Final = frozenset(
    {
        *SENSITIVE_QUERY_PARAMETER_NAMES,
        "x-amz-credential",
        "x-amz-security-token",
        "x-amz-signature",
    }
)


class PlanningFailed(RuntimeError):
    """Fixed failure at the untrusted planning boundary."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("monitor planning failed")

    def __repr__(self) -> str:
        return "PlanningFailed(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ProbeResult:
    target: ResolvedTarget
    document: SourceDocument
    robots: RobotsDecision
    auth_profile_ref: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.warnings) is not tuple:
            raise ValueError("invalid probe result")

    def __repr__(self) -> str:
        return "<ProbeResult redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class PreviewItem:
    fields: Mapping[str, Scalar] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def __repr__(self) -> str:
        return "<PreviewItem redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class ProposedMonitor:
    spec: MonitorSpec
    preview_items: tuple[PreviewItem, ...]
    resolved_strategy: FetchStrategy
    robots: RobotsDecision
    warnings: tuple[str, ...]
    explanation: str
    candidate_version_id: str
    spec_hash: str
    pending_action: PendingAction
    _runtime_binding_hash: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "preview_items", tuple(self.preview_items))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def __repr__(self) -> str:
        return "<ProposedMonitor redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmedProposal:
    spec: MonitorSpec
    candidate_version_id: str
    spec_hash: str

    def __repr__(self) -> str:
        return "<ConfirmedProposal redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class _Dependency:
    root: object
    owner: object | None
    name: str | None
    call: object


class _ProposalSnapshot(NamedTuple):
    spec_json: str
    items_json: str
    probe_json: str
    document_body: bytes

    def __repr__(self) -> str:
        return "<_ProposalSnapshot redacted>"


class MonitorPlanner:
    __slots__ = (
        "_actions",
        "_actions_anchor",
        "_extractor",
        "_extractor_anchor",
        "_id_source",
        "_id_source_anchor",
        "_now_source",
        "_now_source_anchor",
        "_policy",
        "_policy_anchor",
        "_probe",
        "_probe_anchor",
        "_sanitizer",
        "_sanitizer_anchor",
        "_validator",
        "_validator_anchor",
        "_worker",
        "_worker_anchor",
    )

    def __init__(
        self,
        policy: object,
        probe: object,
        worker: object,
        actions: PendingActionService,
        *,
        extractor: object | None = None,
        validator: object | None = None,
        sanitizer: object = sanitize_for_ai,
        id_source: object = _ID_SOURCE,
        now_source: object | None = None,
    ) -> None:
        if now_source is None:
            from personal_monitor.storage.schema import utc_now

            now_source = utc_now
        candidate_extractor = DeclarativeExtractor() if extractor is None else extractor
        candidate_validator = ObservationValidator() if validator is None else validator
        anchors: tuple[_Dependency, ...] | None = None
        with suppress(Exception):
            if type(actions) is not PendingActionService:
                raise TypeError
            anchors = (
                _capture_dependency(policy, "validate"),
                _capture_dependency(probe, "probe"),
                _capture_dependency(worker, "run"),
                _capture_dependency(actions, "create"),
                _capture_dependency(candidate_extractor, "extract"),
                _capture_dependency(candidate_validator, "validate"),
                _capture_dependency(sanitizer, None),
                _capture_dependency(id_source, None),
                _capture_dependency(now_source, None),
            )
        if anchors is None:
            raise PlanningFailed
        (
            policy_anchor,
            probe_anchor,
            worker_anchor,
            actions_anchor,
            extractor_anchor,
            validator_anchor,
            sanitizer_anchor,
            id_source_anchor,
            now_source_anchor,
        ) = anchors
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(self, "_policy_anchor", policy_anchor)
        object.__setattr__(self, "_probe", probe)
        object.__setattr__(self, "_probe_anchor", probe_anchor)
        object.__setattr__(self, "_worker", worker)
        object.__setattr__(self, "_worker_anchor", worker_anchor)
        object.__setattr__(self, "_actions", actions)
        object.__setattr__(self, "_actions_anchor", actions_anchor)
        object.__setattr__(self, "_extractor", candidate_extractor)
        object.__setattr__(self, "_extractor_anchor", extractor_anchor)
        object.__setattr__(self, "_validator", candidate_validator)
        object.__setattr__(self, "_validator_anchor", validator_anchor)
        object.__setattr__(self, "_sanitizer", sanitizer)
        object.__setattr__(self, "_sanitizer_anchor", sanitizer_anchor)
        object.__setattr__(self, "_id_source", id_source)
        object.__setattr__(self, "_id_source_anchor", id_source_anchor)
        object.__setattr__(self, "_now_source", now_source)
        object.__setattr__(self, "_now_source_anchor", now_source_anchor)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("MonitorPlanner composition is sealed")

    def __repr__(self) -> str:
        return "<MonitorPlanner redacted>"

    async def propose(
        self,
        request: ControlRequest,
        intent: IntentResult,
    ) -> ProposedMonitor:
        validated_input = _validate_input(request, intent)
        if validated_input is None or not self._all_dependencies_intact():
            raise PlanningFailed
        safe_request, safe_intent = validated_input
        target = await self._validate_target(safe_intent.target_url)
        result = await self._probe_once(safe_request.owner_id, target)
        projection = _project_worker_input(safe_request, safe_intent, result)
        if projection is None:
            raise PlanningFailed
        projected_message, projected_intent, secrets_to_remove = projection
        sanitized = self._sanitize(result, secrets_to_remove)

        feedback: list[str] = []
        for model, effort in _ATTEMPTS:
            plan_request = self._plan_request(
                owner_id=safe_request.owner_id,
                message=projected_message,
                intent=projected_intent,
                sanitized=sanitized,
                feedback=feedback,
                forbidden=secrets_to_remove,
            )
            worker_result: object | None = None
            worker_failed = False
            local_failed = False
            if not _dependency_intact(self._worker, self._worker_anchor):
                raise PlanningFailed
            try:
                worker_result = await self._worker_anchor.call(
                    plan_request,
                    model=model,
                    effort=effort,
                )
            except asyncio.CancelledError:
                raise
            except CodexWorkerError as error:
                if type(error) is CodexWorkerError:
                    worker_failed = True
                else:
                    local_failed = True
            except Exception:
                local_failed = True
            if local_failed:
                raise PlanningFailed
            if worker_failed:
                feedback.append(_FEEDBACK_WORKER)
                continue

            candidate, category = self._validate_candidate(
                worker_result,
                request=safe_request,
                intent=safe_intent,
                projected_intent=projected_intent,
                probe=result,
                forbidden=secrets_to_remove,
            )
            if candidate is None:
                feedback.append(category)
                continue
            spec_value, items = candidate
            return self._persist_proposal(spec_value, items, result)
        raise PlanningFailed

    async def _validate_target(self, url: str | None) -> ResolvedTarget:
        if type(url) is not str or not _dependency_intact(self._policy, self._policy_anchor):
            raise PlanningFailed
        raw: object | None = None
        failed = False
        try:
            raw = await self._policy_anchor.call(url)
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True
        target = None if failed else _fresh_target(raw, expected_url=url)
        if target is None:
            raise PlanningFailed
        return target

    async def _probe_once(self, owner_id: str, target: ResolvedTarget) -> ProbeResult:
        if not _dependency_intact(self._probe, self._probe_anchor):
            raise PlanningFailed
        raw: object | None = None
        failed = False
        try:
            raw = await self._probe_anchor.call(owner_id, target)
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True
        result = None if failed else _fresh_probe(raw, owner_id=owner_id, target=target)
        if result is None:
            raise PlanningFailed
        return result

    def _sanitize(self, probe: ProbeResult, secrets_to_remove: tuple[str, ...]) -> str:
        if not _dependency_intact(self._sanitizer, self._sanitizer_anchor):
            raise PlanningFailed
        decoded: str | None = None
        with suppress(Exception):
            if probe.document.content_type == "application/json" or (
                probe.document.content_type.endswith("+json")
            ):
                decoded = probe.document.body.decode("utf-8", errors="strict")
            else:
                decoded = probe.document.body.decode("utf-8", errors="replace")
        if decoded is None:
            raise PlanningFailed
        sanitized: object | None = None
        failed = False
        try:
            sanitized = self._sanitizer_anchor.call(
                decoded,
                secret_values=secrets_to_remove,
            )
        except Exception:
            failed = True
        if (
            failed
            or type(sanitized) is not str
            or len(sanitized) > 40_000
            or not _safe_unicode(
                sanitized,
                max_chars=40_000,
                allow_empty=True,
                allow_layout_controls=True,
            )
        ):
            raise PlanningFailed
        sanitized = redact_sensitive_text(sanitized)
        if not _worker_text_is_clean(sanitized, secrets_to_remove):
            raise PlanningFailed
        return sanitized

    def _plan_request(
        self,
        *,
        owner_id: str,
        message: str,
        intent: IntentResult,
        sanitized: str,
        feedback: list[str],
        forbidden: tuple[str, ...],
    ) -> PlanRequest:
        request_id = self._new_id()
        value: PlanRequest | None = None
        with suppress(Exception):
            value = PlanRequest(
                request_id=request_id,
                owner_id=owner_id,
                message=message,
                intent=intent,
                sanitized_document=sanitized,
                observed_preview_values=list(feedback),
            )
        if value is None:
            raise PlanningFailed
        if not _worker_request_is_clean(value, forbidden):
            raise PlanningFailed
        return value

    def _new_id(self) -> str:
        if not _dependency_intact(self._id_source, self._id_source_anchor):
            raise PlanningFailed
        value: object | None = None
        failed = False
        try:
            if self._id_source_anchor.call is _ID_SOURCE:
                value = self._id_source_anchor.call(16)
            else:
                value = self._id_source_anchor.call()
        except Exception:
            failed = True
        if failed or type(value) is not str or _OPAQUE_ID_RE.fullmatch(value) is None:
            raise PlanningFailed
        return value

    def _validate_candidate(
        self,
        raw: object,
        *,
        request: ControlRequest,
        intent: IntentResult,
        projected_intent: IntentResult,
        probe: ProbeResult,
        forbidden: tuple[str, ...],
    ) -> tuple[tuple[MonitorSpec, tuple[ObservedItem, ...]] | None, str]:
        fresh = _fresh_plan_result(raw)
        if fresh is None:
            return None, _FEEDBACK_SCHEMA
        spec = fresh.spec
        if (
            spec.owner_id != request.owner_id
            or spec.target_url != projected_intent.target_url
            or spec.source_adapter is not SourceAdapterKind.SCRAPLING
            or spec.adapter_ref is not None
            or spec.auth_profile_ref is not None
            or spec.fetch_strategy not in {FetchStrategy.AUTO, probe.document.strategy}
            or len(spec.extract.fields) > _MAX_FIELDS
            or _contains_forbidden_value(spec.model_dump(mode="python"), forbidden)
        ):
            return None, _FEEDBACK_BINDING
        if not _dependency_intact(
            self._extractor, self._extractor_anchor
        ) or not _dependency_intact(
            self._validator,
            self._validator_anchor,
        ):
            raise PlanningFailed
        extracted: object | None = None
        invalid = False
        local_failed = False
        try:
            extracted = self._extractor_anchor.call(probe.document, spec.extract)
        except MonitorError as error:
            if type(error) is MonitorError:
                invalid = True
            else:
                local_failed = True
        except Exception:
            local_failed = True
        if local_failed:
            raise PlanningFailed
        if invalid:
            return None, _FEEDBACK_EXTRACT

        validated: object | None = None
        try:
            validated = self._validator_anchor.call(extracted, spec.extract, spec.validators)
        except MonitorError as error:
            if type(error) is MonitorError:
                invalid = True
            else:
                local_failed = True
        except Exception:
            local_failed = True
        if local_failed:
            raise PlanningFailed
        if (
            invalid
            or type(validated) is not tuple
            or any(type(item) is not ObservedItem for item in validated)
        ):
            return None, _FEEDBACK_EXTRACT
        bound = _bind_application_spec(
            spec,
            owner_id=request.owner_id,
            target_url=intent.target_url,
            strategy=probe.document.strategy,
            auth_profile_ref=probe.auth_profile_ref,
        )
        if bound is None:
            return None, _FEEDBACK_BINDING
        fresh_items = _fresh_observations(validated, bound)
        if fresh_items is None:
            return None, _FEEDBACK_EXTRACT
        return (bound, fresh_items), ""

    def _persist_proposal(
        self,
        spec: MonitorSpec,
        items: tuple[ObservedItem, ...],
        probe: ProbeResult,
    ) -> ProposedMonitor:
        snapshot = _capture_proposal_snapshot(spec, items, probe)
        if snapshot is None:
            raise PlanningFailed
        candidate_version_id = self._new_id()
        if not _dependency_intact(self._now_source, self._now_source_anchor):
            raise PlanningFailed
        now: datetime | None = None
        try:
            now = _fresh_action_time(self._now_source_anchor.call())
        except Exception:
            now = None
        if now is None or not self._all_dependencies_intact():
            raise PlanningFailed
        rebuilt = _rebuild_proposal_inputs(
            snapshot,
            live_spec=spec,
            live_items=items,
            live_probe=probe,
        )
        if rebuilt is None:
            raise PlanningFailed
        fresh_spec, fresh_items, fresh_probe = rebuilt
        spec_json = fresh_spec.model_dump(mode="json")
        digest = hashlib.sha256(canonical_json(spec_json).encode("utf-8")).hexdigest()
        previews: tuple[PreviewItem, ...] | None = None
        with suppress(Exception):
            previews = tuple(_preview_item(item, fresh_spec) for item in fresh_items[:3])
        if previews is None or not _valid_previews(previews, fresh_spec):
            raise PlanningFailed
        payload = {
            "candidate_version_id": candidate_version_id,
            "spec_hash": digest,
            "binding_hash": _proposal_binding(
                fresh_spec.owner_id,
                candidate_version_id,
                digest,
            ),
            "spec": spec_json,
        }
        runtime_material = _runtime_material(
            fresh_spec.owner_id,
            candidate_version_id,
            digest,
            previews,
            fresh_probe.document.strategy,
            fresh_probe.robots,
            fresh_probe.warnings,
            _SAFE_EXPLANATION,
        )
        action_input = _fresh_action_input(payload)
        if action_input is None:
            raise PlanningFailed
        actions = self._actions_anchor.root
        if type(actions) is not PendingActionService:
            raise PlanningFailed
        pending: object | None = None
        proposal: ProposedMonitor | None = None
        try:
            with transaction(actions.connection):
                pending = self._actions_anchor.call(
                    fresh_spec.owner_id,
                    "create",
                    action_input,
                    now=now,
                )
                if type(pending) is not PendingAction:
                    raise ValueError
                proposal = ProposedMonitor(
                    spec=fresh_spec,
                    preview_items=previews,
                    resolved_strategy=fresh_probe.document.strategy,
                    robots=fresh_probe.robots,
                    warnings=fresh_probe.warnings,
                    explanation=_SAFE_EXPLANATION,
                    candidate_version_id=candidate_version_id,
                    spec_hash=digest,
                    pending_action=pending,
                    _runtime_binding_hash=_complete_runtime_binding(
                        runtime_material,
                        pending,
                    ),
                )
                if not _valid_proposal(proposal):
                    raise ValueError
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PlanningFailed from None
        if proposal is None:
            raise PlanningFailed
        return proposal

    def _all_dependencies_intact(self) -> bool:
        try:
            return all(
                (
                    _dependency_intact(self._policy, self._policy_anchor),
                    _dependency_intact(self._probe, self._probe_anchor),
                    _dependency_intact(self._worker, self._worker_anchor),
                    _dependency_intact(self._actions, self._actions_anchor),
                    _dependency_intact(self._extractor, self._extractor_anchor),
                    _dependency_intact(self._validator, self._validator_anchor),
                    _dependency_intact(self._sanitizer, self._sanitizer_anchor),
                    _dependency_intact(self._id_source, self._id_source_anchor),
                    _dependency_intact(self._now_source, self._now_source_anchor),
                )
            )
        except Exception:
            return False


def reconstruct_confirmed_spec(
    action: ConsumedAction,
    *,
    owner_id: str,
) -> ConfirmedProposal:
    result: ConfirmedProposal | None = None
    with suppress(Exception):
        if (
            type(action) is not ConsumedAction
            or action.action != "create"
            or type(owner_id) is not str
            or _OWNER_RE.fullmatch(owner_id) is None
            or type(action.payload) not in {dict, MappingProxyType}
            or set(action.payload) != {"candidate_version_id", "spec_hash", "binding_hash", "spec"}
            or not isinstance(action.payload["spec"], Mapping)
        ):
            raise ValueError
        candidate_version_id = action.payload["candidate_version_id"]
        spec_hash = action.payload["spec_hash"]
        binding_hash = action.payload["binding_hash"]
        if (
            type(candidate_version_id) is not str
            or _OPAQUE_ID_RE.fullmatch(candidate_version_id) is None
            or type(spec_hash) is not str
            or _HASH_RE.fullmatch(spec_hash) is None
            or type(binding_hash) is not str
            or _HASH_RE.fullmatch(binding_hash) is None
            or binding_hash != _proposal_binding(owner_id, candidate_version_id, spec_hash)
        ):
            raise ValueError
        value = MonitorSpec.model_validate(_thaw_json(action.payload["spec"]))
        canonical = canonical_json(value.model_dump(mode="json"))
        if (
            value.owner_id != owner_id
            or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != spec_hash
        ):
            raise ValueError
        result = ConfirmedProposal(value, candidate_version_id, spec_hash)
    if result is None:
        raise PlanningFailed
    return result


def _capture_dependency(value: object, method_name: str | None) -> _Dependency:
    if inspect.ismethod(value) and value.__self__ is not None:
        return _Dependency(value, value.__self__, value.__name__, value)
    if inspect.isfunction(value) or inspect.isbuiltin(value):
        return _Dependency(value, None, None, value)
    if method_name is not None:
        with suppress(Exception):
            method = getattr(value, method_name)
            if callable(method):
                return _Dependency(value, value, method_name, method)
    call = value.__call__  # noqa: B004
    if not callable(call):
        raise TypeError
    return _Dependency(value, value, "__call__", call)


def _dependency_intact(value: object, anchor: _Dependency) -> bool:
    try:
        if anchor.owner is None:
            return value is anchor.root and anchor.call is value and callable(value)
        if value is not anchor.root:
            return False
        current = getattr(anchor.owner, anchor.name)
        return _same_callable(anchor.call, current, anchor.owner)
    except Exception:
        return False


def _same_callable(captured: object, current: object, owner: object) -> bool:
    if not callable(captured) or not callable(current):
        return False
    if current is captured:
        return True
    return (
        getattr(captured, "__self__", None) is owner
        and getattr(current, "__self__", None) is owner
        and getattr(captured, "__func__", None) is getattr(current, "__func__", None)
    )


def _validate_input(
    request: object,
    intent: object,
) -> tuple[ControlRequest, IntentResult] | None:
    value: tuple[ControlRequest, IntentResult] | None = None
    with suppress(Exception):
        if type(request) is not ControlRequest or type(intent) is not IntentResult:
            raise TypeError
        fresh_request = ControlRequest(request.owner_id, request.chat_id, request.text)
        fresh_intent = IntentResult.model_validate(intent.model_dump(mode="python"))
        if (
            fresh_intent.kind is not IntentKind.CREATE
            or fresh_intent.target_monitor_ids != []
            or fresh_intent.target_url is None
            or fresh_intent.clarification is not None
            or type(fresh_intent.confidence) is not float
            or not math.isfinite(fresh_intent.confidence)
            or fresh_intent.confidence < 0.75
            or not _safe_web_url(fresh_intent.target_url, reject_sensitive_query=True)
        ):
            raise ValueError
        value = fresh_request, fresh_intent
    return value


def _project_worker_input(
    request: ControlRequest,
    intent: IntentResult,
    probe: ProbeResult,
) -> tuple[str, IntentResult, tuple[str, ...]] | None:
    result: tuple[str, IntentResult, tuple[str, ...]] | None = None
    with suppress(Exception):
        original_url = intent.target_url
        if type(original_url) is not str:
            raise ValueError
        safe_url = _redact_url(original_url)
        parts = urlsplit(original_url)
        secrets_to_remove: list[str] = []
        if parts.query:
            secrets_to_remove.append(original_url)
            secrets_to_remove.append(parts.query)
        for name, value in parse_qsl(parts.query, keep_blank_values=True):
            pair = f"{name}={value}"
            if pair != "=":
                secrets_to_remove.append(pair)
            normalized_name = name.casefold()
            credential_name = (
                normalized_name in _CREDENTIAL_QUERY_NAMES or normalized_name.startswith("x-amz-")
            )
            if value and (credential_name or len(value) >= 8):
                secrets_to_remove.append(value)
            if credential_name:
                secrets_to_remove.append(name)
        for pair in parts.query.split("&"):
            if pair:
                secrets_to_remove.append(pair)
            if "=" in pair:
                raw_name, raw_value = pair.split("=", 1)
                normalized_raw_name = raw_name.casefold()
                if raw_value and (
                    len(raw_value) >= 8
                    or normalized_raw_name in _CREDENTIAL_QUERY_NAMES
                    or normalized_raw_name.startswith("x-amz-")
                ):
                    secrets_to_remove.append(raw_value)
        if probe.auth_profile_ref is not None:
            secrets_to_remove.append(probe.auth_profile_ref)
        forbidden = tuple(
            sorted(
                {value for value in secrets_to_remove if value},
                key=lambda value: (-len(value), value),
            )
        )
        message = _project_text(request.text, original_url, safe_url, forbidden)
        condition = _project_optional_text(
            intent.condition_text,
            original_url,
            safe_url,
            forbidden,
        )
        schedule = _project_optional_text(
            intent.schedule_text,
            original_url,
            safe_url,
            forbidden,
        )
        projected = IntentResult(
            kind=IntentKind.CREATE,
            target_monitor_ids=[],
            target_url=safe_url,
            condition_text=condition,
            schedule_text=schedule,
            clarification=None,
            confidence=intent.confidence,
        )
        if (
            not _worker_text_is_clean(message, forbidden)
            or not _worker_text_is_clean(projected.target_url, forbidden)
            or (condition is not None and not _worker_text_is_clean(condition, forbidden))
            or (schedule is not None and not _worker_text_is_clean(schedule, forbidden))
        ):
            raise ValueError
        result = message, projected, forbidden
    return result


def _project_optional_text(
    value: str | None,
    original_url: str,
    safe_url: str,
    forbidden: tuple[str, ...],
) -> str | None:
    if value is None:
        return None
    return _project_text(value, original_url, safe_url, forbidden)


def _project_text(
    value: str,
    original_url: str,
    safe_url: str,
    forbidden: tuple[str, ...],
) -> str:
    projected = value.replace(original_url, safe_url)
    for secret in forbidden:
        projected = projected.replace(secret, "[숨김]")
    projected = redact_sensitive_text(projected)
    if not _safe_unicode(projected, max_chars=2_000):
        raise ValueError
    return projected


def _worker_request_is_clean(value: PlanRequest, forbidden: tuple[str, ...]) -> bool:
    try:
        intent = value.intent
        target_url = intent.target_url
        surfaces = (
            value.message,
            value.sanitized_document,
            intent.condition_text,
            intent.schedule_text,
        )
        return (
            type(value) is PlanRequest
            and _OPAQUE_ID_RE.fullmatch(value.request_id) is not None
            and _OWNER_RE.fullmatch(value.owner_id) is not None
            and intent.kind is IntentKind.CREATE
            and intent.target_monitor_ids == []
            and type(target_url) is str
            and _safe_web_url(target_url)
            and not urlsplit(target_url).query
            and not urlsplit(target_url).fragment
            and _worker_text_is_clean(target_url, ())
            and all(
                item is None or (type(item) is str and _worker_text_is_clean(item, forbidden))
                for item in surfaces
            )
            and all(
                item
                in {
                    _FEEDBACK_SCHEMA,
                    _FEEDBACK_BINDING,
                    _FEEDBACK_EXTRACT,
                    _FEEDBACK_WORKER,
                }
                for item in value.observed_preview_values
            )
        )
    except Exception:
        return False


def _nested_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return tuple(item for nested in value.values() for item in _nested_strings(nested))
    if isinstance(value, list | tuple):
        return tuple(item for nested in value for item in _nested_strings(nested))
    return (value,) if type(value) is str else ()


def _worker_text_is_clean(value: str, forbidden: tuple[str, ...]) -> bool:
    try:
        folded = value.casefold()
        return not contains_sensitive_text(value) and all(
            secret.casefold() not in folded for secret in forbidden
        )
    except Exception:
        return False


def _contains_forbidden_value(value: object, forbidden: tuple[str, ...]) -> bool:
    try:
        return any(
            secret.casefold() in item.casefold()
            for item in _nested_strings(value)
            for secret in forbidden
        )
    except Exception:
        return True


def _bind_application_spec(
    candidate: MonitorSpec,
    *,
    owner_id: str,
    target_url: str | None,
    strategy: FetchStrategy,
    auth_profile_ref: str | None,
) -> MonitorSpec | None:
    result: MonitorSpec | None = None
    with suppress(Exception):
        if type(candidate) is not MonitorSpec or type(target_url) is not str:
            raise TypeError
        payload = candidate.model_dump(mode="json")
        payload.update(
            {
                "owner_id": owner_id,
                "target_url": target_url,
                "source_adapter": SourceAdapterKind.SCRAPLING.value,
                "adapter_ref": None,
                "fetch_strategy": strategy.value,
                "auth_profile_ref": auth_profile_ref,
            }
        )
        result = MonitorSpec.model_validate(payload)
    return result


def _fresh_observations(
    values: object,
    spec: MonitorSpec,
) -> tuple[ObservedItem, ...] | None:
    result: tuple[ObservedItem, ...] | None = None
    with suppress(Exception):
        if type(values) is not tuple:
            raise TypeError
        independently_validated = ObservationValidator().validate(
            values,
            spec.extract,
            spec.validators,
        )
        copied: list[ObservedItem] = []
        for item in independently_validated:
            if type(item) is not ObservedItem or not isinstance(item.fields, Mapping):
                raise TypeError
            fields = dict(item.fields)
            expected_id = stable_item_id(fields)
            if type(item.item_id) is not str or item.item_id != expected_id:
                raise ValueError
            copied.append(ObservedItem(expected_id, fields))
        result = ObservationValidator().validate(
            tuple(copied),
            spec.extract,
            spec.validators,
        )
    return result


def _capture_proposal_snapshot(
    spec: MonitorSpec,
    items: tuple[ObservedItem, ...],
    probe: ProbeResult,
) -> _ProposalSnapshot | None:
    result: _ProposalSnapshot | None = None
    with suppress(Exception):
        if (
            type(spec) is not MonitorSpec
            or type(items) is not tuple
            or type(probe) is not ProbeResult
            or type(probe.document.body) is not bytes
        ):
            raise TypeError
        result = _ProposalSnapshot(
            spec_json=canonical_json(spec.model_dump(mode="json")),
            items_json=canonical_json(
                [
                    {"item_id": item.item_id, "fields": dict(item.fields)}
                    for item in items
                    if type(item) is ObservedItem
                ]
            ),
            probe_json=canonical_json(_probe_primitives(probe)),
            document_body=bytes(probe.document.body),
        )
        if len(json.loads(result.items_json)) != len(items):
            raise ValueError
    return result


def _probe_primitives(probe: ProbeResult) -> dict[str, object]:
    return {
        "target": {
            "normalized_url": probe.target.normalized_url,
            "hostname": probe.target.hostname,
            "port": probe.target.port,
            "addresses": sorted(probe.target.addresses),
        },
        "document": {
            "final_url": probe.document.final_url,
            "status": probe.document.status,
            "content_type": probe.document.content_type,
            "headers": dict(probe.document.headers),
            "body_hash": hashlib.sha256(probe.document.body).hexdigest(),
            "strategy": probe.document.strategy.value,
            "redirect_urls": list(probe.document.redirect_urls),
            "redirect_location": probe.document.redirect_location,
            "peer_ip": probe.document.peer_ip,
        },
        "robots": {
            "allowed": probe.robots.allowed,
            "crawl_delay_seconds": probe.robots.crawl_delay_seconds,
            "checked_at": probe.robots.checked_at.isoformat(),
            "policy_fetched": probe.robots.policy_fetched,
        },
        "auth_profile_ref": probe.auth_profile_ref,
        "warnings": list(probe.warnings),
    }


def _rebuild_proposal_inputs(
    snapshot: _ProposalSnapshot,
    *,
    live_spec: MonitorSpec,
    live_items: tuple[ObservedItem, ...],
    live_probe: ProbeResult,
) -> tuple[MonitorSpec, tuple[ObservedItem, ...], ProbeResult] | None:
    result: tuple[MonitorSpec, tuple[ObservedItem, ...], ProbeResult] | None = None
    with suppress(Exception):
        if _capture_proposal_snapshot(live_spec, live_items, live_probe) != snapshot:
            raise ValueError
        spec_payload = json.loads(snapshot.spec_json)
        fresh_spec = MonitorSpec.model_validate(spec_payload)
        if canonical_json(fresh_spec.model_dump(mode="json")) != snapshot.spec_json:
            raise ValueError

        item_payload = json.loads(snapshot.items_json)
        if type(item_payload) is not list:
            raise TypeError
        copied_items: list[ObservedItem] = []
        for item in item_payload:
            if (
                type(item) is not dict
                or set(item) != {"item_id", "fields"}
                or type(item["item_id"]) is not str
                or type(item["fields"]) is not dict
            ):
                raise TypeError
            copied_items.append(ObservedItem(item["item_id"], item["fields"]))
        fresh_items = _fresh_observations(tuple(copied_items), fresh_spec)
        if fresh_items is None:
            raise ValueError

        probe_payload = json.loads(snapshot.probe_json)
        fresh_probe = _probe_from_primitives(
            probe_payload,
            snapshot.document_body,
            owner_id=fresh_spec.owner_id,
        )
        if (
            fresh_probe is None
            or fresh_probe.target.normalized_url != fresh_spec.target_url
            or canonical_json(_probe_primitives(fresh_probe)) != snapshot.probe_json
        ):
            raise ValueError
        result = fresh_spec, fresh_items, fresh_probe
    return result


def _probe_from_primitives(
    value: object,
    body: bytes,
    *,
    owner_id: str,
) -> ProbeResult | None:
    result: ProbeResult | None = None
    with suppress(Exception):
        if type(value) is not dict:
            raise TypeError
        target_value = value["target"]
        document_value = value["document"]
        robots_value = value["robots"]
        if (
            type(target_value) is not dict
            or type(document_value) is not dict
            or type(robots_value) is not dict
            or type(body) is not bytes
            or hashlib.sha256(body).hexdigest() != document_value["body_hash"]
        ):
            raise TypeError
        target = ResolvedTarget(
            normalized_url=target_value["normalized_url"],
            hostname=target_value["hostname"],
            port=target_value["port"],
            addresses=frozenset(target_value["addresses"]),
        )
        document = SourceDocument(
            final_url=document_value["final_url"],
            status=document_value["status"],
            content_type=document_value["content_type"],
            headers=document_value["headers"],
            body=body,
            strategy=FetchStrategy(document_value["strategy"]),
            redirect_urls=tuple(document_value["redirect_urls"]),
            redirect_location=document_value["redirect_location"],
            peer_ip=document_value["peer_ip"],
        )
        robots = RobotsDecision(
            allowed=robots_value["allowed"],
            crawl_delay_seconds=robots_value["crawl_delay_seconds"],
            checked_at=datetime.fromisoformat(robots_value["checked_at"]),
            policy_fetched=robots_value["policy_fetched"],
        )
        raw = ProbeResult(
            target=target,
            document=document,
            robots=robots,
            auth_profile_ref=value["auth_profile_ref"],
            warnings=tuple(value["warnings"]),
        )
        result = _fresh_probe(raw, owner_id=owner_id, target=target)
    return result


def _fresh_action_input(value: object) -> dict[str, object] | None:
    result: dict[str, object] | None = None
    with suppress(Exception):
        canonical = canonical_json(value)
        rebuilt = json.loads(canonical)
        if type(rebuilt) is not dict or canonical_json(rebuilt) != canonical:
            raise TypeError
        result = rebuilt
    return result


def _fresh_target(value: object, *, expected_url: str) -> ResolvedTarget | None:
    target: ResolvedTarget | None = None
    with suppress(Exception):
        if type(value) is not ResolvedTarget:
            raise TypeError
        if (
            type(value.normalized_url) is not str
            or value.normalized_url != expected_url
            or not _safe_web_url(value.normalized_url, reject_sensitive_query=True)
            or type(value.hostname) is not str
            or canonicalize_hostname(value.hostname) != value.hostname
            or type(value.port) is not int
            or value.port not in {80, 443}
            or type(value.addresses) is not frozenset
            or not 1 <= len(value.addresses) <= 32
        ):
            raise ValueError
        parts = urlsplit(value.normalized_url)
        expected_port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.hostname is None or canonicalize_hostname(parts.hostname) != value.hostname:
            raise ValueError
        if value.port != expected_port:
            raise ValueError
        addresses: set[str] = set()
        for address in value.addresses:
            if type(address) is not str:
                raise ValueError
            parsed = ipaddress.ip_address(address)
            canonical = (
                parsed.ipv4_mapped.compressed
                if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None
                else parsed.compressed
            )
            if address != canonical or not is_public_address(address):
                raise ValueError
            addresses.add(address)
        target = ResolvedTarget(
            normalized_url=value.normalized_url,
            hostname=value.hostname,
            port=value.port,
            addresses=frozenset(addresses),
        )
    return target


def _fresh_probe(
    value: object,
    *,
    owner_id: str,
    target: ResolvedTarget,
) -> ProbeResult | None:
    result: ProbeResult | None = None
    with suppress(Exception):
        if type(value) is not ProbeResult or _OWNER_RE.fullmatch(owner_id) is None:
            raise TypeError
        fresh_target = _fresh_target(value.target, expected_url=target.normalized_url)
        if fresh_target != target:
            raise ValueError
        document = _fresh_document(value.document, target)
        robots = _fresh_robots(value.robots)
        profile = value.auth_profile_ref
        if profile is not None and (
            type(profile) is not str or _PROFILE_RE.fullmatch(profile) is None
        ):
            raise ValueError
        if type(value.warnings) is not tuple or len(value.warnings) > 20:
            raise ValueError
        warnings = tuple(value.warnings)
        if any(type(item) is not str or _WARNING_RE.fullmatch(item) is None for item in warnings):
            raise ValueError
        result = ProbeResult(fresh_target, document, robots, profile, warnings)
    return result


def _fresh_document(value: object, target: ResolvedTarget) -> SourceDocument:
    if (
        type(value) is not SourceDocument
        or type(value.status) is not int
        or not 200 <= value.status <= 299
        or type(value.content_type) is not str
        or value.content_type not in {"text/html", "application/xhtml+xml", "application/json"}
        or type(value.body) is not bytes
        or not 1 <= len(value.body) <= _MAX_DOCUMENT_BYTES
        or type(value.strategy) is not FetchStrategy
        or value.strategy not in {FetchStrategy.HTTP, FetchStrategy.DYNAMIC, FetchStrategy.STEALTHY}
        or type(value.redirect_urls) is not tuple
        or len(value.redirect_urls) > _MAX_REDIRECTS
        or not _safe_web_url(value.final_url)
        or _web_origin(value.final_url) != _web_origin(target.normalized_url)
    ):
        raise ValueError
    redirects = tuple(value.redirect_urls)
    target_origin = _web_origin(target.normalized_url)
    if any(not _safe_web_url(item) or _web_origin(item) != target_origin for item in redirects):
        raise ValueError
    if value.redirect_location is not None and (
        not _safe_web_url(value.redirect_location)
        or _web_origin(value.redirect_location) != target_origin
    ):
        raise ValueError
    peer = value.peer_ip
    if peer is not None and (
        type(peer) is not str or peer not in target.addresses or not is_public_address(peer)
    ):
        raise ValueError
    if not isinstance(value.headers, Mapping) or len(value.headers) > _MAX_HEADERS:
        raise ValueError
    headers: dict[str, str] = {}
    total_header_bytes = 0
    for name, header_value in value.headers.items():
        if (
            type(name) is not str
            or type(header_value) is not str
            or not _safe_unicode(name, max_chars=256)
            or not _safe_unicode(header_value, max_chars=4096)
        ):
            raise ValueError
        total_header_bytes += len(name.encode()) + len(header_value.encode())
        headers[name] = header_value
    if total_header_bytes > 65_536:
        raise ValueError
    return SourceDocument(
        final_url=value.final_url,
        status=value.status,
        content_type=value.content_type,
        headers=headers,
        body=value.body,
        strategy=value.strategy,
        redirect_urls=redirects,
        redirect_location=value.redirect_location,
        peer_ip=peer,
    )


def _fresh_robots(value: object) -> RobotsDecision:
    if (
        type(value) is not RobotsDecision
        or type(value.allowed) is not bool
        or value.allowed is not True
        or type(value.policy_fetched) is not bool
        or type(value.checked_at) is not datetime
        or value.checked_at.tzinfo is None
        or value.checked_at.utcoffset() is None
    ):
        raise ValueError
    delay = value.crawl_delay_seconds
    if delay is not None and (
        type(delay) is not float or not math.isfinite(delay) or not 0 <= delay <= 86_400
    ):
        raise ValueError
    return RobotsDecision(True, delay, value.checked_at, value.policy_fetched)


def _fresh_plan_result(value: object) -> PlanResult | None:
    result: PlanResult | None = None
    with suppress(Exception):
        if type(value) is not PlanResult:
            raise TypeError
        dumped = value.model_dump(mode="json")
        result = PlanResult.model_validate(dumped)
        if type(result.spec) is not MonitorSpec or not _safe_unicode(
            result.explanation, max_chars=1_000
        ):
            result = None
    return result


def _preview_item(item: ObservedItem, spec: MonitorSpec) -> PreviewItem:
    fields: dict[str, Scalar] = {}
    for name in sorted(item.fields):
        if len(fields) >= 8:
            break
        value = item.fields[name]
        field_spec = spec.extract.fields.get(name)
        if field_spec is None:
            continue
        if field_spec.type is FieldType.URL and isinstance(value, str):
            value = _redact_url(value)
        elif isinstance(value, str):
            value = _bounded_plain_text(value, limit=120)
        fields[name] = value
    return PreviewItem(fields)


def _valid_proposal(value: object) -> bool:
    try:
        if type(value) is not ProposedMonitor or type(value.spec) is not MonitorSpec:
            return False
        fresh_spec = MonitorSpec.model_validate(value.spec.model_dump(mode="json"))
        canonical = canonical_json(fresh_spec.model_dump(mode="json"))
        fresh_robots = _fresh_robots(value.robots)
        return (
            fresh_spec == value.spec
            and type(value.preview_items) is tuple
            and len(value.preview_items) <= 3
            and _valid_previews(value.preview_items, fresh_spec)
            and type(value.resolved_strategy) is FetchStrategy
            and value.resolved_strategy
            in {FetchStrategy.HTTP, FetchStrategy.DYNAMIC, FetchStrategy.STEALTHY}
            and fresh_robots == value.robots
            and type(value.warnings) is tuple
            and len(value.warnings) <= 20
            and all(_WARNING_RE.fullmatch(item) is not None for item in value.warnings)
            and value.explanation == _SAFE_EXPLANATION
            and _OPAQUE_ID_RE.fullmatch(value.candidate_version_id) is not None
            and _HASH_RE.fullmatch(value.spec_hash) is not None
            and hashlib.sha256(canonical.encode("utf-8")).hexdigest() == value.spec_hash
            and type(value.pending_action) is PendingAction
            and _OPAQUE_ID_RE.fullmatch(value.pending_action.token) is not None
            and type(value.pending_action.expires_at) is datetime
            and value.pending_action.expires_at.tzinfo is not None
            and value.pending_action.expires_at.utcoffset() is not None
            and value.pending_action.confirm_callback == f"confirm:{value.pending_action.token}"
            and value.pending_action.cancel_callback == f"cancel:{value.pending_action.token}"
            and type(value._runtime_binding_hash) is str
            and value._runtime_binding_hash
            == _complete_runtime_binding(
                _runtime_material(
                    fresh_spec.owner_id,
                    value.candidate_version_id,
                    value.spec_hash,
                    value.preview_items,
                    value.resolved_strategy,
                    value.robots,
                    value.warnings,
                    value.explanation,
                ),
                value.pending_action,
            )
        )
    except Exception:
        return False


def _valid_previews(values: tuple[PreviewItem, ...], spec: MonitorSpec) -> bool:
    try:
        for item in values:
            if type(item) is not PreviewItem or type(item.fields) is not MappingProxyType:
                return False
            if len(item.fields) > 8:
                return False
            for name, value in item.fields.items():
                field_spec = spec.extract.fields.get(name)
                if field_spec is None or not _valid_preview_scalar(value, field_spec.type):
                    return False
        return True
    except Exception:
        return False


def _valid_preview_scalar(value: object, field_type: FieldType) -> bool:
    if value is None:
        return True
    if field_type in {FieldType.TEXT, FieldType.DATE, FieldType.DATETIME, FieldType.URL}:
        return type(value) is str and _safe_unicode(value, max_chars=120, allow_empty=True)
    if field_type in {FieldType.INTEGER, FieldType.KRW}:
        return type(value) is int
    if field_type is FieldType.DECIMAL:
        return type(value) in {int, float} and math.isfinite(value)
    if field_type is FieldType.BOOLEAN:
        return type(value) is bool
    return False


def _safe_web_url(value: object, *, reject_sensitive_query: bool = False) -> bool:
    if (
        type(value) is not str
        or not 8 <= len(value) <= 2_048
        or not _safe_unicode(value, max_chars=2_048)
    ):
        return False
    if "\\" in value or any(character.isspace() for character in value):
        return False
    try:
        parts = urlsplit(value)
        port = parts.port
        host = parts.hostname
        if (
            parts.scheme not in {"http", "https"}
            or host is None
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or port not in {None, 80, 443}
        ):
            return False
        canonicalize_hostname(host)
        return not (
            reject_sensitive_query
            and any(
                name.casefold() in SENSITIVE_QUERY_PARAMETER_NAMES
                for name, _ in parse_qsl(parts.query, keep_blank_values=True)
            )
        )
    except Exception:
        return False


def _query_values(url: str) -> tuple[str, ...]:
    with suppress(Exception):
        return tuple(
            value for _, value in parse_qsl(urlsplit(url).query, keep_blank_values=True) if value
        )
    return ()


def _safe_unicode(
    value: object,
    *,
    max_chars: int,
    allow_empty: bool = False,
    allow_layout_controls: bool = False,
) -> bool:
    if type(value) is not str or len(value) > max_chars or (not allow_empty and not value):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return not any(
        unicodedata.category(character).startswith("C")
        and not (allow_layout_controls and character in {"\n", "\r", "\t"})
        for character in value
    )


def _bounded_plain_text(value: str, *, limit: int) -> str:
    normalized = "".join(
        character if not unicodedata.category(character).startswith("C") else " "
        for character in value
    )
    normalized = " ".join(normalized.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return ""


def _web_origin(value: str) -> tuple[str, str, int]:
    parts = urlsplit(value)
    scheme = parts.scheme.casefold()
    host = canonicalize_hostname(parts.hostname)
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _proposal_binding(owner_id: str, candidate_version_id: str, spec_hash: str) -> str:
    value = canonical_json(
        {
            "owner_id": owner_id,
            "candidate_version_id": candidate_version_id,
            "spec_hash": spec_hash,
        }
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _runtime_material(
    owner_id: str,
    candidate_version_id: str,
    spec_hash: str,
    preview_items: tuple[PreviewItem, ...],
    resolved_strategy: FetchStrategy,
    robots: RobotsDecision,
    warnings: tuple[str, ...],
    explanation: str,
) -> str:
    value = canonical_json(
        {
            "owner_id": owner_id,
            "candidate_version_id": candidate_version_id,
            "spec_hash": spec_hash,
            "preview_items": [dict(item.fields) for item in preview_items],
            "resolved_strategy": resolved_strategy.value,
            "robots": {
                "allowed": robots.allowed,
                "crawl_delay_seconds": robots.crawl_delay_seconds,
                "checked_at": robots.checked_at.isoformat(),
                "policy_fetched": robots.policy_fetched,
            },
            "warnings": warnings,
            "explanation": explanation,
        }
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _complete_runtime_binding(material: str, pending: PendingAction) -> str:
    value = f"{material}:{pending.token}:{pending.expires_at.isoformat()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fresh_action_time(value: object) -> datetime | None:
    result: datetime | None = None
    with suppress(Exception):
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise TypeError
        normalized = value.astimezone(UTC)
        normalized + timedelta(minutes=10)
        result = normalized
    return result
