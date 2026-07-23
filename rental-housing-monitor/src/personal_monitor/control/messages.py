from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from personal_monitor.domain.spec import MonitorStatus
from personal_monitor.security.secret_text import contains_sensitive_text, redact_sensitive_text
from personal_monitor.telegram.types import InlineButton

MAX_CONTROL_REPLY_CHARS: Final = 3_500
_MAX_ROWS: Final = 8
_MAX_BUTTONS: Final = 8
_CALLBACK_RE: Final = re.compile(r"(?:confirm|cancel|edit):[A-Za-z0-9_-]{32}\Z")
_EMBEDDED_URL_RE: Final = re.compile(r"https?://[^\s<>{}\\]+", re.IGNORECASE)
_STATUS_LABELS: Final = {
    MonitorStatus.ACTIVE: "사용 중",
    MonitorStatus.PAUSED_USER: "사용자 일시정지",
    MonitorStatus.PAUSED_AUTH: "로그인 필요",
    MonitorStatus.NEEDS_REVIEW: "검토 필요",
    MonitorStatus.DISABLED: "삭제됨",
}


@dataclass(frozen=True, slots=True, repr=False)
class ControlReply:
    text: str
    buttons: tuple[tuple[InlineButton, ...], ...] = ()

    def __post_init__(self) -> None:
        rows = self.buttons
        if (
            not _safe_text(self.text, MAX_CONTROL_REPLY_CHARS, allow_layout=True)
            or contains_sensitive_text(self.text)
            or not _direct_text_is_safe(self.text)
            or type(rows) is not tuple
            or any(type(row) is not tuple for row in rows)
            or len(rows) > _MAX_ROWS
            or any(not 1 <= len(row) <= _MAX_BUTTONS for row in rows)
            or any(not _safe_button(button) for row in rows for button in row)
        ):
            raise ValueError("invalid control reply")
        object.__setattr__(self, "buttons", rows)

    def __repr__(self) -> str:
        return "<ControlReply redacted>"


def safe_plain(value: object, *, limit: int) -> str:
    if type(value) is not str:
        return "확인할 수 없음"
    bounded = value[: max(1_024, limit * 8)]
    without_queries = _EMBEDDED_URL_RE.sub(
        lambda match: safe_url(match.group(0), limit=max(300, limit)),
        bounded,
    )
    redacted = redact_sensitive_text(without_queries).replace("<", "‹").replace(">", "›")
    return _normalize_plain(redacted, limit=limit)


def _normalize_plain(value: str, *, limit: int) -> str:
    normalized = " ".join(
        "".join(
            " " if unicodedata.category(character).startswith("C") else character
            for character in value
        ).split()
    )
    if not normalized:
        return "확인할 수 없음"
    if len(normalized) > limit:
        normalized = normalized[: max(0, limit - 1)].rstrip() + "…"
    return normalized


def safe_url(value: object, *, limit: int = 300) -> str:
    result = "확인할 수 없음"
    if type(value) is str:
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
            ):
                result = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))
        except Exception:
            pass
    redacted = redact_sensitive_text(result).replace("<", "‹").replace(">", "›")
    return _normalize_plain(redacted, limit=limit)


def status_label(value: MonitorStatus) -> str:
    return _STATUS_LABELS.get(value, "확인할 수 없음")


def time_label(value: datetime | None) -> str:
    if value is None:
        return "없음"
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return "확인할 수 없음"
        return value.isoformat(timespec="minutes")
    except Exception:
        return "확인할 수 없음"


def _safe_button(value: object) -> bool:
    return (
        type(value) is InlineButton
        and _safe_text(value.text, 64)
        and not contains_sensitive_text(value.text)
        and type(value.callback_data) is str
        and value.callback_data.isascii()
        and len(value.callback_data.encode("ascii")) <= 64
        and _CALLBACK_RE.fullmatch(value.callback_data) is not None
    )


def _direct_text_is_safe(value: str) -> bool:
    if "<" in value or ">" in value:
        return False
    try:
        for matched in _EMBEDDED_URL_RE.finditer(value):
            parsed = urlsplit(matched.group(0))
            if parsed.query or parsed.fragment:
                return False
    except Exception:
        return False
    return True


def _safe_text(value: object, limit: int, *, allow_layout: bool = False) -> bool:
    if type(value) is not str or not 1 <= len(value) <= limit or not value.strip():
        return False
    try:
        if len(value.encode("utf-8", errors="strict")) > limit * 4:
            return False
    except UnicodeError:
        return False
    return not any(
        unicodedata.category(character).startswith("C")
        and not (allow_layout and character in "\n\r\t")
        for character in value
    )
