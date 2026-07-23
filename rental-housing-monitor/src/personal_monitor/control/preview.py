from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from personal_monitor.control.planner import PlanningFailed, ProposedMonitor, _valid_proposal
from personal_monitor.domain.observation import Scalar
from personal_monitor.domain.spec import FetchStrategy, FieldType, MonitorSpec, RuleKind
from personal_monitor.telegram.types import InlineButton

_CREDENTIAL_TEXT: Final = re.compile(
    r"(?:access[_-]?token|api[_-]?key|authorization|bearer|client[_-]?secret|"
    r"cookie|credential|passwd|password|secret|session(?:id)?|token)\s*[=:]",
    re.IGNORECASE,
)
_HOURLY_CRON: Final = re.compile(r"0 \*/([1-9]|1[0-9]|2[0-3]) \* \* \*\Z")
_MAX_PREVIEW_CHARS: Final = 3_500
_FIELD_LABELS: Final = {
    "name": "현재 이름",
    "title": "현재 제목",
    "price": "현재 가격",
    "status": "현재 상태",
    "url": "현재 링크",
}
_STRATEGIES: Final = {
    FetchStrategy.HTTP: "Scrapling HTTP",
    FetchStrategy.DYNAMIC: "Scrapling Dynamic",
    FetchStrategy.STEALTHY: "Scrapling Stealthy",
}


@dataclass(frozen=True, slots=True, repr=False)
class PreviewMessage:
    text: str
    buttons: tuple[tuple[InlineButton, ...], ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "buttons",
            tuple(tuple(row) for row in self.buttons),
        )

    def __repr__(self) -> str:
        return "<PreviewMessage redacted>"


def render_preview(proposal: ProposedMonitor) -> PreviewMessage:
    validated = _fresh_proposal_spec(proposal)
    if validated is None:
        raise PlanningFailed
    spec = validated
    token = proposal.pending_action.token
    callbacks = (
        proposal.pending_action.confirm_callback,
        f"edit:{token}",
        proposal.pending_action.cancel_callback,
    )
    if any(
        type(value) is not str or not value.isascii() or len(value.encode("ascii")) > 64
        for value in callbacks
    ):
        raise PlanningFailed

    headers = [
        f"모니터: {_plain(spec.name, limit=120)}",
        f"대상: {_plain(_redact_url(spec.target_url), limit=300)}",
    ]
    optional_lines: list[tuple[bool, str]] = []
    for index, item in enumerate(proposal.preview_items, start=1):
        if len(proposal.preview_items) > 1:
            optional_lines.append((index == 1, f"예시 {index}"))
        for field_index, (name, value) in enumerate(item.fields.items()):
            field_spec = spec.extract.fields.get(name)
            if field_spec is None:
                raise PlanningFailed
            label = _FIELD_LABELS.get(name, f"현재 {_plain(name, limit=64)}")
            optional_lines.append(
                (
                    index == 1 and field_index < 3,
                    f"{label}: {_format_value(value, field_spec.type)}",
                )
            )
    optional_lines.extend((index == 0, line) for index, line in enumerate(_rule_lines(spec)))
    tails = [
        f"시간대: {_plain(spec.timezone, limit=64)}",
        f"확인 주기: {_schedule_text(spec.schedule)}",
        f"수집 방식: {_STRATEGIES[proposal.resolved_strategy]}",
        (
            "robots.txt: 허용 (가져옴)"
            if proposal.robots.policy_fetched
            else "robots.txt: 허용 (가져오기 실패)"
        ),
        (
            "로그인 프로필: 필요"
            if spec.auth_profile_ref is not None
            else "로그인 프로필: 필요 없음"
        ),
    ]
    warning_lines = [
        (index == 0, _warning_text(code)) for index, code in enumerate(proposal.warnings)
    ]
    selected = [required for required, _ in optional_lines]
    selected_warnings = [required for required, _ in warning_lines]

    def compose() -> list[str]:
        return [
            *headers,
            *(line for include, (_, line) in zip(selected, optional_lines, strict=True) if include),
            *tails,
            *(
                line
                for include, (_, line) in zip(
                    selected_warnings,
                    warning_lines,
                    strict=True,
                )
                if include
            ),
        ]

    for index, (required, _) in enumerate(optional_lines):
        if required:
            continue
        selected[index] = True
        if len("\n".join(compose())) > _MAX_PREVIEW_CHARS:
            selected[index] = False
    for index, (required, _) in enumerate(warning_lines):
        if required:
            continue
        selected_warnings[index] = True
        if len("\n".join(compose())) > _MAX_PREVIEW_CHARS:
            selected_warnings[index] = False

    lines = compose()
    text = "\n".join(lines)
    if len(text) > _MAX_PREVIEW_CHARS or not _safe_output(text):
        raise PlanningFailed
    buttons = (
        (
            InlineButton("등록", callbacks[0]),
            InlineButton("수정", callbacks[1]),
            InlineButton("취소", callbacks[2]),
        ),
    )
    return PreviewMessage(text=text, buttons=buttons)


def _fresh_proposal_spec(value: object) -> MonitorSpec | None:
    if type(value) is not ProposedMonitor or not _valid_proposal(value):
        return None
    try:
        fresh = MonitorSpec.model_validate(value.spec.model_dump(mode="json"))
        if fresh != value.spec:
            return None
        if (
            value.robots.allowed is not True
            or type(value.robots.policy_fetched) is not bool
            or len(value.preview_items) > 3
        ):
            return None
        return fresh
    except Exception:
        return None


def _rule_lines(spec: MonitorSpec) -> list[str]:
    lines: list[str] = []
    for rule in spec.rules:
        field = _FIELD_LABELS.get(rule.field or "", rule.field or "새 항목")
        field = field.removeprefix("현재 ")
        if rule.kind is RuleKind.NUMERIC_THRESHOLD:
            operator = {
                "lt": "미만",
                "lte": "이하",
                "eq": "같음",
                "gte": "이상",
                "gt": "초과",
            }[rule.operator]
            field_type = spec.extract.fields[rule.field].type  # type: ignore[index]
            lines.append(f"조건: {field} {_format_value(rule.value, field_type)} {operator}")
        elif rule.kind is RuleKind.NEW_ITEM:
            lines.append("조건: 새 항목 발견")
        elif rule.kind is RuleKind.FIELD_CHANGED:
            lines.append(f"조건: {field} 변경")
        elif rule.kind is RuleKind.STATUS_EQUALS:
            field_type = spec.extract.fields[rule.field].type  # type: ignore[index]
            lines.append(f"조건: {field} {_format_value(rule.value, field_type)}")
        elif rule.kind is RuleKind.KEYWORD_MATCH:
            lines.append(f"조건: {field} 지정 키워드 포함")
    return lines


def _format_value(value: Scalar, field_type: FieldType) -> str:
    if field_type is FieldType.URL and isinstance(value, str):
        return _redact_url(value)
    if field_type is FieldType.KRW and isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}원"
    if field_type is FieldType.DECIMAL and isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            raise PlanningFailed
        return f"{value:,}"
    if field_type is FieldType.INTEGER and isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    if field_type is FieldType.BOOLEAN and type(value) is bool:
        return "예" if value else "아니요"
    if value is None:
        return "없음"
    return _plain(str(value), limit=120)


def _schedule_text(value: str) -> str:
    matched = _HOURLY_CRON.fullmatch(value)
    if matched is not None:
        return f"{int(matched.group(1))}시간마다"
    daily = re.fullmatch(r"([0-5]?[0-9]) ([01]?[0-9]|2[0-3]) \* \* \*", value)
    if daily is not None:
        return f"매일 {int(daily.group(2)):02d}:{int(daily.group(1)):02d}"
    return "설정된 일정"


def _warning_text(code: str) -> str:
    messages = {
        "robots_policy_unavailable": "주의: robots.txt를 가져오지 못했습니다",
        "dynamic_content": "주의: 동적 페이지 수집이 필요합니다",
        "login_required": "주의: 로그인 프로필이 필요합니다",
    }
    return messages.get(code, "주의: 추가 확인이 필요한 항목이 있습니다")


def _redact_url(value: str) -> str:
    result: str | None = None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError
        result = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
    except Exception:
        pass
    if result is None:
        raise PlanningFailed
    return result


def _plain(value: str, *, limit: int) -> str:
    if _CREDENTIAL_TEXT.search(value):
        return "[숨김]"
    normalized = " ".join(value.split())
    normalized = "".join(
        character for character in normalized if not unicodedata.category(character).startswith("C")
    )
    if len(normalized) > limit:
        normalized = normalized[: max(0, limit - 1)].rstrip() + "…"
    return normalized


def _safe_output(value: str) -> bool:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return not any(
        character != "\n" and unicodedata.category(character).startswith("C") for character in value
    )
