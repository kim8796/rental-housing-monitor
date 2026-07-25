from __future__ import annotations

import asyncio
import gc
import inspect
import logging
import signal
import threading
import weakref
from contextlib import suppress
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from time import sleep

import httpx
import pytest
from curl_cffi.const import CurlOpt
from curl_cffi.requests.utils import set_curl_options
from scrapling.engines._browsers._controllers import DynamicSession
from scrapling.engines._browsers._stealth import StealthySession
from scrapling.engines.static import FetcherSession
from scrapling.engines.toolbelt.custom import Response as ScraplingResponse

import personal_monitor.adapters._policy as policy_module
import personal_monitor.scraping.scrapling_backend as backend_module
import personal_monitor.security.egress as egress_module
from personal_monitor.adapters.official_api import BoundedPolicyHttpClient
from personal_monitor.domain.spec import FetchStrategy
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.profiles import BrowserProfileStore
from personal_monitor.scraping.scrapling_backend import (
    MAX_DETECTOR_BYTES,
    MAX_RESPONSE_BYTES,
    FetchError,
    ScraplingBackend,
    normalize_response,
)
from personal_monitor.security.url_policy import ResolvedTarget
from personal_monitor.security.vault import CredentialVault


class FakeHeaders(dict[str, str]):
    def __init__(
        self,
        values: dict[str, str],
        *,
        content_types: list[str] | None = None,
        multiple: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(values)
        self._content_types = content_types
        self._multiple = {key.casefold(): list(items) for key, items in (multiple or {}).items()}

    def get_list(self, name: str) -> list[str]:
        normalized = name.casefold()
        if normalized == "content-type" and self._content_types is not None:
            return list(self._content_types)
        if normalized in self._multiple:
            return list(self._multiple[normalized])
        value = next(
            (value for key, value in self.items() if key.casefold() == name.casefold()),
            None,
        )
        return [] if value is None else [value]


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: object | None = None,
        body: object = b"<html><h1>ok</h1></html>",
        url: object = "https://example.com/final",
        history: object | None = None,
    ) -> None:
        self.status = status
        self.headers = (
            FakeHeaders({"Content-Type": "text/html; charset=UTF-8", "X-Test": "yes"})
            if headers is None
            else headers
        )
        self.body = body
        self.url = url
        self.history = [] if history is None else history


class FakeProfileConnection:
    def __init__(self, wire: object | None, events: list[str]) -> None:
        self._wire = wire
        self._events = events

    def poll(self) -> bool:
        return self._wire is not None

    def recv(self) -> object:
        wire = self._wire
        self._wire = None
        return wire

    def close(self) -> None:
        self._events.append("connection.close")


class FakeProfileProcess:
    def __init__(
        self,
        events: list[str],
        *,
        exits_on_join: bool,
        resists_terminate: bool = False,
        raises_on_start: bool = False,
    ) -> None:
        self._events = events
        self._alive = False
        self._exits_on_join = exits_on_join
        self._resists_terminate = resists_terminate
        self._raises_on_start = raises_on_start
        self.pid = 4242

    def start(self) -> None:
        self._events.append("start")
        if self._raises_on_start:
            raise RuntimeError
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self._events.append("terminate")
        if not self._resists_terminate:
            self._alive = False

    def kill(self) -> None:
        self._events.append("kill")
        self._alive = False

    def join(self, _timeout: float | None = None) -> None:
        self._events.append("join")
        if self._exits_on_join:
            self._alive = False

    def close(self) -> None:
        self._events.append("process.close")


def install_fake_profile_session(
    process: FakeProfileProcess,
    connection: FakeProfileConnection,
    calls: list[tuple[FetchStrategy, str, dict[str, object]]] | None = None,
) -> None:
    if _active_monkeypatch is None:
        raise AssertionError("test construction seam is unavailable")

    class Sender:
        def close(self) -> None:
            return None

    class FakeSpawnContext:
        def Pipe(self, *, duplex: bool):
            assert duplex is False
            return connection, Sender()

        def Process(
            self,
            *,
            target: object,
            args: tuple[object, ...],
            daemon: bool,
        ) -> FakeProfileProcess:
            assert target is backend_module._profiled_browser_child
            assert daemon is False
            strategy_value, url, kwargs, _sender = args
            if calls is not None:
                assert isinstance(strategy_value, str)
                assert isinstance(url, str)
                assert isinstance(kwargs, dict)
                calls.append((FetchStrategy(strategy_value), url, kwargs))
            return process

    def missing_process_group(_process_group: int, _sent_signal: int) -> None:
        raise ProcessLookupError

    _active_monkeypatch.setattr(backend_module.os, "killpg", missing_process_group)
    _active_monkeypatch.setattr(
        backend_module.multiprocessing,
        "get_context",
        lambda method: FakeSpawnContext() if method == "spawn" else None,
    )


def test_production_profile_boundary_signals_entire_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    signals: list[tuple[int, signal.Signals]] = []
    process = FakeProfileProcess(events, exits_on_join=False)
    process.start()
    monkeypatch.setattr(
        backend_module.os,
        "killpg",
        lambda process_group, sent_signal: signals.append((process_group, sent_signal)),
    )

    boundary = backend_module._ProfileProcessBoundary(process)
    boundary.terminate()
    boundary.kill()

    assert signals == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]
    assert events == ["start"]


def test_profiled_supervisor_construction_has_no_internal_injection_parameters() -> None:
    assert tuple(inspect.signature(backend_module._ProfiledBrowserSupervisor).parameters) == (
        "factory",
    )
    assert tuple(
        inspect.signature(backend_module._ProfiledBrowserSupervisor._trusted_factory).parameters
    ) == ("self",)
    assert "profile_supervisor" not in inspect.signature(ScraplingBackend).parameters


def test_production_profile_session_pickles_strategy_instead_of_fetcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receiver = FakeProfileConnection(None, events)
    sender = FakeProfileConnection(None, events)
    process = FakeProfileProcess(events, exits_on_join=True)
    recorded: dict[str, object] = {}

    class FakeSpawnContext:
        def Pipe(self, *, duplex: bool):
            assert duplex is False
            return receiver, sender

        def Process(self, **kwargs: object):
            recorded.update(kwargs)
            return process

    monkeypatch.setattr(
        backend_module.multiprocessing,
        "get_context",
        lambda method: FakeSpawnContext() if method == "spawn" else None,
    )
    child_kwargs: dict[str, object] = {
        "timeout": 90_000,
        "headless": True,
        "user_data_dir": "/private/profile",
    }

    boundary, actual_receiver, actual_sender = backend_module._production_profile_session(
        FetchStrategy.DYNAMIC,
        "https://example.com/source",
        child_kwargs,
    )

    assert isinstance(boundary, backend_module._ProfileProcessBoundary)
    assert actual_receiver is receiver
    assert actual_sender is sender
    assert recorded == {
        "target": backend_module._profiled_browser_child,
        "args": (
            FetchStrategy.DYNAMIC.value,
            "https://example.com/source",
            child_kwargs,
            sender,
        ),
        "daemon": False,
    }
    assert not any(callable(item) for item in recorded["args"])  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (FetchStrategy.DYNAMIC, "dynamic"),
        (FetchStrategy.STEALTHY, "stealthy"),
    ],
)
def test_profiled_child_imports_real_fetcher_by_strategy(
    monkeypatch: pytest.MonkeyPatch,
    strategy: FetchStrategy,
    expected: str,
) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fetcher(kind: str):
        def run(url: str, **kwargs: object) -> FakeResponse:
            calls.append((kind, url, kwargs))
            return FakeResponse()

        return staticmethod(run)

    class Sender:
        wire: object | None = None
        closed = False

        def send(self, wire: object) -> None:
            self.wire = wire

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(backend_module.os, "setsid", lambda: None)
    monkeypatch.setattr(backend_module.DynamicFetcher, "fetch", fetcher("dynamic"))
    monkeypatch.setattr(backend_module.StealthyFetcher, "fetch", fetcher("stealthy"))
    sender = Sender()

    backend_module._profiled_browser_child(
        strategy.value,
        "https://example.com/source",
        {"headless": True},
        sender,
    )

    assert calls == [(expected, "https://example.com/source", {"headless": True})]
    assert isinstance(sender.wire, tuple) and sender.wire[0] == "response"
    assert sender.closed


class RawHttpxStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self):
        yield self._body

    async def aclose(self) -> None:
        return None


def target(url: str = "https://example.com/source") -> ResolvedTarget:
    return ResolvedTarget(
        normalized_url=url,
        hostname="example.com",
        port=443,
        addresses=frozenset({"93.184.216.34"}),
    )


def noop_fetcher(_url: str, **_kwargs: object) -> FakeResponse:
    return FakeResponse()


_ORIGINAL_HTTP_GATE = backend_module.HTTP_EGRESS_GATE
_ORIGINAL_BROWSER_GATE = backend_module._BROWSER_GATE
_active_monkeypatch: pytest.MonkeyPatch | None = None


@pytest.fixture(autouse=True)
def construction_seams(monkeypatch: pytest.MonkeyPatch):
    global _active_monkeypatch
    _active_monkeypatch = monkeypatch
    yield
    _active_monkeypatch = None


def make_test_backend(**overrides: object) -> ScraplingBackend:
    if _active_monkeypatch is None:
        raise AssertionError("test construction seam is unavailable")
    arguments: dict[str, object] = {
        "egress_proxy_url": "http://proxy.test:8080",
    }
    http_fetcher = overrides.pop("http_fetcher", noop_fetcher)
    dynamic_fetcher = overrides.pop("dynamic_fetcher", noop_fetcher)
    stealthy_fetcher = overrides.pop("stealthy_fetcher", noop_fetcher)
    http_gate = overrides.pop("http_gate", _ORIGINAL_HTTP_GATE)
    browser_gate = overrides.pop("browser_gate", _ORIGINAL_BROWSER_GATE)
    _active_monkeypatch.setattr(backend_module, "_DEFAULT_HTTP_FETCHER", http_fetcher)
    _active_monkeypatch.setattr(backend_module, "_DEFAULT_DYNAMIC_FETCHER", dynamic_fetcher)
    _active_monkeypatch.setattr(backend_module, "_DEFAULT_STEALTHY_FETCHER", stealthy_fetcher)
    _active_monkeypatch.setattr(backend_module, "HTTP_EGRESS_GATE", http_gate)
    _active_monkeypatch.setattr(egress_module, "HTTP_EGRESS_GATE", http_gate)
    _active_monkeypatch.setattr(backend_module, "_BROWSER_GATE", browser_gate)
    arguments.update(overrides)
    return ScraplingBackend(**arguments)  # type: ignore[arg-type]


def test_response_is_bounded_normalized_and_immutable() -> None:
    response = FakeResponse()

    document = normalize_response(response, strategy=FetchStrategy.HTTP)

    assert document.final_url == "https://example.com/final"
    assert document.status == 200
    assert document.content_type == "text/html"
    assert document.body == b"<html><h1>ok</h1></html>"
    assert document.strategy is FetchStrategy.HTTP
    assert document.redirect_urls == ()
    assert document.headers == {"content-type": "text/html; charset=UTF-8"}
    response.headers["X-Test"] = "changed"  # type: ignore[index]
    assert "x-test" not in document.headers
    with pytest.raises(TypeError):
        document.headers["new"] = "value"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        document.status = 201  # type: ignore[misc]
    representation = repr(document)
    assert "<html>" not in representation
    assert "example.com" not in representation
    assert "headers" not in representation


def test_proxy_primary_ip_is_not_treated_as_an_attested_origin_peer() -> None:
    response = FakeResponse()
    response.primary_ip = "93.184.216.34"

    document = normalize_response(response, strategy=FetchStrategy.HTTP)

    assert document.peer_ip is None
    assert "93.184.216.34" not in repr(document)


@pytest.mark.parametrize(
    ("raw_content_type", "normalized"),
    [
        ("APPLICATION/XHTML+XML; charset=utf-8", "application/xhtml+xml"),
        ("application/json", "application/json"),
        ("application/problem+json; profile=error", "application/problem+json"),
    ],
)
def test_supported_content_types_are_casefolded_and_parameters_are_stripped(
    raw_content_type: str, normalized: str
) -> None:
    response = FakeResponse(headers=FakeHeaders({"Content-Type": raw_content_type}))

    document = normalize_response(response, strategy=FetchStrategy.HTTP)

    assert document.content_type == normalized


@pytest.mark.parametrize(
    "raw_content_type",
    [
        "application/+json",
        "application/*+json",
        "application/problem json",
        "application/problem@json",
        "app lication/json",
        "application/",
        "/json",
    ],
)
def test_content_type_requires_full_type_subtype_token_grammar(raw_content_type: str) -> None:
    with pytest.raises(MonitorError, match="content type"):
        normalize_response(
            FakeResponse(headers=FakeHeaders({"Content-Type": raw_content_type})),
            strategy=FetchStrategy.HTTP,
        )


def test_navigation_redirect_may_have_an_empty_body_without_content_type() -> None:
    document = normalize_response(
        FakeResponse(
            status=302,
            headers=FakeHeaders({"Location": "https://example.com/next"}),
            body=b"",
        ),
        strategy=FetchStrategy.HTTP,
    )

    assert document.status == 302
    assert document.content_type == ""
    assert document.body == b""
    assert document.headers["location"] == "https://example.com/next"
    assert document.redirect_location == "https://example.com/next"


def test_navigation_redirect_resolves_one_relative_location() -> None:
    document = normalize_response(
        FakeResponse(
            status=301,
            headers=FakeHeaders({"Location": "../next"}),
            body=b"",
            url="https://example.com/path/source",
        ),
        strategy=FetchStrategy.HTTP,
    )

    assert document.redirect_location == "https://example.com/next"


@pytest.mark.parametrize(
    "headers",
    [
        FakeHeaders({}),
        FakeHeaders(
            {"Location": "/one"},
            multiple={"location": ["/one", "/two"]},
        ),
        FakeHeaders({"Location": "https://user:password@example.com/next"}),
        FakeHeaders({"Location": "/next\r\nX-Injected: yes"}),
        FakeHeaders({"Location": "/one, /two"}),
    ],
)
def test_navigation_redirect_requires_one_syntactically_safe_location(
    headers: FakeHeaders,
) -> None:
    with pytest.raises(MonitorError, match="Location") as caught:
        normalize_response(
            FakeResponse(status=302, headers=headers, body=b""),
            strategy=FetchStrategy.HTTP,
        )

    assert caught.value.error_class is ErrorClass.POLICY


def test_redirect_body_is_not_allowed_to_bypass_content_type_policy() -> None:
    with pytest.raises(MonitorError, match="content type") as caught:
        normalize_response(
            FakeResponse(
                status=307,
                headers=FakeHeaders(
                    {
                        "Location": "https://example.com/next",
                        "Content-Type": "application/octet-stream",
                    }
                ),
                body=b"binary",
            ),
            strategy=FetchStrategy.HTTP,
        )

    assert caught.value.error_class is ErrorClass.POLICY


def test_not_modified_is_not_treated_as_a_navigation_redirect() -> None:
    with pytest.raises(MonitorError, match="terminal status"):
        normalize_response(
            FakeResponse(status=304, headers=FakeHeaders({}), body=b""),
            strategy=FetchStrategy.HTTP,
        )


@pytest.mark.parametrize(
    ("headers", "match"),
    [
        (FakeHeaders({}), "Content-Type"),
        (FakeHeaders({"Content-Type": "text/plain"}), "content type"),
        (
            FakeHeaders(
                {"Content-Type": "text/html"},
                content_types=["text/html", "application/json"],
            ),
            "multiple Content-Type",
        ),
        (
            FakeHeaders({"Content-Type": "text/html", "content-type": "application/json"}),
            "multiple Content-Type",
        ),
        (FakeHeaders({"Content-Type": "text/html, application/json"}), "multiple Content-Type"),
    ],
)
def test_missing_invalid_or_multiple_content_types_are_rejected(
    headers: FakeHeaders, match: str
) -> None:
    with pytest.raises(MonitorError, match=match) as caught:
        normalize_response(FakeResponse(headers=headers), strategy=FetchStrategy.HTTP)

    assert caught.value.error_class is ErrorClass.POLICY
    assert caught.value.stage == "fetch"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"ok", b"ok"),
        (bytearray(b"ok"), b"ok"),
        (memoryview(b"ok"), b"ok"),
    ],
)
def test_bytes_like_response_bodies_are_copied(body: object, expected: bytes) -> None:
    document = normalize_response(FakeResponse(body=body), strategy=FetchStrategy.HTTP)
    assert document.body == expected


@pytest.mark.parametrize("body", ["text", None, 123])
def test_non_bytes_response_body_is_rejected(body: object) -> None:
    with pytest.raises(MonitorError, match="body") as caught:
        normalize_response(FakeResponse(body=body), strategy=FetchStrategy.HTTP)

    assert caught.value.error_class is ErrorClass.POLICY


def test_decompressed_response_size_boundary_is_enforced() -> None:
    accepted = normalize_response(
        FakeResponse(body=b"x" * MAX_RESPONSE_BYTES), strategy=FetchStrategy.HTTP
    )
    assert len(accepted.body) == MAX_RESPONSE_BYTES

    with pytest.raises(MonitorError, match="10 MiB") as caught:
        normalize_response(
            FakeResponse(body=b"x" * (MAX_RESPONSE_BYTES + 1)),
            strategy=FetchStrategy.HTTP,
        )

    assert caught.value.error_class is ErrorClass.POLICY


def test_http_callback_rejects_huge_chunk_before_retaining_it() -> None:
    collector = backend_module._BoundedBodyCollector(max_bytes=3)

    with pytest.raises(Exception, match="size limit"):
        collector(b"xxxx")

    assert collector.retained_bytes == 0


def test_http_callback_reports_every_consumed_byte_to_curl() -> None:
    collector = backend_module._BoundedBodyCollector(max_bytes=3)

    assert collector(b"abc") == 3
    assert collector.body == b"abc"


def test_http_callback_body_is_carried_into_normalization() -> None:
    def fetcher(_url: str, **kwargs: object) -> FakeResponse:
        callback = kwargs["content_callback"]
        assert callable(callback)
        callback(b"streamed-body")
        return FakeResponse(body=b"")

    backend = make_test_backend(http_fetcher=fetcher)

    result = asyncio.run(backend.fetch_http(target()))

    assert result.body == b"streamed-body"


def test_http_callback_size_abort_is_policy_not_transient() -> None:
    def fetcher(_url: str, **kwargs: object) -> FakeResponse:
        callback = kwargs["content_callback"]
        assert callable(callback)
        callback(b"x" * (MAX_RESPONSE_BYTES + 1))
        raise AssertionError("callback must abort first")

    backend = make_test_backend(http_fetcher=fetcher)

    with pytest.raises(MonitorError, match="10 MiB") as caught:
        asyncio.run(backend.fetch_http(target()))

    assert caught.value.error_class is ErrorClass.POLICY


@pytest.mark.parametrize(
    ("strategy", "status"),
    [
        (FetchStrategy.HTTP, 100),
        (FetchStrategy.HTTP, 304),
        (FetchStrategy.DYNAMIC, 199),
        (FetchStrategy.STEALTHY, 304),
    ],
)
def test_non_navigation_non_success_scrapling_status_fails_closed(
    strategy: FetchStrategy,
    status: int,
) -> None:
    with pytest.raises(MonitorError) as caught:
        normalize_response(
            FakeResponse(status=status),
            strategy=strategy,
        )

    assert caught.value.error_class is ErrorClass.POLICY


def test_official_total_timeout_includes_waiting_for_the_shared_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquired = 0
    for _ in range(4):
        if backend_module.HTTP_EGRESS_GATE.acquire(blocking=False):
            acquired += 1
    assert acquired == 4
    monkeypatch.setattr(policy_module, "TOTAL_TIMEOUT_SECONDS", 0.02, raising=False)
    client = BoundedPolicyHttpClient(egress_proxy_url="http://proxy.internal:8080")

    async def scenario() -> None:
        with pytest.raises(FetchError, match="timed out") as caught:
            await asyncio.wait_for(client.get_json(target()), timeout=0.2)
        assert caught.value.error_class is ErrorClass.TRANSIENT_NETWORK

    try:
        asyncio.run(scenario())
    finally:
        for _ in range(acquired):
            backend_module.HTTP_EGRESS_GATE.release()


def test_policy_http_requests_share_global_four_request_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_async_client = httpx.AsyncClient
    active = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=RawHttpxStream(b"{}"),
        )

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("proxy")
        return real_async_client(
            **kwargs,  # type: ignore[arg-type]
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(policy_module.httpx, "AsyncClient", client_factory)
    client = BoundedPolicyHttpClient(egress_proxy_url="http://proxy.internal:8080")

    async def scenario() -> None:
        await asyncio.gather(*(client.get_json(target()) for _ in range(8)))

    asyncio.run(scenario())

    assert peak == 4


def test_scrapling_and_policy_http_share_one_global_four_request_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_async_client = httpx.AsyncClient
    lock = threading.Lock()
    active = 0
    peak = 0

    def enter() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)

    def leave() -> None:
        nonlocal active
        with lock:
            active -= 1

    async def handler(_request: httpx.Request) -> httpx.Response:
        enter()
        await asyncio.sleep(0.03)
        leave()
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=RawHttpxStream(b"{}"),
        )

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("proxy")
        return real_async_client(
            **kwargs,  # type: ignore[arg-type]
            transport=httpx.MockTransport(handler),
        )

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        enter()
        sleep(0.03)
        leave()
        return FakeResponse()

    monkeypatch.setattr(policy_module.httpx, "AsyncClient", client_factory)
    client = BoundedPolicyHttpClient(egress_proxy_url="http://proxy.internal:8080")
    backend = make_test_backend(http_fetcher=fetcher)

    async def scenario() -> None:
        await asyncio.gather(
            *(client.get_json(target()) for _ in range(4)),
            *(backend.fetch_http(target()) for _ in range(4)),
        )

    asyncio.run(scenario())

    assert peak == 4


def test_error_response_is_size_bounded_before_status_classification() -> None:
    with pytest.raises(MonitorError, match="10 MiB") as caught:
        normalize_response(
            FakeResponse(status=500, body=b"x" * (MAX_RESPONSE_BYTES + 1)),
            strategy=FetchStrategy.HTTP,
        )

    assert not isinstance(caught.value, FetchError)


def test_error_response_headers_are_validated_before_status_classification() -> None:
    with pytest.raises(MonitorError, match="headers") as caught:
        normalize_response(
            FakeResponse(status=500, headers=object()),
            strategy=FetchStrategy.HTTP,
        )

    assert not isinstance(caught.value, FetchError)


@pytest.mark.parametrize(
    ("status", "error_class"),
    [
        (401, ErrorClass.AUTHENTICATION),
        (403, ErrorClass.AUTHENTICATION),
        (429, ErrorClass.TRANSIENT_NETWORK),
        (500, ErrorClass.TRANSIENT_NETWORK),
        (599, ErrorClass.TRANSIENT_NETWORK),
        (400, ErrorClass.POLICY),
        (404, ErrorClass.POLICY),
    ],
)
def test_failure_statuses_are_classified_without_leaking_response_data(
    status: int, error_class: ErrorClass
) -> None:
    secret = "do-not-leak-token"
    response = FakeResponse(
        status=status,
        body=secret.encode(),
        url=f"https://example.com/?token={secret}",
    )

    with pytest.raises(FetchError) as caught:
        normalize_response(response, strategy=FetchStrategy.HTTP)

    assert caught.value.error_class is error_class
    assert caught.value.stage == "fetch"
    assert caught.value.status == status
    assert caught.value.retry_after_seconds is None
    assert caught.value.detected_interstitial is False
    assert secret not in caught.value.safe_detail
    assert secret not in str(caught.value)


def test_retry_after_delay_seconds_and_http_date_are_exposed_safely() -> None:
    now = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
    for value, expected in (("120", 120.0), ("Thu, 23 Jul 2026 00:02:00 GMT", 120.0)):
        with pytest.raises(FetchError) as caught:
            normalize_response(
                FakeResponse(
                    status=429,
                    headers=FakeHeaders({"Content-Type": "text/plain", "Retry-After": value}),
                ),
                strategy=FetchStrategy.HTTP,
                clock=lambda: now,
            )

        assert caught.value.retry_after_seconds == expected
        assert repr(caught.value).find(value) == -1


def test_bounded_block_detector_distinguishes_interstitial_from_authentication() -> None:
    detector_calls: list[tuple[int, str, bytes]] = []

    def detector(status: int, content_type: str, body: bytes) -> bool:
        detector_calls.append((status, content_type, body))
        return b"challenge" in body

    body = b"challenge" + b"x" * MAX_DETECTOR_BYTES
    with pytest.raises(FetchError) as caught:
        normalize_response(
            FakeResponse(status=403, body=body),
            strategy=FetchStrategy.HTTP,
            block_page_detector=detector,
        )

    assert caught.value.error_class is ErrorClass.POLICY
    assert caught.value.detected_interstitial is True
    assert len(detector_calls[0][2]) == MAX_DETECTOR_BYTES

    with pytest.raises(FetchError) as ordinary:
        normalize_response(
            FakeResponse(status=403),
            strategy=FetchStrategy.HTTP,
            block_page_detector=lambda *_args: False,
        )
    assert ordinary.value.error_class is ErrorClass.AUTHENTICATION
    assert ordinary.value.detected_interstitial is False


def test_malformed_response_metadata_is_rejected_with_safe_error() -> None:
    secret = "do-not-leak-token"
    with pytest.raises(MonitorError) as caught:
        normalize_response(
            FakeResponse(url=f"https://example.com/?token={secret}", headers=object()),
            strategy=FetchStrategy.HTTP,
        )

    assert caught.value.error_class is ErrorClass.POLICY
    assert secret not in str(caught.value)


def test_redirect_history_is_bounded_validated_and_immutable() -> None:
    history = [
        FakeResponse(url="https://example.com/one"),
        FakeResponse(url="https://example.com/two"),
    ]
    document = normalize_response(
        FakeResponse(history=history),
        strategy=FetchStrategy.DYNAMIC,
    )

    assert document.redirect_urls == (
        "https://example.com/one",
        "https://example.com/two",
    )
    history[0].url = "https://example.com/changed"
    assert document.redirect_urls[0] == "https://example.com/one"

    with pytest.raises(MonitorError, match="redirect history"):
        normalize_response(
            FakeResponse(
                history=[FakeResponse(url=f"https://example.com/{index}") for index in range(6)]
            ),
            strategy=FetchStrategy.DYNAMIC,
        )
    with pytest.raises(MonitorError, match="redirect history"):
        normalize_response(
            FakeResponse(history=[FakeResponse(url="file:///etc/passwd")]),
            strategy=FetchStrategy.DYNAMIC,
        )


def test_production_backend_requires_an_egress_proxy() -> None:
    with pytest.raises(ValueError, match="egress proxy"):
        ScraplingBackend(egress_proxy_url=None)
    with pytest.raises(ValueError, match="egress proxy"):
        ScraplingBackend(egress_proxy_url="   ")
    with pytest.raises(TypeError, match="test_mode"):
        ScraplingBackend(  # type: ignore[call-arg]
            egress_proxy_url="http://proxy.test:8080",
            test_mode=True,
        )

    ScraplingBackend(egress_proxy_url="http://proxy.test:8080")


def test_production_backend_rejects_custom_fetchers_and_gates() -> None:
    def evil_fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        return FakeResponse()

    with pytest.raises(TypeError, match="http_fetcher"):
        ScraplingBackend(  # type: ignore[call-arg]
            egress_proxy_url="http://proxy.test:8080",
            http_fetcher=evil_fetcher,
        )
    with pytest.raises(TypeError, match="http_gate"):
        ScraplingBackend(  # type: ignore[call-arg]
            egress_proxy_url="http://proxy.test:8080",
            http_gate=threading.BoundedSemaphore(1),
        )


def test_production_backend_execution_policy_cannot_be_replaced() -> None:
    backend = ScraplingBackend(egress_proxy_url="http://proxy.test:8080")

    with pytest.raises(AttributeError):
        backend._http_fetcher = lambda *_args, **_kwargs: FakeResponse()
    with pytest.raises(AttributeError):
        backend._http_gate = threading.BoundedSemaphore(1)


@pytest.mark.parametrize(
    ("method_name", "fetcher_attribute"),
    [
        ("fetch_http", "_http_fetcher"),
        ("fetch_dynamic", "_dynamic_fetcher"),
        ("fetch_stealthy", "_stealthy_fetcher"),
    ],
)
def test_direct_public_backend_rejects_mutated_fetcher_before_execution(
    method_name: str,
    fetcher_attribute: str,
) -> None:
    executed = False

    def evil_fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        nonlocal executed
        executed = True
        return FakeResponse()

    backend = ScraplingBackend(egress_proxy_url="http://proxy.test:8080")
    object.__setattr__(backend, fetcher_attribute, evil_fetcher)

    with pytest.raises(MonitorError, match="backend policy") as caught:
        asyncio.run(getattr(backend, method_name)(target()))

    assert caught.value.error_class is ErrorClass.POLICY
    assert executed is False


def test_direct_public_backend_rejects_mutated_gate_before_execution() -> None:
    executed = False

    class ExplodingGate:
        def acquire(self, *, blocking: bool) -> bool:
            nonlocal executed
            assert blocking is False
            executed = True
            raise AssertionError("mutated gate executed")

        def release(self) -> None:
            raise AssertionError("mutated gate released")

    backend = ScraplingBackend(egress_proxy_url="http://proxy.test:8080")
    object.__setattr__(backend, "_http_gate", ExplodingGate())

    with pytest.raises(MonitorError, match="backend policy") as caught:
        asyncio.run(backend.fetch_http(target()))

    assert caught.value.error_class is ErrorClass.POLICY
    assert executed is False


def test_direct_public_backend_rejects_mutated_timeout_before_execution() -> None:
    backend = ScraplingBackend(egress_proxy_url="http://proxy.test:8080")
    object.__setattr__(backend, "_http_timeout_seconds", -1.0)

    with pytest.raises(MonitorError, match="backend policy") as caught:
        asyncio.run(backend.fetch_http(target()))

    assert caught.value.error_class is ErrorClass.POLICY


def test_direct_public_backend_rejects_mutated_proxy_policy_before_execution() -> None:
    secret = "proxy-password-secret"
    backend = ScraplingBackend(egress_proxy_url="http://proxy.test:8080")
    object.__setattr__(
        backend._egress_policy,
        "_url",
        f"http://user:{secret}@changed-proxy.test:8080",
    )

    with pytest.raises(MonitorError, match="backend policy") as caught:
        asyncio.run(backend.fetch_http(target()))

    assert caught.value.error_class is ErrorClass.POLICY
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_backend_snapshot_owners_use_identity_and_are_weakly_held() -> None:
    def clock() -> datetime:
        return datetime.now(UTC)

    first = ScraplingBackend(egress_proxy_url="http://proxy.test:8080", clock=clock)
    second = ScraplingBackend(egress_proxy_url="http://proxy.test:8080", clock=clock)

    assert first is not second
    assert first != second
    assert first.is_policy_sealed
    assert second.is_policy_sealed
    object.__setattr__(first._egress_policy, "_url", "http://changed.test:8080")
    assert not first.is_policy_sealed
    assert second.is_policy_sealed
    reference = weakref.ref(first)
    del first
    gc.collect()

    assert reference() is None
    third = ScraplingBackend(egress_proxy_url="http://proxy.test:8080", clock=clock)
    assert third != second
    assert third.is_policy_sealed
    assert second.is_policy_sealed


@pytest.mark.parametrize(
    "proxy",
    [
        "ftp://proxy.example:21",
        "socks4://proxy.example:1080",
        "socks5://proxy.example:1080",
        "http://",
        "http://proxy.example:not-a-port",
        "http://proxy.example/path",
        "http://proxy.example?secret=value",
        "http://proxy.example:8080\r\n",
        "http://proxy.internal:",
        "http://proxy.internal:/",
    ],
)
def test_proxy_url_is_syntax_checked_without_leaking_credentials(proxy: str) -> None:
    with pytest.raises(ValueError, match="proxy URL") as caught:
        ScraplingBackend(egress_proxy_url=proxy)

    assert "secret=value" not in str(caught.value)


def test_http_fetch_uses_approved_url_and_bounded_fetcher_kwargs() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fetcher(url: str, **kwargs: object) -> FakeResponse:
        calls.append((url, kwargs))
        return FakeResponse()

    backend = make_test_backend(
        egress_proxy_url="http://proxy.internal:8080",
        http_fetcher=fetcher,
    )

    document = asyncio.run(backend.fetch_http(target()))

    assert document.strategy is FetchStrategy.HTTP
    callback = calls[0][1].pop("content_callback")
    assert callable(callback)
    assert calls == [
        (
            "https://example.com/source",
            {
                "timeout": (10, 20),
                "follow_redirects": False,
                "max_redirects": 0,
                "retries": 1,
                "proxy": "http://proxy.internal:8080",
                "headers": {"Accept-Encoding": "identity"},
                "accept_encoding": "identity",
                "selector_config": {
                    "adaptive": False,
                    "keep_comments": False,
                    "keep_cdata": False,
                },
            },
        )
    ]

    with FetcherSession() as session:
        merged = session._merge_request_args(  # type: ignore[attr-defined]
            url="https://example.com/source",
            timeout=calls[0][1]["timeout"],
            follow_redirects=False,
            max_redirects=0,
            proxy="http://proxy.internal:8080",
        )
    assert merged["timeout"] == (10, 20)
    assert merged["allow_redirects"] is False

    class RecordingCurl:
        _skip_cacert = False

        def __init__(self) -> None:
            self.options: dict[object, object] = {}

        def setopt(self, option: object, value: object) -> None:
            self.options[option] = value

        def impersonate(self, *_args: object, **_kwargs: object) -> None:
            return None

    curl = RecordingCurl()
    set_curl_options(
        curl,  # type: ignore[arg-type]
        "GET",
        "https://example.com/source",
        params_list=[None, None],
        headers_list=[None, None],
        cookies_list=[None, None],
        proxies_list=[None, None],
        verify_list=[None, None],
        timeout=merged["timeout"],
        allow_redirects=False,
    )
    assert curl.options[CurlOpt.CONNECTTIMEOUT_MS] == 10_000
    assert curl.options[CurlOpt.TIMEOUT_MS] == 30_000


def test_http_fetch_does_not_require_scrapling_default_adaptive_database() -> None:
    def fetcher(_url: str, **kwargs: object) -> FakeResponse:
        selector_config = kwargs["selector_config"]
        assert isinstance(selector_config, dict)
        if selector_config.get("adaptive") is not False:
            raise PermissionError("package storage is read-only")
        return FakeResponse()

    backend = make_test_backend(
        egress_proxy_url="http://proxy.internal:8080",
        http_fetcher=fetcher,
    )

    document = asyncio.run(backend.fetch_http(target()))

    assert document.status == 200


@pytest.mark.parametrize(
    ("method_name", "strategy"),
    [
        ("fetch_dynamic", FetchStrategy.DYNAMIC),
        ("fetch_stealthy", FetchStrategy.STEALTHY),
    ],
)
def test_browser_fetch_uses_profile_and_bounded_fetcher_kwargs(
    tmp_path: Path, method_name: str, strategy: FetchStrategy
) -> None:
    calls: list[tuple[FetchStrategy, str, dict[str, object]]] = []
    events: list[str] = []
    process = FakeProfileProcess(events, exits_on_join=True)
    connection = FakeProfileConnection(
        backend_module._serialize_profile_response(FakeResponse()),
        events,
    )
    install_fake_profile_session(process, connection, calls)

    backend = make_test_backend(
        egress_proxy_url="http://proxy.internal:8080",
    )

    document = asyncio.run(getattr(backend, method_name)(target(), profile=tmp_path))

    assert document.strategy is strategy
    expected_kwargs: dict[str, object] = {
        "timeout": 90_000,
        "headless": True,
        "network_idle": True,
        "disable_resources": True,
        "block_ads": True,
        "google_search": False,
        "dns_over_https": False,
        "retries": 1,
        "proxy": "http://proxy.internal:8080",
        "user_data_dir": str(tmp_path),
        "selector_config": {
            "adaptive": False,
            "keep_comments": False,
            "keep_cdata": False,
        },
    }
    if strategy is FetchStrategy.STEALTHY:
        expected_kwargs.update(solve_cloudflare=False, block_webrtc=True)
    else:
        expected_kwargs["extra_flags"] = [
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--force-webrtc-ip-handling-policy",
        ]
    assert calls == [(strategy, "https://example.com/source", expected_kwargs)]


@pytest.mark.parametrize("method_name", ["fetch_dynamic", "fetch_stealthy"])
def test_browser_fetch_omits_profile_when_absent_and_passes_real_validator(
    method_name: str,
) -> None:
    calls: list[dict[str, object]] = []

    def fetcher(_url: str, **kwargs: object) -> FakeResponse:
        calls.append(kwargs)
        return FakeResponse()

    backend = make_test_backend(
        egress_proxy_url="http://proxy.internal:8080",
        dynamic_fetcher=fetcher,
        stealthy_fetcher=fetcher,
    )

    asyncio.run(getattr(backend, method_name)(target()))

    assert "user_data_dir" not in calls[0]
    session_type = DynamicSession if method_name == "fetch_dynamic" else StealthySession
    session = session_type(**calls[0])
    assert session._config.user_data_dir == ""  # type: ignore[attr-defined]


def test_http_fetch_timeout_is_transient_and_secret_safe() -> None:
    release = threading.Event()

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        release.wait(timeout=1)
        return FakeResponse()

    backend = make_test_backend(
        http_fetcher=fetcher,
        http_timeout_seconds=0.01,
    )

    async def run() -> MonitorError:
        secret = "do-not-leak-token"
        with pytest.raises(MonitorError) as caught:
            await backend.fetch_http(target(f"https://example.com/?token={secret}"))
        release.set()
        await asyncio.sleep(0.01)
        return caught.value

    try:
        error = asyncio.run(run())
    finally:
        release.set()

    assert error.error_class is ErrorClass.TRANSIENT_NETWORK
    assert error.stage == "fetch"
    assert "do-not-leak-token" not in str(error)


def test_fetcher_failure_is_transient_and_secret_safe() -> None:
    secret = "do-not-leak-token"

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        raise RuntimeError(secret)

    backend = make_test_backend(http_fetcher=fetcher)

    with pytest.raises(MonitorError) as caught:
        asyncio.run(backend.fetch_http(target()))

    assert caught.value.error_class is ErrorClass.TRANSIENT_NETWORK
    assert caught.value.stage == "fetch"
    assert secret not in str(caught.value)


def test_timeout_keeps_the_concurrency_slot_until_the_thread_finishes() -> None:
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        nonlocal calls
        with lock:
            calls += 1
        entered.set()
        release.wait(timeout=1)
        return FakeResponse()

    backend = make_test_backend(
        http_fetcher=fetcher,
        http_gate=threading.BoundedSemaphore(1),
        http_timeout_seconds=0.05,
    )

    async def run() -> None:
        first = asyncio.create_task(backend.fetch_http(target()))
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(MonitorError) as first_error:
            await first
        with pytest.raises(MonitorError) as second_error:
            await backend.fetch_http(target())
        assert first_error.value.error_class is ErrorClass.TRANSIENT_NETWORK
        assert second_error.value.error_class is ErrorClass.TRANSIENT_NETWORK
        with lock:
            assert calls == 1
        release.set()
        await asyncio.sleep(0.01)

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_event_loop_shutdown_keeps_gate_until_orphan_thread_finishes() -> None:
    gate = threading.BoundedSemaphore(1)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        nonlocal calls
        with lock:
            calls += 1
        entered.set()
        release.wait(timeout=2)
        return FakeResponse()

    def run_once() -> None:
        with suppress(MonitorError):
            asyncio.run(
                make_test_backend(
                    http_fetcher=fetcher,
                    http_gate=gate,
                    http_timeout_seconds=0.03,
                ).fetch_http(target())
            )

    first = threading.Thread(target=run_once)
    second = threading.Thread(target=run_once)
    first.start()
    assert entered.wait(timeout=1)
    sleep(0.06)
    second.start()
    sleep(0.08)
    with lock:
        assert calls == 1
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()


def test_outer_cancellation_is_not_swallowed() -> None:
    started = threading.Event()
    release = threading.Event()

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        started.set()
        release.wait(timeout=1)
        return FakeResponse()

    backend = make_test_backend(http_fetcher=fetcher)

    async def run() -> None:
        task = asyncio.create_task(backend.fetch_http(target()))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(run())


@pytest.mark.parametrize("exit_kind", ["timeout", "cancellation"])
def test_profiled_exit_stops_worker_and_cleans_before_context_return(
    tmp_path: Path, exit_kind: str
) -> None:
    source = tmp_path / "source-profile"
    source.mkdir(mode=0o700)
    (source / "Cookies").write_bytes(b"initial-cookie")
    vault = CredentialVault(tmp_path / "vault", key=b"k" * 32)
    profiles = BrowserProfileStore(vault, materialization_root=tmp_path / "workspaces")
    profiles.archive("profile", source)
    gate = threading.BoundedSemaphore(1)
    events: list[str] = []
    process = FakeProfileProcess(
        events,
        exits_on_join=False,
        resists_terminate=True,
    )
    connection = FakeProfileConnection(None, events)
    install_fake_profile_session(process, connection)

    backend = make_test_backend(
        browser_gate=gate,
        browser_timeout_seconds=0.03,
    )

    async def scenario() -> None:
        workspace: Path | None = None
        async with profiles.materialize("profile") as active_workspace:
            workspace = active_workspace
            if exit_kind == "timeout":
                with pytest.raises(MonitorError, match="timed out"):
                    await backend.fetch_dynamic(target(), profile=active_workspace)
            else:
                task = asyncio.create_task(
                    backend.fetch_dynamic(target(), profile=active_workspace)
                )
                while "start" not in events:
                    await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert workspace is not None and not workspace.exists()
        assert gate.acquire(blocking=False)
        gate.release()
        async with profiles.materialize("profile"):
            pass

    asyncio.run(scenario())

    assert not process.is_alive()
    assert events == [
        "start",
        "terminate",
        "join",
        "kill",
        "join",
        "process.close",
        "connection.close",
    ]


def test_profiled_normal_response_uses_primitive_wire(tmp_path: Path) -> None:
    events: list[str] = []
    process = FakeProfileProcess(events, exits_on_join=True)
    wire = backend_module._serialize_profile_response(FakeResponse())
    connection = FakeProfileConnection(wire, events)
    install_fake_profile_session(process, connection)
    backend = make_test_backend()

    document = asyncio.run(backend.fetch_dynamic(target(), profile=tmp_path))

    assert document.status == 200
    assert document.body == b"<html><h1>ok</h1></html>"
    assert document.strategy is FetchStrategy.DYNAMIC
    assert events == [
        "start",
        "terminate",
        "join",
        "process.close",
        "connection.close",
    ]


def test_profiled_response_still_terminates_hung_child_before_return(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    process = FakeProfileProcess(
        events,
        exits_on_join=False,
        resists_terminate=True,
    )
    connection = FakeProfileConnection(
        backend_module._serialize_profile_response(FakeResponse()),
        events,
    )
    install_fake_profile_session(process, connection)
    backend = make_test_backend()

    document = asyncio.run(backend.fetch_dynamic(target(), profile=tmp_path))

    assert document.status == 200
    assert not process.is_alive()
    assert events == [
        "start",
        "terminate",
        "join",
        "kill",
        "join",
        "process.close",
        "connection.close",
    ]


def test_profiled_start_failure_closes_process_connection_and_gate(tmp_path: Path) -> None:
    events: list[str] = []
    gate = threading.BoundedSemaphore(1)
    process = FakeProfileProcess(
        events,
        exits_on_join=False,
        raises_on_start=True,
    )
    connection = FakeProfileConnection(None, events)
    install_fake_profile_session(process, connection)
    backend = make_test_backend(
        browser_gate=gate,
    )

    with pytest.raises(MonitorError, match="fetch failed") as caught:
        asyncio.run(backend.fetch_dynamic(target(), profile=tmp_path))

    assert caught.value.error_class is ErrorClass.TRANSIENT_NETWORK
    assert events == ["start", "process.close", "connection.close"]
    assert gate.acquire(blocking=False)
    gate.release()


@pytest.mark.parametrize(
    ("wire", "expected_type", "expected_class"),
    [
        (("failure",), MonitorError, ErrorClass.TRANSIENT_NETWORK),
        (
            (
                "response",
                401,
                "https://example.com/final",
                (),
                (("content-type", ("text/html",)),),
                b"",
            ),
            FetchError,
            ErrorClass.AUTHENTICATION,
        ),
    ],
)
def test_profiled_error_and_status_wires_are_reconstructed_safely(
    tmp_path: Path,
    wire: object,
    expected_type: type[MonitorError],
    expected_class: ErrorClass,
) -> None:
    events: list[str] = []
    process = FakeProfileProcess(events, exits_on_join=True)
    connection = FakeProfileConnection(wire, events)
    install_fake_profile_session(process, connection)
    backend = make_test_backend()

    with pytest.raises(expected_type) as caught:
        asyncio.run(backend.fetch_dynamic(target(), profile=tmp_path))

    assert caught.value.error_class is expected_class
    assert not process.is_alive()


@pytest.mark.parametrize(
    ("method_name", "limit"),
    [("fetch_http", 4), ("fetch_dynamic", 1), ("fetch_stealthy", 1)],
)
def test_fetch_concurrency_is_bounded(method_name: str, limit: int) -> None:
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    maximum = 0
    target_calls = limit + 2

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait(timeout=2)
        with lock:
            active -= 1
        return FakeResponse()

    backend = make_test_backend(
        http_fetcher=fetcher,
        dynamic_fetcher=fetcher,
        stealthy_fetcher=fetcher,
    )

    async def run() -> None:
        tasks = [
            asyncio.create_task(getattr(backend, method_name)(target()))
            for _ in range(target_calls)
        ]
        try:
            while True:
                with lock:
                    if maximum >= limit:
                        break
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.01)
        finally:
            release.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())

    assert maximum == limit


def test_dynamic_and_stealthy_share_one_browser_semaphore() -> None:
    lock = threading.Lock()
    release = threading.Event()
    first_started = threading.Event()
    active = 0
    maximum = 0

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        first_started.set()
        release.wait(timeout=2)
        with lock:
            active -= 1
        return FakeResponse()

    backend = make_test_backend(
        dynamic_fetcher=fetcher,
        stealthy_fetcher=fetcher,
    )

    async def run() -> None:
        dynamic = asyncio.create_task(backend.fetch_dynamic(target()))
        stealthy = asyncio.create_task(backend.fetch_stealthy(target()))
        await asyncio.to_thread(first_started.wait, 1)
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.gather(dynamic, stealthy)

    asyncio.run(run())

    assert maximum == 1


def test_default_browser_semaphore_is_shared_across_backend_instances() -> None:
    lock = threading.Lock()
    release = threading.Event()
    first_started = threading.Event()
    active = 0
    maximum = 0

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        first_started.set()
        release.wait(timeout=2)
        with lock:
            active -= 1
        return FakeResponse()

    first = make_test_backend(dynamic_fetcher=fetcher)
    second = make_test_backend(dynamic_fetcher=fetcher)

    async def run() -> None:
        one = asyncio.create_task(first.fetch_dynamic(target()))
        two = asyncio.create_task(second.fetch_dynamic(target()))
        await asyncio.to_thread(first_started.wait, 1)
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.gather(one, two)

    asyncio.run(run())

    assert maximum == 1


def test_default_browser_gate_is_process_wide_across_threads_and_event_loops() -> None:
    lock = threading.Lock()
    release = threading.Event()
    first_started = threading.Event()
    active = 0
    maximum = 0
    failures: list[BaseException] = []

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        first_started.set()
        release.wait(timeout=2)
        with lock:
            active -= 1
        return FakeResponse()

    def run_backend() -> None:
        try:
            asyncio.run(make_test_backend(dynamic_fetcher=fetcher).fetch_dynamic(target()))
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=run_backend) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert first_started.wait(timeout=1)
    sleep(0.05)
    with lock:
        assert maximum == 1
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []


@pytest.mark.filterwarnings("ignore:The 'strip_cdata' option of HTMLParser")
def test_scrapling_context_logger_does_not_emit_secret_urls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "do-not-log-this-token"

    def fetcher(url: str, **_kwargs: object) -> ScraplingResponse:
        return ScraplingResponse(
            url=url,
            content=b"<html></html>",
            status=200,
            reason="OK",
            cookies={},
            headers={"content-type": "text/html"},
            request_headers={},
        )

    caplog.set_level(logging.INFO)
    document = asyncio.run(
        make_test_backend(http_fetcher=fetcher).fetch_http(
            target(f"https://example.com/?token={secret}")
        )
    )

    assert secret not in caplog.text
    assert secret not in repr(document)


def test_proxy_credentials_are_absent_from_logs_and_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "proxy-password-secret"

    def fetcher(_url: str, **kwargs: object) -> FakeResponse:
        from scrapling.core.utils import log

        log.error("proxy failure: %s", kwargs["proxy"])
        raise RuntimeError(kwargs["proxy"])

    backend = make_test_backend(
        egress_proxy_url=f"http://user:{secret}@proxy.internal:8080",
        http_fetcher=fetcher,
    )

    with pytest.raises(MonitorError) as caught:
        asyncio.run(backend.fetch_http(target()))

    assert secret not in caplog.text
    assert secret not in str(caught.value)
