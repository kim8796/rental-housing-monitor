from __future__ import annotations

import asyncio
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from personal_monitor.domain.spec import FetchStrategy
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.scrapling_backend import (
    MAX_RESPONSE_BYTES,
    ScraplingBackend,
    normalize_response,
)
from personal_monitor.security.url_policy import ResolvedTarget


class FakeHeaders(dict[str, str]):
    def __init__(self, values: dict[str, str], *, content_types: list[str] | None = None) -> None:
        super().__init__(values)
        self._content_types = content_types

    def get_list(self, name: str) -> list[str]:
        if name.casefold() != "content-type":
            return []
        if self._content_types is not None:
            return list(self._content_types)
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


def test_response_is_bounded_normalized_and_immutable() -> None:
    response = FakeResponse()

    document = normalize_response(response, strategy=FetchStrategy.HTTP)

    assert document.final_url == "https://example.com/final"
    assert document.status == 200
    assert document.content_type == "text/html"
    assert document.body == b"<html><h1>ok</h1></html>"
    assert document.strategy is FetchStrategy.HTTP
    assert document.redirect_urls == ()
    assert document.headers == {"Content-Type": "text/html; charset=UTF-8", "X-Test": "yes"}
    response.headers["X-Test"] = "changed"  # type: ignore[index]
    assert document.headers["X-Test"] == "yes"
    with pytest.raises(TypeError):
        document.headers["new"] = "value"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        document.status = 201  # type: ignore[misc]


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
    assert document.headers["Location"] == "https://example.com/next"


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

    with pytest.raises(MonitorError) as caught:
        normalize_response(response, strategy=FetchStrategy.HTTP)

    assert caught.value.error_class is error_class
    assert caught.value.stage == "fetch"
    assert secret not in caught.value.safe_detail
    assert secret not in str(caught.value)


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

    ScraplingBackend(egress_proxy_url=None, test_mode=True)


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
                "timeout": 30,
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
    assert calls == [("https://example.com/source", expected_kwargs)]


def test_http_fetch_timeout_is_transient_and_secret_safe() -> None:
    release = threading.Event()

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        release.wait(timeout=1)
        return FakeResponse()

    backend = ScraplingBackend(
        egress_proxy_url=None,
        test_mode=True,
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

    backend = ScraplingBackend(egress_proxy_url=None, test_mode=True, http_fetcher=fetcher)

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

    backend = ScraplingBackend(
        egress_proxy_url=None,
        test_mode=True,
        http_fetcher=fetcher,
        http_semaphore=asyncio.Semaphore(1),
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


def test_outer_cancellation_is_not_swallowed() -> None:
    started = threading.Event()
    release = threading.Event()

    def fetcher(_url: str, **_kwargs: object) -> FakeResponse:
        started.set()
        release.wait(timeout=1)
        return FakeResponse()

    backend = ScraplingBackend(egress_proxy_url=None, test_mode=True, http_fetcher=fetcher)

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

    backend = ScraplingBackend(
        egress_proxy_url=None,
        test_mode=True,
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

    backend = ScraplingBackend(
        egress_proxy_url=None,
        test_mode=True,
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

    first = ScraplingBackend(
        egress_proxy_url=None,
        test_mode=True,
        dynamic_fetcher=fetcher,
    )
    second = ScraplingBackend(
        egress_proxy_url=None,
        test_mode=True,
        dynamic_fetcher=fetcher,
    )

    async def run() -> None:
        one = asyncio.create_task(first.fetch_dynamic(target()))
        two = asyncio.create_task(second.fetch_dynamic(target()))
        await asyncio.to_thread(first_started.wait, 1)
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.gather(one, two)

    asyncio.run(run())

    assert maximum == 1
