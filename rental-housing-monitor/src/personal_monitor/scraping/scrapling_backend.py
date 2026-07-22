from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from scrapling.core.utils import reset_logger, set_logger
from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher

from personal_monitor.domain.spec import FetchStrategy
from personal_monitor.engine.errors import ErrorClass, FetchError, MonitorError
from personal_monitor.scraping.document import SourceDocument
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

_HTTP_GATE = threading.BoundedSemaphore(HTTP_CONCURRENCY)
_BROWSER_GATE = threading.BoundedSemaphore(BROWSER_CONCURRENCY)

_QUIET_LOGGER = logging.getLogger("personal_monitor.scrapling.quiet")
_QUIET_LOGGER.handlers.clear()
_QUIET_LOGGER.addHandler(logging.NullHandler())
_QUIET_LOGGER.propagate = False
_QUIET_LOGGER.setLevel(logging.CRITICAL + 1)


def normalize_response(
    response: object,
    *,
    strategy: FetchStrategy,
    block_page_detector: BlockPageDetector | None = None,
    clock: Clock = lambda: datetime.now(UTC),
) -> SourceDocument:
    status = _read_status(response)
    final_url = _read_final_url(response)
    redirect_urls = _read_redirect_urls(response)
    peer_ip = _read_peer_ip(response)
    headers, header_values = _copy_safe_headers(response)
    body = _read_body(response)
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
        peer_ip=peer_ip,
    )


class ScraplingBackend:
    """A bounded async boundary around Scrapling's synchronous fetchers.

    The default execution gates are process-wide threading semaphores, so separate
    event loops and worker threads still share the HTTP-4/browser-1 policy. Gate
    acquisition polls without occupying the loop's default executor. Once egress
    work starts, timeout or cancellation leaves the gate held until its underlying
    executor call actually finishes.
    """

    def __init__(
        self,
        *,
        egress_proxy_url: str | None,
        http_fetcher: BlockingFetcher = _DEFAULT_HTTP_FETCHER,
        dynamic_fetcher: BlockingFetcher = _DEFAULT_DYNAMIC_FETCHER,
        stealthy_fetcher: BlockingFetcher = _DEFAULT_STEALTHY_FETCHER,
        http_gate: threading.BoundedSemaphore | None = None,
        browser_gate: threading.BoundedSemaphore | None = None,
        http_timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
        browser_timeout_seconds: float = BROWSER_TIMEOUT_SECONDS,
        block_page_detector: BlockPageDetector | None = None,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        proxy = _validate_proxy_url(egress_proxy_url)
        if not proxy:
            raise ValueError("an egress proxy is required for every backend")

        self._egress_proxy_url = proxy
        self._http_fetcher = http_fetcher
        self._dynamic_fetcher = dynamic_fetcher
        self._stealthy_fetcher = stealthy_fetcher
        self._http_gate = http_gate or _HTTP_GATE
        self._browser_gate = browser_gate or _BROWSER_GATE
        self._http_timeout_seconds = _bounded_timeout(
            http_timeout_seconds,
            maximum=HTTP_TIMEOUT_SECONDS,
            label="HTTP",
        )
        self._browser_timeout_seconds = _bounded_timeout(
            browser_timeout_seconds,
            maximum=BROWSER_TIMEOUT_SECONDS,
            label="browser",
        )
        self._block_page_detector = block_page_detector
        self._clock = clock
        self._background_calls: set[asyncio.Task[object]] = set()
        self._policy_sealed = (
            http_fetcher is _DEFAULT_HTTP_FETCHER
            and dynamic_fetcher is _DEFAULT_DYNAMIC_FETCHER
            and stealthy_fetcher is _DEFAULT_STEALTHY_FETCHER
            and http_gate is None
            and browser_gate is None
        )

    @property
    def is_policy_sealed(self) -> bool:
        """Whether production fetchers and process-wide concurrency gates are intact."""
        return self._policy_sealed

    async def fetch_http(self, target: ResolvedTarget) -> SourceDocument:
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
                "proxy": self._egress_proxy_url,
                "headers": {"Accept-Encoding": "identity"},
                "selector_config": dict(_SELECTOR_CONFIG),
            },
        )

    async def fetch_dynamic(
        self, target: ResolvedTarget, *, profile: Path | None = None
    ) -> SourceDocument:
        return await self._fetch_browser(
            target,
            profile=profile,
            strategy=FetchStrategy.DYNAMIC,
            fetcher=self._dynamic_fetcher,
        )

    async def fetch_stealthy(
        self, target: ResolvedTarget, *, profile: Path | None = None
    ) -> SourceDocument:
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
            "proxy": self._egress_proxy_url,
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
            raise MonitorError(ErrorClass.TRANSIENT_NETWORK, "fetch", "fetch timed out") from None
        except MonitorError:
            raise
        except Exception:
            raise MonitorError(ErrorClass.TRANSIENT_NETWORK, "fetch", "fetch failed") from None
        return normalize_response(
            response,
            strategy=strategy,
            block_page_detector=self._block_page_detector,
            clock=self._clock,
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


def _validate_proxy_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("egress proxy URL is invalid")
    normalized = value.strip()
    if not normalized:
        return None
    if normalized != value or has_unsafe_url_characters(value):
        raise ValueError("egress proxy URL is invalid")
    try:
        parts = urlsplit(normalized)
        port = parts.port
    except ValueError:
        raise ValueError("egress proxy URL is invalid") from None
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not parts.hostname
        or (parts.path not in {"", "/"})
        or parts.query
        or parts.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError("egress proxy URL is invalid")
    return normalized


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


def _read_peer_ip(response: object) -> str | None:
    try:
        value = getattr(response, "primary_ip", None)
    except Exception:
        raise _policy_error("response peer metadata is invalid") from None
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise _policy_error("response peer metadata is invalid")
    return value


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


def _read_body(response: object) -> bytes:
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
