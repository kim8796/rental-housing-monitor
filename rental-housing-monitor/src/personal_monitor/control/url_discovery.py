from __future__ import annotations

import asyncio
import re
import secrets
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from personal_monitor.ai.contracts import (
    UrlDiscoveryRequest,
    UrlDiscoveryResult,
)
from personal_monitor.ai.worker import CodexWorkerError
from personal_monitor.control.planner import PlanningFailed
from personal_monitor.storage.registry import RegistryRepository

_OWNER_RE: Final = re.compile(r"telegram-user:[1-9][0-9]{0,18}\Z")
_ATTEMPTS: Final = (
    ("gpt-5.6-terra", "medium"),
    ("gpt-5.6-terra", "medium"),
    ("gpt-5.6-sol", "high"),
)
_DEFAULT_CLARIFICATION: Final = (
    "공식 사이트를 찾지 못했습니다. 사이트명이나 게시판명을 더 알려주세요."
)


class UrlDiscoveryError(RuntimeError):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("URL discovery failed")

    def __repr__(self) -> str:
        return "UrlDiscoveryError(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ValidatedUrlCandidate:
    name: str
    url: str

    def __post_init__(self) -> None:
        if not _safe_text(self.name, 120) or not _safe_text(self.url, 2_048):
            raise ValueError("invalid validated URL candidate")

    def __repr__(self) -> str:
        return "<ValidatedUrlCandidate redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class UrlDiscoveryOutcome:
    alias_name: str
    candidates: tuple[ValidatedUrlCandidate, ...]
    clarification: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if (
            not _safe_text(self.alias_name, 300)
            or len(self.candidates) > 3
            or (self.candidates and self.clarification is not None)
            or (
                not self.candidates
                and not _safe_text(self.clarification, 500)
            )
        ):
            raise ValueError("invalid URL discovery outcome")

    def __repr__(self) -> str:
        return "<UrlDiscoveryOutcome redacted>"


class UrlDiscoveryService:
    __slots__ = (
        "_get_alias",
        "_planner",
        "_registry",
        "_run",
        "_validate",
        "_worker",
    )

    def __init__(
        self,
        worker: object,
        planner: object,
        registry: RegistryRepository,
    ) -> None:
        try:
            run = worker.run
            validate = planner.validate_discovery_url
            get_alias = registry.get_url_alias
        except Exception:
            raise UrlDiscoveryError from None
        if (
            type(registry) is not RegistryRepository
            or not callable(run)
            or not callable(validate)
            or not callable(get_alias)
        ):
            raise UrlDiscoveryError
        object.__setattr__(self, "_worker", worker)
        object.__setattr__(self, "_planner", planner)
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_run", run)
        object.__setattr__(self, "_validate", validate)
        object.__setattr__(self, "_get_alias", get_alias)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("UrlDiscoveryService composition is sealed")

    def __repr__(self) -> str:
        return "<UrlDiscoveryService redacted>"

    async def discover(self, owner_id: str, query: str) -> UrlDiscoveryOutcome:
        if (
            type(owner_id) is not str
            or _OWNER_RE.fullmatch(owner_id) is None
            or not _safe_text(query, 300)
            or not self._integrity_ok()
        ):
            raise UrlDiscoveryError

        alias: object | None = None
        with suppress(Exception):
            alias = self._get_alias(owner_id, query)
        if type(alias) is str:
            validated = await self._validate_one(owner_id, alias)
            if validated is not None:
                return UrlDiscoveryOutcome(
                    alias_name=query,
                    candidates=(ValidatedUrlCandidate(query, validated),),
                    clarification=None,
                )

        result = await self._search(query)
        if result is None:
            return UrlDiscoveryOutcome(query, (), _DEFAULT_CLARIFICATION)
        if not result.candidates:
            clarification = (
                result.clarification
                if _safe_text(result.clarification, 500)
                else _DEFAULT_CLARIFICATION
            )
            return UrlDiscoveryOutcome(query, (), clarification)

        candidates: list[ValidatedUrlCandidate] = []
        seen: set[str] = set()
        for candidate in result.candidates:
            if not _safe_text(candidate.name, 120) or not _safe_text(candidate.url, 2_048):
                continue
            validated = await self._validate_one(owner_id, candidate.url)
            if validated is None or validated in seen:
                continue
            seen.add(validated)
            candidates.append(ValidatedUrlCandidate(candidate.name, validated))
        return UrlDiscoveryOutcome(
            query,
            tuple(candidates),
            None if candidates else _DEFAULT_CLARIFICATION,
        )

    async def validate_selected_url(self, owner_id: str, url: str) -> str:
        if (
            type(owner_id) is not str
            or _OWNER_RE.fullmatch(owner_id) is None
            or not _safe_text(url, 2_048)
            or not self._integrity_ok()
        ):
            raise UrlDiscoveryError
        validated = await self._validate_one(owner_id, url)
        if validated is None:
            raise UrlDiscoveryError
        return validated

    async def _search(self, query: str) -> UrlDiscoveryResult | None:
        request = UrlDiscoveryRequest(request_id=secrets.token_urlsafe(18), query=query)
        for model, effort in _ATTEMPTS:
            try:
                value = await self._run(request, model=model, effort=effort)
            except asyncio.CancelledError:
                raise
            except CodexWorkerError as error:
                if type(error) is CodexWorkerError:
                    continue
                raise UrlDiscoveryError from None
            except Exception:
                raise UrlDiscoveryError from None
            fresh: UrlDiscoveryResult | None = None
            with suppress(Exception):
                if type(value) is UrlDiscoveryResult:
                    fresh = UrlDiscoveryResult.model_validate(value.model_dump(mode="python"))
            if fresh is None:
                continue
            if bool(fresh.candidates) == bool(fresh.clarification):
                continue
            return fresh
        return None

    async def _validate_one(self, owner_id: str, url: str) -> str | None:
        try:
            value = await self._validate(owner_id, url)
        except asyncio.CancelledError:
            raise
        except PlanningFailed as error:
            if type(error) is PlanningFailed:
                return None
            raise UrlDiscoveryError from None
        except Exception:
            raise UrlDiscoveryError from None
        return value if _safe_text(value, 2_048) else None

    def _integrity_ok(self) -> bool:
        try:
            return (
                self._run == self._worker.run
                and self._validate == self._planner.validate_discovery_url
                and self._get_alias == self._registry.get_url_alias
            )
        except Exception:
            return False


def _safe_text(value: object, limit: int) -> bool:
    if type(value) is not str or not 1 <= len(value) <= limit:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return not any(unicodedata.category(character).startswith("C") for character in value)
