from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from weakref import WeakKeyDictionary

from personal_monitor.adapters._policy import (
    MONITOR_USER_AGENT,
    BoundedPolicyHttpClient,
    PolicyHttpResponse,
)
from personal_monitor.adapters.official_api import (
    _max_delay,
    _origin,
    _require_aware,
    _robots_url,
    _safe_retry_after,
)
from personal_monitor.domain.observation import ObservationBatch
from personal_monitor.domain.spec import FetchStrategy, MonitorSpec, SourceAdapterKind
from personal_monitor.engine.errors import ErrorClass, FailureCode, FetchError, MonitorError
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.extractor import DeclarativeExtractor
from personal_monitor.scraping.scrapling_backend import ScraplingBackend
from personal_monitor.scraping.validator import ObservationValidator
from personal_monitor.security.egress import EgressProxyPolicy
from personal_monitor.security.rate_limit import HostRateLimiter
from personal_monitor.security.robots import RobotsPolicy
from personal_monitor.security.url_policy import MAX_REDIRECTS, ResolvedTarget, UrlPolicy

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRY_DELAYS = (1.0, 4.0)
Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]
ShellDetector = Callable[[SourceDocument], bool]


def _provenance_accessors():
    registry: WeakKeyDictionary[object, EgressProxyPolicy] = WeakKeyDictionary()

    def bind(owner: object, policy: EgressProxyPolicy) -> None:
        registry[owner] = policy

    def lookup(owner: object) -> EgressProxyPolicy | None:
        return registry.get(owner)

    return bind, lookup


_bind_egress_provenance, _lookup_egress_provenance = _provenance_accessors()


class ProfileProvider(Protocol):
    def materialize(self, reference: str) -> object: ...


class ScraplingSourceAdapter:
    """Run declarative Scrapling monitors through the outbound policy chain."""

    def __init__(
        self,
        *,
        url_policy: UrlPolicy,
        rate_limiter: HostRateLimiter,
        egress_proxy_url: str | None,
        extractor: DeclarativeExtractor | None = None,
        validator: ObservationValidator | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        sleeper: Sleeper = asyncio.sleep,
        js_shell_detector: ShellDetector | None = None,
        profile_provider: ProfileProvider | None = None,
    ) -> None:
        egress_policy = EgressProxyPolicy.from_url(egress_proxy_url)
        http_client = BoundedPolicyHttpClient._from_egress_policy(egress_policy, clock=clock)
        backend = ScraplingBackend._from_egress_policy(egress_policy, clock=clock)
        self._initialize(
            url_policy=url_policy,
            rate_limiter=rate_limiter,
            http_client=http_client,
            backend=backend,
            extractor=extractor,
            validator=validator,
            clock=clock,
            sleeper=sleeper,
            js_shell_detector=js_shell_detector,
            profile_provider=profile_provider,
            egress_policy=egress_policy,
        )

    def _initialize(
        self,
        *,
        url_policy: UrlPolicy,
        rate_limiter: HostRateLimiter,
        http_client: BoundedPolicyHttpClient,
        backend: ScraplingBackend,
        extractor: DeclarativeExtractor | None,
        validator: ObservationValidator | None,
        clock: Clock,
        sleeper: Sleeper,
        js_shell_detector: ShellDetector | None,
        profile_provider: ProfileProvider | None,
        egress_policy: EgressProxyPolicy,
    ) -> None:
        if (
            type(http_client) is not BoundedPolicyHttpClient
            or type(backend) is not ScraplingBackend
        ):
            raise TypeError("adapter egress components are invalid")
        _bind_egress_provenance(self, egress_policy)
        self._url_policy = url_policy
        self._rate_limiter = rate_limiter
        self._http_client = http_client
        self._backend = backend
        self._extractor = extractor or DeclarativeExtractor()
        self._validator = validator or ObservationValidator()
        self._clock = clock
        self._sleeper = sleeper
        self._shell_detector = js_shell_detector
        self._profile_provider = profile_provider

    async def fetch(self, monitor_id: str, spec: MonitorSpec) -> ObservationBatch:
        if spec.source_adapter is not SourceAdapterKind.SCRAPLING or spec.adapter_ref is not None:
            raise MonitorError(ErrorClass.POLICY, "adapter", "monitor adapter is incompatible")
        self._require_sealed_egress()

        if spec.fetch_strategy is not FetchStrategy.AUTO:
            return await self._run_strategy(monitor_id, spec, spec.fetch_strategy)

        try:
            return await self._run_strategy(
                monitor_id,
                spec,
                FetchStrategy.HTTP,
                classify_missing_shell=True,
            )
        except MonitorError as error:
            if error.code is not FailureCode.REQUIRED_CONTENT_ABSENT:
                raise

        try:
            return await self._run_strategy(monitor_id, spec, FetchStrategy.DYNAMIC)
        except FetchError as error:
            if not error.detected_interstitial:
                raise
        return await self._run_strategy(monitor_id, spec, FetchStrategy.STEALTHY)

    async def _run_strategy(
        self,
        monitor_id: str,
        spec: MonitorSpec,
        strategy: FetchStrategy,
        *,
        classify_missing_shell: bool = False,
    ) -> ObservationBatch:
        retry_after: float | None = None
        for attempt in range(3):
            try:
                document = await self._fetch_document_once(
                    spec,
                    strategy,
                    retry_after=retry_after,
                )
                try:
                    return self._build_batch(monitor_id, spec, document)
                except MonitorError as error:
                    if classify_missing_shell and error.code is FailureCode.REQUIRED_CONTENT_ABSENT:
                        self._classify_shell(document)
                    raise
            except asyncio.CancelledError:
                raise
            except MonitorError as error:
                if error.error_class is not ErrorClass.TRANSIENT_NETWORK or attempt == 2:
                    raise
                retry_after = _safe_retry_after(error)
                await self._sleeper(_RETRY_DELAYS[attempt])
        raise AssertionError("unreachable")

    async def _fetch_document_once(
        self,
        spec: MonitorSpec,
        strategy: FetchStrategy,
        *,
        retry_after: float | None,
    ) -> SourceDocument:
        target, crawl_delay = await self._prepare_target(
            spec.target_url,
            redirect_count=None,
            retry_after=retry_after,
        )
        seen = {target.normalized_url}
        redirect_count = 0
        while True:
            await self._rate_limiter.acquire(target.hostname, crawl_delay)
            document = await self._backend_fetch(strategy, target, spec.auth_profile_ref)
            await self._validate_document(document, requested=target)
            if strategy is FetchStrategy.HTTP and document.status in _REDIRECT_STATUSES:
                redirect_count += 1
                if redirect_count > MAX_REDIRECTS or document.redirect_location is None:
                    raise MonitorError(
                        ErrorClass.POLICY,
                        "redirect",
                        "redirect limit or location is invalid",
                    )
                next_target = await self._url_policy.validate_redirect(
                    document.redirect_location,
                    redirect_count=redirect_count,
                )
                if next_target.normalized_url in seen:
                    raise MonitorError(ErrorClass.POLICY, "redirect", "redirect loop detected")
                seen.add(next_target.normalized_url)
                crawl_delay = await self._prepare_resolved(next_target, retry_after=None)
                target = next_target
                continue
            if document.status in _REDIRECT_STATUSES:
                raise MonitorError(ErrorClass.POLICY, "redirect", "browser redirect is invalid")
            return document

    async def _backend_fetch(
        self,
        strategy: FetchStrategy,
        target: ResolvedTarget,
        profile_reference: str | None,
    ) -> SourceDocument:
        self._require_sealed_egress()
        if strategy is FetchStrategy.HTTP:
            return await self._backend.fetch_http(target)
        method = (
            self._backend.fetch_dynamic
            if strategy is FetchStrategy.DYNAMIC
            else self._backend.fetch_stealthy
        )
        if profile_reference is None:
            return await method(target, profile=None)
        return await self._fetch_with_profile(method, target, profile_reference)

    async def _fetch_with_profile(
        self,
        method: Callable[..., Awaitable[SourceDocument]],
        target: ResolvedTarget,
        profile_reference: str,
    ) -> SourceDocument:
        if self._profile_provider is None:
            raise _profile_error()
        try:
            context = self._profile_provider.materialize(profile_reference)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _profile_error() from None

        async_enter = getattr(context, "__aenter__", None)
        async_exit = getattr(context, "__aexit__", None)
        if callable(async_enter) and callable(async_exit):
            try:
                profile = await async_enter()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise _profile_error() from None
            try:
                result = await method(target, profile=_profile_path(profile))
            except BaseException as original:
                exception = sys.exc_info()
                try:
                    await async_exit(*exception)
                except BaseException:
                    if not isinstance(original, Exception):
                        raise original from None
                    raise _profile_error() from None
                raise
            try:
                await async_exit(None, None, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise _profile_error() from None
            return result

        enter = getattr(context, "__enter__", None)
        exit_context = getattr(context, "__exit__", None)
        if not callable(enter) or not callable(exit_context):
            raise _profile_error()
        try:
            profile = enter()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _profile_error() from None
        try:
            result = await method(target, profile=_profile_path(profile))
        except BaseException as original:
            exception = sys.exc_info()
            try:
                exit_context(*exception)
            except BaseException:
                if not isinstance(original, Exception):
                    raise original from None
                raise _profile_error() from None
            raise
        try:
            exit_context(None, None, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _profile_error() from None
        return result

    def _build_batch(
        self,
        monitor_id: str,
        spec: MonitorSpec,
        document: SourceDocument,
    ) -> ObservationBatch:
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

    def _classify_shell(self, document: SourceDocument) -> None:
        if self._shell_detector is None:
            return
        try:
            result = self._shell_detector(document)
        except Exception:
            raise MonitorError(ErrorClass.INTERNAL, "detect", "shell detector failed") from None
        if not isinstance(result, bool):
            raise MonitorError(
                ErrorClass.INTERNAL,
                "detect",
                "shell detector returned invalid result",
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
        decision, robots_retry_after = await self._robots_decision(target)
        decision.require_allowed()
        return _max_delay(decision.crawl_delay_seconds, robots_retry_after)

    async def _robots_decision(self, target: ResolvedTarget):
        robots_url = _robots_url(target.normalized_url)
        robots_target = await self._url_policy.validate(robots_url)
        initial_origin = _origin(robots_target.normalized_url)
        seen = {robots_target.normalized_url}
        redirect_count = 0
        while True:
            self._require_sealed_egress()
            try:
                response = await self._http_client.get_robots(robots_target)
            except FetchError as error:
                if error.error_class is not ErrorClass.TRANSIENT_NETWORK:
                    raise
                policy = RobotsPolicy.from_fetch_failure(robots_url, checked_at=self._clock())
                return (
                    policy.check(MONITOR_USER_AGENT, target.normalized_url),
                    _safe_retry_after(error),
                )

            await self._validate_policy_response(response, requested=robots_target)
            if response.status in _REDIRECT_STATUSES:
                redirect_count += 1
                if redirect_count > MAX_REDIRECTS or response.redirect_location is None:
                    raise MonitorError(ErrorClass.POLICY, "robots", "robots redirect is invalid")
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

            if response.status == 200:
                policy = RobotsPolicy.from_text(
                    response.body.decode("utf-8", errors="replace"),
                    robots_url,
                    checked_at=self._clock(),
                )
                retry_after = None
            elif response.status in {404, 410}:
                policy = RobotsPolicy.from_fetch_failure(robots_url, checked_at=self._clock())
                retry_after = None
            elif response.status == 429 or 500 <= response.status <= 599:
                policy = RobotsPolicy.from_fetch_failure(robots_url, checked_at=self._clock())
                retry_after = response.retry_after_seconds
            else:
                raise MonitorError(
                    ErrorClass.POLICY,
                    "robots",
                    "robots response returned an invalid status",
                )
            return policy.check(MONITOR_USER_AGENT, target.normalized_url), retry_after

    def _require_sealed_egress(self) -> None:
        egress_policy = _lookup_egress_provenance(self)
        if (
            egress_policy is None
            or not egress_policy.is_valid
            or not self._http_client._uses_egress_policy(egress_policy)
            or not self._backend._uses_egress_policy(egress_policy)
            or not self._backend.is_policy_sealed
        ):
            raise MonitorError(
                ErrorClass.POLICY,
                "adapter",
                "adapter backend policy integrity check failed",
            )

    async def _validate_policy_response(
        self,
        response: PolicyHttpResponse,
        *,
        requested: ResolvedTarget,
    ) -> None:
        final_target = await self._url_policy.validate(response.final_url)
        if final_target.normalized_url != requested.normalized_url:
            raise MonitorError(ErrorClass.POLICY, "redirect", "unapproved final URL")

    async def _validate_document(
        self,
        document: SourceDocument,
        *,
        requested: ResolvedTarget,
    ) -> None:
        if (
            document.strategy in {FetchStrategy.DYNAMIC, FetchStrategy.STEALTHY}
            and document.redirect_urls
        ):
            raise MonitorError(
                ErrorClass.POLICY,
                "redirect",
                "browser redirect history cannot be safely preflighted",
            )
        approved_chain: list[str] = []
        for redirect_count, redirect_url in enumerate(document.redirect_urls, start=1):
            history_target = await self._url_policy.validate_redirect(
                redirect_url,
                redirect_count=redirect_count,
            )
            if redirect_count == 1 and history_target.normalized_url != requested.normalized_url:
                raise MonitorError(
                    ErrorClass.POLICY,
                    "redirect",
                    "redirect history does not start at the approved request",
                )
            if history_target.normalized_url in approved_chain:
                raise MonitorError(ErrorClass.POLICY, "redirect", "redirect loop detected")
            approved_chain.append(history_target.normalized_url)
        final_target = await self._url_policy.validate(document.final_url)
        if approved_chain and final_target.normalized_url in approved_chain:
            raise MonitorError(ErrorClass.POLICY, "redirect", "redirect loop detected")
        if not approved_chain and final_target.normalized_url != requested.normalized_url:
            raise MonitorError(ErrorClass.POLICY, "redirect", "unapproved final URL")


def _profile_path(value: object) -> Path:
    if isinstance(value, Path):
        return value
    raise _profile_error()


def _profile_error() -> MonitorError:
    return MonitorError(
        ErrorClass.AUTHENTICATION,
        "profile",
        "required browser profile is unavailable",
    )
