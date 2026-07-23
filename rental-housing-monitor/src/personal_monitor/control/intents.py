from __future__ import annotations

import asyncio
import inspect
import math
import re
import secrets
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from personal_monitor.ai.contracts import IntentKind, IntentRequest, IntentResult
from personal_monitor.ai.worker import CodexWorkerError
from personal_monitor.security.url_policy import canonicalize_hostname
from personal_monitor.telegram.gateway import ControlRequest

_OWNER_RE: Final = re.compile(r"telegram-user:[1-9][0-9]{0,18}\Z")
_MONITOR_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_STATUS_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_INVALID_PERCENT_ESCAPE_RE: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")
_TARGET_COMMAND_RE: Final = re.compile(
    r"/(status|pause|resume|delete) ([A-Za-z0-9][A-Za-z0-9._:-]{0,127})\Z"
)
_COMMAND_KINDS: Final = {
    "status": IntentKind.STATUS,
    "pause": IntentKind.PAUSE,
    "resume": IntentKind.RESUME,
    "delete": IntentKind.DELETE,
}
_ATTEMPTS: Final = (
    ("gpt-5.6-terra", "medium"),
    ("gpt-5.6-terra", "medium"),
    ("gpt-5.6-sol", "high"),
)
_GENERIC_CLARIFICATION: Final = "요청을 더 구체적으로 알려주세요"
_COMMAND_CLARIFICATION: Final = "명령과 모니터 ID를 정확히 알려주세요"
_CANCEL_CLARIFICATION: Final = "요청을 취소했습니다"
_FAILED_CLARIFICATION: Final = "요청을 이해하지 못했습니다"
_MAX_MONITORS: Final = 100
_MAX_NAME: Final = 300
_MAX_CLARIFICATION: Final = 500


class IntentRouterError(RuntimeError):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("intent routing failed")

    def __repr__(self) -> str:
        return "IntentRouterError(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class OwnedMonitorSummary:
    owner_id: str
    id: str
    name: str
    status: str

    def __post_init__(self) -> None:
        if (
            type(self.owner_id) is not str
            or _OWNER_RE.fullmatch(self.owner_id) is None
            or type(self.id) is not str
            or _MONITOR_ID_RE.fullmatch(self.id) is None
            or not _safe_text(self.name, _MAX_NAME)
            or type(self.status) is not str
            or _STATUS_RE.fullmatch(self.status) is None
        ):
            raise ValueError("invalid monitor summary") from None

    def __repr__(self) -> str:
        return "<OwnedMonitorSummary redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class _Dependency:
    owner: object | None
    name: str | None
    call: object


class _Validation(StrEnum):
    ACCEPT = "accept"
    TERMINAL = "terminal"
    INVALID = "invalid"


class IntentRouter:
    __slots__ = (
        "_provider",
        "_provider_anchor",
        "_worker",
        "_worker_anchor",
    )

    def __init__(self, monitor_provider: object, worker: object) -> None:
        provider_anchor: _Dependency | None = None
        worker_anchor: _Dependency | None = None
        with suppress(Exception):
            provider_anchor = _capture_dependency(monitor_provider, "list_monitors")
            worker_anchor = _capture_dependency(worker, "run")
        if provider_anchor is None or worker_anchor is None:
            raise IntentRouterError
        object.__setattr__(self, "_provider", monitor_provider)
        object.__setattr__(self, "_provider_anchor", provider_anchor)
        object.__setattr__(self, "_worker", worker)
        object.__setattr__(self, "_worker_anchor", worker_anchor)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("IntentRouter composition is sealed")

    def __repr__(self) -> str:
        return "<IntentRouter redacted>"

    async def route(self, request: ControlRequest) -> IntentResult:
        if type(request) is not ControlRequest:
            raise IntentRouterError
        message = request.text
        if not _safe_message(message):
            return _clarification(_COMMAND_CLARIFICATION if _looks_like_command(message) else None)
        normalized = message.strip()

        if normalized == "/monitors":
            return _action(IntentKind.LIST)
        if normalized == "/cancel":
            return _clarification(_CANCEL_CLARIFICATION, confidence=1)
        matched = _TARGET_COMMAND_RE.fullmatch(normalized)
        if matched is not None:
            summaries = self._summaries(request.owner_id)
            target = matched.group(2)
            if target not in {summary.id for summary in summaries}:
                return _clarification(_COMMAND_CLARIFICATION)
            return _action(_COMMAND_KINDS[matched.group(1)], targets=[target])
        if _looks_like_command(normalized):
            return _clarification(_COMMAND_CLARIFICATION)

        summaries = self._summaries(request.owner_id)
        intent_request: IntentRequest | None = None
        with suppress(Exception):
            intent_request = IntentRequest(
                request_id=secrets.token_urlsafe(18),
                owner_id=request.owner_id,
                message=message,
                monitor_summaries=[
                    f"id={summary.id} | name={summary.name} | status={summary.status}"
                    for summary in summaries
                ],
            )
        if intent_request is None:
            raise IntentRouterError

        owned_ids = frozenset(summary.id for summary in summaries)
        for model, effort in _ATTEMPTS:
            if not _dependency_intact(self._worker, self._worker_anchor):
                raise IntentRouterError
            boundary_failed = False
            try:
                result = await self._worker_anchor.call(
                    intent_request,
                    model=model,
                    effort=effort,
                )
            except asyncio.CancelledError:
                raise
            except CodexWorkerError as error:
                if type(error) is CodexWorkerError:
                    continue
                boundary_failed = True
            except Exception:
                boundary_failed = True
            if boundary_failed:
                raise IntentRouterError

            validation = _validate_result(result, owned_ids)
            if validation is _Validation.ACCEPT:
                return result
            if validation is _Validation.TERMINAL:
                return _terminal_clarification(result)

        return _clarification(_FAILED_CLARIFICATION, confidence=0)

    def _summaries(self, owner_id: str) -> tuple[OwnedMonitorSummary, ...]:
        if not _dependency_intact(self._provider, self._provider_anchor):
            raise IntentRouterError
        summaries: tuple[OwnedMonitorSummary, ...] | None = None
        with suppress(Exception):
            values = self._provider_anchor.call(owner_id)
            if type(values) in {list, tuple} and len(values) <= _MAX_MONITORS:
                candidate = tuple(values)
                if all(_valid_summary(value, owner_id) for value in candidate):
                    ids = [value.id for value in candidate]
                    if len(set(ids)) == len(ids):
                        summaries = tuple(
                            sorted(
                                candidate, key=lambda value: (value.id, value.name, value.status)
                            )
                        )
        if summaries is None:
            raise IntentRouterError
        return summaries


def _capture_dependency(value: object, method_name: str) -> _Dependency:
    if inspect.ismethod(value) and value.__self__ is not None:
        owner = value.__self__
        name = value.__name__
        return _Dependency(owner, name, value)
    if inspect.isfunction(value) or inspect.isbuiltin(value):
        return _Dependency(None, None, value)
    if callable(value):
        call = value.__call__  # noqa: B004
        if not callable(call):
            raise IntentRouterError
        return _Dependency(value, "__call__", call)
    call = getattr(value, method_name)
    if not callable(call):
        raise IntentRouterError
    return _Dependency(value, method_name, call)


def _dependency_intact(value: object, anchor: _Dependency) -> bool:
    try:
        if anchor.owner is None:
            return anchor.call is value and callable(anchor.call)
        if anchor.owner is not value and not (
            inspect.ismethod(value) and value.__self__ is anchor.owner
        ):
            return False
        current = getattr(anchor.owner, anchor.name)
        return _same_callable(anchor.call, current, anchor.owner)
    except Exception:
        return False


def _same_callable(captured: object, current: object, owner: object) -> bool:
    if not callable(captured) or not callable(current):
        return False
    if current is captured:
        return True
    return (
        getattr(captured, "__self__", None) is owner
        and getattr(current, "__self__", None) is owner
        and getattr(captured, "__func__", None) is getattr(current, "__func__", None)
    )


def _safe_message(value: object) -> bool:
    return _safe_text(value, 2_000) and not any(
        unicodedata.category(character).startswith("C") for character in value
    )


def _safe_text(value: object, limit: int) -> bool:
    if type(value) is not str or not 1 <= len(value) <= limit or not value.strip():
        return False
    try:
        if len(value.encode("utf-8", errors="strict")) > limit * 4:
            return False
    except UnicodeEncodeError:
        return False
    return not any(unicodedata.category(character).startswith("C") for character in value)


def _valid_summary(value: object, owner_id: str) -> bool:
    return (
        type(value) is OwnedMonitorSummary
        and type(value.owner_id) is str
        and _OWNER_RE.fullmatch(value.owner_id) is not None
        and value.owner_id == owner_id
        and type(value.id) is str
        and _MONITOR_ID_RE.fullmatch(value.id) is not None
        and _safe_text(value.name, _MAX_NAME)
        and type(value.status) is str
        and _STATUS_RE.fullmatch(value.status) is not None
    )


def _looks_like_command(value: object) -> bool:
    if type(value) is not str:
        return False
    candidate = value.lstrip()
    if not candidate:
        return False
    prefix = candidate[0]
    normalized = unicodedata.normalize("NFKC", prefix)
    if normalized.startswith("/"):
        return True
    name = unicodedata.name(prefix, "")
    return any(token in name for token in ("SLASH", "SOLIDUS", "DIAGONAL"))


def _action(
    kind: IntentKind,
    *,
    targets: list[str] | None = None,
) -> IntentResult:
    return IntentResult(
        kind=kind,
        target_monitor_ids=[] if targets is None else targets,
        target_url=None,
        condition_text=None,
        schedule_text=None,
        clarification=None,
        confidence=1,
    )


def _clarification(value: str | None, *, confidence: float = 0) -> IntentResult:
    return IntentResult(
        kind=IntentKind.UNKNOWN,
        target_monitor_ids=[],
        target_url=None,
        condition_text=None,
        schedule_text=None,
        clarification=value if _safe_text(value, _MAX_CLARIFICATION) else _GENERIC_CLARIFICATION,
        confidence=confidence,
    )


def _terminal_clarification(result: IntentResult) -> IntentResult:
    clarification = result.clarification
    if not _safe_text(clarification, _MAX_CLARIFICATION):
        clarification = _GENERIC_CLARIFICATION
    return _clarification(clarification, confidence=result.confidence)


def _validate_result(value: object, owned_ids: frozenset[str]) -> _Validation:
    if type(value) is not IntentResult:
        return _Validation.INVALID
    if (
        type(value.kind) is not IntentKind
        or type(value.target_monitor_ids) is not list
        or type(value.confidence) is not float
        or not math.isfinite(value.confidence)
    ):
        return _Validation.INVALID
    if any(
        type(target) is not str or _MONITOR_ID_RE.fullmatch(target) is None
        for target in value.target_monitor_ids
    ):
        return _Validation.INVALID
    if len(set(value.target_monitor_ids)) != len(value.target_monitor_ids):
        return _Validation.INVALID
    if any(
        item is not None and not _safe_text(item, limit)
        for item, limit in (
            (value.target_url, 2_048),
            (value.condition_text, 2_000),
            (value.schedule_text, 500),
        )
    ):
        return _Validation.INVALID

    if value.kind is IntentKind.UNKNOWN:
        if (
            value.target_monitor_ids
            or value.target_url is not None
            or value.condition_text is not None
            or value.schedule_text is not None
        ):
            return _Validation.INVALID
        return _Validation.TERMINAL

    targets = value.target_monitor_ids
    if any(target not in owned_ids for target in targets):
        return _Validation.INVALID
    if value.kind in {IntentKind.CREATE, IntentKind.LIST} and targets:
        return _Validation.INVALID
    if (
        value.kind
        in {
            IntentKind.UPDATE,
            IntentKind.PAUSE,
            IntentKind.RESUME,
            IntentKind.DELETE,
            IntentKind.STATUS,
        }
        and len(targets) != 1
    ):
        return _Validation.INVALID

    semantically_valid = True
    if value.kind is IntentKind.CREATE:
        semantically_valid = _safe_http_url(value.target_url)
    elif value.target_url is not None:
        semantically_valid = False
    elif value.kind is IntentKind.UPDATE:
        semantically_valid = value.condition_text is not None or value.schedule_text is not None
    elif value.kind is IntentKind.LIST or value.kind in {
        IntentKind.PAUSE,
        IntentKind.RESUME,
        IntentKind.DELETE,
        IntentKind.STATUS,
    }:
        semantically_valid = value.condition_text is None and value.schedule_text is None
    if not semantically_valid:
        return _Validation.INVALID
    if value.confidence < 0.75:
        return _Validation.TERMINAL
    if value.clarification is not None:
        return _Validation.INVALID
    return _Validation.ACCEPT


def _safe_http_url(value: object) -> bool:
    if (
        not _safe_text(value, 2_048)
        or any(character.isspace() for character in value)
        or "\\" in value
        or _INVALID_PERCENT_ESCAPE_RE.search(value) is not None
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        if hostname is None:
            return False
        dotted_hostname = hostname.translate(str.maketrans({"。": ".", "．": ".", "｡": "."}))
        if ":" not in dotted_hostname:
            labels = dotted_hostname.split(".")
            if labels[-1] == "":
                labels = labels[:-1]
            if not labels or any(not label for label in labels):
                return False
        canonicalize_hostname(hostname)
        return (
            parsed.scheme.casefold() in {"http", "https"}
            and bool(parsed.netloc)
            and parsed.username is None
            and parsed.password is None
            and parsed.fragment == ""
            and _valid_url_port(parsed.netloc, port)
        )
    except Exception:
        return False


def _valid_url_port(netloc: str, parsed_port: int | None) -> bool:
    authority = netloc.rsplit("@", 1)[-1]
    suffix = ""
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            return False
        suffix = authority[closing + 1 :]
    elif ":" in authority:
        if authority.count(":") != 1:
            return False
        suffix = ":" + authority.rsplit(":", 1)[1]
    if not suffix:
        return parsed_port is None
    if (
        not suffix.startswith(":")
        or len(suffix) == 1
        or not suffix[1:].isascii()
        or not suffix[1:].isdigit()
    ):
        return False
    return parsed_port is not None and 1 <= parsed_port <= 65_535
