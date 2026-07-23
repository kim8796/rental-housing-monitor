from __future__ import annotations

import pytest

from personal_monitor.security.secret_text import (
    contains_sensitive_text,
    redact_sensitive_text,
)

QUOTED_ASSIGNMENTS = (
    'authorization: "supersecretvalue"',
    "password='supersecretvalue'",
    '"authorization": "supersecretvalue"',
    "'api_key': 'supersecretvalue'",
    " authorization \t = \t supersecretvalue ",
    "prefix password : unquoted-secret-value suffix",
    "session_id=supersecretvalue",
    'session-id="supersecretvalue"',
    "auth: supersecretvalue",
    "credentials=supersecretvalue",
    "signature: supersecretvalue",
    "key=supersecretvalue",
    "`authorization`: `supersecretvalue`",
)


@pytest.mark.parametrize("value", QUOTED_ASSIGNMENTS)
def test_sensitive_assignment_detection_and_redaction_have_identical_semantics(
    value: str,
) -> None:
    assert contains_sensitive_text(value)
    assert redact_sensitive_text(value) == "[숨김]"


@pytest.mark.parametrize(
    "value",
    (
        "ordinary authorization documentation",
        "password policy changed",
        "https://example.com/catalog",
        "상품 가격을 알려줘",
    ),
)
def test_benign_text_is_unchanged(value: str) -> None:
    assert not contains_sensitive_text(value)
    assert redact_sensitive_text(value) == value


def test_assignment_detection_has_no_whitespace_length_bypass() -> None:
    value = "authorization" + (" " * 10_000) + '= "supersecretvalue"'

    assert contains_sensitive_text(value)
    assert redact_sensitive_text(value) == "[숨김]"


@pytest.mark.parametrize(
    "value",
    (
        "Bearer abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiJ9.YWJjZGVm.signature",
        "sk-privateOpenAIKey123456",
        *QUOTED_ASSIGNMENTS,
    ),
)
def test_contains_if_and_only_if_redaction_hides_entire_string(value: str) -> None:
    assert contains_sensitive_text(value) is (redact_sensitive_text(value) == "[숨김]")
