from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from personal_monitor.ai.contracts import IntentKind, IntentRequest, IntentResult
from personal_monitor.ai.worker import CodexWorkerError
from personal_monitor.control.intents import (
    IntentRouter,
    IntentRouterError,
    OwnedMonitorSummary,
)
from personal_monitor.telegram.gateway import ControlRequest

OWNER = "telegram-user:7"
OTHER_OWNER = "telegram-user:8"
REQUEST = lambda text: ControlRequest(OWNER, "42", text)  # noqa: E731
MONITORS = (
    OwnedMonitorSummary(OWNER, "m1", "임대주택 알림", "active"),
    OwnedMonitorSummary(OWNER, "m2", "가격 알림", "paused_user"),
)


class FakeProvider:
    def __init__(self, rows: object = MONITORS) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def list_monitors(self, owner_id: str) -> object:
        self.calls.append(owner_id)
        return self.rows


class FakeWorker:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.calls: list[tuple[IntentRequest, str, str]] = []

    async def run(self, request: IntentRequest, *, model: str, effort: str) -> object:
        self.calls.append((request, model, effort))
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _result(
    kind: IntentKind,
    *,
    targets: list[str] | None = None,
    url: str | None = None,
    condition: str | None = None,
    schedule: str | None = None,
    clarification: str | None = None,
    confidence: float = 0.95,
) -> IntentResult:
    return IntentResult(
        kind=kind,
        target_monitor_ids=[] if targets is None else targets,
        target_url=url,
        condition_text=condition,
        schedule_text=schedule,
        clarification=clarification,
        confidence=confidence,
    )


def run(value: object) -> Any:
    return asyncio.run(value)  # type: ignore[arg-type]


def _router(
    values: list[object],
    rows: object = MONITORS,
) -> tuple[IntentRouter, FakeProvider, FakeWorker]:
    provider = FakeProvider(rows)
    worker = FakeWorker(values)
    return IntentRouter(provider, worker), provider, worker


def test_fixed_korean_evaluation_fixture_routes_expected_structured_kinds() -> None:
    fixture = Path("tests/fixtures/personal_monitor/intent_cases.json")
    cases = json.loads(fixture.read_text(encoding="utf-8"))
    outputs = {
        "list": _result(IntentKind.LIST),
        "pause": _result(IntentKind.PAUSE, targets=["m1"]),
        "update": _result(IntentKind.UPDATE, targets=["m1"], schedule="매일 한 번"),
        "delete": _result(IntentKind.DELETE, targets=["m2"]),
        "create": _result(
            IntentKind.CREATE,
            url="https://example.com/p/7",
            condition="10만 원 아래",
        ),
        "unknown": _result(IntentKind.UNKNOWN, clarification="어떤 모니터 요청인지 알려주세요"),
    }
    router, provider, worker = _router([outputs[case["kind"]] for case in cases])

    results = [run(router.route(REQUEST(case["text"]))) for case in cases]

    assert [result.kind.value for result in results] == [case["kind"] for case in cases]
    assert provider.calls == [OWNER] * len(cases)
    assert len(worker.calls) == len(cases)


@pytest.mark.parametrize(
    ("text", "kind", "target"),
    [
        ("/monitors", IntentKind.LIST, None),
        ("/status m1", IntentKind.STATUS, "m1"),
        ("/pause m1", IntentKind.PAUSE, "m1"),
        ("/resume m1", IntentKind.RESUME, "m1"),
        ("/delete m1", IntentKind.DELETE, "m1"),
        ("/cancel", IntentKind.UNKNOWN, None),
    ],
)
def test_exact_commands_bypass_worker(text: str, kind: IntentKind, target: str | None) -> None:
    router, provider, worker = _router([])

    result = run(router.route(REQUEST(f"  {text}  ")))

    assert result.kind is kind
    assert result.target_monitor_ids == ([] if target is None else [target])
    assert result.confidence == 1
    assert worker.calls == []
    assert provider.calls == ([] if text in {"/monitors", "/cancel"} else [OWNER])


@pytest.mark.parametrize(
    "text",
    (
        "구글 크레딧 얼마나 남았어?",
        "GCP 무료 크레딧 잔액 알려줘",
        "구글 클라우드 비용 사용량 보여줘",
    ),
)
def test_common_billing_status_language_bypasses_worker(text: str) -> None:
    router, provider, worker = _router([])

    result = run(router.route(REQUEST(text)))

    assert result == _result(IntentKind.BILLING_STATUS, confidence=1)
    assert provider.calls == []
    assert worker.calls == []


def test_worker_billing_status_requires_no_monitor_target_or_other_fields() -> None:
    output = _result(IntentKind.BILLING_STATUS)
    router, provider, worker = _router([output])

    result = run(router.route(REQUEST("이번 달 클라우드 쓴 돈은?")))

    assert result is output
    assert provider.calls == [OWNER]
    assert len(worker.calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        "/pause",
        "/pause  m1",
        "/pause\tm1",
        "/pause m1 extra",
        "/pause m1\npayload",
        "/unknown",
        "/MONITORS",
        "//monitors",
        "／pause m1",
        "/status m1;delete",
        "/delete " + "x" * 129,
    ],
)
def test_malformed_and_unknown_slash_commands_clarify_without_dependencies(text: str) -> None:
    router, provider, worker = _router([])

    result = run(router.route(REQUEST(text)))

    assert result.kind is IntentKind.UNKNOWN
    assert result.target_monitor_ids == []
    assert result.clarification
    assert provider.calls == []
    assert worker.calls == []


@pytest.mark.parametrize(
    "prefix",
    ["⧸", "⫽", "╱", "⁄", "∕", "／", "\u0338", "⟋", "ꜘ"],
)
def test_unicode_slash_lookalikes_never_reach_dependencies(prefix: str) -> None:
    router, provider, worker = _router([_result(IntentKind.LIST)])

    result = run(router.route(REQUEST(f"{prefix}pause m1")))

    assert result.kind is IntentKind.UNKNOWN
    assert result.clarification
    assert provider.calls == []
    assert worker.calls == []


@pytest.mark.parametrize("text", ["🫤 요즘 어때?", "▩ 이 표시는 뭐야?", "⛞ 상태 알려줘"])
def test_unrelated_diagonal_named_symbols_remain_natural_language(text: str) -> None:
    output = _result(IntentKind.UNKNOWN, clarification="어떤 요청인지 알려주세요")
    router, provider, worker = _router([output])

    result = run(router.route(REQUEST(text)))

    assert result.clarification == output.clarification
    assert provider.calls == [OWNER]
    assert len(worker.calls) == 1
    assert worker.calls[0][0].message == text


def test_unknown_or_unowned_exact_command_target_does_not_guess() -> None:
    router, provider, worker = _router([])

    result = run(router.route(REQUEST("/delete other-owner-monitor")))

    assert result.kind is IntentKind.UNKNOWN
    assert result.target_monitor_ids == []
    assert result.clarification
    assert provider.calls == [OWNER]
    assert worker.calls == []


def test_natural_request_contains_only_owned_bounded_summaries_and_no_chat_id() -> None:
    output = _result(IntentKind.LIST)
    router, provider, worker = _router([output])
    text = "내 모니터 보여줘"

    result = run(router.route(REQUEST(text)))

    assert result is output
    request, _, _ = worker.calls[0]
    assert request.owner_id == OWNER
    assert request.message == text
    assert request.monitor_summaries == [
        "id=m1 | name=임대주택 알림 | status=active",
        "id=m2 | name=가격 알림 | status=paused_user",
    ]
    dumped = request.model_dump_json()
    assert '"42"' not in dumped
    assert provider.calls == [OWNER]
    assert text not in request.request_id


@pytest.mark.parametrize(
    "rows",
    [
        tuple(OwnedMonitorSummary(OWNER, f"m{i}", "name", "active") for i in range(101)),
        (OwnedMonitorSummary(OTHER_OWNER, "m1", "name", "active"),),
        (OwnedMonitorSummary(OWNER, "m1", "name", "active"),) * 2,
        ("not-a-summary",),
    ],
)
def test_invalid_provider_output_fails_with_fixed_redacted_boundary(rows: object) -> None:
    secret = "private-provider-secret"
    router, _, worker = _router([_result(IntentKind.LIST)], rows)

    with pytest.raises(IntentRouterError) as caught:
        run(router.route(REQUEST(secret)))

    assert str(caught.value) == "intent routing failed"
    assert repr(caught.value) == "IntentRouterError(<redacted>)"
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert worker.calls == []


def test_hostile_provider_iteration_fails_without_leaking_input() -> None:
    class HostileRows:
        def __iter__(self):
            raise RuntimeError("private-provider-secret")

    router, _, worker = _router([_result(IntentKind.LIST)], HostileRows())

    with pytest.raises(IntentRouterError) as caught:
        run(router.route(REQUEST("private-message-secret")))

    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert worker.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_id", OTHER_OWNER),
        ("owner_id", 7),
        ("id", "bad\nid"),
        ("id", "x" * 129),
        ("id", 7),
        ("name", "bad\nname"),
        ("name", "x" * 301),
        ("name", 7),
        ("status", "bad\nstatus"),
        ("status", "bad status"),
        ("status", "x" * 65),
        ("status", 7),
    ],
)
def test_mutated_provider_summaries_are_revalidated_before_worker(
    field: str, value: object
) -> None:
    summary = OwnedMonitorSummary(OWNER, "m1", "private-name", "active")
    object.__setattr__(summary, field, value)
    router, provider, worker = _router([_result(IntentKind.LIST)], (summary,))

    with pytest.raises(IntentRouterError) as caught:
        run(router.route(REQUEST("private-message")))

    assert str(caught.value) == "intent routing failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert provider.calls == [OWNER]
    assert worker.calls == []


def test_forged_provider_summary_is_revalidated_before_worker() -> None:
    summary = object.__new__(OwnedMonitorSummary)
    object.__setattr__(summary, "owner_id", OWNER)
    object.__setattr__(summary, "id", "m1")
    object.__setattr__(summary, "name", "private-name")
    object.__setattr__(summary, "status", "")
    router, provider, worker = _router([_result(IntentKind.LIST)], (summary,))

    with pytest.raises(IntentRouterError) as caught:
        run(router.route(REQUEST("private-message")))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert provider.calls == [OWNER]
    assert worker.calls == []


def test_invalid_worker_results_retry_exact_closed_model_sequence_then_fallback() -> None:
    router, _, worker = _router([object(), object(), object()])

    result = run(router.route(REQUEST("모호한 요청")))

    assert [(model, effort) for _, model, effort in worker.calls] == [
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-sol", "high"),
    ]
    assert result == _result(
        IntentKind.UNKNOWN,
        clarification="요청을 이해하지 못했습니다",
        confidence=0,
    )


def test_fixed_worker_failures_retry_but_no_fourth_call() -> None:
    router, _, worker = _router(
        [CodexWorkerError(), CodexWorkerError(), CodexWorkerError(), _result(IntentKind.LIST)]
    )

    result = run(router.route(REQUEST("모니터 목록")))

    assert result.clarification == "요청을 이해하지 못했습니다"
    assert len(worker.calls) == 3


def test_stops_after_first_semantically_valid_result() -> None:
    output = _result(IntentKind.LIST)
    router, _, worker = _router([output, object()])

    assert run(router.route(REQUEST("목록"))) is output
    assert len(worker.calls) == 1


@pytest.mark.parametrize(
    "output",
    [
        _result(IntentKind.LIST, confidence=0.74, clarification="목록을 말한 건가요?"),
        _result(IntentKind.UNKNOWN, clarification="무엇을 모니터할까요?"),
    ],
)
def test_valid_low_confidence_or_unknown_is_terminal_and_clarifies(output: IntentResult) -> None:
    router, _, worker = _router([output, _result(IntentKind.LIST)])

    result = run(router.route(REQUEST("요즘 어때?")))

    assert result.kind is IntentKind.UNKNOWN
    assert result.target_monitor_ids == []
    assert result.clarification
    assert len(worker.calls) == 1


@pytest.mark.parametrize(
    "output",
    [
        _result(IntentKind.PAUSE, targets=["not-owned"], confidence=0.5),
        _result(IntentKind.DELETE, targets=["m1", "m2"], confidence=0.5),
        _result(IntentKind.STATUS, targets=["m1"], url="https://example.com", confidence=0.5),
        _result(IntentKind.LIST, condition="smuggled", confidence=0.5),
    ],
)
def test_semantically_invalid_low_confidence_results_retry(output: IntentResult) -> None:
    router, _, worker = _router([output, object(), object()])

    result = run(router.route(REQUEST("모호한 요청")))

    assert result.clarification == "요청을 이해하지 못했습니다"
    assert len(worker.calls) == 3


def test_empty_update_is_invalid_and_retries_to_safe_fallback() -> None:
    empty_update = _result(IntentKind.UPDATE, targets=["m1"])
    router, _, worker = _router([empty_update, object(), object()])

    result = run(router.route(REQUEST("이 모니터 바꿔줘")))

    assert result.kind is IntentKind.UNKNOWN
    assert result.clarification == "요청을 이해하지 못했습니다"
    assert len(worker.calls) == 3


@pytest.mark.parametrize("confidence", [0.95, 0.5])
def test_update_with_smuggled_url_is_invalid_at_every_confidence(
    confidence: float,
) -> None:
    smuggled = _result(
        IntentKind.UPDATE,
        targets=["m1"],
        url="https://example.com/smuggled",
        schedule="매일",
        confidence=confidence,
    )
    router, _, worker = _router([smuggled, object(), object()])

    result = run(router.route(REQUEST("주기를 바꿔줘")))

    assert result.kind is IntentKind.UNKNOWN
    assert result.clarification == "요청을 이해하지 못했습니다"
    assert len(worker.calls) == 3


def test_cancellation_propagates_without_retry() -> None:
    router, _, worker = _router([asyncio.CancelledError(), _result(IntentKind.LIST)])

    with pytest.raises(asyncio.CancelledError):
        run(router.route(REQUEST("목록")))

    assert len(worker.calls) == 1


def test_arbitrary_worker_exception_is_not_retried_and_is_redacted() -> None:
    router, _, worker = _router([RuntimeError("private-worker-secret"), _result(IntentKind.LIST)])

    with pytest.raises(IntentRouterError) as caught:
        run(router.route(REQUEST("private-message-secret")))

    assert str(caught.value) == "intent routing failed"
    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(worker.calls) == 1


@pytest.mark.parametrize(
    "output",
    [
        _result(IntentKind.CREATE),
        _result(IntentKind.CREATE, targets=["m1"], url="https://example.com"),
        _result(IntentKind.CREATE, url="ftp://example.com/x"),
        _result(IntentKind.LIST, targets=["m1"]),
        _result(IntentKind.LIST, condition="smuggled"),
        _result(IntentKind.UNKNOWN, url="https://example.com"),
        _result(IntentKind.UPDATE),
        _result(IntentKind.UPDATE, targets=["m1", "m2"]),
        _result(IntentKind.UPDATE, targets=["m1"], url="https://example.com"),
        _result(IntentKind.PAUSE, targets=["m1"], condition="smuggled"),
        _result(IntentKind.RESUME, targets=["m1"], schedule="smuggled"),
        _result(IntentKind.DELETE, targets=["m1"], url="https://example.com"),
        _result(IntentKind.STATUS, targets=["m1"], condition="smuggled"),
        _result(IntentKind.PAUSE, targets=["m1", "m1"]),
        _result(IntentKind.PAUSE, targets=["not-owned"]),
        _result(IntentKind.LIST, clarification="unexpected"),
    ],
)
def test_semantically_conflicting_results_never_pass_as_actions(output: IntentResult) -> None:
    router, _, worker = _router([output, object(), object()])

    result = run(router.route(REQUEST("요청")))

    assert result.kind is IntentKind.UNKNOWN
    assert result.target_monitor_ids == []
    assert result.clarification == "요청을 이해하지 못했습니다"
    assert len(worker.calls) == 3


@pytest.mark.parametrize(
    "url",
    [
        "https://exa mple.com/path",
        "https://example.com/a path",
        "https://.",
        "https://..",
        "https://.example.com",
        "https://example..com",
        "https://example.com:",
        "https://example.com:abc/path",
        "https://example.com:+80/path",
        "https://example.com:0/path",
        "https://example.com:65536/path",
        "https://example.com/%zz",
        "https://user@example.com/path",
        "https://example.com/path#fragment",
        "https://[2001:db8::1/path",
    ],
)
def test_malformed_create_urls_are_invalid_and_retry(url: str) -> None:
    output = _result(IntentKind.CREATE, url=url)
    router, _, worker = _router([output, object(), object()])

    result = run(router.route(REQUEST("이 URL 모니터해줘")))

    assert result.kind is IntentKind.UNKNOWN
    assert result.clarification == "요청을 이해하지 못했습니다"
    assert len(worker.calls) == 3


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path?x=1",
        "https://예시.한국/상품",
        "https://example.com./path",
        "http://192.0.2.1:8080/path",
        "https://[2001:db8::1]:443/path",
    ],
)
def test_syntactically_valid_create_urls_are_preserved(url: str) -> None:
    output = _result(IntentKind.CREATE, url=url)
    router, _, worker = _router([output])

    result = run(router.route(REQUEST("이 URL 모니터해줘")))

    assert result is output
    assert len(worker.calls) == 1


@pytest.mark.parametrize(
    "field",
    ["target_monitor_ids", "target_url", "condition_text", "schedule_text", "clarification"],
)
def test_worker_control_characters_are_rejected(field: str) -> None:
    values: dict[str, object] = {
        "kind": IntentKind.CREATE,
        "target_monitor_ids": [],
        "target_url": "https://example.com",
        "condition_text": None,
        "schedule_text": None,
        "clarification": None,
        "confidence": 0.9,
    }
    if field == "target_monitor_ids":
        values[field] = ["bad\nid"]
    else:
        values[field] = "bad\nvalue"
    output = IntentResult(**values)  # type: ignore[arg-type]
    router, _, worker = _router([output, object(), object()])

    result = run(router.route(REQUEST("요청")))

    assert result.clarification == "요청을 이해하지 못했습니다"
    assert len(worker.calls) == 3


def test_ambiguous_target_conflict_returns_one_clarification_and_no_guess() -> None:
    ambiguous = _result(IntentKind.DELETE, targets=["m1", "m2"])
    router, _, _ = _router([ambiguous, object(), object()])

    result = run(router.route(REQUEST("둘 중 그거 삭제해줘")))

    assert result.kind is IntentKind.UNKNOWN
    assert result.target_monitor_ids == []
    assert type(result.clarification) is str
    assert "\n" not in result.clarification


def test_safe_worker_clarification_is_preserved_but_unsafe_one_is_replaced() -> None:
    safe = _result(
        IntentKind.UNKNOWN,
        clarification="어느 모니터를 말씀하시는지 알려주세요",
    )
    unsafe = _result(IntentKind.UNKNOWN, clarification="\nprivate-secret")
    safe_router, _, _ = _router([safe])
    unsafe_router, _, unsafe_worker = _router([unsafe])

    assert run(safe_router.route(REQUEST("그거"))).clarification == safe.clarification
    result = run(unsafe_router.route(REQUEST("그거")))

    assert result.clarification != unsafe.clarification
    assert "private" not in result.clarification
    assert len(unsafe_worker.calls) == 1


def test_owned_summary_is_immutable_validated_and_redacted() -> None:
    summary = OwnedMonitorSummary(OWNER, "m1", "private-name", "active")

    assert "private-name" not in repr(summary)
    with pytest.raises(FrozenInstanceError):
        summary.name = "changed"  # type: ignore[misc]
    for values in [
        (OWNER, "bad\nid", "name", "active"),
        (OWNER, "m1", "bad\nname", "active"),
        (OWNER, "m1", "name", "bad\nstatus"),
        (OWNER, "m1", "name", "bad status"),
        ("invalid-owner", "m1", "name", "active"),
        (OWNER, "x" * 129, "name", "active"),
    ]:
        with pytest.raises(ValueError, match="invalid monitor summary"):
            OwnedMonitorSummary(*values)


def test_router_and_requests_do_not_leak_text_to_repr_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    text = "private-message-secret"
    router, _, worker = _router([_result(IntentKind.LIST)])

    with caplog.at_level(logging.DEBUG):
        result = run(router.route(REQUEST(text)))

    assert text not in repr(router)
    assert text not in repr(worker.calls[0][0])
    assert text not in repr(result)
    assert text not in caplog.text


def test_bound_methods_and_stable_instance_callables_are_supported() -> None:
    calls: list[tuple[str, str]] = []

    class ProviderCallable:
        def __call__(self, owner_id: str) -> object:
            calls.append(("provider", owner_id))
            return MONITORS

    class WorkerCallable:
        async def __call__(self, request: IntentRequest, *, model: str, effort: str) -> object:
            calls.append((model, effort))
            return _result(IntentKind.LIST)

    provider_callable = ProviderCallable()
    worker_callable = WorkerCallable()
    direct = IntentRouter(provider_callable, worker_callable)
    bound = IntentRouter(FakeProvider().list_monitors, FakeWorker([_result(IntentKind.LIST)]).run)

    assert run(direct.route(REQUEST("목록"))).kind is IntentKind.LIST
    assert run(bound.route(REQUEST("목록"))).kind is IntentKind.LIST
    assert calls == [("provider", OWNER), ("gpt-5.6-terra", "medium")]


def test_dependency_method_replacement_fails_closed_before_use() -> None:
    provider = FakeProvider()
    worker = FakeWorker([_result(IntentKind.LIST)])
    router = IntentRouter(provider, worker)
    provider.list_monitors = lambda owner_id: ()  # type: ignore[method-assign]

    with pytest.raises(IntentRouterError):
        run(router.route(REQUEST("목록")))

    assert provider.calls == []
    assert worker.calls == []


def test_changing_or_hostile_descriptors_fail_with_fixed_constructor_error() -> None:
    class Hostile:
        @property
        def run(self) -> Callable[..., object]:
            raise RuntimeError("private-descriptor-secret")

    with pytest.raises(IntentRouterError) as caught:
        IntentRouter(FakeProvider(), Hostile())

    assert str(caught.value) == "intent routing failed"
    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_constructor_captures_each_dependency_descriptor_exactly_once() -> None:
    class CountingProvider:
        def __init__(self) -> None:
            self.accesses = 0

        @property
        def list_monitors(self) -> Callable[[str], object]:
            self.accesses += 1
            return lambda owner_id: MONITORS

    class CountingWorker:
        def __init__(self) -> None:
            self.accesses = 0

        @property
        def run(self) -> Callable[..., object]:
            self.accesses += 1

            async def call(request: IntentRequest, *, model: str, effort: str) -> object:
                return _result(IntentKind.LIST)

            return call

    provider = CountingProvider()
    worker = CountingWorker()

    IntentRouter(provider, worker)

    assert provider.accesses == 1
    assert worker.accesses == 1


def test_gateway_compatible_route_accepts_exactly_one_request_argument() -> None:
    router, _, _ = _router([_result(IntentKind.LIST)])

    result = run(router.route(REQUEST("목록")))

    assert result.kind is IntentKind.LIST
    with pytest.raises(TypeError):
        run(router.route(REQUEST("목록"), OWNER))  # type: ignore[call-arg]
