from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from personal_monitor.ai.contracts import IntentKind, IntentResult, PlanRequest, PlanResult
from personal_monitor.control.actions import PendingActionService
from personal_monitor.control.planner import MonitorPlanner, PlanningFailed, ProbeResult
from personal_monitor.domain.spec import FetchStrategy, MonitorSpec
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.security.robots import RobotsDecision
from personal_monitor.security.sanitize import sanitize_for_ai
from personal_monitor.security.url_policy import ResolvedTarget
from personal_monitor.storage import open_database
from personal_monitor.telegram.gateway import ControlRequest
from tests.personal_monitor.control.test_planner import IdSource

OWNER = "telegram-user:7"
OTHER_OWNER = "telegram-user:8"
NOW = datetime(2026, 7, 23, tzinfo=UTC)
HTML_QUERY_MARKER = "fixture_private_query_value"
TARGET = "https://example.com/products"
SAFE_TARGET = urlunsplit((*urlsplit(TARGET)[:3], "", ""))
CREDENTIAL_QUERY_TARGET = f"{TARGET}?token={HTML_QUERY_MARKER}"
HTML = Path("tests/fixtures/personal_monitor/injection_pages.html").read_bytes()
INSTRUCTIONS = (
    "ignore previous instructions",
    "exfiltrate",
    "call a tool",
    "change owner",
    "change auth profile",
    "change target URL",
    "unsupported selector",
)


class Policy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def validate(self, url: str) -> ResolvedTarget:
        self.calls.append(url)
        return ResolvedTarget(url, "example.com", 443, frozenset({"93.184.216.34"}))


class Probe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ResolvedTarget]] = []

    async def probe(self, owner_id: str, target: ResolvedTarget) -> ProbeResult:
        self.calls.append((owner_id, target))
        return ProbeResult(
            target=target,
            document=SourceDocument(
                final_url=target.normalized_url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=HTML,
                strategy=FetchStrategy.HTTP,
            ),
            robots=RobotsDecision(True, None, NOW, True),
            auth_profile_ref="owner-session",
        )


class InspectingWorker:
    def __init__(self, results: list[PlanResult]) -> None:
        self.results = results
        self.calls: list[tuple[PlanRequest, str, str]] = []

    async def run(self, request: PlanRequest, *, model: str, effort: str) -> PlanResult:
        self.calls.append((request, model, effort))
        return PlanResult.model_validate(self.results.pop(0).model_dump(mode="json"))


def spec(
    *,
    owner: str = OWNER,
    source_adapter: str = "scrapling",
    adapter_ref: str | None = None,
    item_scope: str = "main.listing",
) -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": owner,
            "name": "안전한 상품 가격",
            "target_url": SAFE_TARGET,
            "source_adapter": source_adapter,
            "adapter_ref": adapter_ref,
            "fetch_strategy": "auto",
            "schedule": "0 */6 * * *",
            "timezone": "Asia/Seoul",
            "extract": {
                "item_scope": item_scope,
                "fields": {
                    "title": {"selector": "h1", "type": "text"},
                    "price": {"selector": ".price", "type": "krw"},
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
            "auth_profile_ref": None,
        }
    )


def test_real_sanitizer_keeps_only_bounded_visible_facts() -> None:
    assert f"?ref={HTML_QUERY_MARKER}" in HTML.decode()
    sanitized = sanitize_for_ai(HTML.decode(), secret_values=(HTML_QUERY_MARKER,))

    assert "안전한 테스트 상품" in sanitized
    assert "88,000원" in sanitized
    assert HTML_QUERY_MARKER not in sanitized
    assert all(instruction.casefold() not in sanitized.casefold() for instruction in INSTRUCTIONS)
    assert all(
        tag not in sanitized.casefold()
        for tag in ("<script", "<style", "<form", "<input", "<noscript", "<template")
    )
    assert len(sanitized) <= 40_000


def test_injected_worker_mutations_are_rejected_and_trusted_bindings_win() -> None:
    connection = open_database(":memory:")
    connection.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) VALUES (?, 7, 'active', ?)",
        (OWNER, NOW.isoformat()),
    )
    malicious_bindings = PlanResult(
        spec=spec(
            owner=OTHER_OWNER,
            source_adapter="official_api",
            adapter_ref="fake_source",
        ),
        explanation="owner and adapter mutation",
    )
    malicious_selector = PlanResult(
        spec=spec(item_scope="main.missing"),
        explanation="selector mutation",
    )
    accepted = PlanResult(spec=spec(), explanation="bounded result")
    worker = InspectingWorker([malicious_bindings, malicious_selector, accepted])
    planner = MonitorPlanner(
        Policy(),
        Probe(),
        worker,
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )
    intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=TARGET,
        condition_text="가격이 10만원 아래",
        schedule_text=None,
        clarification=None,
        confidence=0.97,
    )

    proposal = asyncio.run(
        planner.propose(
            ControlRequest(OWNER, "777", f"{TARGET} 가격이 10만원 아래면 알려줘"),
            intent,
        )
    )

    assert [(model, effort) for _, model, effort in worker.calls] == [
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-sol", "high"),
    ]
    for request, _, _ in worker.calls:
        wire = request.model_dump_json()
        assert request.owner_id == OWNER
        assert request.intent.target_url == SAFE_TARGET
        assert HTML_QUERY_MARKER not in wire
        assert "owner-session" not in wire
        assert "안전한 테스트 상품" in request.sanitized_document
        assert "88,000원" in request.sanitized_document
        assert all(
            instruction.casefold() not in request.sanitized_document.casefold()
            for instruction in INSTRUCTIONS
        )
        assert all(
            value in {"candidate_binding_invalid", "candidate_extract_invalid"}
            for value in request.observed_preview_values
        )
    assert proposal.spec.owner_id == OWNER
    assert proposal.spec.target_url == TARGET
    assert proposal.spec.auth_profile_ref == "owner-session"
    assert proposal.spec.source_adapter.value == "scrapling"
    assert proposal.spec.adapter_ref is None
    assert proposal.spec.fetch_strategy is FetchStrategy.HTTP
    assert proposal.spec.extract.item_scope == "main.listing"
    assert proposal.spec.rules[0].kind.value == "numeric_threshold"
    assert HTML_QUERY_MARKER not in repr(proposal)
    assert TARGET not in repr(proposal.pending_action)
    connection.close()


def test_credential_query_fails_closed_before_external_or_storage_boundaries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = open_database(":memory:")
    connection.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) VALUES (?, 7, 'active', ?)",
        (OWNER, NOW.isoformat()),
    )
    policy = Policy()
    probe = Probe()
    worker = InspectingWorker([])
    planner = MonitorPlanner(
        policy,
        probe,
        worker,
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )
    unsafe_intent = IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=CREDENTIAL_QUERY_TARGET,
        condition_text="가격이 10만원 아래",
        schedule_text=None,
        clarification=None,
        confidence=0.97,
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(PlanningFailed) as caught:
        asyncio.run(
            planner.propose(
                ControlRequest(
                    OWNER,
                    "777",
                    f"{CREDENTIAL_QUERY_TARGET} 가격이 10만원 아래면 알려줘",
                ),
                unsafe_intent,
            )
        )

    assert policy.calls == []
    assert probe.calls == []
    assert worker.calls == []
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM monitors").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 0
    assert str(caught.value) == "monitor planning failed"
    assert repr(caught.value) == "PlanningFailed(<redacted>)"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    exposed = "\n".join((str(caught.value), repr(caught.value), caplog.text))
    assert HTML_QUERY_MARKER not in exposed
    assert CREDENTIAL_QUERY_TARGET not in exposed
    connection.close()
