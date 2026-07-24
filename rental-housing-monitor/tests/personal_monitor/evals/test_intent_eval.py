from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from personal_monitor.ai.contracts import IntentKind, IntentRequest, IntentResult
from personal_monitor.control.intents import IntentRouter, OwnedMonitorSummary
from personal_monitor.telegram.gateway import ControlRequest

OWNER = "telegram-user:7"
OTHER_OWNER = "telegram-user:8"
CHAT_ID = "-100777"
TOKEN_MARKER = "fixture_telegram_token_value"


@dataclass(frozen=True)
class IntentCase:
    message: str
    result: dict[str, object]
    url_expected: bool
    condition_expected: bool
    schedule_expected: bool
    clarification_expected: bool = False


CASES = (
    IntentCase(
        "https://example.com/products 가격이 10만원 아래면 알려줘",
        {
            "kind": "create",
            "target_monitor_ids": [],
            "target_url": "https://example.com/products",
            "condition_text": "가격 10만원 미만",
            "schedule_text": None,
            "clarification": None,
            "confidence": 0.97,
        },
        True,
        True,
        False,
    ),
    IntentCase(
        "https://example.com/stock 재고가 입고면 알려줘",
        {
            "kind": "create",
            "target_monitor_ids": [],
            "target_url": "https://example.com/stock",
            "condition_text": "재고 상태 입고",
            "schedule_text": None,
            "clarification": None,
            "confidence": 0.94,
        },
        True,
        True,
        False,
    ),
    IntentCase(
        "https://example.com/notices 청년 공고가 나오면 평일마다 확인해줘",
        {
            "kind": "create",
            "target_monitor_ids": [],
            "target_url": "https://example.com/notices",
            "condition_text": "청년 키워드",
            "schedule_text": "평일마다",
            "clarification": None,
            "confidence": 0.93,
        },
        True,
        True,
        True,
    ),
    IntentCase(
        "https://example.com/listing 같은 매물의 상태가 바뀌면 알려줘",
        {
            "kind": "create",
            "target_monitor_ids": [],
            "target_url": "https://example.com/listing",
            "condition_text": "같은 매물 상태 변경",
            "schedule_text": None,
            "clarification": None,
            "confidence": 0.95,
        },
        True,
        True,
        False,
    ),
    IntentCase(
        "가격 알림 상태 알려줘",
        {
            "kind": "status",
            "target_monitor_ids": ["price-monitor"],
            "target_url": None,
            "condition_text": None,
            "schedule_text": None,
            "clarification": None,
            "confidence": 0.96,
        },
        False,
        False,
        False,
    ),
    IntentCase(
        "임대 공고 모니터 잠깐 멈춰줘",
        {
            "kind": "pause",
            "target_monitor_ids": ["notice-monitor"],
            "target_url": None,
            "condition_text": None,
            "schedule_text": None,
            "clarification": None,
            "confidence": 0.92,
        },
        False,
        False,
        False,
    ),
    IntentCase(
        "임대 공고 모니터 다시 시작해줘",
        {
            "kind": "resume",
            "target_monitor_ids": ["notice-monitor"],
            "target_url": None,
            "condition_text": None,
            "schedule_text": None,
            "clarification": None,
            "confidence": 0.92,
        },
        False,
        False,
        False,
    ),
    IntentCase(
        "가격 알림을 매시간 확인하도록 바꿔줘",
        {
            "kind": "update",
            "target_monitor_ids": ["price-monitor"],
            "target_url": None,
            "condition_text": None,
            "schedule_text": "매시간",
            "clarification": None,
            "confidence": 0.95,
        },
        False,
        False,
        True,
    ),
    IntentCase(
        "임대 공고 모니터 삭제해줘",
        {
            "kind": "delete",
            "target_monitor_ids": ["notice-monitor"],
            "target_url": None,
            "condition_text": None,
            "schedule_text": None,
            "clarification": None,
            "confidence": 0.91,
        },
        False,
        False,
        False,
    ),
    IntentCase(
        "등록한 모니터 목록 보여줘",
        {
            "kind": "list",
            "target_monitor_ids": [],
            "target_url": None,
            "condition_text": None,
            "schedule_text": None,
            "clarification": None,
            "confidence": 0.98,
        },
        False,
        False,
        False,
    ),
    IntentCase(
        "그거 다시 확인해줘",
        {
            "kind": "unknown",
            "target_monitor_ids": [],
            "target_url": None,
            "condition_text": None,
            "schedule_text": None,
            "clarification": "어느 모니터인지 알려주세요",
            "confidence": 0.42,
        },
        False,
        False,
        False,
        True,
    ),
)


class Provider:
    def list_monitors(self, owner_id: str) -> tuple[OwnedMonitorSummary, ...]:
        assert owner_id == OWNER
        return (
            OwnedMonitorSummary(OWNER, "notice-monitor", "임대 공고", "active"),
            OwnedMonitorSummary(OWNER, "price-monitor", "가격 알림", "paused_user"),
        )


class SchemaWorker:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[IntentRequest, str, str]] = []

    async def run(self, request: IntentRequest, *, model: str, effort: str) -> object:
        self.calls.append((request, model, effort))
        output = self.outputs.pop(0)
        if isinstance(output, dict):
            return IntentResult.model_validate(output)
        return output


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.result["kind"])
def test_korean_intents_obey_structured_owned_boundary(case: IntentCase) -> None:
    worker = SchemaWorker([case.result])
    router = IntentRouter(Provider(), worker)

    result = run(router.route(ControlRequest(OWNER, CHAT_ID, case.message)))

    assert isinstance(result, IntentResult)
    assert result.kind.value == case.result["kind"]
    assert bool(result.target_url) is case.url_expected
    assert bool(result.condition_text) is case.condition_expected
    assert bool(result.schedule_text) is case.schedule_expected
    assert bool(result.clarification) is case.clarification_expected
    assert 0 <= result.confidence <= 1
    assert all(
        target in {"notice-monitor", "price-monitor"} for target in result.target_monitor_ids
    )
    assert [(model, effort) for _, model, effort in worker.calls] == [("gpt-5.6-terra", "medium")]


def test_invalid_owned_target_uses_bounded_escalation_without_guessing() -> None:
    invalid = {
        "kind": "delete",
        "target_monitor_ids": ["other-owner-monitor"],
        "target_url": None,
        "condition_text": None,
        "schedule_text": None,
        "clarification": None,
        "confidence": 0.99,
    }
    valid = {
        **invalid,
        "target_monitor_ids": ["price-monitor"],
    }
    worker = SchemaWorker([invalid, invalid, valid])
    router = IntentRouter(Provider(), worker)

    result = run(router.route(ControlRequest(OWNER, CHAT_ID, "가격 알림을 삭제해줘")))

    assert isinstance(result, IntentResult)
    assert result.target_monitor_ids == ["price-monitor"]
    assert [(model, effort) for _, model, effort in worker.calls] == [
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-sol", "high"),
    ]


def test_ambiguous_low_confidence_result_clarifies_without_unbounded_retry() -> None:
    worker = SchemaWorker([CASES[-1].result])
    router = IntentRouter(Provider(), worker)

    result = run(router.route(ControlRequest(OWNER, CHAT_ID, CASES[-1].message)))

    assert isinstance(result, IntentResult)
    assert result.kind is IntentKind.UNKNOWN
    assert result.target_monitor_ids == []
    assert result.clarification
    assert len(worker.calls) == 1


@pytest.mark.parametrize(
    "command",
    [
        "/monitors",
        "/status price-monitor",
        "/pause price-monitor",
        "/resume price-monitor",
        "/delete price-monitor",
        "/cancel",
    ],
)
def test_exact_commands_never_call_worker(command: str) -> None:
    worker = SchemaWorker([])
    router = IntentRouter(Provider(), worker)

    result = run(router.route(ControlRequest(OWNER, CHAT_ID, command)))

    assert isinstance(result, IntentResult)
    assert worker.calls == []


def test_worker_request_has_only_owner_and_owned_bounded_summaries() -> None:
    worker = SchemaWorker([CASES[0].result])
    router = IntentRouter(Provider(), worker)

    run(router.route(ControlRequest(OWNER, CHAT_ID, CASES[0].message)))

    request = worker.calls[0][0]
    wire = request.model_dump_json()
    assert request.owner_id == OWNER
    assert request.monitor_summaries == [
        "id=notice-monitor | name=임대 공고 | status=active",
        "id=price-monitor | name=가격 알림 | status=paused_user",
    ]
    for forbidden in (
        OTHER_OWNER,
        CHAT_ID,
        TOKEN_MARKER,
        "confirm:",
        "cancel:",
        "edit:",
        "sqlite",
    ):
        assert forbidden not in wire
