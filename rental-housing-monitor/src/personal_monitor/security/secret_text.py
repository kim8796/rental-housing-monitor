from __future__ import annotations

import re
from typing import Final

from personal_monitor.security.credential_names import (
    SENSITIVE_CREDENTIAL_NAMES,
    is_sensitive_credential_name,
)

_REDACTION: Final = "[숨김]"
_TOKEN_PATTERNS: Final = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
)
_SENSITIVE_KEY: Final = (
    "(?:"
    + "|".join(
        sorted(
            (re.escape(name).replace("_", "[-_]?") for name in SENSITIVE_CREDENTIAL_NAMES),
            key=lambda value: (-len(value), value),
        )
    )
    + ")"
)
_ASSIGNMENT: Final = re.compile(
    rf"""(?:^|[\s{{\[(,;])(?P<key_quote>["'`]?)\s*{_SENSITIVE_KEY}"""
    r"\s*(?P=key_quote)"
    r"\s*[:=]\s*"
    r"""(?:"[^"\r\n]|'[^'\r\n]|`[^`\r\n]|[^\s<>"'`])""",
    re.IGNORECASE,
)


def contains_sensitive_text(value: str) -> bool:
    if not isinstance(value, str):
        return True
    return (
        _ASSIGNMENT.search(value) is not None
        or is_sensitive_credential_name(value)
        or any(pattern.search(value) is not None for pattern in _TOKEN_PATTERNS)
    )


def redact_sensitive_text(value: str) -> str:
    if type(value) is not str:
        raise TypeError("value must be a string")
    return _REDACTION if contains_sensitive_text(value) else value
