from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from personal_monitor.domain.observation import Scalar
from personal_monitor.domain.spec import (
    SENSITIVE_QUERY_PARAMETER_NAMES,
    FieldType,
)
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.security.url_policy import (
    canonicalize_hostname,
    has_unsafe_url_characters,
)

_TRACKING_QUERY_KEYS = frozenset(
    {"_ga", "_gl", "dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid"}
)
_TRUE_VALUES = frozenset({"1", "true", "yes"})
_FALSE_VALUES = frozenset({"0", "false", "no"})


def normalize_value(field_type: FieldType, value: str, base_url: str) -> Scalar:
    """Normalize one extracted scalar without including source material in failures."""
    try:
        normalized = NORMALIZERS[field_type](value, base_url)
    except MonitorError:
        raise
    except (TypeError, ValueError, OverflowError):
        raise _normalization_error() from None
    if normalized is None or isinstance(normalized, str) and not normalized:
        raise _normalization_error()
    if isinstance(normalized, float) and not math.isfinite(normalized):
        raise _normalization_error()
    return normalized


def parse_boolean(value: str, _base_url: str) -> bool:
    normalized = " ".join(value.split()).casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError("invalid boolean")


def normalize_url(value: str, base_url: str = "") -> str:
    """Resolve and canonicalize a safe absolute HTTP URL."""
    if (
        not isinstance(value, str)
        or not isinstance(base_url, str)
        or _has_unsafe_url_text(value)
        or _has_unsafe_url_text(base_url)
    ):
        raise _normalization_error()
    try:
        candidate = urljoin(base_url, value)
    except (TypeError, ValueError):
        raise _normalization_error() from None
    if _has_unsafe_url_text(candidate):
        raise _normalization_error()
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except (TypeError, ValueError):
        raise _normalization_error() from None

    scheme = parts.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.netloc.rsplit("@", 1)[-1].endswith(":")
        or port not in {None, 80, 443}
    ):
        raise _normalization_error()

    try:
        host = canonicalize_hostname(parts.hostname)
        query = parse_qsl(
            parts.query,
            keep_blank_values=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=1000,
        )
    except (MonitorError, UnicodeError, ValueError):
        raise _normalization_error() from None
    if any(key.casefold() in SENSITIVE_QUERY_PARAMETER_NAMES for key, _value in query):
        raise _normalization_error()
    query = [(key, item) for key, item in query if not _is_tracking_query_key(key)]

    display_host = f"[{host}]" if ":" in host else host
    default_port = 80 if scheme == "http" else 443
    netloc = display_host if port in {None, default_port} else f"{display_host}:{port}"
    return urlunsplit(
        (
            scheme,
            netloc,
            parts.path or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def _is_tracking_query_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized.startswith("utm_") or normalized in _TRACKING_QUERY_KEYS


def _has_unsafe_url_text(value: str) -> bool:
    return (
        has_unsafe_url_characters(value)
        or any(character.isspace() for character in value)
        or re.search(r"%(?![0-9a-fA-F]{2})", value) is not None
    )


def _normalize_text(value: str, _base_url: str) -> str:
    return " ".join(value.split())


def _normalize_integer(value: str, _base_url: str) -> int:
    return int(re.sub(r"[^0-9-]", "", value))


def _normalize_decimal(value: str, _base_url: str) -> float:
    return float(re.sub(r"[^0-9.-]", "", value))


def _normalize_krw(value: str, _base_url: str) -> int:
    return int(re.sub(r"[^0-9]", "", value))


def _normalize_date(value: str, _base_url: str) -> str:
    return date.fromisoformat(value).isoformat()


def _normalize_datetime(value: str, _base_url: str) -> str:
    return datetime.fromisoformat(value).isoformat()


def _normalize_url(value: str, base_url: str) -> str:
    return normalize_url(value, base_url)


NORMALIZERS: dict[FieldType, Callable[[str, str], Scalar]] = {
    FieldType.TEXT: _normalize_text,
    FieldType.INTEGER: _normalize_integer,
    FieldType.DECIMAL: _normalize_decimal,
    FieldType.KRW: _normalize_krw,
    FieldType.DATE: _normalize_date,
    FieldType.DATETIME: _normalize_datetime,
    FieldType.BOOLEAN: parse_boolean,
    FieldType.URL: _normalize_url,
}


def _normalization_error() -> MonitorError:
    return MonitorError(ErrorClass.VALIDATION, "extract", "field normalization failed")
