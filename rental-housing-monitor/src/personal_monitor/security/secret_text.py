from __future__ import annotations

import re
from typing import Final

_REDACTION: Final = "[숨김]"
_TOKEN_PATTERNS: Final = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
)
_SENSITIVE_KEY: Final = (
    r"(?:access[_-]?token|api[_-]?key|authorization|client[_-]?secret|"
    r"cookie|set-cookie|credential|passwd|password|secret|session(?:id)?|token)"
)
_ASSIGNMENT: Final = re.compile(
    rf"(?<![A-Za-z0-9_-])[\"']?{_SENSITIVE_KEY}[\"']?"
    r"\s*[:=]\s*"
    r"(?:\"[^\"\r\n]|'[^'\r\n]|[^\s<>\"'])",
    re.IGNORECASE,
)
_KEY_ONLY: Final = re.compile(rf"^{_SENSITIVE_KEY}$", re.IGNORECASE)


def contains_sensitive_text(value: str) -> bool:
    if not isinstance(value, str):
        return True
    return (
        _ASSIGNMENT.search(value) is not None
        or _KEY_ONLY.fullmatch(value) is not None
        or any(pattern.search(value) is not None for pattern in _TOKEN_PATTERNS)
    )


def redact_sensitive_text(value: str) -> str:
    if type(value) is not str:
        raise TypeError("value must be a string")
    return _REDACTION if contains_sensitive_text(value) else value
