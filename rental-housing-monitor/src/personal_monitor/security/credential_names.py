from __future__ import annotations

from typing import Final

_ALLOWED_KEY_QUOTES: Final = frozenset({"'", '"', "`"})
_MAX_NORMALIZED_NAME_LENGTH: Final = 128

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


def normalize_credential_name(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    if normalized[0] in _ALLOWED_KEY_QUOTES or normalized[-1] in _ALLOWED_KEY_QUOTES:
        if len(normalized) < 2 or normalized[-1] != normalized[0]:
            return ""
        normalized = normalized[1:-1].strip()
    if not normalized or len(normalized) > _MAX_NORMALIZED_NAME_LENGTH:
        return ""
    return normalized.casefold().replace("_", "").replace("-", "")


def is_sensitive_credential_name(value: str) -> bool:
    return normalize_credential_name(value) in _SENSITIVE_CREDENTIAL_COMPACT_NAMES
