from __future__ import annotations

import asyncio
import logging
import math
import multiprocessing
import os
import re
import signal
import threading
import weakref
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
from personal_monitor.engine.errors import ErrorClass, FailureCode, FetchError, MonitorError
from personal_monitor.scraping.document import SourceDocument
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
PROFILE_PROCESS_POLL_SECONDS = 0.005
PROFILE_PROCESS_JOIN_SECONDS = 0.25
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


def _private_supervisor_pins():
    pins: weakref.WeakKeyDictionary[object, object] = weakref.WeakKeyDictionary()
    lock = threading.RLock()

    def bind(owner: object, factory: object) -> None:
        with lock:
            if owner in pins:
                raise RuntimeError("profiled browser supervisor is already bound")
            pins[owner] = factory

    def acquire(owner: object) -> object | None:
        with lock:
            return pins.get(owner)

    return bind, acquire


def _private_backend_supervisors():
    pins: weakref.WeakKeyDictionary[object, object] = weakref.WeakKeyDictionary()
    lock = threading.RLock()

    def bind(owner: object, supervisor: object) -> None:
        with lock:
            if owner in pins:
                raise RuntimeError("backend supervisor is already bound")
            pins[owner] = supervisor

    def matches(owner: object, supervisor: object) -> bool:
        with lock:
            return pins.get(owner) is supervisor

    return bind, matches


def _supervisor_type_guard():
    expected: list[type[object]] = []

    def bind(value: type[object]) -> None:
        if expected:
            raise RuntimeError("profiled browser supervisor type is already bound")
        expected.append(value)

    def matches(value: object) -> bool:
        return len(expected) == 1 and type(value) is expected[0]

    return bind, matches


_pin_supervisor, _acquire_supervisor = _private_supervisor_pins()
_pin_backend_supervisor, _matches_backend_supervisor = _private_backend_supervisors()
_bind_supervisor_type, _is_exact_supervisor = _supervisor_type_guard()


class _WireHistoryItem:
    __slots__ = ("url",)

    def __init__(self, url: str) -> None:
        self.url = url


class _WireHeaders(dict[str, str]):
    __slots__ = ("_values",)

    def __init__(self, values: tuple[tuple[str, tuple[str, ...]], ...]) -> None:
        self._values = dict(values)
        super().__init__((name, items[0]) for name, items in values if len(items) == 1)

    def get_list(self, name: str) -> list[str]:
        return list(self._values.get(name.casefold(), ()))


class _WireResponse:
    __slots__ = ("body", "headers", "history", "status", "url")

    def __init__(
        self,
        status: object,
        url: object,
        history: object,
        headers: object,
        body: object,
    ) -> None:
        self.status = status
        self.url = url
        self.history = (
            tuple(_WireHistoryItem(item) for item in history)
            if isinstance(history, tuple) and all(isinstance(item, str) for item in history)
            else history
        )
        self.headers = _WireHeaders(headers) if isinstance(headers, tuple) else headers
        self.body = body


def _serialize_profile_response(response: object) -> tuple[object, ...]:
    status = _read_status(response)
    final_url = _read_final_url(response)
    redirects = _read_redirect_urls(response)
    _headers, header_values = _copy_safe_headers(response)
    body = _read_body(response)
    return (
        "response",
        status,
        final_url,
        redirects,
        tuple(sorted(header_values.items())),
        body,
    )


def _profiled_browser_child(
    strategy_value: str,
    url: str,
    kwargs: dict[str, object],
    sender: object,
) -> None:
    try:
        if hasattr(os, "setsid"):
            os.setsid()
        from scrapling.fetchers import DynamicFetcher as ChildDynamicFetcher
        from scrapling.fetchers import StealthyFetcher as ChildStealthyFetcher

        fetcher = (
            ChildDynamicFetcher.fetch
            if strategy_value == FetchStrategy.DYNAMIC.value
            else ChildStealthyFetcher.fetch
        )
        response = _invoke_quietly(fetcher, url, kwargs)
        wire: tuple[object, ...] = _serialize_profile_response(response)
    except MonitorError as error:
        wire = (
            "monitor_error",
            error.error_class.value,
            error.stage,
            error.safe_detail,
            error.code.value if error.code is not None else None,
        )
    except BaseException:
        wire = ("failure",)
    try:
        sender.send(wire)  # type: ignore[attr-defined]
    except BaseException:
        pass
    finally:
        with suppress(BaseException):
            sender.close()  # type: ignore[attr-defined]


class _ProfileProcessBoundary:
    __slots__ = ("_process",)

    def __init__(self, process: object) -> None:
        self._process = process

    @property
    def pid(self) -> object:
        return self._process.pid  # type: ignore[attr-defined]

    def start(self) -> None:
        self._process.start()  # type: ignore[attr-defined]

    def is_alive(self) -> bool:
        if self._process.is_alive():  # type: ignore[attr-defined]
            return True
        pid = self.pid
        if isinstance(pid, int) and hasattr(os, "killpg"):
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            else:
                return True
        return False

    def terminate(self) -> None:
        self._signal_group(signal.SIGTERM, self._process.terminate)  # type: ignore[attr-defined]

    def kill(self) -> None:
        self._signal_group(signal.SIGKILL, self._process.kill)  # type: ignore[attr-defined]

    def join(self, timeout: float | None = None) -> None:
        self._process.join(timeout)  # type: ignore[attr-defined]

    def close(self) -> None:
        self._process.close()  # type: ignore[attr-defined]

    def _signal_group(
        self,
        sent_signal: signal.Signals,
        fallback: Callable[[], None],
    ) -> None:
        pid = self.pid
        if isinstance(pid, int) and hasattr(os, "killpg"):
            try:
                os.killpg(pid, sent_signal)
                return
            except ProcessLookupError:
                pass
        fallback()


def _production_profile_session(
    strategy: FetchStrategy,
    url: str,
    kwargs: dict[str, object],
) -> tuple[object, object, object]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_profiled_browser_child,
        args=(strategy.value, url, kwargs, sender),
        daemon=False,
    )
    return _ProfileProcessBoundary(process), receiver, sender


class _ProfiledBrowserSupervisor:
    __slots__ = ("_factory", "_sealed", "__weakref__")

    def __init__(
        self,
        factory: Callable[..., tuple[object, ...]],
        _is_original_type: Callable[[object], bool] = _is_exact_supervisor,
        _pin: Callable[[object, object], None] = _pin_supervisor,
    ) -> None:
        if not _is_original_type(self):
            raise TypeError("profiled browser supervisor subclasses are not allowed")
        if not callable(factory):
            raise TypeError("profiled browser process factory must be callable")
        self._factory = factory
        self._sealed = True
        _pin(self, factory)

    @classmethod
    def _for_test(cls, factory: Callable[..., tuple[object, ...]]):
        return cls(factory)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("profiled browser supervisor is sealed")
        object.__setattr__(self, name, value)

    def _trusted_factory(
        self,
        _acquire: Callable[[object], object | None] = _acquire_supervisor,
        _is_original_type: Callable[[object], bool] = _is_exact_supervisor,
    ) -> Callable[..., tuple[object, ...]]:
        factory = _acquire(self)
        if (
            not _is_original_type(self)
            or factory is None
            or self._factory is not factory
            or not callable(factory)
        ):
            raise MonitorError(
                ErrorClass.POLICY,
                "fetch",
                "profiled browser supervisor integrity check failed",
            )
        return factory

    async def fetch(
        self,
        *,
        strategy: FetchStrategy,
        url: str,
        kwargs: dict[str, object],
        gate: threading.BoundedSemaphore,
        timeout_seconds: float,
    ) -> tuple[object, ...]:
        factory = self._trusted_factory()
        process: object | None = None
        receiver: object | None = None
        sender: object | None = None
        gate_acquired = False
        process_started = False
        try:
            async with asyncio.timeout(timeout_seconds):
                await _acquire_gate(gate)
                gate_acquired = True
                session = factory(strategy, url, dict(kwargs))
                if not isinstance(session, tuple) or len(session) not in {2, 3}:
                    raise RuntimeError
                process, receiver = session[:2]
                sender = session[2] if len(session) == 3 else None
                process.start()  # type: ignore[attr-defined]
                process_started = True
                if sender is not None:
                    sender.close()  # type: ignore[attr-defined]
                    sender = None
                while True:
                    if receiver.poll():  # type: ignore[attr-defined]
                        wire = receiver.recv()  # type: ignore[attr-defined]
                        if not isinstance(wire, tuple):
                            raise RuntimeError
                        _stop_profile_process(process, force=True)
                        process = None
                        return wire
                    if not process.is_alive():  # type: ignore[attr-defined]
                        raise RuntimeError
                    await asyncio.sleep(PROFILE_PROCESS_POLL_SECONDS)
        except asyncio.CancelledError as error:
            if process_started and process is not None:
                try:
                    _stop_profile_process(process, force=True)
                    process = None
                except Exception:
                    error.add_note("profiled browser cleanup failed")
            raise
        except TimeoutError:
            if process_started and process is not None:
                try:
                    _stop_profile_process(process, force=True)
                    process = None
                except Exception:
                    raise _profile_process_cleanup_error() from None
            raise MonitorError(
                ErrorClass.TRANSIENT_NETWORK,
                "fetch",
                "fetch timed out",
            ) from None
        except MonitorError:
            raise
        except Exception:
            if process is not None:
                try:
                    if process_started or process.is_alive():  # type: ignore[attr-defined]
                        _stop_profile_process(process, force=True)
                    else:
                        process.close()  # type: ignore[attr-defined]
                    process = None
                except Exception:
                    raise _profile_process_cleanup_error() from None
            raise MonitorError(ErrorClass.TRANSIENT_NETWORK, "fetch", "fetch failed") from None
        finally:
            if sender is not None:
                with suppress(BaseException):
                    sender.close()  # type: ignore[attr-defined]
            if receiver is not None:
                with suppress(BaseException):
                    receiver.close()  # type: ignore[attr-defined]
            if gate_acquired:
                gate.release()


_bind_supervisor_type(_ProfiledBrowserSupervisor)
_DEFAULT_PROFILE_SUPERVISOR = _ProfiledBrowserSupervisor(_production_profile_session)


def _stop_profile_process(process: object, *, force: bool) -> None:
    try:
        if force and process.is_alive():  # type: ignore[attr-defined]
            process.terminate()  # type: ignore[attr-defined]
        process.join(PROFILE_PROCESS_JOIN_SECONDS)  # type: ignore[attr-defined]
        if process.is_alive():  # type: ignore[attr-defined]
            process.kill()  # type: ignore[attr-defined]
            process.join(PROFILE_PROCESS_JOIN_SECONDS)  # type: ignore[attr-defined]
        if process.is_alive():  # type: ignore[attr-defined]
            raise RuntimeError
        process.close()  # type: ignore[attr-defined]
    except Exception:
        raise _profile_process_cleanup_error() from None


def _profile_process_cleanup_error() -> MonitorError:
    return MonitorError(
        ErrorClass.INTERNAL,
        "fetch",
        "profiled browser cleanup failed",
    )


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
    _profile_supervisor: _ProfiledBrowserSupervisor = field(repr=False)

    def __init__(
        self,
        *,
        egress_proxy_url: str | None,
        http_timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
        browser_timeout_seconds: float = BROWSER_TIMEOUT_SECONDS,
        block_page_detector: BlockPageDetector | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        profile_supervisor: _ProfiledBrowserSupervisor = _DEFAULT_PROFILE_SUPERVISOR,
    ) -> None:
        self._initialize(
            EgressProxyPolicy.from_url(egress_proxy_url),
            http_timeout_seconds=http_timeout_seconds,
            browser_timeout_seconds=browser_timeout_seconds,
            block_page_detector=block_page_detector,
            clock=clock,
            profile_supervisor=profile_supervisor,
        )

    @classmethod
    def _from_egress_policy(
        cls,
        policy: EgressProxyPolicy,
        *,
        clock: Clock,
        profile_supervisor: _ProfiledBrowserSupervisor = _DEFAULT_PROFILE_SUPERVISOR,
    ) -> ScraplingBackend:
        instance = cls.__new__(cls)
        instance._initialize(
            policy,
            http_timeout_seconds=HTTP_TIMEOUT_SECONDS,
            browser_timeout_seconds=BROWSER_TIMEOUT_SECONDS,
            block_page_detector=None,
            clock=clock,
            profile_supervisor=profile_supervisor,
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
        profile_supervisor: _ProfiledBrowserSupervisor,
        _pin_supervisor: Callable[[object, object], None] = _pin_backend_supervisor,
        _is_original_supervisor: Callable[[object], bool] = _is_exact_supervisor,
    ) -> None:
        if not _is_original_supervisor(profile_supervisor):
            raise TypeError("profiled browser supervisor is invalid")
        profile_supervisor._trusted_factory()
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
        object.__setattr__(self, "_profile_supervisor", profile_supervisor)
        _pin_supervisor(self, profile_supervisor)
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
            and self._has_pinned_profile_supervisor()
        )

    def _has_pinned_profile_supervisor(
        self,
        _matches: Callable[[object, object], bool] = _matches_backend_supervisor,
        _is_original_supervisor: Callable[[object], bool] = _is_exact_supervisor,
    ) -> bool:
        try:
            supervisor = self._profile_supervisor
            return (
                _is_original_supervisor(supervisor)
                and _matches(self, supervisor)
                and supervisor._trusted_factory() is not None
            )
        except Exception:
            return False

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
        if profile is not None:
            wire = await self._profile_supervisor.fetch(
                strategy=strategy,
                url=target.normalized_url,
                kwargs=kwargs,
                gate=self._browser_gate,
                timeout_seconds=self._browser_timeout_seconds,
            )
            response = _profile_wire_response(wire)
            return normalize_response(
                response,
                strategy=strategy,
                block_page_detector=self._block_page_detector,
                clock=self._clock,
            )
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
        body_collector: _BoundedBodyCollector | None = None,
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


def _profile_wire_response(wire: tuple[object, ...]) -> _WireResponse:
    if len(wire) == 6 and wire[0] == "response":
        return _WireResponse(wire[1], wire[2], wire[3], wire[4], wire[5])
    if len(wire) == 5 and wire[0] == "monitor_error":
        try:
            error_class = ErrorClass(wire[1])
            stage = wire[2]
            detail = wire[3]
            raw_code = wire[4]
            code = FailureCode(raw_code) if raw_code is not None else None
            if not isinstance(stage, str) or not isinstance(detail, str):
                raise ValueError
        except (TypeError, ValueError):
            raise MonitorError(
                ErrorClass.TRANSIENT_NETWORK,
                "fetch",
                "fetch failed",
            ) from None
        raise MonitorError(error_class, stage, detail, code=code)
    raise MonitorError(ErrorClass.TRANSIENT_NETWORK, "fetch", "fetch failed")


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
