from __future__ import annotations

import asyncio
import hashlib
import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

import personal_monitor.control.planner as planner_module
from personal_monitor.ai.contracts import IntentKind, IntentResult, PlanRequest, PlanResult
from personal_monitor.ai.worker import CodexWorkerError
from personal_monitor.control.actions import ActionDenied, ConsumedAction, PendingActionService
from personal_monitor.control.planner import (
    MonitorPlanner,
    PlanningFailed,
    ProbeResult,
    _proposal_binding,
    reconstruct_confirmed_spec,
)
from personal_monitor.domain.spec import FetchStrategy, MonitorSpec
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.extractor import DeclarativeExtractor
from personal_monitor.scraping.validator import ObservationValidator
from personal_monitor.security.robots import RobotsDecision
from personal_monitor.security.url_policy import PolicyError, ResolvedTarget
from personal_monitor.storage import open_database
from personal_monitor.telegram.gateway import ControlRequest
from tests.credential_alias_cases import PUNCTUATED_ASSIGNMENTS, SENSITIVE_ASSIGNMENTS

OWNER = "telegram-user:7"
NOW = datetime(2026, 7, 23, tzinfo=UTC)
TARGET_URL = "https://example.com/product/7"
BODY = """<html><body>
<script>token=raw-script-secret</script>
<form><input value="raw-form-secret"></form>
<main><h1>상품 A</h1><span class="price">99,000원</span></main>
</body></html>""".encode()


class FakePolicy:
    def __init__(self, result: object | None = None) -> None:
        self.result = result or target()
        self.calls: list[str] = []

    async def validate(self, url: str) -> object:
        self.calls.append(url)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeProbe:
    def __init__(self, result: object | None = None) -> None:
        self.result = result or probe_result()
        self.calls: list[tuple[str, ResolvedTarget]] = []

    async def probe(self, owner_id: str, value: ResolvedTarget) -> object:
        self.calls.append((owner_id, value))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeWorker:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.calls: list[tuple[PlanRequest, str, str]] = []

    async def run(self, request: PlanRequest, *, model: str, effort: str) -> object:
        self.calls.append((request, model, effort))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class IdSource:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> str:
        self.count += 1
        return f"{self.count:032x}"


def target(url: str = TARGET_URL) -> ResolvedTarget:
    return ResolvedTarget(url, "example.com", 443, frozenset({"93.184.216.34"}))


def projected_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def document(
    *,
    body: bytes = BODY,
    strategy: FetchStrategy = FetchStrategy.HTTP,
    final_url: str = TARGET_URL,
) -> SourceDocument:
    return SourceDocument(
        final_url=final_url,
        status=200,
        content_type="text/html",
        headers={"content-type": "text/html", "x-private": "raw-header-secret"},
        body=body,
        strategy=strategy,
    )


def probe_result(
    *,
    value: ResolvedTarget | None = None,
    source: SourceDocument | None = None,
    profile: str | None = None,
    warnings: tuple[str, ...] = ("robots_policy_unavailable",),
) -> ProbeResult:
    return ProbeResult(
        target=value or target(),
        document=source or document(),
        robots=RobotsDecision(
            allowed=True,
            crawl_delay_seconds=None,
            checked_at=NOW,
            policy_fetched=False,
        ),
        auth_profile_ref=profile,
        warnings=warnings,
    )


def intent(url: str = TARGET_URL) -> IntentResult:
    return IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=url,
        condition_text="10만 원 아래",
        schedule_text="6시간마다",
        clarification=None,
        confidence=0.96,
    )


def request(text: str = "이 상품이 10만 원 아래면 알려줘") -> ControlRequest:
    return ControlRequest(OWNER, "42", text)


def spec(
    *,
    owner: str = OWNER,
    url: str = TARGET_URL,
    strategy: FetchStrategy = FetchStrategy.AUTO,
    profile: str | None = None,
    item_scope: str = "main",
    price_selector: str = ".price",
    schedule: str = "0 */6 * * *",
) -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": owner,
            "name": "상품 가격 감시",
            "target_url": url,
            "source_adapter": "scrapling",
            "adapter_ref": None,
            "fetch_strategy": strategy.value,
            "schedule": schedule,
            "timezone": "Asia/Seoul",
            "extract": {
                "item_scope": item_scope,
                "fields": {
                    "name": {"selector": "h1", "type": "text"},
                    "price": {"selector": price_selector, "type": "krw"},
                },
            },
            "validators": {"min_items": 1, "max_items": 3},
            "rules": [
                {
                    "kind": "numeric_threshold",
                    "field": "price",
                    "operator": "lt",
                    "value": 100000,
                }
            ],
            "notify_on_no_change": False,
            "auth_profile_ref": profile,
        }
    )


def plan(
    value: MonitorSpec | None = None,
    explanation: str = "가격 조건을 검증했습니다",
) -> PlanResult:
    return PlanResult(spec=value or spec(), explanation=explanation)


@pytest.fixture
def connection() -> sqlite3.Connection:
    value = open_database(":memory:")
    value.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) VALUES (?, 7, 'active', ?)",
        (OWNER, NOW.isoformat()),
    )
    yield value
    value.close()


def planner(
    connection: sqlite3.Connection,
    results: list[object],
    *,
    policy: FakePolicy | None = None,
    probe: FakeProbe | None = None,
) -> tuple[MonitorPlanner, FakePolicy, FakeProbe, FakeWorker]:
    url_policy = policy or FakePolicy()
    page_probe = probe or FakeProbe()
    worker = FakeWorker(results)
    value = MonitorPlanner(
        url_policy,
        page_probe,
        worker,
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )
    return value, url_policy, page_probe, worker


def run(value: object) -> Any:
    return asyncio.run(value)  # type: ignore[arg-type]


def pending_count(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0]


def test_update_planning_reuses_hardened_probe_without_persisting_an_action(
    connection: sqlite3.Connection,
) -> None:
    updated = spec(schedule="0 9 * * *")
    value, policy, page_probe, worker = planner(connection, [plan(updated)])
    update = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=["monitor-1"],
        target_url=None,
        condition_text=None,
        schedule_text="매일 오전 9시",
        clarification=None,
        confidence=0.98,
    )

    candidate = run(value.plan_update(request(), update, spec()))

    assert candidate.spec == updated
    assert len(policy.calls) == len(page_probe.calls) == len(worker.calls) == 1
    assert pending_count(connection) == 0


def test_update_planning_keeps_query_and_profile_out_of_worker_then_rebinds_them(
    connection: sqlite3.Connection,
) -> None:
    private_value = "opaque-private-query-value"
    profile = "private-profile-ref"
    url = f"{TARGET_URL}?ref={private_value}"
    projected = projected_url(url)
    url_policy = FakePolicy(target(url))
    page_probe = FakeProbe(
        probe_result(
            value=target(url),
            source=document(final_url=url),
            profile=profile,
        )
    )
    candidate_spec = spec(url=projected, schedule="0 9 * * *")
    value, _, _, worker = planner(
        connection,
        [plan(candidate_spec)],
        policy=url_policy,
        probe=page_probe,
    )
    update = IntentResult(
        kind=IntentKind.UPDATE,
        target_monitor_ids=["monitor-1"],
        target_url=None,
        condition_text=None,
        schedule_text="매일 오전 9시",
        clarification=None,
        confidence=0.98,
    )

    candidate = run(value.plan_update(request(), update, spec(url=url, profile=profile)))

    sent = worker.calls[0][0].model_dump_json()
    assert candidate.spec.target_url == url
    assert candidate.spec.auth_profile_ref == profile
    assert private_value not in sent
    assert profile not in sent
    assert "?" not in worker.calls[0][0].intent.target_url
    assert pending_count(connection) == 0


def mutate_planner_local(name: str, mutation: object) -> None:
    frame = inspect.currentframe()
    try:
        frame = None if frame is None else frame.f_back
        while frame is not None:
            value = frame.f_locals.get(name)
            if value is not None:
                mutation(value)  # type: ignore[operator]
                return
            frame = frame.f_back
    finally:
        del frame


class MutatingIdSource:
    def __init__(self, mutation: object) -> None:
        self.count = 0
        self.mutation = mutation

    def __call__(self) -> str:
        self.count += 1
        if self.count == 2:
            self.mutation()  # type: ignore[operator]
        return f"{self.count:032x}"


def test_policy_precedes_single_probe_and_closed_attempt_sequence(
    connection: sqlite3.Connection,
) -> None:
    value, policy, page_probe, worker = planner(
        connection,
        [object(), plan(spec(price_selector=".missing")), plan()],
    )

    proposal = run(value.propose(request(), intent()))

    assert policy.calls == [TARGET_URL]
    assert page_probe.calls == [(OWNER, target())]
    assert [(model, effort) for _, model, effort in worker.calls] == [
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-sol", "high"),
    ]
    assert worker.calls[0][0].observed_preview_values == []
    assert worker.calls[1][0].observed_preview_values == ["candidate_schema_invalid"]
    assert worker.calls[2][0].observed_preview_values == [
        "candidate_schema_invalid",
        "candidate_extract_invalid",
    ]
    assert proposal.spec == spec(strategy=FetchStrategy.HTTP)
    assert proposal.preview_items[0].fields == {"name": "상품 A", "price": 99000}
    assert proposal.resolved_strategy is FetchStrategy.HTTP
    assert pending_count(connection) == 1


def test_policy_failure_has_no_probe_worker_or_action_and_is_redacted(
    connection: sqlite3.Connection,
) -> None:
    secret = "https://example.com/?token=private-query-secret"
    unsafe = FakePolicy(PolicyError("private-policy-secret"))
    value, _, page_probe, worker = planner(connection, [plan()], policy=unsafe)

    with pytest.raises(PlanningFailed) as caught:
        run(value.propose(request(secret), intent(secret)))

    assert str(caught.value) == "monitor planning failed"
    assert repr(caught.value) == "PlanningFailed(<redacted>)"
    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert page_probe.calls == []
    assert worker.calls == []
    assert pending_count(connection) == 0


def test_sanitized_worker_document_never_contains_active_content_headers_or_query_secrets(
    connection: sqlite3.Connection,
) -> None:
    value, _, _, worker = planner(connection, [plan()])

    run(value.propose(request(), intent()))

    sent = worker.calls[0][0].sanitized_document
    assert "raw-script-secret" not in sent
    assert "raw-form-secret" not in sent
    assert "raw-header-secret" not in sent
    assert len(sent) <= 40_000
    assert worker.calls[0][0].message == request().text


@pytest.mark.parametrize(
    "failure",
    [
        object(),
        plan(spec(owner="telegram-user:8")),
        plan(spec(url="https://example.com/other")),
        plan(spec(strategy=FetchStrategy.STEALTHY)),
        plan(spec(price_selector=".missing")),
    ],
)
def test_invalid_candidates_retry_safely_without_writes(
    connection: sqlite3.Connection,
    failure: object,
) -> None:
    value, _, page_probe, worker = planner(connection, [failure, failure, failure])

    with pytest.raises(PlanningFailed):
        run(value.propose(request("private-message-secret"), intent()))

    assert len(page_probe.calls) == 1
    assert len(worker.calls) == 3
    assert pending_count(connection) == 0


def test_exact_worker_failures_retry_but_arbitrary_errors_do_not(
    connection: sqlite3.Connection,
) -> None:
    retrying, _, _, retry_worker = planner(
        connection,
        [CodexWorkerError(), CodexWorkerError(), plan()],
    )
    assert run(retrying.propose(request(), intent())).spec == spec(strategy=FetchStrategy.HTTP)
    assert len(retry_worker.calls) == 3

    immediate, _, _, immediate_worker = planner(
        connection,
        [RuntimeError("private-worker-secret"), plan()],
    )
    with pytest.raises(PlanningFailed) as caught:
        run(immediate.propose(request(), intent()))
    assert len(immediate_worker.calls) == 1
    assert "private" not in str(caught.value)


def test_cancellation_propagates_without_writes(connection: sqlite3.Connection) -> None:
    value, _, _, worker = planner(connection, [asyncio.CancelledError(), plan()])

    with pytest.raises(asyncio.CancelledError):
        run(value.propose(request(), intent()))

    assert len(worker.calls) == 1
    assert pending_count(connection) == 0


def test_mutated_result_and_spec_are_freshly_revalidated(
    connection: sqlite3.Connection,
) -> None:
    bad_result = plan()
    object.__setattr__(bad_result, "explanation", "bad\x00explanation")
    bad_spec = spec()
    object.__setattr__(bad_spec, "owner_id", "telegram-user:8")
    bad_plan = plan()
    object.__setattr__(bad_plan, "spec", bad_spec)
    value, _, _, worker = planner(connection, [bad_result, bad_plan, plan()])

    proposal = run(value.propose(request(), intent()))

    assert proposal.spec.owner_id == OWNER
    assert len(worker.calls) == 3


def test_success_payload_reconstructs_full_spec_and_detects_tampering_or_wrong_binding(
    connection: sqlite3.Connection,
) -> None:
    value, _, _, _ = planner(connection, [plan()])
    proposal = run(value.propose(request(), intent()))
    consumed = PendingActionService(connection).consume(
        proposal.pending_action.token,
        OWNER,
        now=NOW,
    )

    restored = reconstruct_confirmed_spec(consumed, owner_id=OWNER)

    assert restored.spec == proposal.spec
    assert restored.candidate_version_id == proposal.candidate_version_id
    assert restored.spec_hash == proposal.spec_hash
    assert repr(restored) == "<ConfirmedProposal redacted>"
    with pytest.raises(PlanningFailed):
        reconstruct_confirmed_spec(consumed, owner_id="telegram-user:8")


def test_dependency_replacement_and_hostile_descriptors_fail_fixed(
    connection: sqlite3.Connection,
) -> None:
    page_probe = FakeProbe()
    value, _, _, worker = planner(connection, [plan()], probe=page_probe)
    page_probe.probe = lambda *_: probe_result()  # type: ignore[method-assign]

    with pytest.raises(PlanningFailed) as caught:
        run(value.propose(request("private-message"), intent()))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert worker.calls == []
    assert pending_count(connection) == 0


def test_bound_method_and_callable_probe_are_supported(connection: sqlite3.Connection) -> None:
    async def policy_method(url: str) -> ResolvedTarget:
        return target(url)

    class CallableProbe:
        async def __call__(self, owner_id: str, value: ResolvedTarget) -> ProbeResult:
            assert owner_id == OWNER
            return probe_result(value=value)

    worker = FakeWorker([plan()])
    value = MonitorPlanner(
        policy_method,
        CallableProbe(),
        worker,
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    assert run(value.propose(request(), intent())).spec == spec(strategy=FetchStrategy.HTTP)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("normalized_url", "https://example.com/other"),
        ("hostname", "EXAMPLE.com"),
        ("port", 8443),
        ("addresses", frozenset({"127.0.0.1"})),
        ("addresses", {"93.184.216.34"}),
    ],
)
def test_mutated_policy_targets_fail_before_probe(
    connection: sqlite3.Connection,
    field: str,
    replacement: object,
) -> None:
    hostile = target()
    object.__setattr__(hostile, field, replacement)
    value, _, page_probe, worker = planner(
        connection,
        [plan()],
        policy=FakePolicy(hostile),
    )

    with pytest.raises(PlanningFailed):
        run(value.propose(request("private-message"), intent()))

    assert page_probe.calls == []
    assert worker.calls == []
    assert pending_count(connection) == 0


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("target", "normalized_url"), "https://example.com/other"),
        (("document", "status"), 500),
        (("document", "content_type"), "text/plain"),
        (("document", "body"), b""),
        (("document", "strategy"), FetchStrategy.AUTO),
        (("document", "final_url"), "https://other.example/private"),
        (("document", "peer_ip"), "127.0.0.1"),
        (("robots", "allowed"), False),
        (("robots", "checked_at"), datetime(2026, 7, 23)),
        (("robots", "crawl_delay_seconds"), float("nan")),
        (("robots", "policy_fetched"), 1),
        (("result", "warnings"), ("private_warning=token",)),
        (("result", "warnings"), ["robots_policy_unavailable"]),
        (("result", "auth_profile_ref"), "bad/profile"),
    ],
)
def test_mutated_probe_evidence_fails_before_worker(
    connection: sqlite3.Connection,
    path: tuple[str, str],
    replacement: object,
) -> None:
    result = probe_result()
    parent = result if path[0] == "result" else getattr(result, path[0])
    object.__setattr__(parent, path[1], replacement)
    value, _, page_probe, worker = planner(
        connection,
        [plan()],
        probe=FakeProbe(result),
    )

    with pytest.raises(PlanningFailed) as caught:
        run(value.propose(request("private-message"), intent()))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(page_probe.calls) == 1
    assert worker.calls == []
    assert pending_count(connection) == 0


def test_credential_query_is_rejected_before_policy_or_worker(
    connection: sqlite3.Connection,
) -> None:
    url = "https://example.com/product/7?token=private-query-secret"
    value, policy, page_probe, worker = planner(connection, [plan()])

    with pytest.raises(PlanningFailed):
        run(value.propose(request(url), intent(url)))

    assert policy.calls == []
    assert page_probe.calls == []
    assert worker.calls == []
    assert pending_count(connection) == 0


def test_sanitizer_bound_failure_has_no_worker_or_action(
    connection: sqlite3.Connection,
) -> None:
    worker = FakeWorker([plan()])
    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        worker,
        PendingActionService(connection),
        sanitizer=lambda _html, *, secret_values: "x" * 40_001,
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    with pytest.raises(PlanningFailed):
        run(value.propose(request(), intent()))

    assert worker.calls == []
    assert pending_count(connection) == 0


def test_bounded_sanitized_layout_whitespace_is_allowed(
    connection: sqlite3.Connection,
) -> None:
    worker = FakeWorker([plan()])
    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        worker,
        PendingActionService(connection),
        sanitizer=lambda _html, *, secret_values: "<pre>safe\nlayout\ttext</pre>",
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    proposal = run(value.propose(request(), intent()))

    assert proposal.spec == spec(strategy=FetchStrategy.HTTP)
    assert worker.calls[0][0].sanitized_document == "<pre>safe\nlayout\ttext</pre>"


def test_extractor_receives_original_document_not_sanitized_copy(
    connection: sqlite3.Connection,
) -> None:
    class RecordingExtractor:
        def __init__(self) -> None:
            self.documents: list[SourceDocument] = []
            self.real = DeclarativeExtractor()

        def extract(self, source: SourceDocument, extract_spec: object):
            self.documents.append(source)
            return self.real.extract(source, extract_spec)  # type: ignore[arg-type]

    recorder = RecordingExtractor()
    worker = FakeWorker([plan()])
    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        worker,
        PendingActionService(connection),
        extractor=recorder,
        sanitizer=lambda _html, *, secret_values: "<p>sanitized-only</p>",
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    proposal = run(value.propose(request(), intent()))

    assert proposal.preview_items[0].fields["price"] == 99000
    assert recorder.documents[0].body == BODY
    assert worker.calls[0][0].sanitized_document == "<p>sanitized-only</p>"


def test_observation_validator_failure_retries_and_never_writes(
    connection: sqlite3.Connection,
) -> None:
    class RejectingValidator:
        def validate(self, *_args: object):
            raise MonitorError(ErrorClass.VALIDATION, "validate", "private-value-secret")

    worker = FakeWorker([plan(), plan(), plan()])
    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        worker,
        PendingActionService(connection),
        validator=RejectingValidator(),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    with pytest.raises(PlanningFailed) as caught:
        run(value.propose(request("private-message"), intent()))

    assert len(worker.calls) == 3
    assert all(
        call[0].observed_preview_values[-1:] == ["candidate_extract_invalid"]
        for call in worker.calls[1:]
    )
    assert "private" not in str(caught.value)
    assert pending_count(connection) == 0


@pytest.mark.parametrize("mutation", ["schedule", "rule", "adapter"])
def test_mutated_semantic_spec_invariants_retry_without_action(
    connection: sqlite3.Connection,
    mutation: str,
) -> None:
    candidate = spec()
    if mutation == "schedule":
        object.__setattr__(candidate, "schedule", "* * * * *")
    elif mutation == "rule":
        object.__setattr__(candidate.rules[0], "field", "name")
    else:
        object.__setattr__(candidate, "adapter_ref", "smuggled")
    result = plan()
    object.__setattr__(result, "spec", candidate)
    value, _, _, worker = planner(connection, [result, result, result])

    with pytest.raises(PlanningFailed):
        run(value.propose(request(), intent()))

    assert len(worker.calls) == 3
    assert pending_count(connection) == 0


def test_profile_and_strategy_mismatch_are_candidate_failures(
    connection: sqlite3.Connection,
) -> None:
    page_probe = FakeProbe(probe_result(profile="owner-profile"))
    disclosed_profile = plan(spec(profile="owner-profile"))
    wrong_strategy = plan(spec(strategy=FetchStrategy.DYNAMIC))
    valid = plan()
    value, _, _, worker = planner(
        connection,
        [disclosed_profile, wrong_strategy, valid],
        probe=page_probe,
    )

    proposal = run(value.propose(request(), intent()))

    assert proposal.spec.auth_profile_ref == "owner-profile"
    assert proposal.spec.fetch_strategy is FetchStrategy.HTTP
    assert all("owner-profile" not in call[0].model_dump_json() for call in worker.calls)
    assert len(worker.calls) == 3
    assert pending_count(connection) == 1


def _all_strings(value: object) -> list[str]:
    if isinstance(value, dict | MappingProxyType):
        return [item for nested in value.values() for item in _all_strings(nested)]
    if isinstance(value, list | tuple):
        return [item for nested in value for item in _all_strings(nested)]
    return [value] if isinstance(value, str) else []


def test_worker_projection_removes_original_url_all_query_values_and_profile(
    connection: sqlite3.Connection,
) -> None:
    original = (
        "https://example.com/product/7?ref=opaque-private-query-value"
        "&X-Amz-Signature=aws-private-signature"
    )
    safe = projected_url(original)
    profile = "owner-private-profile"
    projected_candidate = spec(url=safe)
    page_probe = FakeProbe(
        probe_result(
            value=target(original),
            source=document(final_url=original),
            profile=profile,
        )
    )
    worker = FakeWorker([plan(projected_candidate)])
    value = MonitorPlanner(
        FakePolicy(target(original)),
        page_probe,
        worker,
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )
    control = request(f"{original} opaque-private-query-value aws-private-signature 감시")
    create = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=original,
        condition_text="ref=opaque-private-query-value 이하",
        schedule_text="X-Amz-Signature=aws-private-signature 매일",
        clarification=None,
        confidence=0.96,
    )

    proposal = run(value.propose(control, create))

    sent = worker.calls[0][0]
    all_values = _all_strings(sent.model_dump(mode="python"))
    forbidden = {
        original,
        "opaque-private-query-value",
        "aws-private-signature",
        "X-Amz-Signature",
        profile,
    }
    assert sent.intent.target_url == safe
    assert all(secret not in item for secret in forbidden for item in all_values)
    assert proposal.spec.target_url == original
    assert proposal.spec.auth_profile_ref == profile
    assert proposal.spec.fetch_strategy is FetchStrategy.HTTP
    consumed = PendingActionService(connection).consume(
        proposal.pending_action.token,
        OWNER,
        now=NOW,
    )
    assert consumed.payload["spec"]["target_url"] == original  # type: ignore[index]
    assert consumed.payload["spec"]["auth_profile_ref"] == profile  # type: ignore[index]


def test_ordinary_short_query_values_do_not_collide_with_application_metadata(
    connection: sqlite3.Connection,
) -> None:
    original = f"{TARGET_URL}?page=7&rank=1&mode=create&scheme=http"
    safe = projected_url(original)
    worker = FakeWorker([plan(spec(url=safe))])
    value = MonitorPlanner(
        FakePolicy(target(original)),
        FakeProbe(
            probe_result(
                value=target(original),
                source=document(final_url=original),
            )
        ),
        worker,
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    proposal = run(
        value.propose(
            request(f"{original} 상품 7개를 모니터해줘"),
            intent(original),
        )
    )

    sent = worker.calls[0][0]
    assert original not in sent.message
    assert "?page=7" not in sent.message
    assert "상품 7개" in sent.message
    assert sent.owner_id == OWNER
    assert sent.intent.target_url == safe
    assert proposal.spec.target_url == original
    assert pending_count(connection) == 1


def test_broad_jwt_is_redacted_from_worker_sanitized_document(
    connection: sqlite3.Connection,
) -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.YWJjZGVm.signature"
    body = f"<main><h1>{jwt}</h1><span class='price'>99,000원</span></main>".encode()
    page_probe = FakeProbe(probe_result(source=document(body=body)))
    value, _, _, worker = planner(connection, [plan()], probe=page_probe)

    run(value.propose(request(), intent()))

    assert jwt not in worker.calls[0][0].sanitized_document


@pytest.mark.parametrize(
    "secret",
    (
        'authorization: "supersecretvalue"',
        "password='supersecretvalue'",
        '"authorization": "supersecretvalue"',
        "'api_key': 'supersecretvalue'",
        "session_id=supersecretvalue",
        'session-id="supersecretvalue"',
        "auth: supersecretvalue",
        "credentials=supersecretvalue",
        "signature: supersecretvalue",
        "key=supersecretvalue",
        "`authorization`: `supersecretvalue`",
        *SENSITIVE_ASSIGNMENTS,
        *PUNCTUATED_ASSIGNMENTS,
    ),
)
def test_quoted_assignment_is_fully_hidden_from_worker_message_and_document(
    connection: sqlite3.Connection,
    secret: str,
) -> None:
    worker = FakeWorker([plan()])
    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        worker,
        PendingActionService(connection),
        sanitizer=lambda _html, *, secret_values: secret,
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    run(value.propose(request(f"prefix {secret} suffix"), intent()))

    sent = worker.calls[0][0]
    assert sent.message == "[숨김]"
    assert sent.sanitized_document == "[숨김]"


def test_sanitizer_that_retains_established_query_secret_fails_before_worker(
    connection: sqlite3.Connection,
) -> None:
    original = "https://example.com/product/7?ref=opaque-private-query-value"
    worker = FakeWorker([plan(spec(url=projected_url(original)))])
    value = MonitorPlanner(
        FakePolicy(target(original)),
        FakeProbe(
            probe_result(
                value=target(original),
                source=document(final_url=original),
            )
        ),
        worker,
        PendingActionService(connection),
        sanitizer=lambda _html, *, secret_values: "<main>opaque-private-query-value</main>",
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    with pytest.raises(PlanningFailed):
        run(value.propose(request(original), intent(original)))

    assert worker.calls == []
    assert pending_count(connection) == 0


def test_extractor_valid_strategy_mutation_cannot_change_trusted_probe_baseline(
    connection: sqlite3.Connection,
) -> None:
    class MutatingExtractor:
        def extract(self, source: SourceDocument, extract_spec: object):
            values = DeclarativeExtractor().extract(source, extract_spec)  # type: ignore[arg-type]
            object.__setattr__(source, "strategy", FetchStrategy.DYNAMIC)
            return values

    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        FakeWorker([plan()]),
        PendingActionService(connection),
        extractor=MutatingExtractor(),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    proposal = run(value.propose(request(), intent()))

    assert proposal.resolved_strategy is FetchStrategy.HTTP
    assert proposal.spec.fetch_strategy is FetchStrategy.HTTP
    consumed = PendingActionService(connection).consume(
        proposal.pending_action.token,
        OWNER,
        now=NOW,
    )
    assert consumed.payload["spec"]["fetch_strategy"] == "http"  # type: ignore[index]


def test_extractor_valid_selector_mutation_cannot_change_verified_spec_baseline(
    connection: sqlite3.Connection,
) -> None:
    class MutatingExtractor:
        def extract(self, source: SourceDocument, extract_spec: object):
            values = DeclarativeExtractor().extract(source, extract_spec)  # type: ignore[arg-type]
            object.__setattr__(
                extract_spec.fields["price"],  # type: ignore[attr-defined]
                "selector",
                ".future-missing",
            )
            return values

    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        FakeWorker([plan()]),
        PendingActionService(connection),
        extractor=MutatingExtractor(),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    proposal = run(value.propose(request(), intent()))

    assert proposal.spec.extract.fields["price"].selector == ".price"
    consumed = PendingActionService(connection).consume(
        proposal.pending_action.token,
        OWNER,
        now=NOW,
    )
    assert consumed.payload["spec"]["extract"]["fields"]["price"]["selector"] == ".price"  # type: ignore[index]


@pytest.mark.parametrize("mutation", ("strategy", "extract", "rules"))
def test_validator_valid_mutation_cannot_change_any_trusted_baseline(
    connection: sqlite3.Connection,
    mutation: str,
) -> None:
    class MutatingValidator:
        def validate(self, items: object, extract_spec: object, validators: object):
            values = ObservationValidator().validate(  # type: ignore[arg-type]
                items,
                extract_spec,
                validators,
            )
            if mutation == "strategy":
                mutate_planner_local(
                    "probe",
                    lambda value: object.__setattr__(
                        value.document,
                        "strategy",
                        FetchStrategy.DYNAMIC,
                    ),
                )
            elif mutation == "extract":
                object.__setattr__(
                    extract_spec.fields["price"],  # type: ignore[attr-defined]
                    "selector",
                    ".future-missing",
                )
            else:
                mutate_planner_local(
                    "spec",
                    lambda value: object.__setattr__(value.rules[0], "value", 1),
                )
            return values

    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        FakeWorker([plan()]),
        PendingActionService(connection),
        validator=MutatingValidator(),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    proposal = run(value.propose(request(), intent()))

    assert proposal.resolved_strategy is FetchStrategy.HTTP
    assert proposal.spec.fetch_strategy is FetchStrategy.HTTP
    assert proposal.spec.extract.fields["price"].selector == ".price"
    assert proposal.spec.rules[0].value == 100000
    consumed = PendingActionService(connection).consume(
        proposal.pending_action.token,
        OWNER,
        now=NOW,
    )
    stored = consumed.payload["spec"]
    assert stored["fetch_strategy"] == "http"  # type: ignore[index]
    assert stored["extract"]["fields"]["price"]["selector"] == ".price"  # type: ignore[index]
    assert stored["rules"][0]["value"] == 100000  # type: ignore[index]


def test_id_callback_mutating_source_strategy_never_leaves_orphan_action(
    connection: sqlite3.Connection,
) -> None:
    ids = MutatingIdSource(
        lambda: mutate_planner_local(
            "probe",
            lambda value: object.__setattr__(
                value.document,
                "strategy",
                FetchStrategy.AUTO,
            ),
        )
    )
    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        FakeWorker([plan()]),
        PendingActionService(connection),
        id_source=ids,
        now_source=lambda: NOW,
    )

    with pytest.raises(PlanningFailed):
        run(value.propose(request(), intent()))

    assert pending_count(connection) == 0


def test_id_callback_mutating_local_spec_never_leaves_orphan_action(
    connection: sqlite3.Connection,
) -> None:
    ids = MutatingIdSource(
        lambda: mutate_planner_local(
            "spec",
            lambda value: object.__setattr__(value.extract, "item_scope", "bad;selector"),
        )
    )
    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        FakeWorker([plan()]),
        PendingActionService(connection),
        id_source=ids,
        now_source=lambda: NOW,
    )

    with pytest.raises(PlanningFailed):
        run(value.propose(request(), intent()))

    assert pending_count(connection) == 0


def test_clock_callback_mutating_local_spec_never_leaves_orphan_action(
    connection: sqlite3.Connection,
) -> None:
    def mutating_clock() -> datetime:
        mutate_planner_local(
            "spec",
            lambda value: object.__setattr__(value.extract, "item_scope", "bad;selector"),
        )
        return NOW

    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        FakeWorker([plan()]),
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=mutating_clock,
    )

    with pytest.raises(PlanningFailed):
        run(value.propose(request(), intent()))

    assert pending_count(connection) == 0


def test_post_insert_proposal_validation_failure_is_atomically_rolled_back(
    connection: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = planner_module._valid_proposal
    calls = 0

    def reject_once_after_insert(value: object) -> bool:
        nonlocal calls
        calls += 1
        return False if calls == 1 else original(value)

    monkeypatch.setattr(planner_module, "_valid_proposal", reject_once_after_insert)
    value, _, _, _ = planner(connection, [plan()])

    with pytest.raises(PlanningFailed):
        run(value.propose(request(), intent()))

    assert calls == 1
    assert pending_count(connection) == 0


@pytest.mark.parametrize("dependency", ["extractor", "validator"])
def test_hostile_dependency_input_mutation_is_disposable(
    connection: sqlite3.Connection,
    dependency: str,
) -> None:
    class MutatingExtractor:
        def extract(self, source: SourceDocument, extract_spec: object):
            values = DeclarativeExtractor().extract(source, extract_spec)  # type: ignore[arg-type]
            object.__setattr__(extract_spec, "item_scope", "bad;selector")
            return values

    class MutatingValidator:
        def validate(self, items: object, extract_spec: object, validators: object):
            values = ObservationValidator().validate(  # type: ignore[arg-type]
                items,
                extract_spec,
                validators,
            )
            object.__setattr__(extract_spec, "item_scope", "bad;selector")
            return values

    kwargs: dict[str, object] = {}
    if dependency == "extractor":
        kwargs["extractor"] = MutatingExtractor()
    else:
        kwargs["validator"] = MutatingValidator()
    worker = FakeWorker([plan()])
    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        worker,
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: NOW,
        **kwargs,
    )

    proposal = run(value.propose(request("private-message"), intent()))

    assert proposal.spec.extract.item_scope == "main"
    assert len(worker.calls) == 1
    assert pending_count(connection) == 1


def test_hostile_validator_return_mutation_never_writes_pending_action(
    connection: sqlite3.Connection,
) -> None:
    class MutatingObservationValidator:
        def validate(self, items: object, extract_spec: object, validators: object):
            values = ObservationValidator().validate(  # type: ignore[arg-type]
                items,
                extract_spec,
                validators,
            )
            object.__setattr__(
                values[0],
                "fields",
                MappingProxyType({"name": "상품 A", "price": "not-a-number"}),
            )
            return values

    worker = FakeWorker([plan(), plan(), plan()])
    value = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        worker,
        PendingActionService(connection),
        validator=MutatingObservationValidator(),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )

    with pytest.raises(PlanningFailed) as caught:
        run(value.propose(request("private-message"), intent()))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(worker.calls) == 3
    assert pending_count(connection) == 0


def test_field_count_bound_and_invalid_preview_never_leave_orphan_action(
    connection: sqlite3.Connection,
) -> None:
    many = spec()
    fields = {f"field_{index}": {"selector": "h1", "type": "text"} for index in range(51)}
    many = MonitorSpec.model_validate(
        {
            **many.model_dump(mode="json"),
            "extract": {"item_scope": "main", "fields": fields},
            "rules": [{"kind": "new_item"}],
        }
    )
    value, _, _, _ = planner(connection, [plan(many), plan(many), plan(many)])

    with pytest.raises(PlanningFailed):
        run(value.propose(request(), intent()))

    assert pending_count(connection) == 0


def test_confirmation_is_restart_safe_owner_bound_single_use_and_expiring(
    connection: sqlite3.Connection,
) -> None:
    value, _, _, _ = planner(connection, [plan()])
    proposal = run(value.propose(request(), intent()))
    restarted = PendingActionService(connection)

    with pytest.raises(ActionDenied):
        restarted.consume(proposal.pending_action.token, "telegram-user:8", now=NOW)
    consumed = restarted.consume(proposal.pending_action.token, OWNER, now=NOW)
    confirmed = reconstruct_confirmed_spec(consumed, owner_id=OWNER)

    assert confirmed.spec == proposal.spec
    assert confirmed.candidate_version_id == proposal.candidate_version_id
    with pytest.raises(ActionDenied):
        restarted.consume(proposal.pending_action.token, OWNER, now=NOW)

    expiring, _, _, _ = planner(connection, [plan()])
    second = run(expiring.propose(request(), intent()))
    with pytest.raises(ActionDenied):
        restarted.consume(second.pending_action.token, OWNER, now=NOW + timedelta(minutes=10))


@pytest.mark.parametrize("tamper", ["candidate", "hash", "spec", "owner", "extra"])
def test_confirmation_rejects_every_tampered_payload_without_oracle(
    connection: sqlite3.Connection,
    tamper: str,
) -> None:
    value, _, _, _ = planner(connection, [plan()])
    proposal = run(value.propose(request(), intent()))
    payload: dict[str, object] = {
        "candidate_version_id": proposal.candidate_version_id,
        "spec_hash": proposal.spec_hash,
        "binding_hash": _proposal_binding(
            OWNER,
            proposal.candidate_version_id,
            proposal.spec_hash,
        ),
        "spec": proposal.spec.model_dump(mode="json"),
    }
    if tamper == "candidate":
        payload["candidate_version_id"] = "f" * 32
    elif tamper == "hash":
        payload["spec_hash"] = "f" * 64
    elif tamper == "spec":
        payload["spec"] = {**payload["spec"], "name": "변조"}  # type: ignore[dict-item]
    elif tamper == "owner":
        payload["spec"] = {
            **payload["spec"],  # type: ignore[dict-item]
            "owner_id": "telegram-user:8",
        }
        payload["spec_hash"] = hashlib.sha256(
            __import__("personal_monitor.storage.schema", fromlist=["canonical_json"])
            .canonical_json(payload["spec"])
            .encode()
        ).hexdigest()
    else:
        payload["extra"] = "smuggled"

    action = ConsumedAction("create", payload, OWNER)
    with pytest.raises(PlanningFailed) as caught:
        reconstruct_confirmed_spec(action, owner_id=OWNER)

    assert str(caught.value) == "monitor planning failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "field",
    [
        "_policy",
        "_probe",
        "_worker",
        "_actions",
        "_extractor",
        "_validator",
        "_sanitizer",
        "_id_source",
        "_now_source",
    ],
)
def test_every_dependency_replacement_fails_closed_before_writes(
    connection: sqlite3.Connection,
    field: str,
) -> None:
    value, _, _, worker = planner(connection, [plan()])
    object.__setattr__(value, field, object())

    with pytest.raises(PlanningFailed) as caught:
        run(value.propose(request("private-message"), intent()))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert worker.calls == []
    assert pending_count(connection) == 0


def test_hostile_descriptor_is_rejected_without_secret_leak(
    connection: sqlite3.Connection,
) -> None:
    class HostilePolicy:
        @property
        def validate(self):
            raise RuntimeError("private-descriptor-secret")

    with pytest.raises(PlanningFailed) as caught:
        MonitorPlanner(
            HostilePolicy(),
            FakeProbe(),
            FakeWorker([plan()]),
            PendingActionService(connection),
        )

    assert "private" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_same_origin_redirect_metadata_and_final_path_are_accepted(
    connection: sqlite3.Connection,
) -> None:
    redirected = SourceDocument(
        final_url="https://example.com/final",
        status=200,
        content_type="text/html",
        headers={"content-type": "text/html"},
        body=BODY,
        strategy=FetchStrategy.HTTP,
        redirect_urls=("https://example.com/next",),
        redirect_location="https://example.com/final",
        peer_ip="93.184.216.34",
    )
    value, _, page_probe, worker = planner(
        connection,
        [plan()],
        probe=FakeProbe(probe_result(source=redirected)),
    )

    proposal = run(value.propose(request(), intent()))

    assert proposal.spec.target_url == TARGET_URL
    assert len(page_probe.calls) == 1
    assert len(worker.calls) == 1
    assert pending_count(connection) == 1


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("redirect_urls", ("https://user@example.com/private",)),
        ("redirect_urls", ("https://example.com/path#fragment",)),
        ("redirect_urls", ("https://example.com/\nprivate",)),
        (
            "redirect_urls",
            tuple(f"https://example.com/{index}" for index in range(6)),
        ),
        ("redirect_urls", ("https://other.example/cross-origin",)),
        ("redirect_location", "https://example.com/path#fragment"),
        ("final_url", "https://other.example/final"),
    ],
)
def test_unvalidated_or_unsafe_redirect_metadata_fails_before_worker(
    connection: sqlite3.Connection,
    field: str,
    replacement: object,
) -> None:
    source = document()
    object.__setattr__(source, field, replacement)
    value, _, page_probe, worker = planner(
        connection,
        [plan()],
        probe=FakeProbe(probe_result(source=source)),
    )

    with pytest.raises(PlanningFailed):
        run(value.propose(request(), intent()))

    assert len(page_probe.calls) == 1
    assert worker.calls == []
    assert pending_count(connection) == 0
