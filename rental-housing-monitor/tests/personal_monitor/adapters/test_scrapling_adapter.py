from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from personal_monitor.adapters.official_api import BoundedPolicyHttpClient
from personal_monitor.adapters.scrapling import ScraplingSourceAdapter
from personal_monitor.domain.spec import FetchStrategy, MonitorSpec
from personal_monitor.engine.errors import ErrorClass, FetchError, MonitorError
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.scraping.scrapling_backend import ScraplingBackend
from personal_monitor.security.url_policy import UrlPolicy
from tests.personal_monitor.adapters._helpers import (
    make_policy_client,
    make_scrapling_backend,
)

NOW = datetime(2026, 7, 23, 3, 0, tzinfo=UTC)
pytestmark = pytest.mark.filterwarnings(
    "ignore:The 'strip_cdata' option of HTMLParser.*:DeprecationWarning"
)


class Resolver:
    async def resolve(self, _hostname: str, _port: int) -> list[str]:
        return ["93.184.216.34"]


class RecordingRateLimiter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    async def acquire(self, host: str, retry_after_seconds: float | None = None) -> None:
        self.calls.append((host, retry_after_seconds))


class FakeBackend:
    def __init__(self, **results: object) -> None:
        self.results = {
            name: list(value) if isinstance(value, list) else [value]
            for name, value in results.items()
        }
        self.calls: list[tuple[str, str, Path | None]] = []

    async def fetch_http(self, target):
        self.calls.append(("http", target.normalized_url, None))
        return self._take("http")

    async def fetch_dynamic(self, target, *, profile: Path | None = None):
        self.calls.append(("dynamic", target.normalized_url, profile))
        return self._take("dynamic")

    async def fetch_stealthy(self, target, *, profile: Path | None = None):
        self.calls.append(("stealthy", target.normalized_url, profile))
        return self._take("stealthy")

    def _take(self, name: str):
        value = self.results[name].pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def spec(**overrides: object) -> MonitorSpec:
    payload: dict[str, object] = {
        "schema_version": 1,
        "owner_id": "owner-1",
        "name": "web monitor",
        "target_url": "https://example.com/products",
        "source_adapter": "scrapling",
        "fetch_strategy": "auto",
        "extract": {
            "item_scope": "main",
            "fields": {
                "title": {"selector": "h1", "type": "text"},
                "price": {"selector": ".price", "type": "krw"},
            },
        },
        "validators": {"min_items": 1, "max_items": 3},
        "rules": [{"kind": "new_item"}],
    }
    payload.update(overrides)
    return MonitorSpec.model_validate(payload)


def document(
    body: bytes = b"<main><h1>Keyboard</h1><span class='price'>99,000</span></main>",
    *,
    strategy: FetchStrategy = FetchStrategy.HTTP,
    status: int = 200,
    final_url: str = "https://example.com/products",
    redirect_location: str | None = None,
    redirect_urls: tuple[str, ...] = (),
    peer_ip: str | None = None,
) -> SourceDocument:
    return SourceDocument(
        final_url=final_url,
        status=status,
        content_type="" if status in {301, 302, 303, 307, 308} else "text/html",
        headers={},
        body=body,
        strategy=strategy,
        redirect_location=redirect_location,
        redirect_urls=redirect_urls,
        peer_ip=peer_ip,
    )


def policy_client(handler=None) -> BoundedPolicyHttpClient:
    return make_policy_client(handler)


def adapter(backend: FakeBackend, **overrides: object) -> ScraplingSourceAdapter:
    arguments: dict[str, object] = {
        "url_policy": UrlPolicy(Resolver()),
        "rate_limiter": RecordingRateLimiter(),
        "backend": make_scrapling_backend(backend),
        "clock": lambda: NOW,
    }
    arguments.update(overrides)
    if "http_client" not in arguments:
        arguments["http_client"] = policy_client()
    return ScraplingSourceAdapter(**arguments)  # type: ignore[arg-type]


def strategies(backend: FakeBackend) -> list[str]:
    return [name for name, _url, _profile in backend.calls]


def test_auto_stops_after_valid_http_result() -> None:
    backend = FakeBackend(http=document())

    batch = asyncio.run(adapter(backend).fetch("monitor-1", spec()))

    assert strategies(backend) == ["http"]
    assert batch.monitor_id == "monitor-1"
    assert batch.items[0].fields == {"title": "Keyboard", "price": 99000}
    assert batch.observed_at == NOW
    assert len(batch.source_hash) == 64


def test_production_constructor_rejects_an_unsealed_backend() -> None:
    async def unused_fetcher(*_args, **_kwargs):
        raise AssertionError("must not run")

    unsealed = ScraplingBackend(
        egress_proxy_url="http://proxy.internal:8080",
        http_fetcher=unused_fetcher,
        dynamic_fetcher=unused_fetcher,
        stealthy_fetcher=unused_fetcher,
    )
    for backend in (FakeBackend(http=document()), unsealed):
        with pytest.raises(TypeError, match="backend"):
            ScraplingSourceAdapter(
                url_policy=UrlPolicy(Resolver()),
                rate_limiter=RecordingRateLimiter(),  # type: ignore[arg-type]
                http_client=policy_client(),
                backend=backend,  # type: ignore[arg-type]
                clock=lambda: NOW,
            )


def test_production_constructor_rejects_a_sealed_backend_subclass() -> None:
    class SubclassedBackend(ScraplingBackend):
        pass

    with pytest.raises(TypeError, match="backend"):
        ScraplingSourceAdapter(
            url_policy=UrlPolicy(Resolver()),
            rate_limiter=RecordingRateLimiter(),  # type: ignore[arg-type]
            http_client=policy_client(),
            backend=SubclassedBackend(
                egress_proxy_url="http://proxy.internal:8080",
            ),
            clock=lambda: NOW,
        )


def test_production_adapter_has_no_public_test_backend_factory() -> None:
    assert not hasattr(ScraplingSourceAdapter, "for_test")


def test_production_adapter_rejects_mismatched_policy_proxies() -> None:
    with pytest.raises(TypeError, match="proxy"):
        ScraplingSourceAdapter(
            url_policy=UrlPolicy(Resolver()),
            rate_limiter=RecordingRateLimiter(),  # type: ignore[arg-type]
            http_client=BoundedPolicyHttpClient(
                egress_proxy_url="http://policy-proxy.internal:8080"
            ),
            backend=ScraplingBackend(egress_proxy_url="http://scrapling-proxy.internal:8080"),
            clock=lambda: NOW,
        )


def test_auto_uses_dynamic_only_for_required_content_absence() -> None:
    backend = FakeBackend(
        http=document(b"<html><div id='app'></div></html>"),
        dynamic=document(strategy=FetchStrategy.DYNAMIC),
    )

    asyncio.run(adapter(backend, js_shell_detector=lambda _document: True).fetch("m", spec()))

    assert strategies(backend) == ["http", "dynamic"]


@pytest.mark.parametrize(
    "http_result",
    [
        document(
            b"<main><h1>Keyboard</h1><span class='price'>1</span>"
            b"<span class='price'>2</span></main>"
        ),
        document(b"<main><h1>Keyboard</h1><span class='price'>not-a-price</span></main>"),
        FetchError(ErrorClass.AUTHENTICATION, "authentication was rejected", status=401),
    ],
)
def test_auto_does_not_escalate_ambiguous_validation_or_authentication(
    http_result: object,
) -> None:
    backend = FakeBackend(http=http_result, dynamic=document(strategy=FetchStrategy.DYNAMIC))

    with pytest.raises(MonitorError):
        asyncio.run(adapter(backend).fetch("m", spec()))

    assert strategies(backend) == ["http"]


@pytest.mark.parametrize("detector", [lambda _document: "yes", lambda _document: 1 / 0])
def test_shell_detector_failure_is_closed_internal(detector) -> None:
    backend = FakeBackend(
        http=document(b"<div id='app'></div>"),
        dynamic=document(strategy=FetchStrategy.DYNAMIC),
    )

    with pytest.raises(MonitorError) as caught:
        asyncio.run(adapter(backend, js_shell_detector=detector).fetch("m", spec()))

    assert caught.value.error_class is ErrorClass.INTERNAL
    assert strategies(backend) == ["http"]


def test_auto_uses_stealthy_only_for_authoritative_dynamic_interstitial() -> None:
    backend = FakeBackend(
        http=document(b"<div id='app'></div>"),
        dynamic=FetchError(
            ErrorClass.POLICY,
            "block page detected",
            status=403,
            detected_interstitial=True,
        ),
        stealthy=document(strategy=FetchStrategy.STEALTHY),
    )

    asyncio.run(adapter(backend).fetch("m", spec()))

    assert strategies(backend) == ["http", "dynamic", "stealthy"]


def test_auto_does_not_use_stealthy_for_normal_dynamic_structure_failure() -> None:
    backend = FakeBackend(
        http=document(b"<div id='app'></div>"),
        dynamic=document(b"<div id='app'></div>", strategy=FetchStrategy.DYNAMIC),
        stealthy=document(strategy=FetchStrategy.STEALTHY),
    )

    with pytest.raises(MonitorError) as caught:
        asyncio.run(adapter(backend).fetch("m", spec()))

    assert caught.value.error_class is ErrorClass.STRUCTURE
    assert strategies(backend) == ["http", "dynamic"]


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (FetchStrategy.HTTP, "http"),
        (FetchStrategy.DYNAMIC, "dynamic"),
        (FetchStrategy.STEALTHY, "stealthy"),
    ],
)
def test_explicit_strategy_never_escalates(strategy: FetchStrategy, expected: str) -> None:
    backend = FakeBackend(**{expected: document(strategy=strategy)})

    asyncio.run(adapter(backend).fetch("m", spec(fetch_strategy=strategy.value)))

    assert strategies(backend) == [expected]


def test_transient_retry_has_three_total_attempts_and_exact_sleeps() -> None:
    error = FetchError(ErrorClass.TRANSIENT_NETWORK, "temporary failure", status=503)
    backend = FakeBackend(http=[error, error, error])
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(FetchError):
        asyncio.run(adapter(backend, sleeper=sleep).fetch("m", spec(fetch_strategy="http")))

    assert strategies(backend) == ["http", "http", "http"]
    assert sleeps == [1.0, 4.0]


def test_retry_after_is_forwarded_to_next_full_policy_attempt() -> None:
    backend = FakeBackend(
        http=[
            FetchError(
                ErrorClass.TRANSIENT_NETWORK,
                "temporary failure",
                status=429,
                retry_after_seconds=25,
            ),
            document(),
        ]
    )
    rate = RecordingRateLimiter()

    asyncio.run(
        adapter(backend, rate_limiter=rate, sleeper=lambda _seconds: asyncio.sleep(0)).fetch(
            "m", spec(fetch_strategy="http")
        )
    )

    assert ("example.com", 25.0) in rate.calls
    assert strategies(backend) == ["http", "http"]


def test_cancelled_error_is_preserved_without_retry() -> None:
    cancellation = asyncio.CancelledError()
    backend = FakeBackend(http=cancellation)

    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(adapter(backend).fetch("m", spec(fetch_strategy="http")))

    assert caught.value is cancellation
    assert strategies(backend) == ["http"]


def test_profile_materialization_is_browser_only_and_context_managed(tmp_path: Path) -> None:
    profile_path = tmp_path / "secret-profile-path"

    class Profiles:
        def __init__(self) -> None:
            self.calls: list[str] = []

        @contextmanager
        def materialize(self, reference: str):
            self.calls.append(reference)
            yield profile_path

    profiles = Profiles()
    dynamic = FakeBackend(dynamic=document(strategy=FetchStrategy.DYNAMIC))
    asyncio.run(
        adapter(dynamic, profile_provider=profiles).fetch(
            "m", spec(fetch_strategy="dynamic", auth_profile_ref="profile-1")
        )
    )
    http = FakeBackend(http=document())
    asyncio.run(
        adapter(http, profile_provider=profiles).fetch(
            "m", spec(fetch_strategy="http", auth_profile_ref="profile-1")
        )
    )

    assert profiles.calls == ["profile-1"]
    assert dynamic.calls[0][2] == profile_path
    assert http.calls[0][2] is None


def test_sync_profile_context_receives_the_real_fetch_exception(tmp_path: Path) -> None:
    failure = MonitorError(ErrorClass.POLICY, "fetch", "safe failure")
    exits: list[tuple[object, object, object]] = []

    class Context:
        def __enter__(self):
            return tmp_path

        def __exit__(self, exc_type, exc, traceback):
            exits.append((exc_type, exc, traceback))

    class Profiles:
        def materialize(self, _reference: str):
            return Context()

    backend = FakeBackend(dynamic=failure)

    with pytest.raises(MonitorError) as caught:
        asyncio.run(
            adapter(backend, profile_provider=Profiles()).fetch(
                "m", spec(fetch_strategy="dynamic", auth_profile_ref="profile-1")
            )
        )

    assert caught.value is failure
    assert exits[0][0] is MonitorError
    assert exits[0][1] is failure
    assert exits[0][2] is not None


def test_async_profile_context_receives_the_real_fetch_exception(tmp_path: Path) -> None:
    failure = MonitorError(ErrorClass.POLICY, "fetch", "safe failure")
    exits: list[tuple[object, object, object]] = []

    class Context:
        async def __aenter__(self):
            return tmp_path

        async def __aexit__(self, exc_type, exc, traceback):
            exits.append((exc_type, exc, traceback))

    class Profiles:
        def materialize(self, _reference: str):
            return Context()

    with pytest.raises(MonitorError) as caught:
        asyncio.run(
            adapter(FakeBackend(dynamic=failure), profile_provider=Profiles()).fetch(
                "m", spec(fetch_strategy="dynamic", auth_profile_ref="profile-1")
            )
        )

    assert caught.value is failure
    assert exits[0][0] is MonitorError
    assert exits[0][1] is failure
    assert exits[0][2] is not None


@pytest.mark.parametrize("asynchronous", [False, True])
def test_profile_cleanup_cannot_mask_cancellation(tmp_path: Path, asynchronous: bool) -> None:
    cancellation = asyncio.CancelledError()

    class SyncContext:
        def __enter__(self):
            return tmp_path

        def __exit__(self, *_args):
            raise RuntimeError("secret cleanup detail")

    class AsyncContext:
        async def __aenter__(self):
            return tmp_path

        async def __aexit__(self, *_args):
            raise RuntimeError("secret cleanup detail")

    class Profiles:
        def materialize(self, _reference: str):
            return AsyncContext() if asynchronous else SyncContext()

    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(
            adapter(FakeBackend(dynamic=cancellation), profile_provider=Profiles()).fetch(
                "m", spec(fetch_strategy="dynamic", auth_profile_ref="profile-1")
            )
        )

    assert caught.value is cancellation


def test_missing_profile_is_safe_authentication_failure() -> None:
    backend = FakeBackend(dynamic=document(strategy=FetchStrategy.DYNAMIC))

    with pytest.raises(MonitorError) as caught:
        asyncio.run(
            adapter(backend).fetch(
                "m", spec(fetch_strategy="dynamic", auth_profile_ref="secret-profile")
            )
        )

    assert caught.value.error_class is ErrorClass.AUTHENTICATION
    assert "secret-profile" not in str(caught.value)
    assert "secret-profile" not in repr(caught.value)
    assert backend.calls == []


def test_http_redirect_is_validated_before_next_policy_fetch() -> None:
    backend = FakeBackend(
        http=[
            document(
                b"",
                status=302,
                redirect_location="https://other.example/final",
            ),
            document(final_url="https://other.example/final"),
        ]
    )

    asyncio.run(adapter(backend).fetch("m", spec(fetch_strategy="http")))

    assert [url for _name, url, _profile in backend.calls] == [
        "https://example.com/products",
        "https://other.example/final",
    ]


def test_http_redirect_loop_is_rejected_before_another_policy_fetch() -> None:
    robots_requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal robots_requests
        robots_requests += 1
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")

    backend = FakeBackend(
        http=document(
            b"",
            status=302,
            redirect_location="https://example.com/products",
        )
    )

    with pytest.raises(MonitorError, match="loop"):
        asyncio.run(
            adapter(backend, http_client=policy_client(handler)).fetch(
                "m", spec(fetch_strategy="http")
            )
        )

    assert robots_requests == 1
    assert strategies(backend) == ["http"]


def test_sixth_http_redirect_is_rejected_without_fetching_its_destination() -> None:
    redirects = [
        document(
            b"",
            status=302,
            final_url=(
                "https://example.com/products"
                if index == 0
                else f"https://example.com/redirect-{index}"
            ),
            redirect_location=f"https://example.com/redirect-{index + 1}",
        )
        for index in range(6)
    ]
    backend = FakeBackend(http=redirects)

    with pytest.raises(MonitorError, match="redirect limit"):
        asyncio.run(adapter(backend).fetch("m", spec(fetch_strategy="http")))

    assert len(backend.calls) == 6


def test_browser_history_must_start_at_the_approved_request() -> None:
    backend = FakeBackend(
        dynamic=document(
            strategy=FetchStrategy.DYNAMIC,
            final_url="https://other.example/final",
            redirect_urls=("https://unrelated.example/start",),
        )
    )

    with pytest.raises(MonitorError) as caught:
        asyncio.run(adapter(backend).fetch("m", spec(fetch_strategy="dynamic")))

    assert caught.value.error_class is ErrorClass.POLICY
    assert caught.value.stage == "redirect"


def test_browser_redirect_history_is_rejected_until_preflight_is_available() -> None:
    backend = FakeBackend(
        dynamic=document(
            strategy=FetchStrategy.DYNAMIC,
            final_url="https://other.example/private",
            redirect_urls=("https://example.com/products",),
        )
    )

    with pytest.raises(MonitorError, match="browser redirect"):
        asyncio.run(adapter(backend).fetch("m", spec(fetch_strategy="dynamic")))


def test_untrusted_browser_peer_metadata_is_ignored_for_same_final_url() -> None:
    backend = FakeBackend(
        dynamic=document(
            strategy=FetchStrategy.DYNAMIC,
            final_url="https://example.com/products",
            peer_ip="10.0.0.7",
        )
    )

    batch = asyncio.run(adapter(backend).fetch("m", spec(fetch_strategy="dynamic")))

    assert batch.items


def test_robots_disallow_prevents_source_fetch() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            text="User-agent: *\nDisallow: /products\n",
        )

    backend = FakeBackend(http=document())
    with pytest.raises(MonitorError) as caught:
        asyncio.run(
            adapter(backend, http_client=policy_client(handler)).fetch(
                "m", spec(fetch_strategy="http")
            )
        )

    assert caught.value.error_class is ErrorClass.POLICY
    assert caught.value.stage == "robots"
    assert backend.calls == []


def test_genuine_robots_fetch_failure_does_not_claim_permission_or_block_fetch() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="temporary")

    backend = FakeBackend(http=document())

    batch = asyncio.run(
        adapter(backend, http_client=policy_client(handler)).fetch("m", spec(fetch_strategy="http"))
    )

    assert batch.monitor_id == "m"
    assert strategies(backend) == ["http"]


def test_scrapling_robots_transient_status_passes_retry_deadline_to_source() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "41"})

    rate = RecordingRateLimiter()
    backend = FakeBackend(http=document())

    asyncio.run(
        adapter(backend, http_client=policy_client(handler), rate_limiter=rate).fetch(
            "m", spec(fetch_strategy="http")
        )
    )

    assert ("example.com", 41.0) in rate.calls


@pytest.mark.parametrize("status", [404, 410])
def test_scrapling_robots_absence_allows_source(status: int) -> None:
    backend = FakeBackend(http=document())

    asyncio.run(
        adapter(
            backend,
            http_client=policy_client(lambda _request: httpx.Response(status)),
        ).fetch("m", spec(fetch_strategy="http"))
    )

    assert strategies(backend) == ["http"]


@pytest.mark.parametrize("status", [100, 300, 400, 401, 403, 418])
def test_scrapling_robots_other_statuses_fail_closed(status: int) -> None:
    backend = FakeBackend(http=document())

    with pytest.raises(MonitorError) as caught:
        asyncio.run(
            adapter(
                backend,
                http_client=policy_client(lambda _request: httpx.Response(status)),
            ).fetch("m", spec(fetch_strategy="http"))
        )

    assert caught.value.error_class is ErrorClass.POLICY
    assert backend.calls == []


def test_same_origin_robots_redirect_is_validated_before_request() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(302, headers={"Location": "/robots-v2"})
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")

    backend = FakeBackend(http=document())

    asyncio.run(
        adapter(backend, http_client=policy_client(handler)).fetch("m", spec(fetch_strategy="http"))
    )

    assert paths == ["/robots.txt", "/robots-v2"]


def test_cross_origin_robots_redirect_is_rejected_before_request() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host or "")
        return httpx.Response(302, headers={"Location": "https://other.example/robots.txt"})

    backend = FakeBackend(http=document())

    with pytest.raises(MonitorError, match="changed origin"):
        asyncio.run(
            adapter(backend, http_client=policy_client(handler)).fetch(
                "m", spec(fetch_strategy="http")
            )
        )

    assert hosts == ["example.com"]
    assert backend.calls == []


@pytest.mark.parametrize(
    "error_class",
    [
        ErrorClass.AUTHENTICATION,
        ErrorClass.STRUCTURE,
        ErrorClass.VALIDATION,
        ErrorClass.POLICY,
        ErrorClass.INTERNAL,
    ],
)
def test_non_transient_failures_are_never_retried(error_class: ErrorClass) -> None:
    backend = FakeBackend(http=MonitorError(error_class, "fetch", "safe failure"))
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(MonitorError):
        asyncio.run(adapter(backend, sleeper=sleep).fetch("m", spec(fetch_strategy="http")))

    assert strategies(backend) == ["http"]
    assert sleeps == []


def test_direct_call_rejects_incompatible_adapter_without_network() -> None:
    backend = FakeBackend(http=document())
    incompatible = MonitorSpec.model_validate(
        {
            **spec().model_dump(mode="json"),
            "source_adapter": "official_api",
            "adapter_ref": "json_get",
        }
    )

    with pytest.raises(MonitorError) as caught:
        asyncio.run(adapter(backend).fetch("m", incompatible))

    assert caught.value.error_class is ErrorClass.POLICY
    assert backend.calls == []
