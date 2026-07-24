from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_PROJECT_ID_RE = re.compile(r"[a-z][a-z0-9-]{4,61}[a-z0-9]\Z")
_MAX_MICROS = 2**63 - 1


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _safe_name(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 200
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _micros(value: object, *, positive: bool = False) -> bool:
    if type(value) is not int or value < 0 or value > _MAX_MICROS:
        return False
    return not positive or value > 0


@dataclass(frozen=True, slots=True, repr=False)
class CreditGrant:
    id: str
    name: str
    original_micros: int
    baseline_remaining_micros: int
    starts_on: date
    ends_on: date
    baseline_as_of: datetime

    def __post_init__(self) -> None:
        if (
            type(self.id) is not str
            or _ID_RE.fullmatch(self.id) is None
            or not _safe_name(self.name)
            or not _micros(self.original_micros, positive=True)
            or not _micros(self.baseline_remaining_micros)
            or self.baseline_remaining_micros > self.original_micros
            or type(self.starts_on) is not date
            or type(self.ends_on) is not date
            or self.ends_on <= self.starts_on
            or not _aware(self.baseline_as_of)
        ):
            raise ValueError("invalid billing credit grant")

    def __repr__(self) -> str:
        return "<CreditGrant redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class ProjectSpend:
    project_id: str
    project_name: str
    cost_micros: int

    def __post_init__(self) -> None:
        if (
            type(self.project_id) is not str
            or _PROJECT_ID_RE.fullmatch(self.project_id) is None
            or not _safe_name(self.project_name)
            or not _micros(self.cost_micros)
        ):
            raise ValueError("invalid project spend")

    def __repr__(self) -> str:
        return "<ProjectSpend redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class BillingAggregate:
    observed_at: datetime
    promotion_consumed_micros: int
    recent_7d_consumed_micros: int
    projects: tuple[ProjectSpend, ...]

    def __post_init__(self) -> None:
        if (
            not _aware(self.observed_at)
            or not _micros(self.promotion_consumed_micros)
            or not _micros(self.recent_7d_consumed_micros)
            or type(self.projects) is not tuple
            or len(self.projects) > 1_000
            or any(type(item) is not ProjectSpend for item in self.projects)
            or len({item.project_id for item in self.projects}) != len(self.projects)
        ):
            raise ValueError("invalid billing aggregate")

    def __repr__(self) -> str:
        return "<BillingAggregate redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class BillingSnapshot:
    grant_id: str
    observed_at: datetime
    source: str
    original_micros: int
    remaining_micros: int
    used_micros: int
    remaining_basis_points: int
    daily_burn_micros: int
    projected_exhaustion_on: date | None
    ends_on: date
    projects: tuple[ProjectSpend, ...]

    def __repr__(self) -> str:
        return "<BillingSnapshot redacted>"
