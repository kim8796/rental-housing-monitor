from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher

from personal_monitor.domain.spec import FetchStrategy
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.security.url_policy import MAX_REDIRECTS, ResolvedTarget

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
HTTP_CONCURRENCY = 4
BROWSER_CONCURRENCY = 1
HTTP_TIMEOUT_SECONDS = 30.0
BROWSER_TIMEOUT_SECONDS = 90.0
NAVIGATION_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_SELECTOR_CONFIG = {
    "adaptive": True,
    "keep_comments": False,
    "keep_cdata": False,
}

BlockingFetcher = Callable[..., object]

_shared_semaphore_lock = Lock()
_shared_semaphores: WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[asyncio.Semaphore, asyncio.Semaphore]
] = WeakKeyDictionary()


def normalize_response(response: object, *, strategy: FetchStrategy) -> SourceDocument:
    status = _read_status(response)
    _raise_for_status(status)
    final_url = _read_final_url(response)
    redirect_urls = _read_redirect_urls(response)
    headers = _copy_headers(response)
    body = _read_body(response)
    content_type = _read_content_type(
        response,
        headers,
        allow_missing=status in NAVIGATION_REDIRECT_STATUSES and not body,
    )
    return SourceDocument(
        final_url=final_url,
        status=status,
        content_type=content_type,
        headers=headers,
        body=body,
        strategy=strategy,
        redirect_urls=redirect_urls,
    )


class ScraplingBackend:
    """A bounded async boundary around Scrapling's synchronous fetchers.

    Python cannot forcibly stop a thread already running inside ``asyncio.to_thread``.
    After outer cancellation or timeout, an already-started worker therefore retains
    its semaphore slot until Scrapling's own timeout bounds and finishes the work.
    """

    def __init__(
        self,
        *,
        egress_proxy_url: str | None,
        test_mode: bool = False,
        http_fetcher: BlockingFetcher = Fetcher.get,
        dynamic_fetcher: BlockingFetcher = DynamicFetcher.fetch,
        stealthy_fetcher: BlockingFetcher = StealthyFetcher.fetch,
        http_semaphore: asyncio.Semaphore | None = None,
        browser_semaphore: asyncio.Semaphore | None = None,
        http_timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
        browser_timeout_seconds: float = BROWSER_TIMEOUT_SECONDS,
    ) -> None:
        proxy = egress_proxy_url.strip() if isinstance(egress_proxy_url, str) else None
        if not test_mode and not proxy:
            raise ValueError("an egress proxy is required outside test mode")
        self._egress_proxy_url = proxy
        self._http_fetcher = http_fetcher
        self._dynamic_fetcher = dynamic_fetcher
        self._stealthy_fetcher = stealthy_fetcher
        self._http_semaphore = http_semaphore
        self._browser_semaphore = browser_semaphore
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
        self._background_calls: set[asyncio.Task[object]] = set()

    async def fetch_http(self, target: ResolvedTarget) -> SourceDocument:
        return await self._fetch(
            target,
            strategy=FetchStrategy.HTTP,
            fetcher=self._http_fetcher,
            semaphore=self._http_capacity(),
            outer_timeout_seconds=self._http_timeout_seconds,
            kwargs={
                "timeout": 30,
                "follow_redirects": False,
                "max_redirects": 0,
                # Scrapling 0.4.11 defines this as total attempts, despite the
                # option name. One means exactly one attempt; zero skips I/O.
                "retries": 1,
                "proxy": self._egress_proxy_url,
                # Scrapling 0.4.11's Fetcher.get accepts ``headers`` rather than
                # the browser-only ``extra_headers`` option.
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
        return await self._fetch(
            target,
            strategy=strategy,
            fetcher=fetcher,
            semaphore=self._browser_capacity(),
            outer_timeout_seconds=self._browser_timeout_seconds,
            kwargs={
                "timeout": 90_000,
                "network_idle": True,
                "disable_resources": True,
                "block_ads": True,
                "google_search": False,
                "dns_over_https": False,
                # Browser fetchers also count total attempts, not retries.
                "retries": 1,
                "proxy": self._egress_proxy_url,
                "user_data_dir": str(profile) if profile else None,
                "selector_config": dict(_SELECTOR_CONFIG),
                **(
                    {"solve_cloudflare": False, "block_webrtc": True}
                    if strategy is FetchStrategy.STEALTHY
                    else {}
                ),
            },
        )

    def _http_capacity(self) -> asyncio.Semaphore:
        if self._http_semaphore is not None:
            return self._http_semaphore
        return _shared_fetch_semaphores()[0]

    def _browser_capacity(self) -> asyncio.Semaphore:
        if self._browser_semaphore is not None:
            return self._browser_semaphore
        return _shared_fetch_semaphores()[1]

    async def _fetch(
        self,
        target: ResolvedTarget,
        *,
        strategy: FetchStrategy,
        fetcher: BlockingFetcher,
        semaphore: asyncio.Semaphore,
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
                semaphore=semaphore,
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
            raise MonitorError(
                ErrorClass.TRANSIENT_NETWORK,
                "fetch",
                "fetch timed out",
            ) from None
        except MonitorError:
            raise
        except Exception:
            raise MonitorError(
                ErrorClass.TRANSIENT_NETWORK,
                "fetch",
                "fetch failed",
            ) from None
        return normalize_response(response, strategy=strategy)

    @staticmethod
    async def _run_blocking(
        fetcher: BlockingFetcher,
        url: str,
        *,
        kwargs: dict[str, object],
        semaphore: asyncio.Semaphore,
        started: asyncio.Event,
    ) -> object:
        async with semaphore:
            started.set()
            thread = asyncio.create_task(asyncio.to_thread(fetcher, url, **kwargs))
            try:
                return await asyncio.shield(thread)
            except asyncio.CancelledError:
                # Loop shutdown can cancel this bookkeeping task. Do not release
                # capacity while its blocking thread is still doing egress work.
                with suppress(BaseException):
                    await thread
                raise

    def _discard_background_call(self, task: asyncio.Task[object]) -> None:
        self._background_calls.discard(task)
        if task.cancelled():
            return
        task.exception()


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


def _raise_for_status(status: int) -> None:
    if status in {401, 403}:
        raise MonitorError(ErrorClass.AUTHENTICATION, "fetch", "authentication was rejected")
    if status == 429 or 500 <= status <= 599:
        raise MonitorError(
            ErrorClass.TRANSIENT_NETWORK,
            "fetch",
            "remote service is temporarily unavailable",
        )
    if 400 <= status <= 499:
        raise _policy_error("remote service rejected the request")


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
    if not isinstance(value, str) or not value:
        raise _policy_error(detail)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise _policy_error(detail) from None
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise _policy_error(detail)
    return value


def _copy_headers(response: object) -> dict[str, str]:
    try:
        raw_headers = response.headers
    except Exception:
        raise _policy_error("response headers are missing or invalid") from None
    if not isinstance(raw_headers, Mapping):
        raise _policy_error("response headers are missing or invalid")
    copied: dict[str, str] = {}
    try:
        for key, value in raw_headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError
            copied[key] = value
    except Exception:
        raise _policy_error("response headers are missing or invalid") from None
    return copied


def _read_content_type(
    response: object,
    headers: Mapping[str, str],
    *,
    allow_missing: bool = False,
) -> str:
    raw_headers = response.headers
    mapped_values = [value for key, value in headers.items() if key.casefold() == "content-type"]
    listed_values: list[str] | None = None
    get_list = getattr(raw_headers, "get_list", None)
    if callable(get_list):
        try:
            values = get_list("content-type")
            listed_values = list(values)
        except Exception:
            raise _policy_error("response Content-Type is missing or invalid") from None
        if any(not isinstance(value, str) for value in listed_values):
            raise _policy_error("response Content-Type is missing or invalid")
    values = listed_values if listed_values is not None else mapped_values
    if not values:
        if allow_missing:
            return ""
        raise _policy_error("response Content-Type is missing or invalid")
    if len(values) != 1 or len(mapped_values) != 1 or "," in values[0]:
        raise _policy_error("response has multiple Content-Type values")
    content_type = values[0].split(";", 1)[0].strip().casefold()
    if not content_type:
        raise _policy_error("response Content-Type is missing or invalid")
    if content_type not in {"text/html", "application/xhtml+xml", "application/json"} and not (
        "/" in content_type and content_type.endswith("+json")
    ):
        raise _policy_error("response content type is not allowed")
    return content_type


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


def _shared_fetch_semaphores() -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
    loop = asyncio.get_running_loop()
    with _shared_semaphore_lock:
        capacities = _shared_semaphores.get(loop)
        if capacities is None:
            capacities = (
                asyncio.Semaphore(HTTP_CONCURRENCY),
                asyncio.Semaphore(BROWSER_CONCURRENCY),
            )
            _shared_semaphores[loop] = capacities
        return capacities
