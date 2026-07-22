from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from scrapling.core.utils import reset_logger, set_logger
from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher

from personal_monitor.domain.spec import FetchStrategy
from personal_monitor.engine.errors import ErrorClass, FetchError, MonitorError
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.profiles import attach_profile_worker
from personal_monitor.security.egress import (
    HTTP_EGRESS_GATE,
    EgressProxyIdentity,
    EgressProxyPolicy,
    _bind_egress_snapshot,
    _matches_egress_snapshot,
)
from personal_monitor.security.url_policy import (
    MAX_REDIRECTS,
    ResolvedTarget,
    has_unsafe_url_characters,
)

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_DETECTOR_BYTES = 256 * 1024
HTTP_CONCURRENCY = 4
BROWSER_CONCURRENCY = 1
HTTP_TIMEOUT_SECONDS = 30.0
BROWSER_TIMEOUT_SECONDS = 90.0
GATE_POLL_SECONDS = 0.01
NAVIGATION_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_RETAINED_HEADERS = frozenset({"content-type", "location", "retry-after"})
_MEDIA_TOKEN = r"[!#$%&'*+\-.^_`|~0-9a-z]+"
_MEDIA_TYPE = re.compile(rf"^({_MEDIA_TOKEN})/({_MEDIA_TOKEN})$")
_SELECTOR_CONFIG = {
    "adaptive": True,
    "keep_comments": False,
    "keep_cdata": False,
}
_DYNAMIC_WEBRTC_FLAGS = [
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--force-webrtc-ip-handling-policy",
]
BlockingFetcher = Callable[..., object]
BlockPageDetector = Callable[[int, str, bytes], bool]
Clock = Callable[[], datetime]

_DEFAULT_HTTP_FETCHER = Fetcher.get
_DEFAULT_DYNAMIC_FETCHER = DynamicFetcher.fetch
_DEFAULT_STEALTHY_FETCHER = StealthyFetcher.fetch

_BROWSER_GATE = threading.BoundedSemaphore(BROWSER_CONCURRENCY)

_QUIET_LOGGER = logging.getLogger("personal_monitor.scrapling.quiet")
_QUIET_LOGGER.handlers.clear()
_QUIET_LOGGER.addHandler(logging.NullHandler())
_QUIET_LOGGER.propagate = False
_QUIET_LOGGER.setLevel(logging.CRITICAL + 1)


class _BodyLimitExceeded(RuntimeError):
    pass


class _BoundedBodyCollector:
    def __init__(self, *, max_bytes: int = MAX_RESPONSE_BYTES) -> None:
        self._max_bytes = max_bytes
        self._body = bytearray()
        self.saw_chunk = False
        self.exceeded = False

    @property
    def retained_bytes(self) -> int:
        return len(self._body)

    @property
    def body(self) -> bytes:
        return bytes(self._body)

    def __call__(self, chunk: bytes) -> None:
        self.saw_chunk = True
        if len(chunk) > self._max_bytes - len(self._body):
            self.exceeded = True
            raise _BodyLimitExceeded("response size limit exceeded")
        self._body.extend(chunk)


def normalize_response(
    response: object,
    *,
    strategy: FetchStrategy,
    block_page_detector: BlockPageDetector | None = None,
    clock: Clock = lambda: datetime.now(UTC),
    body_override: bytes | None = None,
) -> SourceDocument:
    status = _read_status(response)
    final_url = _read_final_url(response)
    redirect_urls = _read_redirect_urls(response)
    headers, header_values = _copy_safe_headers(response)
    body = _read_body(response, body_override=body_override)
    content_type = _normalize_content_type(header_values.get("content-type", ()))
    detected_interstitial = _detect_interstitial(
        block_page_detector,
        status=status,
        content_type=content_type,
        body=body,
    )
    retry_after_seconds = _parse_retry_after(header_values.get("retry-after", ()), clock=clock)
    _raise_for_status(
        status,
        retry_after_seconds=retry_after_seconds,
        detected_interstitial=detected_interstitial,
    )
    if status not in NAVIGATION_REDIRECT_STATUSES and not 200 <= status <= 299:
        raise _policy_error("response returned an invalid terminal status")

    redirect_location = None
    if status in NAVIGATION_REDIRECT_STATUSES:
        redirect_location = _read_redirect_location(
            header_values.get("location", ()),
            base_url=final_url,
        )
    allow_missing_content_type = status in NAVIGATION_REDIRECT_STATUSES and not body
    _validate_content_type(content_type, allow_missing=allow_missing_content_type)

    return SourceDocument(
        final_url=final_url,
        status=status,
        content_type=content_type,
        headers=headers,
        body=body,
        strategy=strategy,
        redirect_urls=redirect_urls,
        redirect_location=redirect_location,
        peer_ip=None,
    )


@dataclass(frozen=True, slots=True, init=False, eq=False, weakref_slot=True)
class ScraplingBackend:
    """A bounded async boundary around Scrapling's synchronous fetchers.

    The default execution gates are process-wide threading semaphores, so separate
    event loops and worker threads still share the HTTP-4/browser-1 policy. Gate
    acquisition polls without occupying the loop's default executor. Once egress
    work starts, timeout or cancellation leaves the gate held until its underlying
    executor call actually finishes.
    """

    _egress_policy: EgressProxyPolicy = field(repr=False)
    _http_fetcher: BlockingFetcher = field(repr=False)
    _dynamic_fetcher: BlockingFetcher = field(repr=False)
    _stealthy_fetcher: BlockingFetcher = field(repr=False)
    _http_gate: threading.BoundedSemaphore = field(repr=False)
    _browser_gate: threading.BoundedSemaphore = field(repr=False)
    _http_timeout_seconds: float = field(repr=False)
    _browser_timeout_seconds: float = field(repr=False)
    _block_page_detector: BlockPageDetector | None = field(repr=False)
    _clock: Clock = field(repr=False)
    _background_calls: set[asyncio.Task[object]] = field(repr=False)

    def __init__(
        self,
        *,
        egress_proxy_url: str | None,
        http_timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
        browser_timeout_seconds: float = BROWSER_TIMEOUT_SECONDS,
        block_page_detector: BlockPageDetector | None = None,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._initialize(
            EgressProxyPolicy.from_url(egress_proxy_url),
            http_timeout_seconds=http_timeout_seconds,
            browser_timeout_seconds=browser_timeout_seconds,
            block_page_detector=block_page_detector,
            clock=clock,
        )

    @classmethod
    def _from_egress_policy(
        cls,
        policy: EgressProxyPolicy,
        *,
        clock: Clock,
    ) -> ScraplingBackend:
        instance = cls.__new__(cls)
        instance._initialize(
            policy,
            http_timeout_seconds=HTTP_TIMEOUT_SECONDS,
            browser_timeout_seconds=BROWSER_TIMEOUT_SECONDS,
            block_page_detector=None,
            clock=clock,
        )
        return instance

    def _initialize(
        self,
        policy: EgressProxyPolicy,
        *,
        http_timeout_seconds: float,
        browser_timeout_seconds: float,
        block_page_detector: BlockPageDetector | None,
        clock: Clock,
    ) -> None:
        object.__setattr__(self, "_egress_policy", policy)
        object.__setattr__(self, "_http_fetcher", _DEFAULT_HTTP_FETCHER)
        object.__setattr__(self, "_dynamic_fetcher", _DEFAULT_DYNAMIC_FETCHER)
        object.__setattr__(self, "_stealthy_fetcher", _DEFAULT_STEALTHY_FETCHER)
        object.__setattr__(self, "_http_gate", HTTP_EGRESS_GATE)
        object.__setattr__(self, "_browser_gate", _BROWSER_GATE)
        object.__setattr__(
            self,
            "_http_timeout_seconds",
            _bounded_timeout(
                http_timeout_seconds,
                maximum=HTTP_TIMEOUT_SECONDS,
                label="HTTP",
            ),
        )
        object.__setattr__(
            self,
            "_browser_timeout_seconds",
            _bounded_timeout(
                browser_timeout_seconds,
                maximum=BROWSER_TIMEOUT_SECONDS,
                label="browser",
            ),
        )
        object.__setattr__(self, "_block_page_detector", block_page_detector)
        object.__setattr__(self, "_clock", clock)
        object.__setattr__(self, "_background_calls", set())
        _bind_egress_snapshot(self, policy)

    @property
    def is_policy_sealed(self) -> bool:
        """Whether production fetchers and process-wide concurrency gates are intact."""
        try:
            _bounded_timeout(
                self._http_timeout_seconds,
                maximum=HTTP_TIMEOUT_SECONDS,
                label="HTTP",
            )
            _bounded_timeout(
                self._browser_timeout_seconds,
                maximum=BROWSER_TIMEOUT_SECONDS,
                label="browser",
            )
        except ValueError:
            return False
        return (
            _matches_egress_snapshot(self, self._egress_policy)
            and self._http_fetcher is _DEFAULT_HTTP_FETCHER
            and self._dynamic_fetcher is _DEFAULT_DYNAMIC_FETCHER
            and self._stealthy_fetcher is _DEFAULT_STEALTHY_FETCHER
            and self._http_gate is HTTP_EGRESS_GATE
            and self._browser_gate is _BROWSER_GATE
        )

    @property
    def proxy_identity(self) -> EgressProxyIdentity:
        self._require_policy_seal()
        return self._egress_policy.identity

    def _uses_egress_policy(self, policy: EgressProxyPolicy) -> bool:
        return self.is_policy_sealed and self._egress_policy is policy

    def _require_policy_seal(self) -> None:
        if not self.is_policy_sealed:
            raise MonitorError(
                ErrorClass.POLICY,
                "fetch",
                "backend policy integrity check failed",
            )

    async def fetch_http(self, target: ResolvedTarget) -> SourceDocument:
        self._require_policy_seal()
        body_collector = _BoundedBodyCollector()
        return await self._fetch(
            target,
            strategy=FetchStrategy.HTTP,
            fetcher=self._http_fetcher,
            gate=self._http_gate,
            outer_timeout_seconds=self._http_timeout_seconds,
            kwargs={
                # curl_cffi accepts (connect timeout, read timeout); Scrapling
                # 0.4.11 forwards this value unchanged to the curl session.
                "timeout": (10, 20),
                "follow_redirects": False,
                "max_redirects": 0,
                # Scrapling 0.4.11 counts total attempts, so one means no retry.
                "retries": 1,
                "proxy": self._egress_policy.url,
                "headers": {"Accept-Encoding": "identity"},
                "accept_encoding": "identity",
                "content_callback": body_collector,
                "selector_config": dict(_SELECTOR_CONFIG),
            },
            body_collector=body_collector,
        )

    async def fetch_dynamic(
        self, target: ResolvedTarget, *, profile: Path | None = None
    ) -> SourceDocument:
        self._require_policy_seal()
        return await self._fetch_browser(
            target,
            profile=profile,
            strategy=FetchStrategy.DYNAMIC,
            fetcher=self._dynamic_fetcher,
        )

    async def fetch_stealthy(
        self, target: ResolvedTarget, *, profile: Path | None = None
    ) -> SourceDocument:
        self._require_policy_seal()
        return await self._fetch_browser(
            target,
            profile=profile,
            strategy=FetchStrategy.STEALTHY,
            fetcher=self._stealthy_fetcher,
        )

    async def _fetch_browser(
        self,
        target: ResolvedTarget,
        *,
        profile: Path | None,
        strategy: FetchStrategy,
        fetcher: BlockingFetcher,
    ) -> SourceDocument:
        kwargs: dict[str, object] = {
            "timeout": 90_000,
            "headless": True,
            "network_idle": True,
            "disable_resources": True,
            "block_ads": True,
            "google_search": False,
            "dns_over_https": False,
            "retries": 1,
            "proxy": self._egress_policy.url,
            "selector_config": dict(_SELECTOR_CONFIG),
        }
        if profile is not None:
            kwargs["user_data_dir"] = str(profile)
        if strategy is FetchStrategy.STEALTHY:
            kwargs.update(solve_cloudflare=False, block_webrtc=True)
        else:
            kwargs["extra_flags"] = list(_DYNAMIC_WEBRTC_FLAGS)
        return await self._fetch(
            target,
            strategy=strategy,
            fetcher=fetcher,
            gate=self._browser_gate,
            outer_timeout_seconds=self._browser_timeout_seconds,
            kwargs=kwargs,
            profile_lease=profile,
        )

    async def _fetch(
        self,
        target: ResolvedTarget,
        *,
        strategy: FetchStrategy,
        fetcher: BlockingFetcher,
        gate: threading.BoundedSemaphore,
        outer_timeout_seconds: float,
        kwargs: dict[str, object],
        body_collector: _BoundedBodyCollector | None = None,
        profile_lease: Path | None = None,
    ) -> SourceDocument:
        if not isinstance(target, ResolvedTarget):
            raise TypeError("target must be an approved ResolvedTarget")
        started = asyncio.Event()
        worker = asyncio.create_task(
            self._run_blocking(
                fetcher,
                target.normalized_url,
                kwargs=kwargs,
                gate=gate,
                started=started,
            )
        )
        self._background_calls.add(worker)
        worker.add_done_callback(self._discard_background_call)
        if profile_lease is not None:
            attach_profile_worker(profile_lease, worker)
        try:
            async with asyncio.timeout(outer_timeout_seconds):
                response = await asyncio.shield(worker)
        except asyncio.CancelledError:
            if not started.is_set():
                worker.cancel()
            raise
        except TimeoutError:
            if not started.is_set():
                worker.cancel()
            if body_collector is not None and body_collector.exceeded:
                raise _response_size_error() from None
            raise MonitorError(ErrorClass.TRANSIENT_NETWORK, "fetch", "fetch timed out") from None
        except MonitorError:
            raise
        except Exception:
            if body_collector is not None and body_collector.exceeded:
                raise _response_size_error() from None
            raise MonitorError(ErrorClass.TRANSIENT_NETWORK, "fetch", "fetch failed") from None
        if body_collector is not None and body_collector.exceeded:
            raise _response_size_error()
        return normalize_response(
            response,
            strategy=strategy,
            block_page_detector=self._block_page_detector,
            clock=self._clock,
            body_override=(
                body_collector.body
                if body_collector is not None and body_collector.saw_chunk
                else None
            ),
        )

    @staticmethod
    async def _run_blocking(
        fetcher: BlockingFetcher,
        url: str,
        *,
        kwargs: dict[str, object],
        gate: threading.BoundedSemaphore,
        started: asyncio.Event,
    ) -> object:
        await _acquire_gate(gate)
        try:
            started.set()
            thread = asyncio.get_running_loop().run_in_executor(
                None,
                _invoke_quietly,
                fetcher,
                url,
                kwargs,
            )
            try:
                return await asyncio.shield(thread)
            except asyncio.CancelledError:
                with suppress(BaseException):
                    await thread
                raise
        finally:
            gate.release()

    def _discard_background_call(self, task: asyncio.Task[object]) -> None:
        self._background_calls.discard(task)
        if not task.cancelled():
            task.exception()


async def _acquire_gate(gate: threading.BoundedSemaphore) -> None:
    while not gate.acquire(blocking=False):
        await asyncio.sleep(GATE_POLL_SECONDS)


def _invoke_quietly(
    fetcher: BlockingFetcher,
    url: str,
    kwargs: dict[str, object],
) -> object:
    token = set_logger(_QUIET_LOGGER)
    try:
        return fetcher(url, **kwargs)
    finally:
        reset_logger(token)


def _bounded_timeout(value: float, *, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} timeout must be finite, positive, and bounded")
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} timeout must be finite, positive, and bounded") from None
    if not math.isfinite(normalized) or normalized <= 0 or normalized > maximum:
        raise ValueError(f"{label} timeout must be finite, positive, and bounded")
    return normalized


def _read_status(response: object) -> int:
    try:
        status = response.status
    except Exception:
        raise _policy_error("response status is missing or invalid") from None
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise _policy_error("response status is missing or invalid")
    return status


def _raise_for_status(
    status: int,
    *,
    retry_after_seconds: float | None,
    detected_interstitial: bool,
) -> None:
    if detected_interstitial:
        raise FetchError(
            ErrorClass.POLICY,
            "block page detected",
            status=status,
            retry_after_seconds=retry_after_seconds,
            detected_interstitial=True,
        )
    if status in {401, 403}:
        raise FetchError(
            ErrorClass.AUTHENTICATION,
            "authentication was rejected",
            status=status,
            retry_after_seconds=retry_after_seconds,
        )
    if status == 429 or 500 <= status <= 599:
        raise FetchError(
            ErrorClass.TRANSIENT_NETWORK,
            "remote service is temporarily unavailable",
            status=status,
            retry_after_seconds=retry_after_seconds,
        )
    if 400 <= status <= 499:
        raise FetchError(
            ErrorClass.POLICY,
            "remote service rejected the request",
            status=status,
            retry_after_seconds=retry_after_seconds,
        )


def _detect_interstitial(
    detector: BlockPageDetector | None,
    *,
    status: int,
    content_type: str,
    body: bytes,
) -> bool:
    if detector is None:
        return False
    try:
        result = detector(status, content_type, body[:MAX_DETECTOR_BYTES])
    except Exception:
        raise MonitorError(ErrorClass.INTERNAL, "fetch", "block detector failed") from None
    if not isinstance(result, bool):
        raise MonitorError(ErrorClass.INTERNAL, "fetch", "block detector returned invalid result")
    return result


def _read_final_url(response: object) -> str:
    try:
        final_url = response.url
    except Exception:
        raise _policy_error("response final URL is missing or invalid") from None
    return _validate_absolute_http_url(final_url, detail="response final URL is missing or invalid")


def _read_redirect_urls(response: object) -> tuple[str, ...]:
    try:
        history = getattr(response, "history", ())
    except Exception:
        raise _policy_error("response redirect history is invalid") from None
    if history is None:
        return ()
    if not isinstance(history, (list, tuple)) or len(history) > MAX_REDIRECTS:
        raise _policy_error("response redirect history is invalid or exceeds the limit")
    urls: list[str] = []
    for item in history:
        try:
            value = item.url
        except Exception:
            raise _policy_error("response redirect history is invalid") from None
        urls.append(
            _validate_absolute_http_url(value, detail="response redirect history is invalid")
        )
    return tuple(urls)


def _validate_absolute_http_url(value: object, *, detail: str) -> str:
    if not isinstance(value, str) or not value or has_unsafe_url_characters(value):
        raise _policy_error(detail)
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        raise _policy_error(detail) from None
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise _policy_error(detail)
    return value


def _copy_safe_headers(
    response: object,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    try:
        raw_headers = response.headers
    except Exception:
        raise _policy_error("response headers are missing or invalid") from None
    if not isinstance(raw_headers, Mapping):
        raise _policy_error("response headers are missing or invalid")

    mapped: dict[str, list[str]] = {}
    try:
        for key, value in raw_headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError
            normalized = key.casefold()
            if normalized in _RETAINED_HEADERS:
                mapped.setdefault(normalized, []).append(value)
    except Exception:
        raise _policy_error("response headers are missing or invalid") from None

    values_by_name: dict[str, tuple[str, ...]] = {}
    get_list = getattr(raw_headers, "get_list", None)
    for name in _RETAINED_HEADERS:
        values = mapped.get(name, [])
        if callable(get_list):
            try:
                listed = list(get_list(name))
            except Exception:
                raise _policy_error("response headers are missing or invalid") from None
            if any(not isinstance(value, str) for value in listed):
                raise _policy_error("response headers are missing or invalid")
            if listed:
                values = listed if len(listed) >= len(values) else values
        values_by_name[name] = tuple(values)

    retained = {name: values[0] for name, values in values_by_name.items() if len(values) == 1}
    return retained, values_by_name


def _normalize_content_type(values: tuple[str, ...]) -> str:
    if not values:
        return ""
    if len(values) != 1 or "," in values[0]:
        raise _policy_error("response has multiple Content-Type values")
    return values[0].split(";", 1)[0].strip().casefold()


def _validate_content_type(content_type: str, *, allow_missing: bool) -> None:
    if not content_type:
        if allow_missing:
            return
        raise _policy_error("response Content-Type is missing or invalid")
    match = _MEDIA_TYPE.fullmatch(content_type)
    if match is None or "*" in content_type:
        raise _policy_error("response content type is not allowed")
    media_type, subtype = match.groups()
    allowed = content_type in {"text/html", "application/xhtml+xml", "application/json"}
    structured_json = (
        subtype.endswith("+json") and len(subtype) > len("+json") and media_type != "*"
    )
    if not allowed and not structured_json:
        raise _policy_error("response content type is not allowed")


def _read_redirect_location(values: tuple[str, ...], *, base_url: str) -> str:
    if len(values) != 1:
        raise _policy_error("navigation redirect requires exactly one Location header")
    raw = values[0]
    if not raw or "," in raw or has_unsafe_url_characters(raw):
        raise _policy_error("navigation redirect Location is invalid")
    try:
        resolved = urljoin(base_url, raw)
    except ValueError:
        raise _policy_error("navigation redirect Location is invalid") from None
    return _validate_absolute_http_url(
        resolved,
        detail="navigation redirect Location is invalid",
    )


def _parse_retry_after(values: tuple[str, ...], *, clock: Clock) -> float | None:
    if len(values) != 1:
        return None
    raw = values[0].strip()
    if raw.isascii() and raw.isdigit() and len(raw) <= 10:
        return float(int(raw))
    try:
        retry_at = parsedate_to_datetime(raw)
        now = clock()
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    if not isinstance(now, datetime):
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return max(0.0, (retry_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds())


def _read_body(response: object, *, body_override: bytes | None = None) -> bytes:
    if body_override is not None:
        return body_override
    try:
        body = response.body
    except Exception:
        raise _policy_error("response body is missing or invalid") from None
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise _policy_error("response body is missing or invalid")
    copied = bytes(body)
    if len(copied) > MAX_RESPONSE_BYTES:
        raise _policy_error("response exceeds the 10 MiB decompressed size limit")
    return copied


def _policy_error(detail: str) -> MonitorError:
    return MonitorError(ErrorClass.POLICY, "fetch", detail)


def _response_size_error() -> MonitorError:
    return _policy_error("response exceeds the 10 MiB decompressed size limit")
