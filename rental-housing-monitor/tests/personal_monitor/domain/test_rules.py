from __future__ import annotations

import pytest

from personal_monitor.domain.observation import ObservedItem
from personal_monitor.domain.rules import evaluate_rules
from personal_monitor.domain.spec import RuleSpec


def item(**fields: str | int | float | bool | None) -> ObservedItem:
    return ObservedItem(item_id="p1", fields=fields)


def test_new_item_matches_only_for_new_items() -> None:
    rule = RuleSpec(kind="new_item")
    matches = evaluate_rules([rule], previous=None, current=item(), is_new=True)
    assert [match.kind.value for match in matches] == ["new_item"]
    assert evaluate_rules([rule], previous=None, current=item(), is_new=False) == []


def test_field_changed_matches_when_value_changes() -> None:
    rule = RuleSpec(kind="field_changed", field="price")
    matches = evaluate_rules(
        [rule], previous=item(price=120_000), current=item(price=99_000), is_new=False
    )
    assert [(match.field, match.previous, match.current) for match in matches] == [
        ("price", 120_000, 99_000)
    ]


def test_threshold_matches_only_on_crossing() -> None:
    rule = RuleSpec(kind="numeric_threshold", field="price", operator="lte", value=100_000)
    matches = evaluate_rules(
        [rule], previous=item(price=120_000), current=item(price=99_000), is_new=False
    )
    assert [match.kind.value for match in matches] == ["numeric_threshold"]
    assert evaluate_rules(
        [rule], previous=item(price=99_000), current=item(price=90_000), is_new=False
    ) == []


def test_threshold_matches_when_previous_field_is_absent() -> None:
    rule = RuleSpec(kind="numeric_threshold", field="price", operator="lte", value=100_000)
    matches = evaluate_rules([rule], previous=item(), current=item(price=99_000), is_new=False)
    assert [match.kind.value for match in matches] == ["numeric_threshold"]


@pytest.mark.parametrize("old, new", [("120000", 99_000), (None, "99000"), (True, 99_000)])
def test_threshold_ignores_non_numeric_current_or_previous_values(
    old: str | int | bool | None, new: str | int | bool | None
) -> None:
    rule = RuleSpec(kind="numeric_threshold", field="price", operator="lte", value=100_000)
    matches = evaluate_rules(
        [rule], previous=item(price=old), current=item(price=new), is_new=False
    )
    assert matches == []


def test_status_equals_matches_when_entering_target_status() -> None:
    rule = RuleSpec(kind="status_equals", field="status", value="closed")
    matches = evaluate_rules(
        [rule], previous=item(status="open"), current=item(status="closed"), is_new=False
    )
    assert [(match.previous, match.current) for match in matches] == [("open", "closed")]
    assert evaluate_rules(
        [rule],
        previous=item(status="closed"),
        current=item(status="closed"),
        is_new=False,
    ) == []


def test_keyword_match_uses_casefold_and_only_new_keywords() -> None:
    rule = RuleSpec(kind="keyword_match", field="title", keywords=["STRASSE", "deal"])
    matches = evaluate_rules(
        [rule], previous=item(title="ordinary"), current=item(title="Straße DEAL"), is_new=False
    )
    assert [(match.previous, match.current) for match in matches] == [("ordinary", "Straße DEAL")]
    assert evaluate_rules(
        [rule], previous=item(title="Straße"), current=item(title="Straße DEAL"), is_new=False
    ) == []
