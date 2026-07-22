from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime
from urllib.parse import urlsplit

from personal_monitor.domain.observation import ObservedItem, Scalar, stable_item_id
from personal_monitor.domain.spec import ExtractSpec, FieldType, ValidatorSpec
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.normalizers import normalize_url


class ObservationValidator:
    """Independently enforce the declared observation contract."""

    def validate(
        self,
        items: Iterable[ObservedItem],
        extract: ExtractSpec,
        validators: ValidatorSpec,
    ) -> tuple[ObservedItem, ...]:
        values = tuple(items)
        if len(values) < validators.min_items:
            raise MonitorError(
                ErrorClass.STRUCTURE,
                "validate",
                "item count is below the required minimum",
            )
        if len(values) > validators.max_items:
            raise _validation_error("item count exceeds the allowed maximum")

        identities: set[str] = set()
        for item in values:
            if not isinstance(item, ObservedItem):
                raise _validation_error("observation item is invalid")
            self._validate_item(item, extract, validators)
            if not isinstance(item.item_id, str) or not item.item_id:
                raise _validation_error("item identity is invalid")
            try:
                expected_identity = stable_item_id(item.fields)
            except Exception:
                raise _validation_error("item identity is invalid") from None
            if item.item_id in identities or item.item_id != expected_identity:
                raise _validation_error("item identity is invalid")
            identities.add(item.item_id)
        return values

    def _validate_item(
        self,
        item: ObservedItem,
        extract: ExtractSpec,
        validators: ValidatorSpec,
    ) -> None:
        for name in item.fields:
            if name not in extract.fields:
                raise _validation_error("observation contains an undeclared field")

        for name, field in extract.fields.items():
            if name not in item.fields or item.fields[name] is None:
                if field.required:
                    raise MonitorError(
                        ErrorClass.STRUCTURE,
                        "validate",
                        "required field is missing",
                    )
                continue
            _validate_scalar(field.type, item.fields[name], validators.allowed_link_domains)


def _validate_scalar(
    field_type: FieldType,
    value: Scalar,
    allowed_domains: tuple[str, ...],
) -> None:
    if not isinstance(value, str | int | float | bool) or value is None:
        raise _validation_error("field type is invalid")

    valid = False
    if field_type is FieldType.TEXT:
        valid = isinstance(value, str) and bool(value) and value == " ".join(value.split())
    elif field_type is FieldType.INTEGER:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif field_type is FieldType.DECIMAL:
        valid = (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )
    elif field_type is FieldType.KRW:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif field_type is FieldType.DATE:
        valid = _valid_date(value)
    elif field_type is FieldType.DATETIME:
        valid = _valid_datetime(value)
    elif field_type is FieldType.BOOLEAN:
        valid = isinstance(value, bool)
    elif field_type is FieldType.URL:
        valid = _valid_url(value, allowed_domains)
    if not valid:
        raise _validation_error("field type is invalid")


def _valid_date(value: Scalar) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _valid_datetime(value: Scalar) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value and "T" in value


def _valid_url(value: Scalar, allowed_domains: tuple[str, ...]) -> bool:
    if not isinstance(value, str):
        return False
    try:
        if normalize_url(value) != value:
            return False
        parts = urlsplit(value)
    except MonitorError:
        return False
    host = parts.hostname.rstrip(".").casefold() if parts.hostname else None
    return host is not None and host in allowed_domains


def _validation_error(detail: str) -> MonitorError:
    return MonitorError(ErrorClass.VALIDATION, "validate", detail)
