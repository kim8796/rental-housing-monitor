from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from personal_monitor.ai.contracts import IntentKind, IntentResult, PlanResult
from personal_monitor.control.actions import PendingActionService
from personal_monitor.control.planner import MonitorPlanner, PlanningFailed
from personal_monitor.domain.spec import FetchStrategy, MonitorSpec, SourceAdapterKind
from personal_monitor.storage import open_database
from personal_monitor.telegram.gateway import ControlRequest
from tests.personal_monitor.control.test_planner import (
    FakePolicy,
    FakeProbe,
    FakeWorker,
    IdSource,
    document,
    probe_result,
    target,
)

OWNER = "telegram-user:7"
NOW_ISO = "2026-07-23T00:00:00+00:00"
FIXTURE = Path("tests/fixtures/personal_monitor/planner_cases.json")


def cases() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def case_spec(case: dict[str, object], *, missing_selector: bool = False) -> MonitorSpec:
    spec = case["spec"]
    assert isinstance(spec, dict)
    fields = json.loads(json.dumps(spec["fields"]))
    if missing_selector:
        first_field = next(iter(fields.values()))
        first_field["selector"] = ".fixture-missing"
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": OWNER,
            "name": spec["name"],
            "target_url": case["url"],
            "source_adapter": "scrapling",
            "adapter_ref": None,
            "fetch_strategy": "auto",
            "schedule": "0 */6 * * *",
            "timezone": "Asia/Seoul",
            "extract": {
                "item_scope": spec["item_scope"],
                "fields": fields,
            },
            "validators": {
                "min_items": spec["min_items"],
                "max_items": spec["max_items"],
            },
            "rules": [spec["rule"]],
            "notify_on_no_change": False,
            "auth_profile_ref": None,
        }
    )


def intent(case: dict[str, object]) -> IntentResult:
    return IntentResult(
        kind=IntentKind.CREATE,
        target_monitor_ids=[],
        target_url=case["url"],
        condition_text=case["condition"],
        schedule_text=None,
        clarification=None,
        confidence=0.96,
    )


def planner_for(
    connection: sqlite3.Connection,
    case: dict[str, object],
    results: list[object],
) -> tuple[MonitorPlanner, FakePolicy, FakeProbe, FakeWorker]:
    url = case["url"]
    assert isinstance(url, str)
    resolved = target(url)
    source = document(body=str(case["html"]).encode(), final_url=url)
    probed = probe_result(
        value=resolved,
        source=source,
        profile=case["profile"],  # type: ignore[arg-type]
        warnings=tuple(case["warnings"]),  # type: ignore[arg-type]
    )
    policy = FakePolicy(resolved)
    probe = FakeProbe(probed)
    worker = FakeWorker(results)
    value = MonitorPlanner(
        policy,
        probe,
        worker,
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: datetime.fromisoformat(NOW_ISO),
    )
    return value, policy, probe, worker


@pytest.fixture
def connection() -> sqlite3.Connection:
    value = open_database(":memory:")
    value.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) VALUES (?, 7, 'active', ?)",
        (OWNER, NOW_ISO),
    )
    yield value
    value.close()


@pytest.mark.parametrize(
    "case",
    [case for case in cases() if case["spec"] is not None],
    ids=lambda case: str(case["id"]),
)
def test_frozen_plan_passes_contract_extractor_and_validator(
    connection: sqlite3.Connection,
    case: dict[str, object],
) -> None:
    plan = PlanResult.model_validate(
        {"spec": case_spec(case).model_dump(mode="json"), "explanation": "고정 평가 결과"}
    )
    value, policy, probe, worker = planner_for(connection, case, [plan])

    proposal = asyncio.run(
        value.propose(
            ControlRequest(OWNER, "777", str(case["message"])),
            intent(case),
        )
    )

    expected = case["expected"]
    assert isinstance(expected, dict)
    rule = proposal.spec.rules[0]
    assert proposal.spec.source_adapter is SourceAdapterKind.SCRAPLING
    assert proposal.spec.fetch_strategy is FetchStrategy.HTTP
    assert proposal.spec.timezone == "Asia/Seoul"
    assert proposal.spec.schedule == "0 */6 * * *"
    assert rule.kind.value == expected["rule_kind"]
    assert rule.field == expected["rule_field"]
    assert proposal.spec.extract.fields[rule.field].type.value == expected["field_type"]
    assert proposal.spec.validators.min_items <= len(proposal.preview_items)
    assert len(proposal.preview_items) <= proposal.spec.validators.max_items
    assert all(field.selector for field in proposal.spec.extract.fields.values())
    assert proposal.spec.auth_profile_ref == case["profile"]
    assert proposal.spec.owner_id == OWNER
    assert proposal.spec.target_url == case["url"]
    assert policy.calls == [case["url"]]
    assert len(probe.calls) == 1
    assert [(model, effort) for _, model, effort in worker.calls] == [("gpt-5.6-terra", "medium")]


def test_invalid_or_ambiguous_case_fails_closed_before_any_candidate_write(
    connection: sqlite3.Connection,
) -> None:
    case = next(case for case in cases() if case["id"] == "ambiguous_invalid")
    worker = FakeWorker([])
    policy = FakePolicy()
    probe = FakeProbe()
    value = MonitorPlanner(
        policy,
        probe,
        worker,
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: datetime.fromisoformat(NOW_ISO),
    )
    ambiguous = IntentResult(
        kind=IntentKind.UNKNOWN,
        target_monitor_ids=[],
        target_url=None,
        condition_text=None,
        schedule_text=None,
        clarification="대상 URL과 조건을 알려주세요",
        confidence=0.2,
    )

    with pytest.raises(PlanningFailed):
        asyncio.run(value.propose(ControlRequest(OWNER, "777", str(case["message"])), ambiguous))

    assert worker.calls == []
    assert policy.calls == []
    assert probe.calls == []
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM monitor_versions").fetchone()[0] == 0


def test_invalid_plans_repair_through_exact_terra_then_sol_sequence(
    connection: sqlite3.Connection,
) -> None:
    case = next(case for case in cases() if case["id"] == "numeric_price")
    invalid = PlanResult(
        spec=case_spec(case, missing_selector=True),
        explanation="선택자 검증이 실패해야 하는 고정 결과",
    )
    valid = PlanResult(spec=case_spec(case), explanation="수정된 고정 결과")
    value, _, _, worker = planner_for(connection, case, [invalid, invalid, valid])

    proposal = asyncio.run(
        value.propose(ControlRequest(OWNER, "777", str(case["message"])), intent(case))
    )

    assert proposal.preview_items[0].fields["price"] == 99000
    assert [(model, effort) for _, model, effort in worker.calls] == [
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-terra", "medium"),
        ("gpt-5.6-sol", "high"),
    ]
    assert worker.calls[0][0].observed_preview_values == []
    assert worker.calls[1][0].observed_preview_values == ["candidate_extract_invalid"]
    assert worker.calls[2][0].observed_preview_values == [
        "candidate_extract_invalid",
        "candidate_extract_invalid",
    ]
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 1
