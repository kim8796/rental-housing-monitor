from __future__ import annotations

import re
from typing import Final

_REDACTION: Final = "[숨김]"
_SECRET_PATTERNS: Final = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(
        r"\b(?:access[_-]?token|api[_-]?key|authorization|client[_-]?secret|"
        r"cookie|set-cookie|credential|passwd|password|secret|session(?:id)?|token)"
        r"\s*[:=]\s*[^\s<>\"']*",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:access[_-]?token|api[_-]?key|authorization|cookie|set-cookie|"
        r"session(?:id)?)$",
        re.IGNORECASE,
    ),
)


def contains_sensitive_text(value: str) -> bool:
    if not isinstance(value, str):
        return True
    return any(pattern.search(value) is not None for pattern in _SECRET_PATTERNS)


def redact_sensitive_text(value: str) -> str:
    if type(value) is not str:
        raise TypeError("value must be a string")
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(_REDACTION, result)
    return result
