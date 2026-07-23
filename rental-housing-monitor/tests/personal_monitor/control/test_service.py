from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from personal_monitor.ai.contracts import IntentKind, IntentResult
from personal_monitor.control.actions import ConsumedAction, PendingAction, PendingActionService
from personal_monitor.control.messages import ControlReply, safe_approval_value, safe_plain
from personal_monitor.control.planner import (
    PlannedMonitor,
    PreviewItem,
    ProposedMonitor,
    _complete_runtime_binding,
    _proposal_binding,
    _runtime_material,
)
from personal_monitor.control.service import ControlService
from personal_monitor.domain.spec import FetchStrategy, MonitorSpec, MonitorStatus
from personal_monitor.engine.scheduler import next_run_at
from personal_monitor.security.robots import RobotsDecision
from personal_monitor.storage import RegistryRepository, open_database
from personal_monitor.storage.schema import canonical_json
from personal_monitor.telegram import InlineButton
from personal_monitor.telegram.gateway import ControlRequest

NOW = datetime(2026, 7, 23, tzinfo=UTC)
OWNER = "telegram-user:7"


def _spec(*, owner: str = OWNER, name: str = "가격 감시") -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": owner,
            "name": name,
            "target_url": "https://example.com/product/1?ref=private-value",
            "source_adapter": "scrapling",
            "schedule": "0 */6 * * *",
            "timezone": "Asia/Seoul",
            "extract": {
                "item_scope": "main",
                "fields": {"price": {"selector": ".price", "type": "krw"}},
            },
            "validators": {"min_items": 1, "max_items": 1},
            "rules": [{"kind": "new_item"}],
        }
    )


def _registry() -> tuple[RegistryRepository, object]:
    connection = open_database(":memory:")
    registry = RegistryRepository(connection)
    registry.create_user(OWNER, 7)
    registry.create_user("telegram-user:8", 8)
    return registry, connection


class FakeIntentRouter:
    def __init__(self, result: IntentResult) -> None:
        self.result = result
        self.calls: list[ControlRequest] = []

    async def route(self, request: ControlRequest) -> IntentResult:
        self.calls.append(request)
        return self.result


class UnusedPlanner:
    def __init__(self, action_service: PendingActionService) -> None:
        self.action_service = action_service

    async def propose(self, *_args: object, **_kwargs: object) -> Any:
        raise AssertionError("planner must not be called")


class FakePlanner:
    def __init__(
        self,
        proposal: ProposedMonitor,
        action_service: PendingActionService,
    ) -> None:
        self.proposal = proposal
        self.action_service = action_service
        self.calls: list[tuple[ControlRequest, IntentResult]] = []

    async def propose(self, request: ControlRequest, intent: IntentResult) -> ProposedMonitor:
        self.calls.append((request, intent))
        return self.proposal


class FakeUpdatePlanner:
    def __init__(
        self,
        planned: PlannedMonitor,
        action_service: PendingActionService,
    ) -> None:
        self.planned = planned
        self.action_service = action_service
        self.calls: list[tuple[ControlRequest, IntentResult, MonitorSpec]] = []

    async def propose(self, *_args: object, **_kwargs: object) -> Any:
        raise AssertionError("create planner must not be called")

    async def plan_update(
        self,
        request: ControlRequest,
        intent: IntentResult,
        current_spec: MonitorSpec,
    ) -> PlannedMonitor:
        self.calls.append((request, intent, current_spec))
        return self.planned


class ErrorPlanner:
    def __init__(self, action_service: PendingActionService) -> None:
        self.action_service = action_service

    async def propose(self, *_args: object, **_kwargs: object) -> Any:
        raise RuntimeError(
            "<html>private</html> cookie=session-secret "
            "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
        )


class BoundUnusedPlanner(UnusedPlanner):
    def __init__(self, action_service: PendingActionService) -> None:
        super().__init__(action_service)


def _intent(kind: IntentKind, monitor_id: str | None = None) -> IntentResult:
    return IntentResult(
        kind=kind,
        target_monitor_ids=[] if monitor_id is None else [monitor_id],
        target_url=None,
        condition_text=None,
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )


def _request(text: str = "/monitors") -> ControlRequest:
    return ControlRequest(OWNER, "42", text)


def _run(value: Any) -> Any:
    import asyncio

    return asyncio.run(value)


def _deep_exception_group(leaf: BaseException, *, depth: int = 1_105) -> BaseExceptionGroup:
    result = BaseExceptionGroup("leaf", [leaf])
    for index in range(depth):
        result = BaseExceptionGroup(f"group-{index}", [result])
    return result


def _proposal(actions: PendingActionService) -> ProposedMonitor:
    spec = _spec()
    candidate = "1" * 32
    digest = hashlib.sha256(canonical_json(spec.model_dump(mode="json")).encode()).hexdigest()
    pending = actions.create(
        OWNER,
        "create",
        {
            "candidate_version_id": candidate,
            "spec_hash": digest,
            "binding_hash": _proposal_binding(OWNER, candidate, digest),
            "spec": spec.model_dump(mode="json"),
        },
        now=NOW,
    )
    previews = (PreviewItem({"price": 99000}),)
    robots = RobotsDecision(True, None, NOW, True)
    material = _runtime_material(
        OWNER,
        candidate,
        digest,
        previews,
        FetchStrategy.HTTP,
        robots,
        (),
        "검증된 모니터 제안입니다.",
    )
    return ProposedMonitor(
        spec=spec,
        preview_items=previews,
        resolved_strategy=FetchStrategy.HTTP,
        robots=robots,
        warnings=(),
        explanation="검증된 모니터 제안입니다.",
        candidate_version_id=candidate,
        spec_hash=digest,
        pending_action=pending,
        _runtime_binding_hash=_complete_runtime_binding(material, pending),
    )


def _rebind_proposal_pending(
    proposal: ProposedMonitor,
    pending: PendingAction,
) -> None:
    material = _runtime_material(
        proposal.spec.owner_id,
        proposal.candidate_version_id,
        proposal.spec_hash,
        proposal.preview_items,
        proposal.resolved_strategy,
        proposal.robots,
        proposal.warnings,
        proposal.explanation,
    )
    object.__setattr__(proposal, "pending_action", pending)
    object.__setattr__(
        proposal,
        "_runtime_binding_hash",
        _complete_runtime_binding(material, pending),
    )


def _planned_update() -> PlannedMonitor:
    payload = _spec().model_dump(mode="json")
    payload["schedule"] = "0 9 * * *"
    updated = MonitorSpec.model_validate(payload)
    return PlannedMonitor(
        spec=updated,
        preview_items=(PreviewItem({"price": 99000}),),
        resolved_strategy=FetchStrategy.HTTP,
        robots=RobotsDecision(True, None, NOW, True),
        warnings=(),
    )


def _text_rule_update_reply(rule: dict[str, object]) -> tuple[ControlReply, int, int]:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    current_payload = _spec().model_dump(mode="json")
    current_payload["extract"]["fields"]["title"] = {
        "selector": ".title",
        "type": "text",
    }
    current = MonitorSpec.model_validate(current_payload)
    monitor_id = registry.create_monitor(current, created_by=OWNER)
    payload = current.model_dump(mode="json")
    payload["rules"] = [rule]
    planned = PlannedMonitor(
        spec=MonitorSpec.model_validate(payload),
        preview_items=(PreviewItem({"title": "검증"}),),
        resolved_strategy=FetchStrategy.HTTP,
        robots=RobotsDecision(True, None, NOW, True),
        warnings=(),
    )
    intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text="조건을 바꿔줘",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakeUpdatePlanner(planned, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("조건을 바꿔줘")))
    version_count = connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0]
    action_count = connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0]
    connection.close()
    return reply, version_count, action_count


def test_control_reply_is_immutable_bounded_and_redacted() -> None:
    reply = ControlReply(
        "모니터 상태를 확인했습니다",
        ((InlineButton("확인", "confirm:" + "x" * 32),),),
    )

    assert reply.text == "모니터 상태를 확인했습니다"
    assert reply.buttons[0][0].text == "확인"
    assert "모니터 상태" not in repr(reply)
    with pytest.raises(FrozenInstanceError):
        reply.text = "변경"  # type: ignore[misc]


@pytest.mark.parametrize(
    "text",
    ("", "x" * 3501, "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"),
)
def test_control_reply_rejects_empty_oversized_or_secret_text(text: str) -> None:
    with pytest.raises(ValueError, match="invalid control reply"):
        ControlReply(text)


def test_safe_plain_strips_embedded_query_and_html_delimiters() -> None:
    value = "대상 https://example.com/path?ref=ordinary-private-value <html>내용</html>"

    rendered = safe_plain(value, limit=200)

    assert "ordinary-private-value" not in rendered
    assert "?" not in rendered
    assert "<html>" not in rendered


@pytest.mark.parametrize(
    "text",
    (
        "대상 https://example.com/path?ref=ordinary-private-value",
        "대상 https://example.com/path#private-fragment",
        "<b>안전해 보이는 HTML</b>",
    ),
)
def test_control_reply_constructor_rejects_unstripped_urls_and_html(text: str) -> None:
    with pytest.raises(ValueError, match="invalid control reply"):
        ControlReply(text)


@pytest.mark.parametrize(
    "label",
    (
        "보기 https://example.com/path?ref=ordinary-private-value",
        "보기 https://example.com/path#private-fragment",
        "<b>확인</b>",
    ),
)
def test_control_reply_rejects_unsafe_button_text(label: str) -> None:
    with pytest.raises(ValueError, match="invalid control reply"):
        ControlReply(
            "안전한 본문",
            ((InlineButton(label, "confirm:" + "x" * 32),),),
        )


def test_consumed_action_carries_exact_authenticated_owner_and_operation() -> None:
    connection = open_database(":memory:")
    connection.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) VALUES (?, 7, 'active', ?)",
        (OWNER, NOW.isoformat()),
    )
    actions = PendingActionService(connection)
    pending = actions.create(OWNER, "delete", {"owner_id": OWNER}, now=NOW)

    consumed = actions.consume(pending.token, OWNER, now=NOW, operation="edit")

    assert consumed.owner_id == OWNER
    assert consumed.operation == "edit"
    assert OWNER not in repr(consumed)
    connection.close()


def test_forged_consumed_action_cannot_mutate_control_service() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    active = registry.get_active_monitor(monitor_id)
    actions = PendingActionService(connection)
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.LIST)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )
    forged = ConsumedAction(
        "delete",
        {
            "owner_id": OWNER,
            "monitor_id": monitor_id,
            "monitor_name": "가격 감시",
            "expected_status": MonitorStatus.ACTIVE.value,
            "expected_active_version_id": active.version_id,
        },
        OWNER,
    )

    reply = _run(service.handle(forged))

    assert "처리하지 못했습니다" in reply.text
    assert registry.get_control_monitor(monitor_id, owner_id=OWNER).status is MonitorStatus.ACTIVE
    connection.close()


def test_control_service_rejects_planner_bound_to_different_action_store() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    other = PendingActionService(connection)

    with pytest.raises(ValueError, match="invalid control service composition"):
        ControlService(
            FakeIntentRouter(_intent(IntentKind.LIST)),
            registry,
            BoundUnusedPlanner(other),
            actions,
            now_source=lambda: NOW,
        )

    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_consumed_action_receipt_is_bound_to_service_and_claimed_once() -> None:
    registry, connection = _registry()
    issuing_actions = PendingActionService(connection)
    service_actions = PendingActionService(connection)
    proposal = _proposal(issuing_actions)
    foreign = issuing_actions.consume(proposal.pending_action.token, OWNER, now=NOW)
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.LIST)),
        registry,
        UnusedPlanner(service_actions),
        service_actions,
        now_source=lambda: NOW,
    )

    foreign_reply = _run(service.handle(foreign))

    assert "처리하지 못했습니다" in foreign_reply.text
    assert connection.execute("SELECT count(*) FROM monitors").fetchone()[0] == 0

    own_proposal = _proposal(service_actions)
    own = service_actions.consume(own_proposal.pending_action.token, OWNER, now=NOW)
    first = _run(service.handle(own))
    second = _run(service.handle(own))

    assert "등록했습니다" in first.text
    assert "처리하지 못했습니다" in second.text
    assert connection.execute("SELECT count(*) FROM monitors").fetchone()[0] == 1
    connection.close()


def test_copied_capability_state_cannot_deny_the_real_exact_action() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    active = registry.get_active_monitor(monitor_id)
    actions = PendingActionService(connection)
    pending = actions.create(
        OWNER,
        "delete",
        {
            "owner_id": OWNER,
            "monitor_id": monitor_id,
            "monitor_name": "가격 감시",
            "expected_status": MonitorStatus.ACTIVE.value,
            "expected_active_version_id": active.version_id,
        },
        now=NOW,
    )
    consumed = actions.consume(pending.token, OWNER, now=NOW)
    forged = ConsumedAction(
        consumed.action,
        consumed.payload,
        consumed.owner_id,
        consumed.operation,
    )
    for name in ("_issuer", "_receipt"):
        with suppress(AttributeError):
            object.__setattr__(forged, name, getattr(consumed, name))
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.LIST)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )

    forged_reply = _run(service.handle(forged))
    real_reply = _run(service.handle(consumed))

    assert "처리하지 못했습니다" in forged_reply.text
    assert "삭제했습니다" in real_reply.text
    assert "_issuer" not in ConsumedAction.__slots__
    assert "_receipt" not in ConsumedAction.__slots__
    assert "_issued" not in PendingActionService.__slots__
    connection.close()


def test_owner_control_status_is_scoped_and_includes_latest_success() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    active = registry.get_active_monitor(monitor_id)
    connection.execute(
        "INSERT INTO runs(id, monitor_id, version_id, stage, status, started_at, finished_at) "
        "VALUES ('run-1', ?, ?, 'complete', 'success', ?, ?)",
        (monitor_id, active.version_id, NOW.isoformat(), NOW.isoformat()),
    )

    status = registry.get_control_monitor(monitor_id, owner_id=OWNER)

    assert status.id == monitor_id
    assert status.status is MonitorStatus.ACTIVE
    assert status.active_version_id == active.version_id
    assert status.last_success_at == NOW
    assert status.spec == _spec()
    with pytest.raises(ValueError, match="control monitor unavailable"):
        registry.get_control_monitor(monitor_id, owner_id="telegram-user:8")
    connection.close()


def test_state_confirmation_requires_exact_owner_status_and_active_version() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    active = registry.get_active_monitor(monitor_id)

    registry.transition_status_exact(
        monitor_id,
        owner_id=OWNER,
        expected_status=MonitorStatus.ACTIVE,
        expected_active_version_id=active.version_id,
        target_status=MonitorStatus.PAUSED_USER,
        changed_at=NOW,
    )

    with pytest.raises(ValueError, match="lifecycle precondition failed"):
        registry.transition_status_exact(
            monitor_id,
            owner_id=OWNER,
            expected_status=MonitorStatus.ACTIVE,
            expected_active_version_id=active.version_id,
            target_status=MonitorStatus.PAUSED_USER,
            changed_at=NOW,
        )
    assert registry.get_control_monitor(monitor_id, owner_id=OWNER).status is (
        MonitorStatus.PAUSED_USER
    )
    connection.close()


def test_list_and_status_are_immediate_owner_only_and_query_redacted() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    registry.create_monitor(
        _spec(owner="telegram-user:8", name="다른 사람 비밀"),
        created_by="telegram-user:8",
    )
    actions = PendingActionService(connection)
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.LIST)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )

    listed = _run(service.handle(_request()))
    status_service = ControlService(
        FakeIntentRouter(_intent(IntentKind.STATUS, monitor_id)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )
    status = _run(status_service.handle(_request(f"/status {monitor_id}")))

    assert "가격 감시" in listed.text
    assert "다른 사람 비밀" not in listed.text
    assert "사용 중" in listed.text
    assert "마지막 성공: 없음" in status.text
    assert "다음 실행:" in status.text
    assert "private-value" not in listed.text + status.text
    assert listed.buttons == status.buttons == ()
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_ambiguous_reference_returns_owner_only_numbered_question_without_writes() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    registry.create_monitor(_spec(name="첫 번째"), created_by=OWNER)
    registry.create_monitor(_spec(name="두 번째"), created_by=OWNER)
    registry.create_monitor(
        _spec(owner="telegram-user:8", name="다른 소유자 비밀"),
        created_by="telegram-user:8",
    )
    unknown = IntentResult(
        kind=IntentKind.UNKNOWN,
        target_monitor_ids=[],
        target_url=None,
        condition_text=None,
        schedule_text=None,
        clarification="모델이 만든 임의 질문",
        confidence=0.4,
    )
    service = ControlService(
        FakeIntentRouter(unknown),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("아까 그거 꺼줘")))

    assert "1. 첫 번째" in reply.text
    assert "2. 두 번째" in reply.text
    assert "다른 소유자 비밀" not in reply.text
    assert "모델이 만든" not in reply.text
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_pause_is_preview_only_then_exact_confirmation_changes_state() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    actions = PendingActionService(connection)
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.PAUSE, monitor_id)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )

    preview = _run(service.handle(_request(f"/pause {monitor_id}")))

    assert registry.get_control_monitor(monitor_id, owner_id=OWNER).status is MonitorStatus.ACTIVE
    assert preview.buttons[0][0].text == "확인"
    token = preview.buttons[0][0].callback_data.removeprefix("confirm:")
    consumed = actions.consume(token, OWNER, now=NOW)
    result = _run(service.handle(consumed))

    assert "일시정지했습니다" in result.text
    assert registry.get_control_monitor(monitor_id, owner_id=OWNER).status is (
        MonitorStatus.PAUSED_USER
    )
    connection.close()


def test_resume_only_allows_user_paused_monitor() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    registry.transition_status(
        monitor_id,
        MonitorStatus.ACTIVE,
        MonitorStatus.PAUSED_USER,
        owner_id=OWNER,
    )
    actions = PendingActionService(connection)
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.RESUME, monitor_id)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )

    preview = _run(service.handle(_request(f"/resume {monitor_id}")))
    token = preview.buttons[0][0].callback_data.removeprefix("confirm:")
    reply = _run(service.handle(actions.consume(token, OWNER, now=NOW)))

    assert "재개했습니다" in reply.text
    assert registry.get_control_monitor(monitor_id, owner_id=OWNER).status is MonitorStatus.ACTIVE
    connection.execute(
        "UPDATE monitors SET status = ? WHERE id = ?",
        (MonitorStatus.PAUSED_AUTH.value, monitor_id),
    )
    denied = _run(service.handle(_request(f"/resume {monitor_id}")))
    assert denied.buttons == ()
    assert "확인할 수 없습니다" in denied.text
    connection.close()


def test_delete_confirmation_soft_deletes_without_removing_versions_or_observations() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    connection.execute(
        "INSERT INTO observations(monitor_id, item_id, fields_json, content_hash, "
        "first_seen_at, last_seen_at) VALUES (?, 'item-1', '{}', 'hash', ?, ?)",
        (monitor_id, NOW.isoformat(), NOW.isoformat()),
    )
    actions = PendingActionService(connection)
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.DELETE, monitor_id)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )

    preview = _run(service.handle(_request(f"/delete {monitor_id}")))
    token = preview.buttons[0][0].callback_data.removeprefix("confirm:")
    result = _run(service.handle(actions.consume(token, OWNER, now=NOW)))

    assert "삭제했습니다" in result.text
    row = connection.execute(
        "SELECT status, disabled_at FROM monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    assert tuple(row) == (MonitorStatus.DISABLED.value, NOW.isoformat())
    assert (
        connection.execute(
            "SELECT count(*) FROM monitor_versions WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT count(*) FROM observations WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_create_is_absent_before_confirmation_and_created_for_authenticated_owner() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    proposal = _proposal(actions)
    create_intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    planner = FakePlanner(proposal, actions)
    service = ControlService(
        FakeIntentRouter(create_intent),
        registry,
        planner,
        actions,
        now_source=lambda: NOW,
    )

    preview = _run(service.handle(_request("새 상품을 모니터해줘")))

    assert registry.list_monitors(OWNER) == []
    assert preview.buttons[0][0].text == "등록"
    consumed = actions.consume(proposal.pending_action.token, OWNER, now=NOW)
    result = _run(service.handle(consumed))

    assert "등록했습니다" in result.text
    assert len(registry.list_monitors(OWNER)) == 1
    assert registry.list_monitors("telegram-user:8") == []
    connection.close()


def test_create_preview_rejects_same_database_pending_with_wrong_payload() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    proposal = _proposal(actions)
    actions.revoke(proposal.pending_action.token, OWNER)
    wrong_pending = actions.create(
        OWNER,
        "create",
        {"candidate_version_id": "wrong-payload"},
        now=NOW,
    )
    _rebind_proposal_pending(proposal, wrong_pending)
    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakePlanner(proposal, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("새 상품을 모니터해줘")))

    assert "처리하지 못했습니다" in reply.text
    assert reply.buttons == ()
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_create_preview_rejects_pending_from_different_database() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    foreign_connection = open_database(":memory:")
    RegistryRepository(foreign_connection).create_user(OWNER, 7)
    foreign_actions = PendingActionService(foreign_connection)
    proposal = _proposal(foreign_actions)
    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakePlanner(proposal, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("새 상품을 모니터해줘")))

    assert "처리하지 못했습니다" in reply.text
    assert reply.buttons == ()
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    assert (
        foreign_connection.execute(
            "SELECT count(*) FROM pending_actions WHERE consumed_at IS NULL"
        ).fetchone()[0]
        == 1
    )
    connection.close()
    foreign_connection.close()


def test_create_preview_rejects_expired_pending_at_trusted_service_time() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    proposal = _proposal(actions)
    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakePlanner(proposal, actions),
        actions,
        now_source=lambda: NOW.replace(minute=11),
    )

    reply = _run(service.handle(_request("새 상품을 모니터해줘")))

    assert "처리하지 못했습니다" in reply.text
    assert reply.buttons == ()
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_planner_error_html_cookie_and_token_never_reach_reply_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        ErrorPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("새 상품을 모니터해줘")))
    exposed = reply.text + caplog.text

    assert "처리하지 못했습니다" in reply.text
    assert "<html>" not in exposed
    assert "session-secret" not in exposed
    assert "eyJhbGci" not in exposed
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_nested_cancellation_group_from_create_planner_is_reraised_unchanged() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    fatal = BaseExceptionGroup(
        "outer",
        [
            RuntimeError("ordinary"),
            BaseExceptionGroup("inner", [asyncio.CancelledError()]),
        ],
    )

    class GroupPlanner:
        action_service = actions

        async def propose(self, *_args: object, **_kwargs: object) -> object:
            raise fatal

    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        GroupPlanner(),
        actions,
        now_source=lambda: NOW,
    )

    observed = "not-raised"
    try:
        _run(service.handle(_request("새 상품을 모니터해줘")))
    except RecursionError:
        observed = "recursion"
    except BaseException as error:
        observed = "original" if error is fatal else "different"

    assert observed == "original"
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM monitors").fetchone()[0] == 0
    connection.close()


def test_nested_cancellation_group_during_create_reply_revokes_pending_and_reraises() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    proposal = _proposal(actions)
    fatal = BaseExceptionGroup(
        "outer",
        [BaseExceptionGroup("inner", [asyncio.CancelledError()])],
    )

    class FatalSpec:
        @property
        def owner_id(self) -> str:
            raise fatal

    object.__setattr__(proposal, "spec", FatalSpec())
    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakePlanner(proposal, actions),
        actions,
        now_source=lambda: NOW,
    )

    observed = "not-raised"
    try:
        _run(service.handle(_request("새 상품을 모니터해줘")))
    except RecursionError:
        observed = "recursion"
    except BaseException as error:
        observed = "original" if error is fatal else "different"

    assert observed == "original"
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM monitors").fetchone()[0] == 0
    connection.close()


def test_depth_1100_cancellation_group_from_planner_is_reraised_unchanged() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    fatal = _deep_exception_group(asyncio.CancelledError())

    class DeepGroupPlanner:
        action_service = actions

        async def propose(self, *_args: object, **_kwargs: object) -> object:
            raise fatal

    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        DeepGroupPlanner(),
        actions,
        now_source=lambda: NOW,
    )

    observed = "not-raised"
    try:
        _run(service.handle(_request("새 상품을 모니터해줘")))
    except RecursionError:
        observed = "recursion"
    except BaseException as error:
        observed = "original" if error is fatal else "different"

    assert observed == "original"
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM monitors").fetchone()[0] == 0
    connection.close()


def test_depth_1100_cancellation_group_from_reply_revokes_and_reraises() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    proposal = _proposal(actions)
    fatal = _deep_exception_group(asyncio.CancelledError())

    class FatalSpec:
        @property
        def owner_id(self) -> str:
            raise fatal

    object.__setattr__(proposal, "spec", FatalSpec())
    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakePlanner(proposal, actions),
        actions,
        now_source=lambda: NOW,
    )

    observed = "not-raised"
    try:
        _run(service.handle(_request("새 상품을 모니터해줘")))
    except RecursionError:
        observed = "recursion"
    except BaseException as error:
        observed = "original" if error is fatal else "different"

    assert observed == "original"
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM monitors").fetchone()[0] == 0
    connection.close()


def test_schedule_update_stages_unapproved_candidate_then_atomically_activates() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    before = registry.get_active_monitor(monitor_id)
    actions = PendingActionService(connection)
    update_intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text=None,
        schedule_text="매일 오전 9시",
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(update_intent),
        registry,
        FakeUpdatePlanner(_planned_update(), actions),
        actions,
        now_source=lambda: NOW,
    )

    preview = _run(service.handle(_request("확인 주기를 매일 오전 9시로 바꿔줘")))
    candidate = connection.execute(
        "SELECT id, approved_at FROM monitor_versions WHERE monitor_id = ? AND id != ?",
        (monitor_id, before.version_id),
    ).fetchone()

    assert candidate is not None
    assert candidate["approved_at"] is None
    assert registry.get_active_monitor(monitor_id).version_id == before.version_id
    token = preview.buttons[0][0].callback_data.removeprefix("confirm:")
    reply = _run(service.handle(actions.consume(token, OWNER, now=NOW)))

    assert "수정했습니다" in reply.text
    assert registry.get_active_monitor(monitor_id).version_id == candidate["id"]
    assert registry.get_control_monitor(monitor_id, owner_id=OWNER).next_run_at == next_run_at(
        _planned_update().spec,
        monitor_id,
        NOW,
    )
    approved = connection.execute(
        "SELECT approved_by, approved_at FROM monitor_versions WHERE id = ?",
        (candidate["id"],),
    ).fetchone()
    assert approved["approved_by"] == OWNER
    assert approved["approved_at"] is not None
    connection.close()


@pytest.mark.parametrize(
    ("threshold", "other_threshold"),
    ((777777, 888888), (888888, 777777)),
)
def test_condition_only_preview_faithfully_summarizes_actual_safe_rule_values(
    threshold: int,
    other_threshold: int,
) -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    payload = _spec().model_dump(mode="json")
    payload["rules"] = [
        {
            "kind": "numeric_threshold",
            "field": "price",
            "operator": "lt",
            "value": threshold,
        }
    ]
    planned = PlannedMonitor(
        spec=MonitorSpec.model_validate(payload),
        preview_items=(PreviewItem({"price": threshold}),),
        resolved_strategy=FetchStrategy.HTTP,
        robots=RobotsDecision(True, None, NOW, True),
        warnings=(),
    )
    intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text="가격 조건을 바꿔줘",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakeUpdatePlanner(planned, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("가격 조건만 바꿔줘")))

    assert "조건 변경: 1개 → 1개" in reply.text
    assert "검증 미리보기: 1개 항목 통과" in reply.text
    assert "숫자 임계값" in reply.text
    assert "price" in reply.text
    assert "lt" in reply.text
    assert str(threshold) in reply.text
    assert str(other_threshold) not in reply.text
    assert ".price" not in reply.text
    connection.close()


def test_compound_update_preview_shows_both_schedule_and_actual_rule_values() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    payload = _spec().model_dump(mode="json")
    payload["schedule"] = "0 9 * * *"
    payload["rules"] = [
        {
            "kind": "numeric_threshold",
            "field": "price",
            "operator": "lte",
            "value": 654321,
        }
    ]
    planned = PlannedMonitor(
        spec=MonitorSpec.model_validate(payload),
        preview_items=(PreviewItem({"price": 654321}),),
        resolved_strategy=FetchStrategy.HTTP,
        robots=RobotsDecision(True, None, NOW, True),
        warnings=(),
    )
    intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text="가격 조건도 바꿔줘",
        schedule_text="매일 오전 9시",
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakeUpdatePlanner(planned, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("일정과 가격 조건을 같이 바꿔줘")))

    assert "일정 변경: 0 */6 * * * → 0 9 * * *" in reply.text
    assert "조건 변경: 1개 → 1개" in reply.text
    assert "숫자 임계값" in reply.text
    assert "price" in reply.text
    assert "lte" in reply.text
    assert "654321" in reply.text
    connection.close()


def test_keyword_preview_redacts_secret_and_preserves_safe_actual_keyword() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    current_payload = _spec().model_dump(mode="json")
    current_payload["extract"]["fields"]["title"] = {
        "selector": ".title",
        "type": "text",
    }
    current = MonitorSpec.model_validate(current_payload)
    monitor_id = registry.create_monitor(current, created_by=OWNER)
    payload = current.model_dump(mode="json")
    payload["rules"] = [
        {
            "kind": "keyword_match",
            "field": "title",
            "keywords": ["cookie=session-secret", "재입고"],
        }
    ]
    planned = PlannedMonitor(
        spec=MonitorSpec.model_validate(payload),
        preview_items=(PreviewItem({"title": "재입고"}),),
        resolved_strategy=FetchStrategy.HTTP,
        robots=RobotsDecision(True, None, NOW, True),
        warnings=(),
    )
    intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text="재입고 키워드로 바꿔줘",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakeUpdatePlanner(planned, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("재입고 키워드로 바꿔줘")))

    assert "키워드 일치" in reply.text
    assert "title" in reply.text
    assert "재입고" in reply.text
    assert "[숨김]" in reply.text
    assert "session-secret" not in reply.text
    connection.close()


def test_keyword_preview_preserves_long_safe_keyword_without_ellipsis() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    current_payload = _spec().model_dump(mode="json")
    current_payload["extract"]["fields"]["title"] = {
        "selector": ".title",
        "type": "text",
    }
    current = MonitorSpec.model_validate(current_payload)
    monitor_id = registry.create_monitor(current, created_by=OWNER)
    keyword = "재입고-" + "안전한키워드" * 30
    payload = current.model_dump(mode="json")
    payload["rules"] = [
        {
            "kind": "keyword_match",
            "field": "title",
            "keywords": [keyword],
        }
    ]
    planned = PlannedMonitor(
        spec=MonitorSpec.model_validate(payload),
        preview_items=(PreviewItem({"title": "재입고"}),),
        resolved_strategy=FetchStrategy.HTTP,
        robots=RobotsDecision(True, None, NOW, True),
        warnings=(),
    )
    intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text="긴 키워드로 바꿔줘",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakeUpdatePlanner(planned, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("긴 키워드로 바꿔줘")))

    assert keyword in reply.text
    assert "…" not in reply.text
    assert len(reply.text) <= 3_500
    connection.close()


def test_approval_preview_preserves_exact_239_character_repeated_spaces() -> None:
    keyword = "시작" + " " * 235 + "종료"
    assert len(keyword) == 239

    reply, versions, actions = _text_rule_update_reply(
        {
            "kind": "keyword_match",
            "field": "title",
            "keywords": [keyword],
        }
    )

    assert f"키워드={json.dumps(keyword, ensure_ascii=False)}" in reply.text
    assert keyword in reply.text
    assert "…" not in reply.text
    assert (versions, actions) == (2, 1)


@pytest.mark.parametrize(
    "keyword",
    (
        "공백  두개",
        '탭\t줄\n따옴표"역슬래시\\유니코드-한글-💡',
        "조합-e\u0301-완성-é",
    ),
)
def test_approval_preview_json_escapes_without_losing_keyword_semantics(
    keyword: str,
) -> None:
    reply, versions, actions = _text_rule_update_reply(
        {
            "kind": "keyword_match",
            "field": "title",
            "keywords": [keyword],
        }
    )

    assert f"키워드={json.dumps(keyword, ensure_ascii=False)}" in reply.text
    assert "…" not in reply.text
    assert (versions, actions) == (2, 1)


@pytest.mark.parametrize(
    ("value", "expected", "category"),
    (
        ("DEL-\x7f-끝", '"DEL-\\u007F-끝"', "Cc"),
        ("C1-\x85-끝", '"C1-\\u0085-끝"', "Cc"),
        ("BIDI-\u202e-끝", '"BIDI-\\u202E-끝"', "Cf"),
        ("ZWJ-\u200d-끝", '"ZWJ-\\u200D-끝"', "Cf"),
        ("SURROGATE-\ud800-끝", '"SURROGATE-\\uD800-끝"', "Cs"),
        ("PRIVATE-\ue000-끝", '"PRIVATE-\\uE000-끝"', "Co"),
        ("UNASSIGNED-\u0378-끝", '"UNASSIGNED-\\u0378-끝"', "Cn"),
        ("TAG-\U000e0020-끝", '"TAG-\\U000E0020-끝"', "Cf"),
    ),
)
def test_approval_value_visibly_and_losslessly_escapes_every_unicode_c_category(
    value: str,
    expected: str,
    category: str,
) -> None:
    unsafe_character = value.split("-")[1]
    assert unicodedata.category(unsafe_character) == category

    rendered = safe_approval_value(value)

    assert rendered == expected
    assert unsafe_character not in rendered
    assert not any(unicodedata.category(character).startswith("C") for character in rendered)


def test_approval_preview_preserves_korean_emoji_and_visibly_represents_zwj() -> None:
    keyword = "개발자 👩\u200d💻 채용"

    reply, versions, actions = _text_rule_update_reply(
        {
            "kind": "keyword_match",
            "field": "title",
            "keywords": [keyword],
        }
    )

    assert '키워드="개발자 👩\\u200D💻 채용"' in reply.text
    assert "\u200d" not in reply.text
    assert (versions, actions) == (2, 1)


def test_distinct_unicode_c_values_produce_distinct_lossless_approval_values() -> None:
    values = ("\x7f", "\x85", "\u202e", "\u200d", "\ud800", "\ue000", "\u0378", "\U000e0020")

    rendered = tuple(safe_approval_value(f"값-{value}-끝") for value in values)

    assert len(set(rendered)) == len(values)


def test_unicode_c_escape_expansion_obeys_the_exact_rendered_limit() -> None:
    with pytest.raises(ValueError, match="invalid approval value"):
        safe_approval_value("\ue000" * 600, limit=3_500)


def test_distinct_whitespace_keywords_produce_distinct_complete_previews() -> None:
    compact = "서울 경기"
    repeated = "서울  경기"

    compact_reply, _, _ = _text_rule_update_reply(
        {
            "kind": "keyword_match",
            "field": "title",
            "keywords": [compact],
        }
    )
    repeated_reply, _, _ = _text_rule_update_reply(
        {
            "kind": "keyword_match",
            "field": "title",
            "keywords": [repeated],
        }
    )

    assert compact_reply.text != repeated_reply.text
    assert json.dumps(compact, ensure_ascii=False) in compact_reply.text
    assert json.dumps(repeated, ensure_ascii=False) in repeated_reply.text


def test_approval_preview_preserves_long_status_string_with_visible_escapes() -> None:
    value = ('상태  값\t줄\n"인용"\\경로-유니코드💡' * 20) + "끝"

    reply, versions, actions = _text_rule_update_reply(
        {
            "kind": "status_equals",
            "field": "title",
            "value": value,
        }
    )

    assert f"값={json.dumps(value, ensure_ascii=False)}" in reply.text
    assert "…" not in reply.text
    assert (versions, actions) == (2, 1)


def test_approval_escape_expansion_overflow_rolls_back_without_writes() -> None:
    keywords = [f"키워드-{index}" + "\t " * 80 + "끝" for index in range(50)]

    reply, versions, actions = _text_rule_update_reply(
        {
            "kind": "keyword_match",
            "field": "title",
            "keywords": keywords,
        }
    )

    assert "처리하지 못했습니다" in reply.text
    assert (versions, actions) == (1, 0)


def test_unicode_c_escape_expansion_overflow_rolls_back_without_writes() -> None:
    keywords = [f"키워드-{index}-" + "\ue000" * 80 + "-끝" for index in range(50)]

    reply, versions, actions = _text_rule_update_reply(
        {
            "kind": "keyword_match",
            "field": "title",
            "keywords": keywords,
        }
    )

    assert "처리하지 못했습니다" in reply.text
    assert (versions, actions) == (1, 0)


def test_status_value_preview_preserves_long_safe_value_without_ellipsis() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    current_payload = _spec().model_dump(mode="json")
    current_payload["extract"]["fields"]["title"] = {
        "selector": ".title",
        "type": "text",
    }
    current = MonitorSpec.model_validate(current_payload)
    monitor_id = registry.create_monitor(current, created_by=OWNER)
    value = "상태-" + "검증된값" * 100
    payload = current.model_dump(mode="json")
    payload["rules"] = [
        {
            "kind": "status_equals",
            "field": "title",
            "value": value,
        }
    ]
    planned = PlannedMonitor(
        spec=MonitorSpec.model_validate(payload),
        preview_items=(PreviewItem({"title": value}),),
        resolved_strategy=FetchStrategy.HTTP,
        robots=RobotsDecision(True, None, NOW, True),
        warnings=(),
    )
    intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text="긴 상태 값으로 바꿔줘",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakeUpdatePlanner(planned, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("긴 상태 값으로 바꿔줘")))

    assert value in reply.text
    assert "…" not in reply.text
    assert len(reply.text) <= 3_500
    connection.close()


def test_oversized_rule_preview_rolls_back_candidate_and_action_atomically() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    current_payload = _spec().model_dump(mode="json")
    current_payload["extract"]["fields"]["title"] = {
        "selector": ".title",
        "type": "text",
    }
    current = MonitorSpec.model_validate(current_payload)
    monitor_id = registry.create_monitor(current, created_by=OWNER)
    payload = current.model_dump(mode="json")
    payload["rules"] = [
        {
            "kind": "keyword_match",
            "field": "title",
            "keywords": [f"keyword-{index}-" + "x" * 200 for index in range(50)],
        }
    ]
    planned = PlannedMonitor(
        spec=MonitorSpec.model_validate(payload),
        preview_items=(PreviewItem({"title": "safe"}),),
        resolved_strategy=FetchStrategy.HTTP,
        robots=RobotsDecision(True, None, NOW, True),
        warnings=(),
    )
    intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text="키워드를 바꿔줘",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakeUpdatePlanner(planned, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("키워드를 바꿔줘")))

    assert "처리하지 못했습니다" in reply.text
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_schedule_only_update_rejects_every_hidden_spec_change_without_writes() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    payload = _spec().model_dump(mode="json")
    payload.update(
        {
            "schedule": "0 9 * * *",
            "name": "몰래 바뀐 이름",
            "notify_on_no_change": True,
            "rules": [
                {
                    "kind": "numeric_threshold",
                    "field": "price",
                    "operator": "lt",
                    "value": 1,
                }
            ],
        }
    )
    hostile = PlannedMonitor(
        spec=MonitorSpec.model_validate(payload),
        preview_items=(PreviewItem({"price": 1}),),
        resolved_strategy=FetchStrategy.HTTP,
        robots=RobotsDecision(True, None, NOW, True),
        warnings=(),
    )
    intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text=None,
        schedule_text="매일 오전 9시",
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakeUpdatePlanner(hostile, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("일정만 매일 오전 9시로 바꿔줘")))

    assert "처리하지 못했습니다" in reply.text
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_compound_update_requires_both_schedule_and_rules_to_change() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text="가격 조건도 바꿔줘",
        schedule_text="매일 오전 9시",
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakeUpdatePlanner(_planned_update(), actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("일정과 가격 조건을 같이 바꿔줘")))

    assert "처리하지 못했습니다" in reply.text
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_router_cannot_swap_registry_database_across_await() -> None:
    registry, connection = _registry()
    other = open_database(":memory:")
    RegistryRepository(other).create_user(OWNER, 7)
    actions = PendingActionService(connection)
    create_intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )

    class SwappingRouter:
        async def route(self, _request: ControlRequest) -> IntentResult:
            registry.connection = other
            return create_intent

    class RecordingPlanner:
        def __init__(self, action_service: PendingActionService) -> None:
            self.action_service = action_service
            self.called = False

        async def propose(self, *_args: object) -> object:
            self.called = True
            raise AssertionError("planner must not run after composition swap")

    planner = RecordingPlanner(actions)
    service = ControlService(
        SwappingRouter(),
        registry,
        planner,
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("새 상품을 모니터해줘")))

    assert "처리하지 못했습니다" in reply.text
    assert planner.called is False
    assert connection.execute("SELECT count(*) FROM monitors").fetchone()[0] == 0
    assert other.execute("SELECT count(*) FROM monitors").fetchone()[0] == 0
    connection.close()
    other.close()


def test_needs_review_status_offers_only_adaptive_repair_and_activates_exact_candidate() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    active = registry.get_active_monitor(monitor_id)
    registry.add_version(
        monitor_id,
        _spec(name="임의 후보"),
        created_by="codex-control",
        approved=False,
    )
    adaptive = registry.add_version(
        monitor_id,
        _spec(name="복구 후보"),
        created_by="scrapling-adaptive",
        approved=False,
    )
    connection.execute(
        "UPDATE monitors SET status = ? WHERE id = ?",
        (MonitorStatus.NEEDS_REVIEW.value, monitor_id),
    )
    actions = PendingActionService(connection)
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.STATUS, monitor_id)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )

    preview = _run(service.handle(_request(f"/status {monitor_id}")))

    assert "일반 알림은" in preview.text
    assert "복구 후보" in preview.text
    assert ".price" not in preview.text
    token = preview.buttons[0][0].callback_data.removeprefix("confirm:")
    result = _run(service.handle(actions.consume(token, OWNER, now=NOW)))

    repaired = registry.get_control_monitor(monitor_id, owner_id=OWNER)
    assert "복구를 적용했습니다" in result.text
    assert repaired.status is MonitorStatus.ACTIVE
    assert repaired.active_version_id == adaptive
    assert repaired.active_version_id != active.version_id
    connection.close()


def test_adaptive_candidate_without_stored_current_parent_is_never_offered() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    candidate = registry.add_version(
        monitor_id,
        _spec(name="출처 없는 복구 후보"),
        created_by="scrapling-adaptive",
        approved=False,
    )
    connection.execute(
        "UPDATE monitor_versions SET parent_version_id = NULL WHERE id = ?",
        (candidate,),
    )
    connection.execute(
        "UPDATE monitors SET status = ? WHERE id = ?",
        (MonitorStatus.NEEDS_REVIEW.value, monitor_id),
    )
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.STATUS, monitor_id)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request(f"/status {monitor_id}")))

    assert "복구 적용" not in reply.text
    assert reply.buttons == ()
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_update_candidate_and_pending_roll_back_together_on_database_failure() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    connection.execute(
        "CREATE TRIGGER reject_pending BEFORE INSERT ON pending_actions "
        "BEGIN SELECT RAISE(ABORT, 'private-db-secret'); END"
    )
    actions = PendingActionService(connection)
    update_intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text=None,
        schedule_text="매일 오전 9시",
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(update_intent),
        registry,
        FakeUpdatePlanner(_planned_update(), actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("매일 오전 9시로 바꿔줘")))

    assert "private-db-secret" not in reply.text
    assert (
        connection.execute(
            "SELECT count(*) FROM monitor_versions WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()[0]
        == 1
    )
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_edit_consumes_update_action_without_activation() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    before = registry.get_active_monitor(monitor_id)
    actions = PendingActionService(connection)
    intent = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=[monitor_id],
        target_url=None,
        condition_text=None,
        schedule_text="매일 오전 9시",
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakeUpdatePlanner(_planned_update(), actions),
        actions,
        now_source=lambda: NOW,
    )
    preview = _run(service.handle(_request("일정을 바꿔줘")))
    token = preview.buttons[0][1].callback_data.removeprefix("edit:")

    guidance = _run(service.handle(actions.consume(token, OWNER, now=NOW, operation="edit")))

    assert guidance.text == "원하는 변경 내용을 새 메시지로 보내주세요."
    assert registry.get_active_monitor(monitor_id).version_id == before.version_id
    assert (
        connection.execute(
            "SELECT count(*) FROM monitor_versions WHERE approved_at IS NULL"
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_stale_pause_confirmation_fails_without_overriding_review_state() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    actions = PendingActionService(connection)
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.PAUSE, monitor_id)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )
    preview = _run(service.handle(_request(f"/pause {monitor_id}")))
    connection.execute(
        "UPDATE monitors SET status = ? WHERE id = ?",
        (MonitorStatus.NEEDS_REVIEW.value, monitor_id),
    )
    token = preview.buttons[0][0].callback_data.removeprefix("confirm:")

    reply = _run(service.handle(actions.consume(token, OWNER, now=NOW)))

    assert "처리하지 못했습니다" in reply.text
    assert registry.get_control_monitor(monitor_id, owner_id=OWNER).status is (
        MonitorStatus.NEEDS_REVIEW
    )
    connection.close()


def test_create_preview_validation_failure_revokes_orphan_action() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    proposal = _proposal(actions)
    object.__setattr__(proposal, "spec_hash", "0" * 64)
    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=_spec().target_url,
        condition_text="새 상품",
        schedule_text=None,
        clarification=None,
        confidence=1.0,
    )
    service = ControlService(
        FakeIntentRouter(intent),
        registry,
        FakePlanner(proposal, actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request("새 상품을 모니터해줘")))

    assert "처리하지 못했습니다" in reply.text
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_stale_repair_confirmation_leaves_candidate_unapproved() -> None:
    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    before = registry.get_active_monitor(monitor_id)
    candidate = registry.add_version(
        monitor_id,
        _spec(name="복구 후보"),
        created_by="scrapling-adaptive",
        approved=False,
    )
    connection.execute(
        "UPDATE monitors SET status = ? WHERE id = ?",
        (MonitorStatus.NEEDS_REVIEW.value, monitor_id),
    )
    actions = PendingActionService(connection)
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.STATUS, monitor_id)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )
    preview = _run(service.handle(_request(f"/status {monitor_id}")))
    connection.execute(
        "UPDATE monitors SET status = ? WHERE id = ?",
        (MonitorStatus.PAUSED_AUTH.value, monitor_id),
    )
    token = preview.buttons[0][0].callback_data.removeprefix("confirm:")

    reply = _run(service.handle(actions.consume(token, OWNER, now=NOW)))

    assert "처리하지 못했습니다" in reply.text
    assert registry.get_active_monitor(monitor_id).version_id == before.version_id
    assert (
        connection.execute(
            "SELECT approved_at FROM monitor_versions WHERE id = ?", (candidate,)
        ).fetchone()[0]
        is None
    )
    connection.close()


def test_corrupt_status_data_returns_fixed_failure_without_database_value() -> None:
    registry, connection = _registry()
    actions = PendingActionService(connection)
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    connection.execute(
        "UPDATE monitors SET next_run_at = 'private-db-secret' WHERE id = ?",
        (monitor_id,),
    )
    service = ControlService(
        FakeIntentRouter(_intent(IntentKind.STATUS, monitor_id)),
        registry,
        UnusedPlanner(actions),
        actions,
        now_source=lambda: NOW,
    )

    reply = _run(service.handle(_request(f"/status {monitor_id}")))

    assert reply.text == "해당 모니터를 확인할 수 없습니다."
    assert "private-db-secret" not in reply.text
    connection.close()


def test_candidate_reply_base_exception_rolls_back_version_and_pending_action() -> None:
    class FatalReply(BaseException):
        pass

    registry, connection = _registry()
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    current = registry.get_control_monitor(monitor_id, owner_id=OWNER)

    with pytest.raises(FatalReply):
        registry.stage_candidate_action(
            monitor_id,
            owner_id=OWNER,
            expected_status=MonitorStatus.ACTIVE,
            expected_active_version_id=current.active_version_id,
            spec=_planned_update().spec,
            action_kind="schedule_change",
            actions=PendingActionService(connection),
            now=NOW,
            reply_factory=lambda *_args: (_ for _ in ()).throw(FatalReply()),
        )

    assert (
        connection.execute(
            "SELECT count(*) FROM monitor_versions WHERE monitor_id = ?", (monitor_id,)
        ).fetchone()[0]
        == 1
    )
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    connection.close()


def test_two_connections_racing_candidate_activation_have_one_atomic_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control-race.db"
    connection = open_database(path)
    registry = RegistryRepository(connection)
    registry.create_user(OWNER, 7)
    monitor_id = registry.create_monitor(_spec(), created_by=OWNER)
    current = registry.get_control_monitor(monitor_id, owner_id=OWNER)
    updated = _planned_update().spec
    digest = hashlib.sha256(canonical_json(updated.model_dump(mode="json")).encode()).hexdigest()
    registry.stage_candidate_action(
        monitor_id,
        owner_id=OWNER,
        expected_status=MonitorStatus.ACTIVE,
        expected_active_version_id=current.active_version_id,
        spec=updated,
        action_kind="schedule_change",
        actions=PendingActionService(connection),
        now=NOW,
        reply_factory=lambda pending, *_: ControlReply(
            "변경 확인",
            ((InlineButton("확인", pending.confirm_callback),),),
        ),
    )
    candidate = connection.execute(
        "SELECT id FROM monitor_versions WHERE monitor_id = ? AND approved_at IS NULL",
        (monitor_id,),
    ).fetchone()["id"]
    connection.close()
    barrier = Barrier(2)

    def activate(_worker: int) -> str:
        worker_connection = open_database(path)
        try:
            worker_registry = RegistryRepository(worker_connection)
            barrier.wait()
            try:
                worker_registry.activate_candidate_exact(
                    monitor_id,
                    candidate,
                    owner_id=OWNER,
                    expected_status=MonitorStatus.ACTIVE,
                    expected_active_version_id=current.active_version_id,
                    expected_created_by="codex-control",
                    spec_hash=digest,
                    activated_at=NOW,
                )
                return "activated"
            except ValueError:
                return "denied"
        finally:
            worker_connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(activate, (1, 2)))

    assert outcomes == ["activated", "denied"]
    final = open_database(path)
    row = final.execute(
        "SELECT m.active_version_id, v.approved_by, v.approved_at "
        "FROM monitors AS m JOIN monitor_versions AS v ON v.id = m.active_version_id "
        "WHERE m.id = ?",
        (monitor_id,),
    ).fetchone()
    assert row["active_version_id"] == candidate
    assert row["approved_by"] == OWNER
    assert row["approved_at"] is not None
    final.close()
