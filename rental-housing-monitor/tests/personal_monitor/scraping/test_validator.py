from __future__ import annotations

from datetime import date, datetime

import pytest

from personal_monitor.domain.observation import ObservedItem, stable_item_id
from personal_monitor.domain.spec import ExtractSpec, ValidatorSpec
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.validator import ObservationValidator


def schemas(
    *, min_items: int = 1, max_items: int = 2, domains: tuple[str, ...] = ("example.com",)
) -> tuple[ExtractSpec, ValidatorSpec]:
    return (
        ExtractSpec.model_validate(
            {
                "item_scope": "main",
                "fields": {
                    "title": {"selector": ".title", "type": "text", "required": True},
                    "count": {"selector": ".count", "type": "integer", "required": True},
                    "price": {"selector": ".price", "type": "decimal", "required": True},
                    "day": {"selector": ".day", "type": "date", "required": True},
                    "time": {"selector": ".time", "type": "datetime", "required": True},
                    "active": {"selector": ".active", "type": "boolean", "required": True},
                    "url": {"selector": "a", "attribute": "href", "type": "url"},
                    "note": {"selector": ".note", "type": "text", "required": False},
                },
            }
        ),
        ValidatorSpec(min_items=min_items, max_items=max_items, allowed_link_domains=domains),
    )


def valid_fields() -> dict[str, object]:
    return {
        "title": "상품",
        "count": 1,
        "price": 1000.5,
        "day": date(2026, 7, 23).isoformat(),
        "time": datetime(2026, 7, 23, 12, 30).isoformat(),
        "active": True,
        "url": "https://example.com/product/1",
    }


def item(fields: dict[str, object] | None = None) -> ObservedItem:
    values = valid_fields() if fields is None else fields
    return ObservedItem(stable_item_id(values), values)  # type: ignore[arg-type]


def test_validator_accepts_valid_items_and_returns_an_immutable_tuple() -> None:
    extract, validators = schemas()
    value = item()

    result = ObservationValidator().validate([value], extract, validators)

    assert result == (value,)


def test_empty_below_minimum_is_structure_but_zero_minimum_accepts_empty() -> None:
    extract, validators = schemas()
    with pytest.raises(MonitorError) as caught:
        ObservationValidator().validate([], extract, validators)
    assert caught.value.error_class is ErrorClass.STRUCTURE

    extract, validators = schemas(min_items=0)
    assert ObservationValidator().validate([], extract, validators) == ()


def test_above_maximum_is_validation_error() -> None:
    extract, validators = schemas(max_items=1)
    values = [item(), ObservedItem("second", valid_fields())]

    with pytest.raises(MonitorError) as caught:
        ObservationValidator().validate(values, extract, validators)

    assert caught.value.error_class is ErrorClass.VALIDATION


def test_validator_rejects_missing_required_and_every_undeclared_field() -> None:
    extract, validators = schemas()
    missing = valid_fields()
    del missing["title"]
    with pytest.raises(MonitorError) as caught:
        ObservationValidator().validate([item(missing)], extract, validators)
    assert caught.value.error_class is ErrorClass.STRUCTURE

    undeclared = valid_fields()
    undeclared["html"] = "<html>source-secret</html>"
    with pytest.raises(MonitorError) as caught:
        ObservationValidator().validate([item(undeclared)], extract, validators)
    assert caught.value.error_class is ErrorClass.VALIDATION
    assert "source-secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", 1),
        ("title", ""),
        ("count", True),
        ("count", 1.5),
        ("price", True),
        ("price", float("nan")),
        ("price", float("inf")),
        ("day", "2026-07-23T00:00:00"),
        ("day", "2026-07-01 "),
        ("time", "2026-07-23"),
        ("time", "2026-07-23 12:30:00"),
        ("active", 1),
        ("url", "not-a-url"),
        ("url", "https://sub.example.com/item"),
        ("url", "https://user@example.com/item"),
        ("url", "https://example.com/item#fragment"),
    ],
)
def test_validator_independently_enforces_scalar_type_finite_temporal_and_url_constraints(
    field: str, value: object
) -> None:
    extract, validators = schemas()
    fields = valid_fields()
    fields[field] = value

    with pytest.raises(MonitorError) as caught:
        ObservationValidator().validate([item(fields)], extract, validators)

    assert caught.value.error_class is ErrorClass.VALIDATION


def test_optional_field_may_be_absent_or_none_but_must_validate_when_present() -> None:
    extract, validators = schemas()
    ObservationValidator().validate([item()], extract, validators)
    with_none = valid_fields()
    with_none["note"] = None
    ObservationValidator().validate([item(with_none)], extract, validators)
    bad = valid_fields()
    bad["note"] = 7

    with pytest.raises(MonitorError):
        ObservationValidator().validate([item(bad)], extract, validators)


def test_duplicate_and_noncanonical_item_ids_are_rejected() -> None:
    extract, validators = schemas(max_items=3)
    first = item()
    second_fields = valid_fields()
    second_fields["count"] = 2
    second = ObservedItem(first.item_id, second_fields)
    with pytest.raises(MonitorError, match="identity"):
        ObservationValidator().validate([first, second], extract, validators)

    with pytest.raises(MonitorError, match="identity"):
        ObservationValidator().validate(
            [ObservedItem("invented-id", valid_fields())], extract, validators
        )


def test_invalid_identity_and_unhashable_content_always_map_to_monitor_error() -> None:
    extract, validators = schemas()
    fields = valid_fields()
    del fields["url"]
    fields["title"] = float("nan")

    with pytest.raises(MonitorError) as caught:
        ObservationValidator().validate(
            [ObservedItem("untrusted-id", fields)],
            extract,
            validators,  # type: ignore[arg-type]
        )

    assert caught.value.error_class is ErrorClass.VALIDATION

    with pytest.raises(MonitorError):
        ObservationValidator().validate(
            [ObservedItem([], valid_fields())],
            extract,
            validators,  # type: ignore[arg-type]
        )


def test_allowed_domain_validation_is_exact_and_supports_normalized_idna() -> None:
    extract, validators = schemas(domains=("xn--vv4b11d.xn--3e0b707e",))
    fields = valid_fields()
    fields["url"] = "https://xn--vv4b11d.xn--3e0b707e/product"

    ObservationValidator().validate([item(fields)], extract, validators)
