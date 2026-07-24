from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Final

from personal_monitor.ai.contracts import IntentKind, IntentResult
from personal_monitor.control.actions import ConsumedAction, PendingActionService
from personal_monitor.control.messages import (
    ControlReply,
    safe_approval_value,
    safe_plain,
    safe_url,
    status_label,
    time_label,
)
from personal_monitor.control.planner import (
    PlannedMonitor,
    ProposedMonitor,
    _proposal_binding,
    reconstruct_confirmed_spec,
    update_scope_is_valid,
)
from personal_monitor.control.preview import render_preview
from personal_monitor.domain.spec import MonitorStatus, RuleKind, RuleSpec
from personal_monitor.storage.registry import ControlMonitor, RegistryRepository
from personal_monitor.storage.schema import canonical_json, transaction, utc_now
from personal_monitor.telegram.gateway import ControlRequest, _is_fatal_exception
from personal_monitor.telegram.types import InlineButton

_SAFE_FAILURE: Final = "요청을 처리하지 못했습니다. 모니터 상태를 다시 확인해 주세요."
_NOT_FOUND: Final = "해당 모니터를 확인할 수 없습니다."
_CLARIFY: Final = "대상과 원하는 작업을 하나씩 더 구체적으로 알려주세요."
_EDIT_GUIDANCE: Final = "원하는 변경 내용을 새 메시지로 보내주세요."


class ControlService:
    __slots__ = (
        "_actions",
        "_actions_anchor",
        "_actions_methods_anchor",
        "_claim_anchor",
        "_billing_status",
        "_billing_status_anchor",
        "_connection_anchor",
        "_now_source",
        "_now_source_anchor",
        "_now_call_anchor",
        "_plan_update_anchor",
        "_planner",
        "_planner_anchor",
        "_propose_anchor",
        "_registry",
        "_registry_anchor",
        "_registry_methods_anchor",
        "_route_anchor",
        "_router",
        "_router_anchor",
        "_validate_create_anchor",
    )

    def __init__(
        self,
        intent_router: object,
        registry: RegistryRepository,
        planner: object,
        actions: PendingActionService,
        *,
        now_source: object = utc_now,
        billing_status: object | None = None,
    ) -> None:
        if (
            type(registry) is not RegistryRepository
            or type(actions) is not PendingActionService
            or registry.connection is not actions.connection
            or not callable(now_source)
        ):
            raise ValueError("invalid control service composition")
        try:
            route = intent_router.route
            propose = planner.propose
            plan_update = getattr(planner, "plan_update", None)
            planner_actions = planner.action_service
            claim = actions.claim
            validate_create = actions.valid_create_membership
            registry_methods = tuple(
                (name, getattr(registry, name))
                for name in (
                    "activate_candidate_exact",
                    "create_monitor",
                    "find_repair_candidate",
                    "get_control_monitor",
                    "list_control_monitors",
                    "soft_delete_exact",
                    "stage_candidate_action",
                    "transition_status_exact",
                )
            )
            actions_methods = tuple(
                (name, getattr(actions, name))
                for name in (
                    "_integrity_ok",
                    "claim",
                    "create",
                    "revoke",
                    "valid_create_membership",
                )
            )
            now_call = _capture_callable(now_source)
            billing_status_call = (
                _capture_callable(billing_status) if billing_status is not None else None
            )
            if (
                not callable(route)
                or not callable(propose)
                or not callable(claim)
                or not callable(validate_create)
                or planner_actions is not actions
            ):
                raise TypeError
            if not all(callable(method) for _, method in (*registry_methods, *actions_methods)):
                raise TypeError
        except Exception:
            raise ValueError("invalid control service composition") from None
        object.__setattr__(self, "_router", intent_router)
        object.__setattr__(self, "_router_anchor", intent_router)
        object.__setattr__(self, "_route_anchor", route)
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_registry_anchor", registry)
        object.__setattr__(self, "_registry_methods_anchor", registry_methods)
        object.__setattr__(self, "_connection_anchor", registry.connection)
        object.__setattr__(self, "_planner", planner)
        object.__setattr__(self, "_planner_anchor", planner)
        object.__setattr__(self, "_propose_anchor", propose)
        object.__setattr__(
            self,
            "_plan_update_anchor",
            plan_update if callable(plan_update) else None,
        )
        object.__setattr__(self, "_actions", actions)
        object.__setattr__(self, "_actions_anchor", actions)
        object.__setattr__(self, "_actions_methods_anchor", actions_methods)
        object.__setattr__(self, "_claim_anchor", claim)
        object.__setattr__(self, "_validate_create_anchor", validate_create)
        object.__setattr__(self, "_now_source", now_source)
        object.__setattr__(self, "_now_source_anchor", now_source)
        object.__setattr__(self, "_now_call_anchor", now_call)
        object.__setattr__(self, "_billing_status", billing_status)
        object.__setattr__(self, "_billing_status_anchor", billing_status_call)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("ControlService composition is sealed")

    def __repr__(self) -> str:
        return "<ControlService redacted>"

    @property
    def action_service(self) -> PendingActionService:
        if not self._integrity_ok():
            raise ValueError("invalid control service composition")
        return self._actions_anchor

    async def route(self, value: ControlRequest | ConsumedAction) -> ControlReply:
        return await self.handle(value)

    async def handle(self, value: ControlRequest | ConsumedAction) -> ControlReply:
        if not self._integrity_ok():
            return ControlReply(_SAFE_FAILURE)
        if type(value) is ControlRequest:
            return await self._handle_request(value)
        if type(value) is ConsumedAction:
            if not self._claim_anchor(value) or not self._integrity_ok():
                return ControlReply(_SAFE_FAILURE)
            return self._handle_action(value)
        return ControlReply(_SAFE_FAILURE)

    async def _handle_request(self, request: ControlRequest) -> ControlReply:
        try:
            safe_request = ControlRequest(request.owner_id, request.chat_id, request.text)
            request_snapshot = (
                safe_request.owner_id,
                safe_request.chat_id,
                safe_request.text,
            )
        except Exception:
            return ControlReply(_SAFE_FAILURE)
        try:
            intent = await self._route_anchor(safe_request)
        except asyncio.CancelledError:
            raise
        except Exception:
            return ControlReply(_SAFE_FAILURE)
        if not self._integrity_ok():
            return ControlReply(_SAFE_FAILURE)
        if (
            safe_request.owner_id,
            safe_request.chat_id,
            safe_request.text,
        ) != request_snapshot:
            return ControlReply(_SAFE_FAILURE)
        fresh = _fresh_intent(intent)
        if fresh is None:
            return ControlReply(_CLARIFY)
        if fresh.kind is IntentKind.UNKNOWN or fresh.confidence < 0.75:
            return self._clarification(safe_request.owner_id)
        if fresh.kind is IntentKind.LIST:
            return self._list_reply(safe_request.owner_id)
        if fresh.kind is IntentKind.BILLING_STATUS:
            return self._billing_status_reply()
        if fresh.kind is IntentKind.CREATE:
            return await self._create_preview(safe_request, fresh)
        if len(fresh.target_monitor_ids) != 1:
            return self._clarification(safe_request.owner_id)
        monitor_id = fresh.target_monitor_ids[0]
        if fresh.kind is IntentKind.STATUS:
            return self._status_reply(safe_request.owner_id, monitor_id)
        if fresh.kind is IntentKind.UPDATE:
            return await self._update_preview(safe_request, fresh, monitor_id)
        if fresh.kind in {IntentKind.PAUSE, IntentKind.RESUME, IntentKind.DELETE}:
            return self._state_preview(safe_request.owner_id, monitor_id, fresh.kind)
        return ControlReply(_CLARIFY)

    def _handle_action(self, action: ConsumedAction) -> ControlReply:
        owner_id = action.owner_id
        if owner_id is None or not _payload_owner_matches(action.payload, owner_id):
            return ControlReply(_SAFE_FAILURE)
        if action.operation == "edit":
            return ControlReply(_EDIT_GUIDANCE)
        if action.operation != "confirm":
            return ControlReply(_SAFE_FAILURE)
        if action.action == "create":
            try:
                confirmed = reconstruct_confirmed_spec(action, owner_id=owner_id)
                self._registry.create_monitor(confirmed.spec, created_by=owner_id)
            except Exception:
                return ControlReply(_SAFE_FAILURE)
            return ControlReply(
                f"{safe_plain(confirmed.spec.name, limit=120)} 모니터를 등록했습니다."
            )
        if action.action in {"pause", "resume", "delete"}:
            return self._confirm_state(action, owner_id)
        if action.action in {"update", "schedule_change"}:
            return self._confirm_candidate(action, owner_id)
        if action.action == "repair_activation":
            return self._confirm_repair(action, owner_id)
        return ControlReply(_SAFE_FAILURE)

    def _billing_status_reply(self) -> ControlReply:
        anchor = self._billing_status_anchor
        if anchor is None or not self._integrity_ok():
            return ControlReply(_SAFE_FAILURE)
        try:
            text = anchor[3]()
            if type(text) is not str or not 1 <= len(text) <= 3_500:
                raise ValueError
            return ControlReply(text)
        except Exception:
            return ControlReply(_SAFE_FAILURE)

    async def _create_preview(
        self,
        request: ControlRequest,
        intent: IntentResult,
    ) -> ControlReply:
        proposal: object | None = None
        try:
            proposal = await self._propose_anchor(request, intent)
            if not self._integrity_ok():
                raise ValueError
            if type(proposal) is not ProposedMonitor or proposal.spec.owner_id != request.owner_id:
                raise ValueError
            preview = render_preview(proposal)
            expected_payload = {
                "candidate_version_id": proposal.candidate_version_id,
                "spec_hash": proposal.spec_hash,
                "binding_hash": _proposal_binding(
                    request.owner_id,
                    proposal.candidate_version_id,
                    proposal.spec_hash,
                ),
                "spec": proposal.spec.model_dump(mode="json"),
            }
            if not self._validate_create_anchor(
                proposal.pending_action,
                request.owner_id,
                expected_payload,
                now=_safe_now(self._call_now()),
            ):
                raise ValueError
            return ControlReply(preview.text, preview.buttons)
        except BaseException as error:
            with suppress(Exception):
                self._actions.revoke(
                    proposal.pending_action.token,
                    request.owner_id,
                )
            if _is_fatal_exception(error):
                raise
            return ControlReply(_SAFE_FAILURE)

    def _list_reply(self, owner_id: str) -> ControlReply:
        if not self._integrity_ok():
            return ControlReply(_SAFE_FAILURE)
        try:
            monitors = self._registry.list_control_monitors(owner_id)
        except Exception:
            return ControlReply(_SAFE_FAILURE)
        if not monitors:
            return ControlReply(
                "등록된 모니터가 없습니다.\n사용자 작업: 새 모니터를 요청해 주세요."
            )
        lines = ["현재 모니터"]
        for index, monitor in enumerate(monitors, start=1):
            lines.extend(_status_lines(monitor, prefix=f"{index}. "))
        return _bounded_reply(lines)

    async def _update_preview(
        self,
        request: ControlRequest,
        intent: IntentResult,
        monitor_id: str,
    ) -> ControlReply:
        try:
            if not self._integrity_ok() or self._plan_update_anchor is None:
                raise ValueError
            current = self._registry.get_control_monitor(monitor_id, owner_id=request.owner_id)
            planned = await self._plan_update_anchor(request, intent, current.spec)
            if not self._integrity_ok():
                raise ValueError
            if type(planned) is not PlannedMonitor:
                raise ValueError
            fresh_spec = type(current.spec).model_validate(planned.spec.model_dump(mode="json"))
            if (
                fresh_spec.owner_id != request.owner_id
                or fresh_spec.target_url != current.spec.target_url
                or fresh_spec.auth_profile_ref != current.spec.auth_profile_ref
                or fresh_spec.source_adapter != current.spec.source_adapter
                or not update_scope_is_valid(current.spec, fresh_spec, intent)
            ):
                raise ValueError
            action_kind = (
                "schedule_change"
                if intent.schedule_text is not None and intent.condition_text is None
                else "update"
            )
            now = _safe_now(self._call_now())

            def make_reply(pending: object, _candidate: str, _digest: str) -> ControlReply:
                from personal_monitor.control.actions import PendingAction

                if type(pending) is not PendingAction:
                    raise ValueError
                lines = [
                    f"모니터: {safe_plain(current.name, limit=120)}",
                    f"대상: {safe_url(current.spec.target_url)}",
                ]
                if current.spec.schedule != fresh_spec.schedule:
                    lines.append(
                        "일정 변경: "
                        f"{safe_plain(current.spec.schedule, limit=80)} → "
                        f"{safe_plain(fresh_spec.schedule, limit=80)}"
                    )
                if current.spec.rules != fresh_spec.rules:
                    lines.append(
                        f"조건 변경: {len(current.spec.rules)}개 → {len(fresh_spec.rules)}개"
                    )
                    lines.extend(
                        _rule_summary(rule, index)
                        for index, rule in enumerate(fresh_spec.rules, start=1)
                    )
                lines.extend(
                    (
                        f"검증 미리보기: {len(planned.preview_items)}개 항목 통과",
                        "검증된 변경 후보입니다. 10분 안에 확인해 주세요.",
                    )
                )
                return ControlReply(
                    "\n".join(lines),
                    (
                        (
                            InlineButton("적용", pending.confirm_callback),
                            InlineButton("수정", f"edit:{pending.token}"),
                            InlineButton("취소", pending.cancel_callback),
                        ),
                    ),
                )

            reply = self._registry.stage_candidate_action(
                monitor_id,
                owner_id=request.owner_id,
                expected_status=current.status,
                expected_active_version_id=current.active_version_id,
                spec=fresh_spec,
                action_kind=action_kind,
                actions=self._actions,
                now=now,
                reply_factory=make_reply,
            )
            if type(reply) is not ControlReply:
                raise ValueError
            return ControlReply(reply.text, reply.buttons)
        except BaseException as error:
            if _is_fatal_exception(error):
                raise
            return ControlReply(_SAFE_FAILURE)

    def _status_reply(self, owner_id: str, monitor_id: str) -> ControlReply:
        if not self._integrity_ok():
            return ControlReply(_SAFE_FAILURE)
        try:
            monitor = self._registry.get_control_monitor(monitor_id, owner_id=owner_id)
        except Exception:
            return ControlReply(_NOT_FOUND)
        if monitor.status is MonitorStatus.NEEDS_REVIEW:
            repair = self._repair_preview(monitor)
            if repair is not None:
                return repair
        return _bounded_reply(_status_lines(monitor))

    def _repair_preview(self, monitor: ControlMonitor) -> ControlReply | None:
        if not self._integrity_ok():
            return None
        try:
            now = _safe_now(self._call_now())
            with transaction(self._registry.connection, immediate=True):
                current = self._registry.get_control_monitor(
                    monitor.id,
                    owner_id=monitor.owner_id,
                )
                if (
                    current.status is not MonitorStatus.NEEDS_REVIEW
                    or current.active_version_id != monitor.active_version_id
                ):
                    raise ValueError
                candidate = self._registry.find_repair_candidate(
                    current.id,
                    owner_id=current.owner_id,
                )
                if (
                    candidate is None
                    or candidate.expected_active_version_id != current.active_version_id
                    or candidate.spec.owner_id != current.owner_id
                    or candidate.spec.target_url != current.spec.target_url
                    or candidate.spec.source_adapter != current.spec.source_adapter
                    or candidate.spec.auth_profile_ref != current.spec.auth_profile_ref
                ):
                    return None
                canonical = canonical_json(candidate.spec.model_dump(mode="json"))
                digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                pending = self._actions.create(
                    current.owner_id,
                    "repair_activation",
                    {
                        "owner_id": current.owner_id,
                        "monitor_id": current.id,
                        "monitor_name": candidate.spec.name,
                        "expected_status": MonitorStatus.NEEDS_REVIEW.value,
                        "expected_active_version_id": current.active_version_id,
                        "candidate_version_id": candidate.id,
                        "spec_hash": digest,
                    },
                    now=now,
                )
                lines = [
                    *_status_lines(current),
                    f"복구 후보: {safe_plain(candidate.spec.name, limit=120)}",
                    f"대상: {safe_url(candidate.spec.target_url)}",
                    f"구조 요약: 필드 {len(candidate.spec.extract.fields)}개, "
                    f"규칙 {len(candidate.spec.rules)}개",
                    "10분 안에 복구 적용을 확인해 주세요.",
                ]
                return ControlReply(
                    "\n".join(lines),
                    (
                        (
                            InlineButton("복구 적용", pending.confirm_callback),
                            InlineButton("취소", pending.cancel_callback),
                        ),
                    ),
                )
        except BaseException as error:
            if _is_fatal_exception(error):
                raise
            return None

    def _clarification(self, owner_id: str) -> ControlReply:
        if not self._integrity_ok():
            return ControlReply(_CLARIFY)
        try:
            monitors = self._registry.list_control_monitors(owner_id)
        except Exception:
            return ControlReply(_CLARIFY)
        if len(monitors) < 2:
            return ControlReply(_CLARIFY)
        lines = ["어느 모니터인지 번호나 이름으로 알려주세요."]
        for index, monitor in enumerate(monitors[:10], start=1):
            lines.append(
                f"{index}. {safe_plain(monitor.name, limit=100)} ({status_label(monitor.status)})"
            )
        return _bounded_reply(lines)

    def _state_preview(
        self,
        owner_id: str,
        monitor_id: str,
        kind: IntentKind,
    ) -> ControlReply:
        if not self._integrity_ok():
            return ControlReply(_SAFE_FAILURE)
        expected = {
            IntentKind.PAUSE: MonitorStatus.ACTIVE,
            IntentKind.RESUME: MonitorStatus.PAUSED_USER,
        }.get(kind)
        try:
            now = _safe_now(self._call_now())
            with transaction(self._registry.connection, immediate=True):
                monitor = self._registry.get_control_monitor(monitor_id, owner_id=owner_id)
                if expected is not None and monitor.status is not expected:
                    raise ValueError
                payload = {
                    "owner_id": owner_id,
                    "monitor_id": monitor.id,
                    "monitor_name": monitor.name,
                    "expected_status": monitor.status.value,
                    "expected_active_version_id": monitor.active_version_id,
                }
                pending = self._actions.create(owner_id, kind.value, payload, now=now)
                verb = {
                    IntentKind.PAUSE: "일시정지",
                    IntentKind.RESUME: "재개",
                    IntentKind.DELETE: "삭제",
                }[kind]
                reply = ControlReply(
                    f"모니터: {safe_plain(monitor.name, limit=120)}\n"
                    f"현재 상태: {status_label(monitor.status)}\n"
                    f"변경: {verb}\n"
                    "10분 안에 확인해 주세요.",
                    (
                        (
                            InlineButton("확인", pending.confirm_callback),
                            InlineButton("취소", pending.cancel_callback),
                        ),
                    ),
                )
            return reply
        except BaseException as error:
            if _is_fatal_exception(error):
                raise
            return ControlReply(_NOT_FOUND)

    def _confirm_state(self, action: ConsumedAction, owner_id: str) -> ControlReply:
        if not self._integrity_ok():
            return ControlReply(_SAFE_FAILURE)
        values = _state_payload(action.payload)
        if values is None or values["owner_id"] != owner_id:
            return ControlReply(_SAFE_FAILURE)
        try:
            expected = MonitorStatus(values["expected_status"])
            now = _safe_now(self._call_now())
            if action.action == "pause":
                self._registry.transition_status_exact(
                    values["monitor_id"],
                    owner_id=owner_id,
                    expected_status=expected,
                    expected_active_version_id=values["expected_active_version_id"],
                    target_status=MonitorStatus.PAUSED_USER,
                    changed_at=now,
                )
                outcome = "일시정지했습니다."
            elif action.action == "resume":
                self._registry.transition_status_exact(
                    values["monitor_id"],
                    owner_id=owner_id,
                    expected_status=expected,
                    expected_active_version_id=values["expected_active_version_id"],
                    target_status=MonitorStatus.ACTIVE,
                    changed_at=now,
                )
                outcome = "재개했습니다."
            else:
                self._registry.soft_delete_exact(
                    values["monitor_id"],
                    owner_id=owner_id,
                    expected_status=expected,
                    expected_active_version_id=values["expected_active_version_id"],
                    disabled_at=now,
                )
                outcome = "삭제했습니다."
        except Exception:
            return ControlReply(_SAFE_FAILURE)
        return ControlReply(f"{safe_plain(values['monitor_name'], limit=120)} 모니터를 {outcome}")

    def _confirm_candidate(self, action: ConsumedAction, owner_id: str) -> ControlReply:
        if not self._integrity_ok():
            return ControlReply(_SAFE_FAILURE)
        values = _candidate_payload(action.payload)
        if (
            values is None
            or values["owner_id"] != owner_id
            or values["action_kind"] != action.action
        ):
            return ControlReply(_SAFE_FAILURE)
        try:
            expected = MonitorStatus(values["expected_status"])
            spec = self._registry.activate_candidate_exact(
                values["monitor_id"],
                values["candidate_version_id"],
                owner_id=owner_id,
                expected_status=expected,
                expected_active_version_id=values["expected_active_version_id"],
                expected_created_by="codex-control",
                spec_hash=values["spec_hash"],
                activated_at=_safe_now(self._call_now()),
            )
        except Exception:
            return ControlReply(_SAFE_FAILURE)
        return ControlReply(f"{safe_plain(spec.name, limit=120)} 모니터를 수정했습니다.")

    def _confirm_repair(self, action: ConsumedAction, owner_id: str) -> ControlReply:
        if not self._integrity_ok():
            return ControlReply(_SAFE_FAILURE)
        values = _repair_payload(action.payload)
        if values is None or values["owner_id"] != owner_id:
            return ControlReply(_SAFE_FAILURE)
        try:
            spec = self._registry.activate_candidate_exact(
                values["monitor_id"],
                values["candidate_version_id"],
                owner_id=owner_id,
                expected_status=MonitorStatus.NEEDS_REVIEW,
                expected_active_version_id=values["expected_active_version_id"],
                expected_created_by="scrapling-adaptive",
                spec_hash=values["spec_hash"],
                activated_at=_safe_now(self._call_now()),
                target_status=MonitorStatus.ACTIVE,
            )
        except Exception:
            return ControlReply(_SAFE_FAILURE)
        return ControlReply(f"{safe_plain(spec.name, limit=120)} 모니터 복구를 적용했습니다.")

    def _integrity_ok(self) -> bool:
        try:
            return (
                self._router is self._router_anchor
                and self._registry is self._registry_anchor
                and self._planner is self._planner_anchor
                and self._actions is self._actions_anchor
                and self._now_source is self._now_source_anchor
                and (
                    (self._billing_status is None and self._billing_status_anchor is None)
                    or (
                        self._billing_status is not None
                        and self._billing_status_anchor is not None
                        and _captured_callable_intact(
                            self._billing_status,
                            self._billing_status_anchor,
                        )
                    )
                )
                and self._registry_anchor.connection is self._connection_anchor
                and self._actions_anchor.connection is self._connection_anchor
                and self._planner_anchor.action_service is self._actions_anchor
                and self._actions_anchor._integrity_ok()
                and _callable_still_attached(self._route_anchor, self._router_anchor, "route")
                and _callable_still_attached(
                    self._propose_anchor,
                    self._planner_anchor,
                    "propose",
                )
                and (
                    self._plan_update_anchor is None
                    or _callable_still_attached(
                        self._plan_update_anchor,
                        self._planner_anchor,
                        "plan_update",
                    )
                )
                and _callable_still_attached(
                    self._claim_anchor,
                    self._actions_anchor,
                    "claim",
                )
                and _callable_still_attached(
                    self._validate_create_anchor,
                    self._actions_anchor,
                    "valid_create_membership",
                )
                and all(
                    _callable_still_attached(method, self._registry_anchor, name)
                    for name, method in self._registry_methods_anchor
                )
                and all(
                    _callable_still_attached(method, self._actions_anchor, name)
                    for name, method in self._actions_methods_anchor
                )
                and _captured_callable_intact(
                    self._now_source_anchor,
                    self._now_call_anchor,
                )
            )
        except Exception:
            return False

    def _call_now(self) -> object:
        if not self._integrity_ok():
            raise ValueError("invalid control service composition")
        return self._now_call_anchor[3]()


def _fresh_intent(value: object) -> IntentResult | None:
    with suppress(Exception):
        if type(value) is IntentResult:
            return IntentResult.model_validate(value.model_dump(mode="python"))
    return None


def _callable_still_attached(captured: object, owner: object, name: str) -> bool:
    try:
        current = getattr(owner, name)
    except Exception:
        return False
    if not callable(captured) or not callable(current):
        return False
    if current is captured:
        return True
    return (
        getattr(captured, "__self__", None) is owner
        and getattr(current, "__self__", None) is owner
        and getattr(captured, "__func__", None) is getattr(current, "__func__", None)
    )


def _capture_callable(
    value: object,
) -> tuple[object, object | None, str | None, object]:
    if inspect.ismethod(value) and value.__self__ is not None:
        return value, value.__self__, value.__name__, value
    if inspect.isfunction(value) or inspect.isbuiltin(value):
        return value, None, None, value
    call = value.__call__  # noqa: B004
    if not callable(call):
        raise TypeError
    return value, value, "__call__", call


def _captured_callable_intact(
    value: object,
    anchor: tuple[object, object | None, str | None, object],
) -> bool:
    root, owner, name, captured = anchor
    if value is not root or not callable(captured):
        return False
    if owner is None:
        return captured is value
    if name is None:
        return False
    return _callable_still_attached(captured, owner, name)


def _safe_now(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(UTC)


def _payload_owner_matches(payload: Mapping[str, object], owner_id: str) -> bool:
    try:
        direct = payload.get("owner_id")
        if direct is not None:
            return type(direct) is str and direct == owner_id
        spec = payload.get("spec")
        return isinstance(spec, Mapping) and spec.get("owner_id") == owner_id
    except Exception:
        return False


def _state_payload(payload: Mapping[str, object]) -> dict[str, str] | None:
    keys = {
        "owner_id",
        "monitor_id",
        "monitor_name",
        "expected_status",
        "expected_active_version_id",
    }
    try:
        if set(payload) != keys:
            return None
        result = {key: payload[key] for key in keys}
        if not all(type(value) is str and value for value in result.values()):
            return None
        return result  # type: ignore[return-value]
    except Exception:
        return None


def _candidate_payload(payload: Mapping[str, object]) -> dict[str, str] | None:
    keys = {
        "owner_id",
        "monitor_id",
        "monitor_name",
        "expected_status",
        "expected_active_version_id",
        "candidate_version_id",
        "spec_hash",
        "action_kind",
    }
    try:
        if set(payload) != keys:
            return None
        result = {key: payload[key] for key in keys}
        if not all(type(value) is str and value for value in result.values()):
            return None
        if result["action_kind"] not in {"update", "schedule_change"}:
            return None
        return result  # type: ignore[return-value]
    except Exception:
        return None


def _repair_payload(payload: Mapping[str, object]) -> dict[str, str] | None:
    keys = {
        "owner_id",
        "monitor_id",
        "monitor_name",
        "expected_status",
        "expected_active_version_id",
        "candidate_version_id",
        "spec_hash",
    }
    try:
        if set(payload) != keys:
            return None
        result = {key: payload[key] for key in keys}
        if not all(type(value) is str and value for value in result.values()):
            return None
        if result["expected_status"] != MonitorStatus.NEEDS_REVIEW.value:
            return None
        return result  # type: ignore[return-value]
    except Exception:
        return None


def _status_lines(monitor: ControlMonitor, *, prefix: str = "") -> list[str]:
    lines = [
        f"{prefix}모니터: {safe_plain(monitor.name, limit=120)}",
        f"상태: {status_label(monitor.status)}",
        f"마지막 성공: {time_label(monitor.last_success_at)}",
        f"다음 실행: {time_label(monitor.next_run_at)}",
    ]
    if monitor.status is MonitorStatus.NEEDS_REVIEW:
        lines.append("일반 알림은 검토가 끝날 때까지 일시정지됩니다.")
    lines.append("사용자 작업: 상태 변경이나 삭제를 요청할 수 있습니다.")
    return lines


_RULE_KIND_LABELS: Final = {
    RuleKind.NEW_ITEM: "새 항목",
    RuleKind.FIELD_CHANGED: "필드 변경",
    RuleKind.NUMERIC_THRESHOLD: "숫자 임계값",
    RuleKind.STATUS_EQUALS: "상태 일치",
    RuleKind.KEYWORD_MATCH: "키워드 일치",
}


def _rule_summary(rule: RuleSpec, index: int) -> str:
    parts = [f"조건 {index}: 종류={_RULE_KIND_LABELS[rule.kind]}"]
    if rule.field is not None:
        parts.append(f"필드={_exact_rule_text(rule.field)}")
    if rule.operator is not None:
        parts.append(f"연산자={_exact_rule_text(rule.operator)}")
    if rule.value is not None:
        parts.append(f"값={_exact_rule_text(rule.value)}")
    if rule.keywords:
        keywords = ", ".join(_exact_rule_text(keyword) for keyword in rule.keywords)
        parts.append(f"키워드={keywords}")
    return ", ".join(parts)


def _exact_rule_text(value: str | int | float | bool) -> str:
    return safe_approval_value(value)


def _bounded_reply(lines: list[str]) -> ControlReply:
    selected: list[str] = []
    for line in lines:
        candidate = "\n".join((*selected, safe_plain(line, limit=300)))
        if len(candidate) > 3_500:
            break
        selected.append(safe_plain(line, limit=300))
    return ControlReply("\n".join(selected) if selected else _SAFE_FAILURE)
