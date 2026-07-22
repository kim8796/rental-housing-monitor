from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from urllib.parse import urlsplit

from personal_monitor.domain.observation import ObservationBatch, Scalar
from personal_monitor.domain.spec import FieldType, MonitorSpec


class BatchValidationError(ValueError):
    """A deterministic adapter-batch contract violation."""


def validate_batch(spec: MonitorSpec, batch: ObservationBatch) -> None:
    """Validate one complete or warning-bearing partial observation batch."""
    item_count = len(batch.items)
    if item_count > spec.validators.max_items:
        raise BatchValidationError("item count invalid")
    if not batch.warnings and item_count < spec.validators.min_items:
        raise BatchValidationError("item count invalid")

    item_ids = [item.item_id for item in batch.items]
    if len(item_ids) != len(set(item_ids)):
        raise BatchValidationError("item identity invalid")

    for item in batch.items:
        for field_name, value in item.fields.items():
            if not _is_scalar(value):
                raise BatchValidationError("field type invalid")
            field = spec.extract.fields.get(field_name)
            if field is None:
                raise BatchValidationError("undeclared field")
            _validate_value(field.type, value, spec.validators.allowed_link_domains)
        for field_name, field in spec.extract.fields.items():
            if field.required and (
                field_name not in item.fields or item.fields[field_name] is None
            ):
                raise BatchValidationError("required field missing")


def _validate_value(
    field_type: FieldType, value: Scalar, allowed_link_domains: tuple[str, ...]
) -> None:
    if value is None:
        return
    if field_type is FieldType.TEXT:
        valid = isinstance(value, str)
    elif field_type is FieldType.INTEGER:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif field_type in {FieldType.DECIMAL, FieldType.KRW}:
        valid = isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value)
    elif field_type is FieldType.BOOLEAN:
        valid = isinstance(value, bool)
    elif field_type is FieldType.DATE:
        valid = _is_iso_date(value)
    elif field_type is FieldType.DATETIME:
        valid = _is_iso_datetime(value)
    elif field_type is FieldType.URL:
        valid = _is_allowed_url(value, allowed_link_domains)
    else:  # pragma: no cover - closed enum exhaustiveness guard
        valid = False
    if not valid:
        raise BatchValidationError("field type invalid")


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _is_iso_date(value: Scalar) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _is_iso_datetime(value: Scalar) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_allowed_url(value: Scalar, allowed_link_domains: tuple[str, ...]) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    host = parts.hostname.rstrip(".").casefold() if parts.hostname else None
    return (
        parts.scheme in {"http", "https"}
        and host is not None
        and parts.username is None
        and parts.password is None
        and host in allowed_link_domains
    )
