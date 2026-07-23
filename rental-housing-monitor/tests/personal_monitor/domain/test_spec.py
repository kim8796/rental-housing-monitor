from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from personal_monitor.domain.spec import (
    ExtractSpec,
    FieldSpec,
    MonitorSpec,
    RuleSpec,
    ValidatorSpec,
)


def valid_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "owner_id": "telegram-user:123456789",
        "name": "상품 가격 감시",
        "target_url": "https://example.com/product/123",
        "source_adapter": "scrapling",
        "adapter_ref": None,
        "fetch_strategy": "auto",
        "schedule": "0 */6 * * *",
        "timezone": "Asia/Seoul",
        "extract": {
            "item_scope": "main",
            "fields": {
                "title": {"selector": "h1", "type": "text", "required": True},
                "price": {"selector": ".price", "type": "krw", "required": True},
            },
        },
        "validators": {
            "min_items": 1,
            "max_items": 1,
            "allowed_link_domains": ["example.com"],
        },
        "rules": [
            {
                "kind": "numeric_threshold",
                "field": "price",
                "operator": "lte",
                "value": 100000,
            }
        ],
        "notify_on_no_change": False,
        "auth_profile_ref": None,
    }


def test_monitor_spec_round_trips() -> None:
    spec = MonitorSpec.model_validate(valid_spec())
    assert spec.model_dump(mode="json", exclude_unset=True) == valid_spec()


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), 2),
        (("target_url",), "file:///etc/passwd"),
        (("schedule",), "*/5 * * * *"),
        (("extract", "fields", "price", "selector"), "__import__('os').system('id')"),
    ],
)
def test_monitor_spec_rejects_unsafe_values(path: tuple[str, ...], value: object) -> None:
    payload = valid_spec()
    target = payload
    for key in path[:-1]:
        target = target[key]  # type: ignore[index,assignment]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(ValidationError):
        MonitorSpec.model_validate(payload)


def test_monitor_spec_rejects_a_later_too_frequent_schedule_gap() -> None:
    payload = valid_spec() | {"schedule": "0,5 0 1,15 * *"}
    with pytest.raises(ValidationError, match="schedule interval"):
        MonitorSpec.model_validate(payload)


def test_monitor_spec_accepts_daily_fold_schedule_by_comparing_real_instants() -> None:
    payload = valid_spec() | {"schedule": "30 1 * * *", "timezone": "America/New_York"}

    assert MonitorSpec.model_validate(payload).schedule == "30 1 * * *"


def test_monitor_spec_rejects_unknown_fields() -> None:
    payload = valid_spec() | {"python_code": "print('unsafe')"}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MonitorSpec.model_validate(payload)


@pytest.mark.parametrize("selector", ["", "a; alert(1)", "${value}", "`command`", "lambda x: x"])
def test_field_spec_rejects_non_declarative_selectors(selector: str) -> None:
    with pytest.raises(ValidationError):
        FieldSpec(selector=selector, type="text")


def test_extract_spec_rejects_an_invalid_field_name() -> None:
    with pytest.raises(ValidationError):
        ExtractSpec.model_validate(
            {"item_scope": "main", "fields": {"Price": {"selector": ".price", "type": "krw"}}}
        )


def test_extract_spec_rejects_a_non_declarative_item_scope() -> None:
    with pytest.raises(ValidationError):
        ExtractSpec.model_validate(
            {
                "item_scope": "main; alert(1)",
                "fields": {"price": {"selector": ".price", "type": "krw"}},
            }
        )


@pytest.mark.parametrize(
    "domains",
    [
        ["Example.COM.", "example.com"],
        ["bad..example.com"],
        [".example.com"],
        ["example.com/path"],
    ],
)
def test_validator_spec_normalizes_hosts_and_rejects_non_hosts(domains: list[str]) -> None:
    if domains == ["Example.COM.", "example.com"]:
        assert ValidatorSpec(allowed_link_domains=domains).allowed_link_domains == ("example.com",)
    else:
        with pytest.raises(ValidationError):
            ValidatorSpec(allowed_link_domains=domains)


@pytest.mark.parametrize(
    "payload",
    [{"min_items": 2, "max_items": 1}, {"min_items": -1}, {"max_items": 10_001}],
)
def test_validator_spec_requires_a_bounded_ordered_item_range(payload: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        ValidatorSpec.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "valid"),
    [
        ({"kind": "new_item"}, True),
        ({"kind": "new_item", "field": "price"}, False),
        ({"kind": "field_changed", "field": "price"}, True),
        ({"kind": "field_changed"}, False),
        ({"kind": "numeric_threshold", "field": "price", "operator": "lte", "value": 1}, True),
        ({"kind": "numeric_threshold", "field": "price", "operator": "lte", "value": True}, False),
        ({"kind": "keyword_match", "field": "title", "keywords": ["sale"]}, True),
        ({"kind": "keyword_match", "field": "title"}, False),
        ({"kind": "status_equals", "field": "status", "value": "sold"}, True),
        ({"kind": "status_equals", "field": "status", "operator": "eq", "value": "sold"}, False),
    ],
)
def test_rule_spec_arguments_must_match_kind(payload: dict[str, object], valid: bool) -> None:
    if valid:
        assert RuleSpec.model_validate(payload).kind.value == payload["kind"]
    else:
        with pytest.raises(ValidationError):
            RuleSpec.model_validate(payload)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/listing",
        "https://user:pass@example.com/listing",
        "https://example.com/listing?access_token=secret",
        "https://example.com/listing?API_KEY=secret",
        "https://example.com/listing?client_secret=secret",
        "https://example.com/listing?password=secret",
        "https://example.com/listing?authorization=secret",
        "https://example.com/listing?session_id=secret",
        "https://example.com/listing?session-id=secret",
        "https://example.com/listing?auth=secret",
        "https://example.com/listing?credentials=secret",
        "https://example.com/listing?signature=secret",
        "https://example.com/listing?key=secret",
    ],
)
def test_monitor_spec_rejects_unsafe_target_urls(url: str) -> None:
    payload = valid_spec() | {"target_url": url}
    with pytest.raises(ValidationError):
        MonitorSpec.model_validate(payload)


def test_monitor_spec_allows_ordinary_target_url_query_parameters() -> None:
    payload = valid_spec() | {"target_url": "https://example.com/listing?page=2&sort=price"}
    assert MonitorSpec.model_validate(payload).target_url == payload["target_url"]


@pytest.mark.parametrize(
    ("source_adapter", "adapter_ref", "valid"),
    [
        ("scrapling", "custom_adapter", False),
        ("official_api", None, False),
        ("python_plugin", None, False),
        ("official_api", "example_api", True),
        ("python_plugin", "example_plugin", True),
    ],
)
def test_monitor_spec_requires_adapter_refs_for_the_right_adapters(
    source_adapter: str, adapter_ref: str | None, valid: bool
) -> None:
    payload = valid_spec() | {"source_adapter": source_adapter, "adapter_ref": adapter_ref}
    if valid:
        assert MonitorSpec.model_validate(payload).adapter_ref == adapter_ref
    else:
        with pytest.raises(ValidationError):
            MonitorSpec.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    [("timezone",), ("schedule",), ("auth_profile_ref",), ("adapter_ref",)],
)
def test_monitor_spec_rejects_invalid_bounded_configuration_values(path: tuple[str, ...]) -> None:
    payload = deepcopy(valid_spec())
    values = {
        "timezone": "Mars/Olympus",
        "schedule": "not a cron expression",
        "auth_profile_ref": "Not Valid",
        "adapter_ref": "Not Valid",
    }
    payload[path[0]] = values[path[0]]
    with pytest.raises(ValidationError):
        MonitorSpec.model_validate(payload)


def test_monitor_spec_is_frozen_and_exports_json_schema() -> None:
    spec = MonitorSpec.model_validate(valid_spec())
    with pytest.raises(ValidationError):
        spec.name = "수정"  # type: ignore[misc]
    assert MonitorSpec.model_json_schema()["title"] == "MonitorSpec"


def test_monitor_spec_is_strict_but_keeps_json_enum_round_trips() -> None:
    payload = valid_spec()
    payload["notify_on_no_change"] = 1
    with pytest.raises(ValidationError):
        MonitorSpec.model_validate(payload)

    spec = MonitorSpec.model_validate_json(json.dumps(valid_spec()))
    assert spec.source_adapter.value == "scrapling"
    assert spec.model_dump(mode="json", exclude_unset=True) == valid_spec()


def test_monitor_spec_nested_collections_are_read_only() -> None:
    spec = MonitorSpec.model_validate(valid_spec())

    with pytest.raises(TypeError):
        spec.extract.fields["price"] = FieldSpec(selector=".other", type="krw")  # type: ignore[index]
    with pytest.raises(AttributeError):
        spec.rules.append(RuleSpec(kind="new_item"))  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        spec.validators.allowed_link_domains.append("other.example")  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        spec.rules[0].keywords.append("sale")  # type: ignore[attr-defined]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_numeric_threshold_rejects_non_finite_values(value: float) -> None:
    payload = valid_spec()
    payload["rules"] = [
        {
            "kind": "numeric_threshold",
            "field": "price",
            "operator": "lte",
            "value": value,
        }
    ]

    with pytest.raises(ValidationError, match="finite"):
        MonitorSpec.model_validate(payload)


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"kind": "field_changed", "field": "prcie"}, "declared"),
        (
            {"kind": "numeric_threshold", "field": "title", "operator": "lte", "value": 1},
            "numeric field",
        ),
        ({"kind": "keyword_match", "field": "price", "keywords": ["sale"]}, "text field"),
        ({"kind": "status_equals", "field": "price", "value": "sold"}, "compatible"),
    ],
)
def test_monitor_spec_rejects_rule_field_typos_and_type_mismatches(
    rule: dict[str, object], message: str
) -> None:
    payload = valid_spec()
    payload["rules"] = [rule]

    with pytest.raises(ValidationError, match=message):
        MonitorSpec.model_validate(payload)


def test_status_equals_literal_type_is_compatible_and_round_trips() -> None:
    payload = valid_spec()
    payload["rules"] = [{"kind": "status_equals", "field": "title", "value": "sold"}]

    spec = MonitorSpec.model_validate(payload)

    assert spec.model_dump(mode="json", exclude_unset=True) == payload
