from __future__ import annotations

import re
from typing import Final

_ALLOWED_KEY_QUOTES: Final = frozenset({"'", '"', "`"})
_MAX_NORMALIZED_NAME_LENGTH: Final = 128
_CAMEL_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_FIELD_SEPARATORS: Final = re.compile(r"[_-]+")
_IDENTIFIER_TOKEN: Final = re.compile(r"[A-Za-z0-9]+\Z")

SENSITIVE_CREDENTIAL_NAMES: Final = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "key",
        "passwd",
        "password",
        "secret",
        "session",
        "session_id",
        "sessionid",
        "set_cookie",
        "signature",
        "token",
    }
)
_SENSITIVE_CREDENTIAL_COMPACT_NAMES: Final = frozenset(
    name.replace("_", "").replace("-", "") for name in SENSITIVE_CREDENTIAL_NAMES
)


def _bounded_unwrapped_name(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    if normalized[0] in _ALLOWED_KEY_QUOTES or normalized[-1] in _ALLOWED_KEY_QUOTES:
        if len(normalized) < 2 or normalized[-1] != normalized[0]:
            return ""
        normalized = normalized[1:-1].strip()
    if not normalized or len(normalized) > _MAX_NORMALIZED_NAME_LENGTH:
        return ""
    return normalized


def normalize_credential_name(value: str) -> str:
    normalized = _bounded_unwrapped_name(value)
    return normalized.casefold().replace("_", "").replace("-", "")


def is_sensitive_credential_name(value: str) -> bool:
    return normalize_credential_name(value) in _SENSITIVE_CREDENTIAL_COMPACT_NAMES


def is_sensitive_compound_field_name(value: str) -> bool:
    normalized = _bounded_unwrapped_name(value)
    if not normalized:
        return False
    tokens: list[str] = []
    for part in _FIELD_SEPARATORS.split(normalized):
        if not part:
            continue
        pieces = _CAMEL_BOUNDARY.split(part)
        if any(_IDENTIFIER_TOKEN.fullmatch(piece) is None for piece in pieces):
            return False
        tokens.extend(piece.casefold() for piece in pieces)
    for start in range(len(tokens)):
        compact = ""
        for token in tokens[start:]:
            compact += token
            if compact in _SENSITIVE_CREDENTIAL_COMPACT_NAMES:
                return True
    return False
