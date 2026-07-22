from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

import httpx
import pytest

import personal_monitor.adapters._policy as policy_module
from personal_monitor.adapters.official_api import BoundedPolicyHttpClient
from personal_monitor.scraping.scrapling_backend import ScraplingBackend

HttpHandler = Callable[[httpx.Request], httpx.Response]
_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _RawStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self):
        yield self._body

    async def aclose(self) -> None:
        return None


class FakeBackend(Protocol):
    async def fetch_http(self, target): ...

    async def fetch_dynamic(self, target, *, profile=None): ...

    async def fetch_stealthy(self, target, *, profile=None): ...


def _default_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="User-agent: *\nAllow: /\n")


_http_handler: HttpHandler = _default_handler
_fake_backend: FakeBackend | None = None


def install_policy_test_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    def async_client_factory(**kwargs: object) -> httpx.AsyncClient:
        assert "proxy" in kwargs
        kwargs.pop("proxy")

        def streaming_handler(request: httpx.Request) -> httpx.Response:
            response = _http_handler(request)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=_RawStream(response.content),
                request=request,
                extensions=response.extensions,
            )

        return _REAL_ASYNC_CLIENT(
            **kwargs,  # type: ignore[arg-type]
            transport=httpx.MockTransport(streaming_handler),
        )

    async def fetch_http(_self: ScraplingBackend, target):
        assert _fake_backend is not None
        return await _fake_backend.fetch_http(target)

    async def fetch_dynamic(_self: ScraplingBackend, target, *, profile=None):
        assert _fake_backend is not None
        return await _fake_backend.fetch_dynamic(target, profile=profile)

    async def fetch_stealthy(_self: ScraplingBackend, target, *, profile=None):
        assert _fake_backend is not None
        return await _fake_backend.fetch_stealthy(target, profile=profile)

    monkeypatch.setattr(policy_module.httpx, "AsyncClient", async_client_factory)
    monkeypatch.setattr(ScraplingBackend, "fetch_http", fetch_http)
    monkeypatch.setattr(ScraplingBackend, "fetch_dynamic", fetch_dynamic)
    monkeypatch.setattr(ScraplingBackend, "fetch_stealthy", fetch_stealthy)


def reset_test_boundaries() -> None:
    global _http_handler, _fake_backend
    _http_handler = _default_handler
    _fake_backend = None


def make_policy_client(
    handler: HttpHandler | None = None,
    *,
    proxy: str = "http://proxy.internal:8080",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BoundedPolicyHttpClient:
    global _http_handler
    _http_handler = handler or _default_handler
    return BoundedPolicyHttpClient(egress_proxy_url=proxy, clock=clock)


def make_scrapling_backend(
    fake_backend: FakeBackend,
    *,
    proxy: str = "http://proxy.internal:8080",
) -> ScraplingBackend:
    global _fake_backend
    _fake_backend = fake_backend
    return ScraplingBackend(egress_proxy_url=proxy)
