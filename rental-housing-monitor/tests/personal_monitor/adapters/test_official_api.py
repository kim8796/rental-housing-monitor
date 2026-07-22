from __future__ import annotations

import asyncio
import gzip
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

import personal_monitor.adapters._policy as policy_module
from personal_monitor.adapters.official_api import (
    MONITOR_USER_AGENT,
    BoundedPolicyHttpClient,
    OfficialJsonAdapter,
)
from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.scrapling_backend import MAX_RESPONSE_BYTES
from personal_monitor.security.url_policy import UrlPolicy
from tests.personal_monitor.adapters._helpers import make_policy_client

NOW = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)


class Resolver:
    async def resolve(self, hostname: str, port: int) -> list[str]:
        assert port in {80, 443}
        return ["93.184.216.34"]


class RecordingRateLimiter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    async def acquire(self, host: str, retry_after_seconds: float | None = None) -> None:
        self.calls.append((host, retry_after_seconds))


def official_spec(**overrides: object) -> MonitorSpec:
    payload: dict[str, object] = {
        "schema_version": 1,
        "owner_id": "owner-1",
        "name": "official JSON",
        "target_url": "https://example.com/products",
        "source_adapter": "official_api",
        "adapter_ref": "json_get",
        "fetch_strategy": "http",
        "extract": {
            "item_scope": "/products",
            "fields": {
                "source_id": {"selector": "/id", "type": "text"},
                "price": {"selector": "/price", "type": "integer"},
            },
        },
        "validators": {"min_items": 1, "max_items": 3},
        "rules": [{"kind": "new_item"}],
    }
    payload.update(overrides)
    return MonitorSpec.model_validate(payload)


def test_official_json_uses_only_fixed_get_headers_and_policy_proxy() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="Allow: /")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"products":[{"id":"sku-1","price":99}]}',
        )

    client = make_policy_client(handler)
    rate = RecordingRateLimiter()
    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=rate,
        http_client=client,
        clock=lambda: NOW,
    )

    batch = asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert [request.url.path for request in requests] == ["/robots.txt", "/products"]
    assert all(request.method == "GET" for request in requests)
    target_headers = dict(requests[1].headers)
    target_headers.pop("host")
    assert target_headers == {
        "user-agent": MONITOR_USER_AGENT,
        "accept": "application/json",
        "accept-encoding": "identity",
    }
    assert requests[1].extensions["timeout"] == {
        "connect": 10.0,
        "read": 30.0,
        "write": 30.0,
        "pool": 30.0,
    }
    assert rate.calls == [
        ("example.com", None),
        ("example.com", None),
    ]
    assert batch.monitor_id == "monitor-1"
    assert batch.observed_at == NOW
    assert batch.items[0].fields == {"source_id": "sku-1", "price": 99}
    assert len(batch.source_hash) == 64
    assert "products" not in repr(batch)


def test_proxy_socket_peer_is_not_misclassified_as_the_origin_peer() -> None:
    class ProxyStream:
        def get_extra_info(self, name: str):
            assert name == "server_addr"
            return ("192.0.2.10", 8080)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain"},
                text="User-agent: *\nAllow: /\n",
                extensions={"network_stream": ProxyStream()},
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"products":[{"id":"sku-1","price":99}]}',
            extensions={"network_stream": ProxyStream()},
        )

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    assert asyncio.run(adapter.fetch("monitor-1", official_spec())).monitor_id == "monitor-1"


def test_official_client_requires_proxy_and_has_no_arbitrary_request_api() -> None:
    with pytest.raises(ValueError, match="egress proxy"):
        BoundedPolicyHttpClient(egress_proxy_url=None)
    with pytest.raises(ValueError, match="egress proxy"):
        BoundedPolicyHttpClient(egress_proxy_url=" ")

    client = make_policy_client(lambda _request: httpx.Response(204))
    assert not hasattr(client, "request")
    assert not hasattr(client, "post")


def test_production_client_has_no_public_test_transport_factory() -> None:
    assert not hasattr(BoundedPolicyHttpClient, "for_test")

    with pytest.raises(TypeError, match="transport"):
        BoundedPolicyHttpClient(  # type: ignore[call-arg]
            egress_proxy_url="http://proxy.internal:8080",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
        )


def test_policy_client_proxy_identity_cannot_be_replaced() -> None:
    client = BoundedPolicyHttpClient(egress_proxy_url="http://proxy.internal:8080")
    other = BoundedPolicyHttpClient(egress_proxy_url="http://other-proxy.internal:8080")

    with pytest.raises((AttributeError, TypeError)):
        client._proxy_identity = other.proxy_identity
    with pytest.raises((AttributeError, TypeError)):
        client._proxy_url = "http://other-proxy.internal:8080"


def test_official_constructor_rejects_policy_client_subclasses() -> None:
    class SubclassedClient(BoundedPolicyHttpClient):
        pass

    with pytest.raises(TypeError, match="http_client"):
        OfficialJsonAdapter(
            url_policy=UrlPolicy(Resolver()),
            rate_limiter=RecordingRateLimiter(),
            http_client=SubclassedClient(egress_proxy_url="http://proxy.internal:8080"),
            clock=lambda: NOW,
        )


@pytest.mark.parametrize(
    ("headers", "body", "match"),
    [
        ({"Content-Type": "text/html"}, b"<html>secret</html>", "JSON"),
        ({"Content-Type": "application/octet-stream"}, b"binary", "JSON"),
        ({}, b"{}", "Content-Type"),
        ({"Content-Type": "application/json"}, b"not-json", "JSON document"),
    ],
)
def test_official_rejects_non_json_or_malformed_responses_safely(
    headers: dict[str, str], body: bytes, match: str
) -> None:
    secret = "response-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="Allow: /")
        return httpx.Response(200, headers=headers, content=body + secret.encode())

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    with pytest.raises(MonitorError, match=match) as caught:
        asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert caught.value.error_class in {ErrorClass.POLICY, ErrorClass.STRUCTURE}
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_official_rejects_oversized_body_before_extraction() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="Allow: /")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b"x" * (MAX_RESPONSE_BYTES + 1),
        )

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    with pytest.raises(MonitorError, match="10 MiB") as caught:
        asyncio.run(adapter.fetch("monitor-1", official_spec()))
    assert caught.value.error_class is ErrorClass.POLICY


def test_bounded_body_rejects_one_huge_chunk_before_retaining_it() -> None:
    accumulator = policy_module._BoundedBodyAccumulator(max_bytes=3)

    with pytest.raises(MonitorError, match="10 MiB"):
        accumulator.extend(b"xxxx")

    assert accumulator.retained_bytes == 0


def test_official_rejects_gzip_body_over_decompressed_limit() -> None:
    compressed = gzip.compress(b"x" * (MAX_RESPONSE_BYTES + 1))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            content=compressed,
        )

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    with pytest.raises(MonitorError, match="(?i)encoding") as caught:
        asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert caught.value.error_class is ErrorClass.POLICY


def test_official_rejects_encoded_body_before_decoded_iteration() -> None:
    decoded_path_entered = False
    secret = "compressed-secret"

    class EncodedResponse:
        status_code = 200
        url = "https://example.com/products"
        headers = httpx.Headers(
            {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            }
        )

        async def aiter_bytes(self):
            nonlocal decoded_path_entered
            decoded_path_entered = True
            raise AssertionError("decoded allocation path entered")
            yield b""  # pragma: no cover

        async def aiter_raw(self):
            yield secret.encode()

    with pytest.raises(MonitorError, match="(?i)encoding") as caught:
        asyncio.run(
            policy_module._read_response(
                EncodedResponse(),  # type: ignore[arg-type]
                require_json=True,
                clock=lambda: NOW,
            )
        )

    assert caught.value.error_class is ErrorClass.POLICY
    assert decoded_path_entered is False
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_official_manual_redirect_rechecks_full_policy_before_next_get() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/products":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"products":[{"id":"sku-1","price":99}]}',
        )

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert paths == ["/robots.txt", "/products", "/robots.txt", "/final"]


def test_official_transient_retry_cap_uses_exact_sleeps_and_retry_after() -> None:
    page_attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal page_attempts
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        page_attempts += 1
        if page_attempts == 1:
            return httpx.Response(503)
        if page_attempts == 2:
            return httpx.Response(429, headers={"Retry-After": "25"})
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"products":[{"id":"sku-1","price":99}]}',
        )

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    rate = RecordingRateLimiter()
    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=rate,
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
        sleeper=sleep,
    )

    asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert page_attempts == 3
    assert sleeps == [1.0, 4.0]
    assert ("example.com", 25.0) in rate.calls


@pytest.mark.parametrize("status", [100, 199, 300, 304, 305, 306, 399])
def test_official_rejects_non_navigation_non_success_terminal_status(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            status,
            headers={"Content-Type": "application/json"},
            content=b'{"products":[]}',
        )

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    with pytest.raises(MonitorError) as caught:
        asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert caught.value.error_class is ErrorClass.POLICY


@pytest.mark.parametrize(
    "content_type",
    [
        "application/+json",
        "application/ +json",
        "application/vnd.example +json",
        "application/vnd example+json",
    ],
)
def test_official_rejects_malformed_json_media_types(content_type: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200,
            headers={"Content-Type": content_type},
            content=b'{"products":[]}',
        )

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    with pytest.raises(MonitorError, match="JSON") as caught:
        asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert caught.value.error_class is ErrorClass.POLICY


def test_official_accepts_valid_structured_json_media_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/problem+json; charset=utf-8"},
            content=b'{"products":[{"id":"sku-1","price":99}]}',
        )

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    batch = asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert batch.items[0].item_id == "source:sku-1"


def test_official_retry_after_http_date_uses_injected_aware_transport_clock() -> None:
    page_attempts = 0
    retry_at = format_datetime(NOW + timedelta(seconds=90), usegmt=True)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal page_attempts
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        page_attempts += 1
        if page_attempts == 1:
            return httpx.Response(503, headers={"Retry-After": retry_at})
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"products":[{"id":"sku-1","price":99}]}',
        )

    async def no_sleep(_seconds: float) -> None:
        return None

    rate = RecordingRateLimiter()
    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=rate,
        http_client=make_policy_client(handler, clock=lambda: NOW),
        clock=lambda: NOW,
        sleeper=no_sleep,
    )

    asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert ("example.com", 90.0) in rate.calls


@pytest.mark.parametrize("status", [429, 500, 503, 599])
def test_official_robots_transient_status_passes_retry_deadline_to_source(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(status, headers={"Retry-After": "37"})
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"products":[{"id":"sku-1","price":99}]}',
        )

    rate = RecordingRateLimiter()
    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=rate,
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert ("example.com", 37.0) in rate.calls


@pytest.mark.parametrize("status", [404, 410])
def test_official_robots_absence_status_allows_source_as_fetch_failure(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(status)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"products":[{"id":"sku-1","price":99}]}',
        )

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    assert asyncio.run(adapter.fetch("monitor-1", official_spec())).items


@pytest.mark.parametrize("status", [100, 300, 400, 401, 403, 418])
def test_official_robots_other_terminal_statuses_fail_closed(status: int) -> None:
    source_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal source_requests
        if request.url.path == "/robots.txt":
            return httpx.Response(status)
        source_requests += 1
        return httpx.Response(200)

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )

    with pytest.raises(MonitorError) as caught:
        asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert caught.value.error_class is ErrorClass.POLICY
    assert source_requests == 0


def test_official_direct_call_rejects_incompatible_spec_before_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=make_policy_client(handler),
        clock=lambda: NOW,
    )
    incompatible = MonitorSpec.model_validate(
        {
            **official_spec().model_dump(mode="json"),
            "source_adapter": "python_plugin",
            "adapter_ref": "json_get",
        }
    )

    with pytest.raises(MonitorError) as caught:
        asyncio.run(adapter.fetch("monitor-1", incompatible))

    assert caught.value.error_class is ErrorClass.POLICY
    assert calls == 0
