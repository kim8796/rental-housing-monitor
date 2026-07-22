from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from time import sleep

import pytest
from scrapling.engines._browsers._controllers import DynamicSession
from scrapling.engines._browsers._stealth import StealthySession
from scrapling.engines.static import FetcherSession
from scrapling.engines.toolbelt.custom import Response as ScraplingResponse

from personal_monitor.domain.spec import FetchStrategy
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.scrapling_backend import (
    MAX_DETECTOR_BYTES,
    MAX_RESPONSE_BYTES,
    FetchError,
    ScraplingBackend,
    normalize_response,
)
from personal_monitor.security.url_policy import ResolvedTarget


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


def target(url: str = "https://example.com/source") -> ResolvedTarget:
    return ResolvedTarget(
        normalized_url=url,
        hostname="example.com",
        port=443,
        addresses=frozenset({"93.184.216.34"}),
    )


def noop_fetcher(_url: str, **_kwargs: object) -> FakeResponse:
    return FakeResponse()


def make_test_backend(**overrides: object) -> ScraplingBackend:
    arguments: dict[str, object] = {
        "egress_proxy_url": None,
        "test_mode": True,
        "http_fetcher": noop_fetcher,
        "dynamic_fetcher": noop_fetcher,
        "stealthy_fetcher": noop_fetcher,
    }
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
    with pytest.raises(MonitorError, match="Content-Type"):
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
    with pytest.raises(ValueError, match="test fetchers"):
        ScraplingBackend(egress_proxy_url=None, test_mode=True)
    with pytest.raises(ValueError, match="test fetchers"):
        ScraplingBackend(
            egress_proxy_url=None,
            test_mode=True,
            http_fetcher=noop_fetcher,
        )

    make_test_backend()


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

    backend = ScraplingBackend(
        egress_proxy_url="http://proxy.internal:8080",
        http_fetcher=fetcher,
    )

    document = asyncio.run(backend.fetch_http(target()))

    assert document.strategy is FetchStrategy.HTTP
    assert calls == [
        (
            "https://example.com/source",
            {
                "timeout": (10, 30),
                "follow_redirects": False,
                "max_redirects": 0,
                "retries": 1,
                "proxy": "http://proxy.internal:8080",
                "headers": {"Accept-Encoding": "identity"},
                "selector_config": {
                    "adaptive": True,
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
    assert merged["timeout"] == (10, 30)
    assert merged["allow_redirects"] is False


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
    calls: list[tuple[str, dict[str, object]]] = []

    def fetcher(url: str, **kwargs: object) -> FakeResponse:
        calls.append((url, kwargs))
        return FakeResponse()

    backend = ScraplingBackend(
        egress_proxy_url="http://proxy.internal:8080",
        dynamic_fetcher=fetcher,
        stealthy_fetcher=fetcher,
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
            "adaptive": True,
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
    assert calls == [("https://example.com/source", expected_kwargs)]


@pytest.mark.parametrize("method_name", ["fetch_dynamic", "fetch_stealthy"])
def test_browser_fetch_omits_profile_when_absent_and_passes_real_validator(
    method_name: str,
) -> None:
    calls: list[dict[str, object]] = []

    def fetcher(_url: str, **kwargs: object) -> FakeResponse:
        calls.append(kwargs)
        return FakeResponse()

    backend = ScraplingBackend(
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

    backend = ScraplingBackend(
        egress_proxy_url=f"http://user:{secret}@proxy.internal:8080",
        http_fetcher=fetcher,
    )

    with pytest.raises(MonitorError) as caught:
        asyncio.run(backend.fetch_http(target()))

    assert secret not in caplog.text
    assert secret not in str(caught.value)
