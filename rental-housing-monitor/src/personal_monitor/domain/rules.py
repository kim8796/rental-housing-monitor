from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from personal_monitor.domain.observation import ObservedItem, Scalar
from personal_monitor.domain.spec import RuleKind, RuleSpec


@dataclass(frozen=True, slots=True)
class RuleMatch:
    kind: RuleKind
    field: str | None
    previous: Scalar
    current: Scalar


def evaluate_rules(
    rules: Sequence[RuleSpec],
    *,
    previous: ObservedItem | None,
    current: ObservedItem,
    is_new: bool,
) -> list[RuleMatch]:
    """Evaluate the closed monitor rule set against one current item."""
    matches: list[RuleMatch] = []
    for rule in rules:
        old = previous.fields.get(rule.field) if previous and rule.field else None
        new = current.fields.get(rule.field) if rule.field else None
        if rule.kind is RuleKind.NEW_ITEM:
            if is_new:
                matches.append(RuleMatch(rule.kind, None, None, None))
            continue

        if (
            (rule.kind is RuleKind.FIELD_CHANGED and old != new)
            or (
                rule.kind is RuleKind.NUMERIC_THRESHOLD
                and _crossed(old, new, rule.operator, rule.value)
            )
            or (rule.kind is RuleKind.STATUS_EQUALS and old != rule.value and new == rule.value)
            or (rule.kind is RuleKind.KEYWORD_MATCH and _new_keyword(old, new, rule.keywords))
        ):
            matches.append(RuleMatch(rule.kind, rule.field, old, new))
    return matches


def _crossed(
    previous: Scalar, current: Scalar, operator: str | None, threshold: Scalar
) -> bool:
    if not (_is_number(current) and _is_number(threshold)):
        return False
    if not _satisfies(current, operator, threshold):
        return False
    if previous is None:
        return True
    return _is_number(previous) and not _satisfies(previous, operator, threshold)


def _satisfies(value: int | float, operator: str | None, threshold: int | float) -> bool:
    if operator == "lt":
        return value < threshold
    if operator == "lte":
        return value <= threshold
    if operator == "eq":
        return value == threshold
    if operator == "gte":
        return value >= threshold
    if operator == "gt":
        return value > threshold
    return False


def _new_keyword(previous: Scalar, current: Scalar, keywords: Sequence[str]) -> bool:
    if not isinstance(current, str):
        return False
    current_text = current.casefold()
    previous_text = previous.casefold() if isinstance(previous, str) else ""
    return any(keyword.casefold() in current_text for keyword in keywords) and not any(
        keyword.casefold() in previous_text for keyword in keywords
    )


def _is_number(value: Scalar) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
