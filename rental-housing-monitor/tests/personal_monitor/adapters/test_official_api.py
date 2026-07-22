from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from personal_monitor.adapters.official_api import (
    MONITOR_USER_AGENT,
    BoundedPolicyHttpClient,
    OfficialJsonAdapter,
)
from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.scraping.scrapling_backend import MAX_RESPONSE_BYTES
from personal_monitor.security.url_policy import UrlPolicy

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

    client = BoundedPolicyHttpClient.for_test(
        egress_proxy_url="http://proxy.internal:8080",
        transport=httpx.MockTransport(handler),
    )
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
        http_client=BoundedPolicyHttpClient.for_test(
            egress_proxy_url="http://proxy.internal:8080",
            transport=httpx.MockTransport(handler),
        ),
        clock=lambda: NOW,
    )

    assert asyncio.run(adapter.fetch("monitor-1", official_spec())).monitor_id == "monitor-1"


def test_official_client_requires_proxy_and_has_no_arbitrary_request_api() -> None:
    with pytest.raises(ValueError, match="egress proxy"):
        BoundedPolicyHttpClient(egress_proxy_url=None)
    with pytest.raises(ValueError, match="egress proxy"):
        BoundedPolicyHttpClient(egress_proxy_url=" ")

    client = BoundedPolicyHttpClient.for_test(
        egress_proxy_url="http://proxy.internal:8080",
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
    )
    assert not hasattr(client, "request")
    assert not hasattr(client, "post")


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
        http_client=BoundedPolicyHttpClient.for_test(
            egress_proxy_url="http://proxy.internal:8080",
            transport=httpx.MockTransport(handler),
        ),
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
        http_client=BoundedPolicyHttpClient.for_test(
            egress_proxy_url="http://proxy.internal:8080",
            transport=httpx.MockTransport(handler),
        ),
        clock=lambda: NOW,
    )

    with pytest.raises(MonitorError, match="10 MiB") as caught:
        asyncio.run(adapter.fetch("monitor-1", official_spec()))
    assert caught.value.error_class is ErrorClass.POLICY


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
        http_client=BoundedPolicyHttpClient.for_test(
            egress_proxy_url="http://proxy.internal:8080",
            transport=httpx.MockTransport(handler),
        ),
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
        http_client=BoundedPolicyHttpClient.for_test(
            egress_proxy_url="http://proxy.internal:8080",
            transport=httpx.MockTransport(handler),
        ),
        clock=lambda: NOW,
        sleeper=sleep,
    )

    asyncio.run(adapter.fetch("monitor-1", official_spec()))

    assert page_attempts == 3
    assert sleeps == [1.0, 4.0]
    assert ("example.com", 25.0) in rate.calls


def test_official_direct_call_rejects_incompatible_spec_before_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    adapter = OfficialJsonAdapter(
        url_policy=UrlPolicy(Resolver()),
        rate_limiter=RecordingRateLimiter(),
        http_client=BoundedPolicyHttpClient.for_test(
            egress_proxy_url="http://proxy.internal:8080",
            transport=httpx.MockTransport(handler),
        ),
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
