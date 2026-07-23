from __future__ import annotations

from typing import Final

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


def normalize_credential_name(value: str) -> str:
    return value.casefold().replace("-", "_")


def is_sensitive_credential_name(value: str) -> bool:
    return normalize_credential_name(value) in SENSITIVE_CREDENTIAL_NAMES
