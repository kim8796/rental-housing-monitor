from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit

from personal_monitor.adapters._policy import (
    MONITOR_USER_AGENT,
    BoundedPolicyHttpClient,
    PolicyHttpResponse,
)
from personal_monitor.domain.observation import ObservationBatch
from personal_monitor.domain.spec import FetchStrategy, MonitorSpec, SourceAdapterKind
from personal_monitor.engine.errors import ErrorClass, FetchError, MonitorError
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.extractor import DeclarativeExtractor
from personal_monitor.scraping.validator import ObservationValidator
from personal_monitor.security.rate_limit import HostRateLimiter
from personal_monitor.security.robots import RobotsPolicy
from personal_monitor.security.url_policy import MAX_REDIRECTS, ResolvedTarget, UrlPolicy

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRY_DELAYS = (1.0, 4.0)
Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


class OfficialJsonAdapter:
    """Fetch a fixed GET JSON endpoint through the complete outbound policy."""

    def __init__(
        self,
        *,
        url_policy: UrlPolicy,
        rate_limiter: HostRateLimiter,
        http_client: BoundedPolicyHttpClient,
        extractor: DeclarativeExtractor | None = None,
        validator: ObservationValidator | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if not isinstance(http_client, BoundedPolicyHttpClient):
            raise TypeError("http_client must preserve the bounded proxy policy")
        self._url_policy = url_policy
        self._rate_limiter = rate_limiter
        self._http_client = http_client
        self._extractor = extractor or DeclarativeExtractor()
        self._validator = validator or ObservationValidator()
        self._clock = clock
        self._sleeper = sleeper

    async def fetch(self, monitor_id: str, spec: MonitorSpec) -> ObservationBatch:
        if (
            spec.source_adapter is not SourceAdapterKind.OFFICIAL_API
            or spec.adapter_ref != "json_get"
        ):
            raise MonitorError(ErrorClass.POLICY, "adapter", "monitor adapter is incompatible")

        retry_after: float | None = None
        for attempt in range(3):
            try:
                return await self._fetch_once(monitor_id, spec, retry_after=retry_after)
            except asyncio.CancelledError:
                raise
            except MonitorError as error:
                if error.error_class is not ErrorClass.TRANSIENT_NETWORK or attempt == 2:
                    raise
                retry_after = _safe_retry_after(error)
                await self._sleeper(_RETRY_DELAYS[attempt])
        raise AssertionError("unreachable")

    async def _fetch_once(
        self,
        monitor_id: str,
        spec: MonitorSpec,
        *,
        retry_after: float | None,
    ) -> ObservationBatch:
        target, crawl_delay = await self._prepare_target(
            spec.target_url,
            redirect_count=None,
            retry_after=retry_after,
        )
        seen = {target.normalized_url}
        redirect_count = 0
        while True:
            await self._rate_limiter.acquire(target.hostname, crawl_delay)
            response = await self._http_client.get_json(target)
            await self._validate_response_destination(response, requested=target)
            if response.status in _REDIRECT_STATUSES:
                redirect_count += 1
                if redirect_count > MAX_REDIRECTS or response.redirect_location is None:
                    raise MonitorError(
                        ErrorClass.POLICY,
                        "redirect",
                        "redirect limit or location is invalid",
                    )
                next_target = await self._url_policy.validate_redirect(
                    response.redirect_location,
                    redirect_count=redirect_count,
                )
                if next_target.normalized_url in seen:
                    raise MonitorError(ErrorClass.POLICY, "redirect", "redirect loop detected")
                seen.add(next_target.normalized_url)
                crawl_delay = await self._prepare_resolved(next_target, retry_after=None)
                target = next_target
                continue

            document = SourceDocument(
                final_url=response.final_url,
                status=response.status,
                content_type=_content_type(response),
                headers=response.headers,
                body=response.body,
                strategy=FetchStrategy.HTTP,
            )
            items = self._extractor.extract(document, spec.extract)
            validated = self._validator.validate(items, spec.extract, spec.validators)
            observed_at = self._clock()
            _require_aware(observed_at)
            return ObservationBatch(
                monitor_id=monitor_id,
                items=validated,
                observed_at=observed_at,
                source_hash=sha256(document.body).hexdigest(),
            )

    async def _prepare_target(
        self,
        url: str,
        *,
        redirect_count: int | None,
        retry_after: float | None,
    ) -> tuple[ResolvedTarget, float | None]:
        if redirect_count is None:
            target = await self._url_policy.validate(url)
        else:
            target = await self._url_policy.validate_redirect(url, redirect_count=redirect_count)
        crawl_delay = await self._prepare_resolved(target, retry_after=retry_after)
        return target, crawl_delay

    async def _prepare_resolved(
        self,
        target: ResolvedTarget,
        *,
        retry_after: float | None,
    ) -> float | None:
        await self._rate_limiter.acquire(target.hostname, retry_after)
        decision = await self._robots_decision(target)
        decision.require_allowed()
        return decision.crawl_delay_seconds

    async def _robots_decision(self, target: ResolvedTarget):
        robots_url = _robots_url(target.normalized_url)
        robots_target = await self._url_policy.validate(robots_url)
        initial_origin = _origin(robots_target.normalized_url)
        seen = {robots_target.normalized_url}
        redirect_count = 0
        while True:
            try:
                response = await self._http_client.get_robots(robots_target)
            except FetchError as error:
                if error.error_class is not ErrorClass.TRANSIENT_NETWORK:
                    raise
                policy = RobotsPolicy.from_fetch_failure(robots_url, checked_at=self._clock())
                return policy.check(MONITOR_USER_AGENT, target.normalized_url)

            await self._validate_response_destination(response, requested=robots_target)
            if response.status in _REDIRECT_STATUSES:
                redirect_count += 1
                if redirect_count > MAX_REDIRECTS or response.redirect_location is None:
                    raise MonitorError(
                        ErrorClass.POLICY,
                        "robots",
                        "robots redirect is invalid",
                    )
                next_target = await self._url_policy.validate_redirect(
                    response.redirect_location,
                    redirect_count=redirect_count,
                )
                if _origin(next_target.normalized_url) != initial_origin:
                    raise MonitorError(
                        ErrorClass.POLICY,
                        "robots",
                        "robots redirect changed origin",
                    )
                if next_target.normalized_url in seen:
                    raise MonitorError(ErrorClass.POLICY, "robots", "robots redirect loop detected")
                seen.add(next_target.normalized_url)
                robots_target = next_target
                await self._rate_limiter.acquire(robots_target.hostname)
                continue

            if response.status != 200:
                policy = RobotsPolicy.from_fetch_failure(robots_url, checked_at=self._clock())
            else:
                policy = RobotsPolicy.from_text(
                    response.body.decode("utf-8", errors="replace"),
                    robots_url,
                    checked_at=self._clock(),
                )
            return policy.check(MONITOR_USER_AGENT, target.normalized_url)

    async def _validate_response_destination(
        self,
        response: PolicyHttpResponse,
        *,
        requested: ResolvedTarget,
    ) -> None:
        final_target = await self._url_policy.validate(response.final_url)
        if final_target.normalized_url != requested.normalized_url:
            raise MonitorError(ErrorClass.POLICY, "redirect", "unapproved final URL")
        if response.peer_ip is not None:
            self._url_policy.validate_peer(final_target, response.peer_ip)


def _content_type(response: PolicyHttpResponse) -> str:
    return response.headers["content-type"].split(";", 1)[0].strip().casefold()


def _robots_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme.casefold() == "https" else 80)
    return parts.scheme.casefold(), (parts.hostname or "").rstrip(".").casefold(), port


def _safe_retry_after(error: MonitorError) -> float | None:
    value = getattr(error, "retry_after_seconds", None)
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _require_aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MonitorError(ErrorClass.INTERNAL, "clock", "clock returned an invalid timestamp")


__all__ = ["MONITOR_USER_AGENT", "BoundedPolicyHttpClient", "OfficialJsonAdapter"]
