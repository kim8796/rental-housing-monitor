from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import urlsplit

import pytest

from personal_monitor.control.actions import PendingActionService
from personal_monitor.control.planner import MonitorPlanner, PlanningFailed
from personal_monitor.control.preview import PreviewMessage, render_preview
from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.security.url_policy import ResolvedTarget
from personal_monitor.storage import open_database
from tests.credential_alias_cases import PUNCTUATED_ASSIGNMENTS, SENSITIVE_ASSIGNMENTS
from tests.personal_monitor.control.test_planner import (
    OWNER,
    FakePolicy,
    FakeProbe,
    FakeWorker,
    IdSource,
    document,
    intent,
    plan,
    probe_result,
    projected_url,
    request,
    spec,
    target,
)

NOW = datetime(2026, 7, 23, tzinfo=UTC)


@pytest.fixture
def connection() -> sqlite3.Connection:
    value = open_database(":memory:")
    value.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) VALUES (?, 7, 'active', ?)",
        (OWNER, NOW.isoformat()),
    )
    yield value
    value.close()


def proposal(connection: sqlite3.Connection):
    planner = MonitorPlanner(
        FakePolicy(),
        FakeProbe(),
        FakeWorker([plan()]),
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: NOW,
    )
    return asyncio.run(planner.propose(request(), intent()))


def test_preview_contains_required_korean_fields_and_exact_buttons(
    connection: sqlite3.Connection,
) -> None:
    value = proposal(connection)

    preview = render_preview(value)

    assert isinstance(preview, PreviewMessage)
    assert "상품 가격 감시" in preview.text
    assert "대상: https://example.com/product/7" in preview.text
    assert "현재 가격: 99,000원" in preview.text
    assert "조건: 가격 100,000원 미만" in preview.text
    assert "시간대: Asia/Seoul" in preview.text
    assert "확인 주기: 6시간마다" in preview.text
    assert "수집 방식: Scrapling HTTP" in preview.text
    assert "robots.txt: 허용 (가져오기 실패)" in preview.text
    assert "로그인 프로필: 필요 없음" in preview.text
    assert [button.text for row in preview.buttons for button in row] == [
        "등록",
        "수정",
        "취소",
    ]
    callbacks = [button.callback_data for row in preview.buttons for button in row]
    assert callbacks == [
        value.pending_action.confirm_callback,
        f"edit:{value.pending_action.token}",
        value.pending_action.cancel_callback,
    ]
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)
    assert value.pending_action.token not in repr(preview)


def test_preview_redacts_url_queries_and_never_renders_internal_bindings(
    connection: sqlite3.Connection,
) -> None:
    value = proposal(connection)

    preview = render_preview(value)

    assert urlsplit(value.spec.target_url).query == ""
    for forbidden in (
        value.pending_action.token,
        value.candidate_version_id,
        value.spec_hash,
        "robots_policy_unavailable",
    ):
        assert forbidden not in preview.text


def test_preview_models_are_redacted_and_immutable(connection: sqlite3.Connection) -> None:
    value = proposal(connection)
    preview = render_preview(value)

    assert repr(value) == "<ProposedMonitor redacted>"
    assert repr(value.preview_items[0]) == "<PreviewItem redacted>"
    assert repr(preview) == "<PreviewMessage redacted>"
    assert "confirm:" not in repr(preview)
    with pytest.raises(FrozenInstanceError):
        preview.text = "변조"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        preview.buttons[0][0].callback_data = "confirm:tampered"  # type: ignore[misc]


def _custom_proposal(
    connection: sqlite3.Connection,
    candidate: MonitorSpec,
    source: SourceDocument,
    *,
    resolved: ResolvedTarget | None = None,
    profile: str | None = None,
    warnings: tuple[str, ...] = (),
    sanitizer: object | None = None,
):
    safe_target = resolved or target(candidate.target_url)
    model_payload = candidate.model_dump(mode="json")
    model_payload["target_url"] = projected_url(candidate.target_url)
    model_payload["auth_profile_ref"] = None
    model_payload["fetch_strategy"] = "auto"
    model_candidate = MonitorSpec.model_validate(model_payload)
    optional: dict[str, object] = {}
    if sanitizer is not None:
        optional["sanitizer"] = sanitizer
    planner = MonitorPlanner(
        FakePolicy(safe_target),
        FakeProbe(
            probe_result(
                value=safe_target,
                source=source,
                profile=profile,
                warnings=warnings,
            )
        ),
        FakeWorker([plan(model_candidate)]),
        PendingActionService(connection),
        id_source=IdSource(),
        now_source=lambda: NOW,
        **optional,
    )
    return asyncio.run(planner.propose(request(), intent(candidate.target_url)))


def test_preview_caps_four_source_items_to_exactly_three(
    connection: sqlite3.Connection,
) -> None:
    candidate = spec()
    candidate = MonitorSpec.model_validate(
        {
            **candidate.model_dump(mode="json"),
            "validators": {"min_items": 1, "max_items": 5},
        }
    )
    body = "".join(
        f"<main><h1>상품 {index}</h1><span class='price'>{index:,}원</span></main>"
        for index in range(1, 5)
    ).encode()
    value = _custom_proposal(connection, candidate, document(body=body))

    preview = render_preview(value)

    assert len(value.preview_items) == 3
    assert preview.text.count("예시 ") == 3
    assert "상품 3" in preview.text
    assert "상품 4" not in preview.text


def test_target_and_url_field_queries_are_preserved_in_payload_but_redacted_in_preview(
    connection: sqlite3.Connection,
) -> None:
    target_url = "https://example.com/product/7?color=red&ref=private-query"
    base = spec(url=target_url)
    payload = base.model_dump(mode="json")
    payload["extract"]["fields"]["link"] = {  # type: ignore[index]
        "selector": "a",
        "attribute": "href",
        "type": "url",
    }
    payload["validators"]["allowed_link_domains"] = ["example.com"]  # type: ignore[index]
    candidate = MonitorSpec.model_validate(payload)
    source = SourceDocument(
        final_url=target_url,
        status=200,
        content_type="text/html",
        headers={"content-type": "text/html"},
        body=(
            "<main><h1>상품</h1><span class='price'>99,000원</span>"
            "<a href='https://example.com/item/1?offer=private-field#part'>보기</a></main>"
        ).encode(),
        strategy=base.fetch_strategy.HTTP,
    )
    value = _custom_proposal(
        connection,
        candidate,
        source,
        resolved=target(target_url),
    )

    preview = render_preview(value)
    consumed = PendingActionService(connection).consume(
        value.pending_action.token,
        OWNER,
        now=NOW,
    )

    assert value.spec.target_url == target_url
    assert consumed.payload["spec"]["target_url"] == target_url  # type: ignore[index]
    assert "?color=" not in preview.text
    assert "private-query" not in preview.text
    assert "?offer=" not in preview.text
    assert "#part" not in preview.text
    assert "https://example.com/item/1" in preview.text


def test_long_credential_like_values_are_bounded_and_hidden(
    connection: sqlite3.Connection,
) -> None:
    body = (
        "<main><h1>token=private-observed-secret "
        + ("긴값" * 500)
        + "</h1><span class='price'>99,000원</span></main>"
    ).encode()
    value = _custom_proposal(
        connection,
        spec(),
        document(body=body),
        sanitizer=lambda _html, *, secret_values: "<main>safe projection</main>",
    )

    preview = render_preview(value)

    assert len(value.preview_items[0].fields["name"]) <= 120
    assert "private-observed-secret" not in preview.text
    assert "[숨김]" in preview.text
    assert len(preview.text) <= 3_500


def test_unknown_warning_is_generic_and_profile_id_is_never_rendered(
    connection: sqlite3.Connection,
) -> None:
    profile = "owner-private-profile"
    candidate = spec(profile=profile)
    value = _custom_proposal(
        connection,
        candidate,
        document(),
        profile=profile,
        warnings=("unknown_safe_warning",),
    )

    preview = render_preview(value)

    assert "로그인 프로필: 필요" in preview.text
    assert profile not in preview.text
    assert "추가 확인이 필요한 항목" in preview.text
    assert "unknown_safe_warning" not in preview.text


@pytest.mark.parametrize(
    "mutation",
    ["spec", "hash", "candidate", "token", "preview", "robots", "warnings"],
)
def test_mutated_proposal_binding_fails_before_rendering_callbacks(
    connection: sqlite3.Connection,
    mutation: str,
) -> None:
    value = proposal(connection)
    if mutation == "spec":
        object.__setattr__(value.spec, "name", "변조된 이름")
    elif mutation == "hash":
        object.__setattr__(value, "spec_hash", "f" * 64)
    elif mutation == "candidate":
        object.__setattr__(value, "candidate_version_id", "f" * 32)
    elif mutation == "token":
        object.__setattr__(value.pending_action, "token", "f" * 32)
    elif mutation == "preview":
        object.__setattr__(
            value.preview_items[0],
            "fields",
            MappingProxyType({"name": "변조", "price": 99000}),
        )
    elif mutation == "robots":
        object.__setattr__(value.robots, "policy_fetched", True)
    else:
        object.__setattr__(value, "warnings", ("unknown_safe_warning",))

    with pytest.raises(PlanningFailed) as caught:
        render_preview(value)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_preview_callback_shapes_and_total_output_are_bounded(
    connection: sqlite3.Connection,
) -> None:
    value = proposal(connection)

    preview = render_preview(value)

    callbacks = tuple(button.callback_data for row in preview.buttons for button in row)
    assert callbacks[0].startswith("confirm:")
    assert callbacks[1].startswith("edit:")
    assert callbacks[2].startswith("cancel:")
    assert all(len(item.encode("utf-8")) <= 64 for item in callbacks)
    assert len(preview.text) <= 3_500


@pytest.mark.parametrize(
    "secret",
    [
        "Bearer abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature",
        "eyJhbGciOiJIUzI1NiJ9.YWJjZGVm.signature",
        "sk-privateOpenAIKey123456",
        "authorization: private-auth-value",
        "Cookie=session-private-value",
        "Set-Cookie: sid=private-cookie",
        "sessionid=private-session",
        "access_token=private-access",
        "api_key=private-api",
    ],
)
def test_every_secret_pattern_is_hidden_in_direct_rendered_strings(
    connection: sqlite3.Connection,
    secret: str,
) -> None:
    candidate = MonitorSpec.model_validate(
        {
            **spec().model_dump(mode="json"),
            "name": secret,
        }
    )
    value = _custom_proposal(connection, candidate, document())

    preview = render_preview(value)

    assert secret not in preview.text
    assert "[숨김]" in preview.text


def test_planner_integrated_observed_jwt_is_hidden_from_preview(
    connection: sqlite3.Connection,
) -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature"
    body = f"<main><h1>{jwt}</h1><span class='price'>99,000원</span></main>".encode()
    value = _custom_proposal(
        connection,
        spec(),
        document(body=body),
        sanitizer=lambda _html, *, secret_values: "<main>safe projection</main>",
    )

    preview = render_preview(value)

    assert jwt not in preview.text
    assert "[숨김]" in preview.text


def test_broad_jwt_is_hidden_from_every_rendered_preview_value(
    connection: sqlite3.Connection,
) -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.YWJjZGVm.signature"
    body = f"<main><h1>{jwt}</h1><span class='price'>99,000원</span></main>".encode()
    candidate = MonitorSpec.model_validate(
        {
            **spec().model_dump(mode="json"),
            "name": jwt,
        }
    )
    value = _custom_proposal(
        connection,
        candidate,
        document(body=body),
        sanitizer=lambda _html, *, secret_values: "<main>safe projection</main>",
    )

    preview = render_preview(value)

    assert jwt not in preview.text
    assert preview.text.count("[숨김]") >= 2


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
def test_quoted_assignment_hides_entire_preview_values(
    connection: sqlite3.Connection,
    secret: str,
) -> None:
    body = f"<main><h1>{secret}</h1><span class='price'>99,000원</span></main>".encode()
    candidate = MonitorSpec.model_validate(
        {
            **spec().model_dump(mode="json"),
            "name": secret,
        }
    )
    value = _custom_proposal(
        connection,
        candidate,
        document(body=body),
        sanitizer=lambda _html, *, secret_values: "<main>safe projection</main>",
    )

    preview = render_preview(value)

    assert "모니터: [숨김]" in preview.text
    assert "현재 이름: [숨김]" in preview.text
    assert "supersecretvalue" not in preview.text


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [
        ("*/15 * * * *", "15분마다"),
        ("30 8 * * *", "매일 08:30"),
        ("0 9 * * 1-5", "평일 09:00"),
        ("0 9 * * 1", "매주 월요일 09:00"),
        ("0 9 1 * *", "매월 1일 09:00"),
        ("5 4 * * 2,4", "cron: 5 4 * * 2,4"),
    ],
)
def test_common_and_unsupported_valid_cron_schedules_preserve_fidelity(
    connection: sqlite3.Connection,
    schedule: str,
    expected: str,
) -> None:
    candidate = MonitorSpec.model_validate({**spec().model_dump(mode="json"), "schedule": schedule})
    value = _custom_proposal(connection, candidate, document())

    preview = render_preview(value)

    assert f"확인 주기: {expected}" in preview.text
    assert len(preview.text) <= 3_500


def test_extreme_valid_spec_renders_bounded_without_orphan_confirmation(
    connection: sqlite3.Connection,
) -> None:
    long_url = "https://example.com/" + ("long-path-" * 190)
    fields = {
        f"field_{index:02d}": {
            "selector": "h1",
            "type": "text",
            "required": True,
        }
        for index in range(49)
    }
    fields["price"] = {"selector": ".price", "type": "krw", "required": True}
    candidate = MonitorSpec.model_validate(
        {
            **spec(url=long_url).model_dump(mode="json"),
            "name": "가" * 120,
            "extract": {"item_scope": "main", "fields": fields},
            "rules": [
                {
                    "kind": "numeric_threshold",
                    "field": "price",
                    "operator": "lt",
                    "value": 100_000 + index,
                }
                for index in range(20)
            ],
        }
    )
    source = SourceDocument(
        final_url=long_url,
        status=200,
        content_type="text/html",
        headers={"content-type": "text/html"},
        body=(
            "<main><h1>"
            + ("매우 긴 현재 값 " * 30)
            + "</h1><span class='price'>99,000원</span></main>"
        ).encode(),
        strategy=candidate.fetch_strategy.HTTP,
    )

    value = _custom_proposal(
        connection,
        candidate,
        source,
        resolved=target(long_url),
    )
    preview = render_preview(value)

    assert len(preview.text) <= 3_500
    assert "모니터:" in preview.text
    assert "확인 주기:" in preview.text
    assert "수집 방식:" in preview.text
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 1
