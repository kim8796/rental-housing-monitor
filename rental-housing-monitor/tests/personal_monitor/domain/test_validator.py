from __future__ import annotations

from datetime import UTC, datetime

import pytest

from personal_monitor.domain.observation import ObservationBatch, ObservedItem, SourceWarning
from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.domain.validator import BatchValidationError, validate_batch

NOW = datetime(2026, 7, 22, tzinfo=UTC)


def make_spec() -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": "owner-1",
            "name": "validator",
            "target_url": "https://example.com/items",
            "source_adapter": "scrapling",
            "extract": {
                "item_scope": "main",
                "fields": {
                    "title": {"selector": ".title", "type": "text", "required": True},
                    "count": {"selector": ".count", "type": "integer", "required": True},
                    "price": {"selector": ".price", "type": "krw", "required": True},
                    "active": {"selector": ".active", "type": "boolean", "required": True},
                    "url": {"selector": "a", "attribute": "href", "type": "url"},
                },
            },
            "validators": {
                "min_items": 1,
                "max_items": 2,
                "allowed_link_domains": ["example.com"],
            },
            "rules": [{"kind": "new_item"}],
        }
    )


def batch(*items: ObservedItem, partial: bool = False) -> ObservationBatch:
    return ObservationBatch(
        monitor_id="monitor-1",
        items=tuple(items),
        observed_at=NOW,
        source_hash="hash",
        warnings=(SourceWarning("source", "fetch", "safe"),) if partial else (),
    )


def valid_item(item_id: str = "one") -> ObservedItem:
    return ObservedItem(
        item_id,
        {
            "title": "상품",
            "count": 1,
            "price": 1000.0,
            "active": True,
            "url": "https://example.com/item/1",
        },
    )


def test_complete_batch_enforces_minimum_and_partial_batch_may_be_below_it() -> None:
    with pytest.raises(BatchValidationError, match="item count"):
        validate_batch(make_spec(), batch())

    validate_batch(make_spec(), batch(partial=True))


def test_partial_batch_still_enforces_maximum() -> None:
    with pytest.raises(BatchValidationError, match="item count"):
        validate_batch(
            make_spec(),
            batch(valid_item("one"), valid_item("two"), valid_item("three"), partial=True),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", None),
        ("count", True),
        ("count", 1.5),
        ("price", True),
        ("price", float("nan")),
        ("active", 1),
        ("url", "https://sub.example.com/item/1"),
        ("url", "not-a-url"),
        ("url", "https://[invalid"),
    ],
)
def test_required_types_finite_numbers_and_exact_url_hosts_are_enforced(
    field: str, value: object
) -> None:
    fields = dict(valid_item().fields)
    if value is None:
        del fields[field]
    else:
        fields[field] = value  # type: ignore[assignment]

    with pytest.raises(BatchValidationError):
        validate_batch(make_spec(), batch(ObservedItem("one", fields)))


def test_validator_accepts_a_complete_valid_batch() -> None:
    validate_batch(make_spec(), batch(valid_item()))
