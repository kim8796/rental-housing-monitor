from __future__ import annotations

import asyncio
import hmac
import http.client
import multiprocessing
import os
import secrets
import socket
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

import pytest
from scrapling.fetchers import DynamicFetcher

from personal_monitor.adapters.registry import DefaultAdapterRegistry
from personal_monitor.adapters.scrapling import ScraplingSourceAdapter
from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.engine.runner import MonitorRunner, RunResult
from personal_monitor.scraping.extractor import DeclarativeExtractor
from personal_monitor.scraping.profiles import BrowserProfileStore, bootstrap_profile
from personal_monitor.scraping.scrapling_backend import _BROWSER_GATE
from personal_monitor.security.rate_limit import HostRateLimiter
from personal_monitor.security.url_policy import PolicyError, ResolvedTarget
from personal_monitor.security.vault import CredentialVault
from personal_monitor.storage import (
    MonitorLease,
    RegistryRepository,
    RuntimeRepository,
    open_database,
)
from rental_monitor.telegram import TelegramClient

NOW = datetime(2026, 7, 23, 3, 0, tzinfo=UTC)
_LOOPBACK = "127.0.0.1"
_MAX_FIXTURE_BODY = 1024 * 1024
_PROXY_CAPABILITY_HEADER = "X-Integration-Proxy-Capability"


@dataclass(frozen=True)
class _FixedClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


@dataclass
class _FixtureState:
    requests: list[str] = field(default_factory=list)
    forwarded_paths: list[str] = field(default_factory=list)
    rejected_proxy_targets: list[str] = field(default_factory=list)
    forwarded_proxy_origins: list[tuple[str, str, int]] = field(default_factory=list)
    proxy_capability: str = field(
        default_factory=lambda: secrets.token_urlsafe(32),
        repr=False,
    )
    direct_origin_rejections: int = 0
    trap_port: int = 0
    trap_hits: int = 0
    active_requests: int = 0
    active_browser_requests: int = 0
    max_active_browser_requests: int = 0
    first_browser_entered: threading.Event = field(default_factory=threading.Event)
    release_first_browser: threading.Event = field(default_factory=threading.Event)
    session_token: str = field(
        default_factory=lambda: secrets.token_urlsafe(32),
        repr=False,
    )
    session_tokens: list[str] = field(default_factory=list, repr=False)
    authenticated_requests: int = 0
    unauthenticated_requests: int = 0
    forwarded_protected_cookie_requests: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.session_tokens.append(self.session_token)

    def record(self, path: str) -> None:
        with self.lock:
            self.requests.append(path)


class _OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: _FixtureState

    def do_GET(self) -> None:
        capability = self.headers.get(_PROXY_CAPABILITY_HEADER, "")
        if not hmac.compare_digest(capability, self.state.proxy_capability):
            with self.state.lock:
                self.state.direct_origin_rejections += 1
            self._send(403, b"fixture proxy capability required", "text/plain")
            return
        with self.state.lock:
            self.state.active_requests += 1
        try:
            path = urlsplit(self.path).path
            self.state.record(_request_path(self.path))
            if path == "/robots.txt":
                self._send(200, b"User-agent: *\nAllow: /\n", "text/plain")
            elif path == "/static":
                self._send(
                    200,
                    (
                        b"<!doctype html><main class='listing'>"
                        b"<h1>Local keyboard</h1>"
                        b"<span class='price'>129,000</span></main>"
                    ),
                    "text/html",
                )
            elif path == "/dynamic":
                self._send(
                    200,
                    _dynamic_body(self.state.trap_port),
                    "text/html",
                )
            elif path in {"/concurrent-a", "/concurrent-b"}:
                self._send_concurrent(path)
            elif path == "/login":
                self._send(
                    200,
                    b"<!doctype html><main><h1>Local login established</h1></main>",
                    "text/html",
                    extra_headers={"Set-Cookie": _session_cookie(self.state.session_token)},
                )
            elif path == "/protected":
                self._send_protected()
            else:
                self._send(404, b"not found", "text/plain")
        finally:
            with self.state.lock:
                self.state.active_requests -= 1

    def _send_protected(self) -> None:
        cookie = self.headers.get("Cookie", "")
        with self.state.lock:
            authenticated = _has_session_cookie(cookie, self.state.session_token)
            if authenticated:
                self.state.authenticated_requests += 1
                next_token = secrets.token_urlsafe(32)
                self.state.session_token = next_token
                self.state.session_tokens.append(next_token)
            else:
                self.state.unauthenticated_requests += 1
                next_token = None
        if not authenticated:
            self._send(
                200,
                b"<!doctype html><main><h1>Sign in required</h1></main>",
                "text/html",
            )
            return
        self._send(
            200,
            (
                b"<!doctype html><main class='listing'>"
                b"<h1>Protected keyboard</h1>"
                b"<span class='price'>141,000</span></main>"
            ),
            "text/html",
            extra_headers={"Set-Cookie": _session_cookie(next_token)},
        )

    def _send_concurrent(self, path: str) -> None:
        with self.state.lock:
            self.state.active_browser_requests += 1
            self.state.max_active_browser_requests = max(
                self.state.max_active_browser_requests,
                self.state.active_browser_requests,
            )
            first = not self.state.first_browser_entered.is_set()
            self.state.first_browser_entered.set()
        try:
            if first and not self.state.release_first_browser.wait(timeout=5):
                raise AssertionError("browser concurrency fixture release timed out")
            label, price = (
                ("Concurrent A", "55,000")
                if path == "/concurrent-a"
                else ("Concurrent B", "66,000")
            )
            self._send(
                200,
                (
                    f"<!doctype html><main class='listing'><h1>{label}</h1>"
                    f"<span class='price'>{price}</span></main>"
                ).encode(),
                "text/html",
            )
        finally:
            with self.state.lock:
                self.state.active_browser_requests -= 1

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


class _TrapHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: _FixtureState

    def do_GET(self) -> None:
        with self.state.lock:
            self.state.trap_hits += 1
        body = b"trap origin must not be reached"
        self.send_response(418)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


def _request_path(value: str) -> str:
    target = urlsplit(value)
    return urlunsplit(("", "", target.path or "/", target.query, ""))


def _dynamic_body(trap_port: int) -> bytes:
    if trap_port <= 0:
        raise AssertionError("dynamic fixture trap is unavailable")
    return (
        "<!doctype html><main class='listing'>"
        "<h1>Browser keyboard</h1><span id='price-slot'></span></main>"
        "<script>window.addEventListener('load',()=>{"
        f"fetch('http://{_LOOPBACK}:{trap_port}/__proxy_trap__').catch(()=>{{}});"
        "setTimeout(()=>{const price=document.createElement('span');"
        "price.className='price';price.textContent='87,000';"
        "document.querySelector('#price-slot').replaceWith(price);},50);"
        "});</script>"
    ).encode()


def _session_cookie(value: str) -> str:
    return f"pm_local_session={value}; Path=/; Max-Age=3600; HttpOnly; SameSite=Lax"


def _has_session_cookie(header: str, expected: str) -> bool:
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if name == "pm_local_session" and separator:
            return hmac.compare_digest(value, expected)
    return False


def _count_profile_cookie_rows(store: BrowserProfileStore, profile_id: str) -> int:
    with store.materialize(profile_id) as profile:
        cookies = profile / "Default" / "Cookies"
        if not cookies.is_file():
            return 0
        connection = sqlite3.connect(f"file:{cookies}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT count(*) FROM cookies WHERE name = ?",
                ("pm_local_session",),
            ).fetchone()
            return int(row[0])
        finally:
            connection.close()


def _bootstrap_noop(_page: object) -> None:
    return None


def _forbid_telegram_send(*_args: object, **_kwargs: object) -> int:
    raise AssertionError("integration monitor runner attempted Telegram delivery")


_forbid_telegram_send._integration_fail_fast = True  # type: ignore[attr-defined]


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    origin_port: int
    state: _FixtureState

    def do_CONNECT(self) -> None:
        self._reject(self.path)

    def do_GET(self) -> None:
        try:
            target = urlsplit(self.path)
            port = target.port
        except ValueError:
            self._reject("malformed")
            return
        if (
            target.scheme != "http"
            or target.hostname != _LOOPBACK
            or port != self.origin_port
            or target.username is not None
            or target.password is not None
        ):
            safe_port = f":{port}" if port is not None else ""
            self._reject(f"{target.hostname or 'invalid'}{safe_port}{target.path}")
            return

        path = urlunsplit(("", "", target.path or "/", target.query, ""))
        with self.state.lock:
            self.state.forwarded_proxy_origins.append(
                (target.scheme, target.hostname, self.origin_port)
            )
            self.state.forwarded_paths.append(path)
        headers = {"Host": f"{_LOOPBACK}:{self.origin_port}", "Connection": "close"}
        headers[_PROXY_CAPABILITY_HEADER] = self.state.proxy_capability
        cookie = self.headers.get("Cookie")
        if cookie is not None:
            headers["Cookie"] = cookie
            if target.path == "/protected":
                with self.state.lock:
                    self.state.forwarded_protected_cookie_requests += 1
        upstream = http.client.HTTPConnection(_LOOPBACK, self.origin_port, timeout=5)
        try:
            upstream.request(
                "GET",
                path,
                headers=headers,
            )
            response = upstream.getresponse()
            body = response.read(_MAX_FIXTURE_BODY + 1)
            if len(body) > _MAX_FIXTURE_BODY:
                raise RuntimeError("fixture response exceeded limit")
            self.send_response(response.status)
            for name in ("Content-Type", "Location", "Retry-After", "Set-Cookie"):
                value = response.getheader(name)
                if value is not None:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        finally:
            upstream.close()

    def _reject(self, target: str) -> None:
        with self.state.lock:
            self.state.rejected_proxy_targets.append(target)
        body = b"proxy target rejected"
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


class _LocalServer:
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self._server = ThreadingHTTPServer((_LOOPBACK, 0), handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=False)
        self._closed = False
        self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise AssertionError("fixture server thread leaked")

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()


class _FixtureOriginPolicy:
    """Test-only capability for one live loopback origin."""

    def __init__(self, origin_url: str) -> None:
        self._origin = _origin(origin_url)

    async def validate(self, url: str) -> ResolvedTarget:
        try:
            parts = urlsplit(url)
            candidate = _origin(url)
        except ValueError:
            raise PolicyError("fixture URL is invalid") from None
        if candidate != self._origin or parts.username is not None or parts.password is not None:
            raise PolicyError("fixture URL escaped the approved origin")
        return ResolvedTarget(
            normalized_url=urlunsplit(
                (parts.scheme, parts.netloc, parts.path or "/", parts.query, "")
            ),
            hostname=_LOOPBACK,
            port=parts.port or 80,
            addresses=frozenset({_LOOPBACK}),
        )

    async def validate_redirect(self, url: str, *, redirect_count: int) -> ResolvedTarget:
        if not isinstance(redirect_count, int) or isinstance(redirect_count, bool):
            raise PolicyError("fixture redirect count is invalid")
        return await self.validate(url)


def _origin(url: str) -> tuple[str, str, int]:
    parts: SplitResult = urlsplit(url)
    if parts.scheme != "http" or parts.hostname != _LOOPBACK or parts.port is None:
        raise ValueError("fixture origin must be an explicit loopback HTTP port")
    return parts.scheme, parts.hostname, parts.port


class _ForbiddenAdapter:
    async def fetch(self, _monitor_id: str, _spec: MonitorSpec):
        raise AssertionError("non-Scrapling adapter ran")


class _TracingExtractor:
    def __init__(self) -> None:
        self._delegate = DeclarativeExtractor()
        self.trace: list[str] = []

    def extract(self, document, spec):
        self.trace.append(document.strategy.value)
        return self._delegate.extract(document, spec)


@dataclass
class _StaticScenario:
    connection: sqlite3.Connection
    registry: RegistryRepository
    runtime: RuntimeRepository
    runner: MonitorRunner
    monitor_id: str
    lease: MonitorLease
    origin_url: str
    state: _FixtureState
    origin_server: _LocalServer
    proxy_server: _LocalServer
    trap_server: _LocalServer | None = None
    strategy_trace: list[str] = field(default_factory=list)

    async def run(self) -> RunResult:
        return await self.runner.run(self.lease)

    async def run_again(self) -> RunResult:
        self.connection.execute(
            "UPDATE monitors SET next_run_at = ? WHERE id = ?",
            (NOW.isoformat(), self.monitor_id),
        )
        lease = self.runtime.claim_due(worker_id="worker-local", now=NOW)[0]
        return await self.runner.run(lease)

    @property
    def origin_requests(self) -> list[str]:
        with self.state.lock:
            return list(self.state.requests)

    @property
    def outbox_count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM outbox").fetchone()[0])

    @property
    def pending_outbox_count(self) -> int:
        return int(
            self.connection.execute(
                "SELECT count(*) FROM outbox WHERE status = 'pending'"
            ).fetchone()[0]
        )

    @property
    def delivery_count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM deliveries").fetchone()[0])

    def route_count(self, path: str) -> int:
        return self.origin_requests.count(path)

    def assert_proxy_ledger_consistent(self) -> None:
        with self.state.lock:
            forwarded_paths = tuple(self.state.forwarded_paths)
            accepted_paths = tuple(self.state.requests)
        assert forwarded_paths == accepted_paths

    def assert_browser_proxy_trap(self) -> None:
        with self.state.lock:
            rejected_targets = tuple(self.state.rejected_proxy_targets)
            trap_targets = tuple(
                target for target in rejected_targets if "/__proxy_trap__" in target
            )
            trap_hits = self.state.trap_hits
        assert len(trap_targets) == 1
        assert trap_hits == 0
        assert all(
            any(target.startswith(host) for target in rejected_targets)
            for host in (
                "clients2.google.com",
                "accounts.google.com",
                "www.google.com",
            )
        )

    def assert_local_only_and_clean(self) -> None:
        self.assert_proxy_ledger_consistent()
        with self.state.lock:
            session_tokens = tuple(self.state.session_tokens)
            assert self.state.forwarded_proxy_origins
            assert set(self.state.forwarded_proxy_origins) == {
                ("http", _LOOPBACK, self.origin_server.port)
            }
            assert all(
                all(token not in target for token in session_tokens)
                for target in self.state.rejected_proxy_targets
            )
            assert self.state.active_requests == 0
        self.proxy_server.close()
        self.origin_server.close()
        if self.trap_server is not None:
            self.trap_server.close()
        assert not self.proxy_server.is_alive
        assert not self.origin_server.is_alive
        assert self.trap_server is None or not self.trap_server.is_alive


@dataclass
class _ConcurrentScenario(_StaticScenario):
    monitor_ids: tuple[str, str] = ("", "")
    leases: tuple[MonitorLease, MonitorLease] = (
        MonitorLease("", 0),
        MonitorLease("", 0),
    )

    async def run_both(self) -> tuple[RunResult, RunResult]:
        first, second = await asyncio.gather(
            self.runner.run(self.leases[0]),
            self.runner.run(self.leases[1]),
        )
        return first, second

    @property
    def first_browser_entered(self) -> threading.Event:
        return self.state.first_browser_entered

    def browser_gate_is_held(self) -> bool:
        acquired = _BROWSER_GATE.acquire(blocking=False)
        if acquired:
            _BROWSER_GATE.release()
        return not acquired

    def release_first_browser(self) -> None:
        self.state.release_first_browser.set()

    @property
    def max_active_browser_requests(self) -> int:
        with self.state.lock:
            return self.state.max_active_browser_requests

    @property
    def observation_count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM observations").fetchone()[0])


@dataclass
class _SessionScenario(_StaticScenario):
    unauthenticated_lease: MonitorLease = MonitorLease("", 0)
    profiled_monitor_id: str = ""
    profiled_lease: MonitorLease = MonitorLease("", 0)
    profile_id: str = "local-profile"
    profile_store: BrowserProfileStore | None = None
    vault: CredentialVault | None = None
    workspace_root: Path = Path("/")
    vault_root: Path = Path("/")
    database_path: Path = Path("/")
    baseline_child_pids: frozenset[int] = frozenset()
    adapter: ScraplingSourceAdapter | None = None
    bootstrapped_cookie_rows: int = 0

    async def run_without_profile(self) -> RunResult:
        return await self.runner.run(self.unauthenticated_lease)

    async def run_profiled(self) -> RunResult:
        return await self.runner.run(self.profiled_lease)

    async def run_profiled_again(self) -> RunResult:
        self.connection.execute(
            "UPDATE monitors SET next_run_at = ? WHERE id = ?",
            (NOW.isoformat(), self.profiled_monitor_id),
        )
        leases = self.runtime.claim_due(worker_id="worker-session", now=NOW)
        assert len(leases) == 1 and leases[0].monitor_id == self.profiled_monitor_id
        return await self.runner.run(leases[0])

    @property
    def observation_count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM observations").fetchone()[0])

    @property
    def authenticated_requests(self) -> int:
        with self.state.lock:
            return self.state.authenticated_requests

    @property
    def unauthenticated_requests(self) -> int:
        with self.state.lock:
            return self.state.unauthenticated_requests

    @property
    def forwarded_protected_cookie_requests(self) -> int:
        with self.state.lock:
            return self.state.forwarded_protected_cookie_requests

    @property
    def safe_run_diagnostics(self) -> list[tuple[str, str, str | None, str | None]]:
        rows = self.connection.execute(
            "SELECT status, stage, error_class, error_detail FROM runs ORDER BY started_at, id"
        ).fetchall()
        return [tuple(row) for row in rows]

    def assert_profile_runtime_clean(self) -> None:
        assert self.workspace_root.is_dir()
        assert list(self.workspace_root.iterdir()) == []
        current_children = {
            process.pid for process in multiprocessing.active_children() if process.pid is not None
        }
        assert current_children <= self.baseline_child_pids
        acquired = _BROWSER_GATE.acquire(blocking=False)
        assert acquired
        _BROWSER_GATE.release()
        assert self.adapter is not None
        assert self.adapter._backend._background_calls == set()
        assert self.profile_store is not None
        lock = self.profile_store._lock_for(self.profile_id)
        assert lock.acquire(blocking=False)
        lock.release()

    def assert_sensitive_material_absent(self) -> None:
        with self.state.lock:
            needles = tuple(token.encode() for token in self.state.session_tokens)
        leaked = False
        for root in (self.vault_root, self.workspace_root):
            for path in root.rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    leaked = leaked or any(needle in data for needle in needles)
        if self.database_path.is_file():
            data = self.database_path.read_bytes()
            leaked = leaked or any(needle in data for needle in needles)
        if leaked:
            pytest.fail("session material was persisted outside the encrypted boundary")

    def close_extra(self) -> None:
        if self.profile_store is not None:
            self.profile_store.close()
        if self.vault is not None:
            self.vault.close()


class _IntegrationHarness:
    def __init__(
        self,
        *,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch
        self._scenarios: list[_StaticScenario] = []

    def static_monitor(self) -> _StaticScenario:
        state = _FixtureState()
        origin_handler = type("OriginHandler", (_OriginHandler,), {"state": state})
        origin_server = _LocalServer(origin_handler)
        proxy_handler = type(
            "ProxyHandler",
            (_ProxyHandler,),
            {"state": state, "origin_port": origin_server.port},
        )
        proxy_server = _LocalServer(proxy_handler)
        origin_url = f"http://{_LOOPBACK}:{origin_server.port}"
        self._install_network_guard()

        connection = open_database(self._tmp_path / "static.sqlite3")
        registry = RegistryRepository(connection)
        runtime = RuntimeRepository(connection)
        registry.create_user("owner-local", 1)
        registry.create_delivery_target("target-local", "owner-local", "unused-local-address")
        spec = MonitorSpec.model_validate(
            {
                "schema_version": 1,
                "owner_id": "owner-local",
                "name": "Local static monitor",
                "target_url": f"{origin_url}/static?view=private#fragment",
                "source_adapter": "scrapling",
                "fetch_strategy": "http",
                "extract": {
                    "item_scope": "main.listing",
                    "fields": {
                        "title": {"selector": "h1", "type": "text"},
                        "price": {"selector": ".price", "type": "krw"},
                    },
                },
                "validators": {"min_items": 1, "max_items": 1},
                "rules": [{"kind": "new_item"}],
            }
        )
        monitor_id = registry.create_monitor(spec, created_by="owner-local")
        connection.execute(
            "UPDATE monitors SET next_run_at = ? WHERE id = ?",
            (NOW.isoformat(), monitor_id),
        )
        lease = runtime.claim_due(worker_id="worker-local", now=NOW)[0]
        adapter = ScraplingSourceAdapter(
            url_policy=_FixtureOriginPolicy(origin_url),  # type: ignore[arg-type]
            rate_limiter=HostRateLimiter(minimum_interval_seconds=0),
            egress_proxy_url=f"http://{_LOOPBACK}:{proxy_server.port}",
            clock=lambda: NOW,
        )
        adapters = DefaultAdapterRegistry(
            scrapling=adapter,
            official_api=_ForbiddenAdapter(),
        )
        scenario = _StaticScenario(
            connection=connection,
            registry=registry,
            runtime=runtime,
            runner=MonitorRunner(
                registry=registry,
                runtime=runtime,
                adapters=adapters,
                clock=_FixedClock(),
                worker_id="worker-local",
            ),
            monitor_id=monitor_id,
            lease=lease,
            origin_url=origin_url,
            state=state,
            origin_server=origin_server,
            proxy_server=proxy_server,
        )
        self._scenarios.append(scenario)
        return scenario

    def dynamic_monitor(self) -> _StaticScenario:
        _require_browser_assets()
        state = _FixtureState()
        trap_handler = type("TrapHandler", (_TrapHandler,), {"state": state})
        trap_server = _LocalServer(trap_handler)
        state.trap_port = trap_server.port
        origin_handler = type("DynamicOriginHandler", (_OriginHandler,), {"state": state})
        origin_server = _LocalServer(origin_handler)
        proxy_handler = type(
            "DynamicProxyHandler",
            (_ProxyHandler,),
            {"state": state, "origin_port": origin_server.port},
        )
        proxy_server = _LocalServer(proxy_handler)
        origin_url = f"http://{_LOOPBACK}:{origin_server.port}"
        self._install_network_guard()

        connection = open_database(self._tmp_path / "dynamic.sqlite3")
        registry = RegistryRepository(connection)
        runtime = RuntimeRepository(connection)
        registry.create_user("owner-dynamic", 2)
        registry.create_delivery_target("target-dynamic", "owner-dynamic", "unused-local-address")
        spec = MonitorSpec.model_validate(
            {
                "schema_version": 1,
                "owner_id": "owner-dynamic",
                "name": "Local dynamic monitor",
                "target_url": f"{origin_url}/dynamic",
                "source_adapter": "scrapling",
                "fetch_strategy": "auto",
                "extract": {
                    "item_scope": "main.listing",
                    "fields": {
                        "title": {"selector": "h1", "type": "text"},
                        "price": {"selector": ".price", "type": "krw"},
                    },
                },
                "validators": {"min_items": 1, "max_items": 1},
                "rules": [{"kind": "new_item"}],
            }
        )
        monitor_id = registry.create_monitor(spec, created_by="owner-dynamic")
        connection.execute(
            "UPDATE monitors SET next_run_at = ? WHERE id = ?",
            (NOW.isoformat(), monitor_id),
        )
        lease = runtime.claim_due(worker_id="worker-dynamic", now=NOW)[0]
        extractor = _TracingExtractor()
        adapter = ScraplingSourceAdapter(
            url_policy=_FixtureOriginPolicy(origin_url),  # type: ignore[arg-type]
            rate_limiter=HostRateLimiter(minimum_interval_seconds=0),
            egress_proxy_url=f"http://{_LOOPBACK}:{proxy_server.port}",
            extractor=extractor,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        scenario = _StaticScenario(
            connection=connection,
            registry=registry,
            runtime=runtime,
            runner=MonitorRunner(
                registry=registry,
                runtime=runtime,
                adapters=DefaultAdapterRegistry(
                    scrapling=adapter,
                    official_api=_ForbiddenAdapter(),
                ),
                clock=_FixedClock(),
                worker_id="worker-dynamic",
            ),
            monitor_id=monitor_id,
            lease=lease,
            origin_url=origin_url,
            state=state,
            origin_server=origin_server,
            proxy_server=proxy_server,
            trap_server=trap_server,
            strategy_trace=extractor.trace,
        )
        self._scenarios.append(scenario)
        return scenario

    def concurrent_dynamic_monitors(self) -> _ConcurrentScenario:
        _require_browser_assets()
        state = _FixtureState()
        origin_handler = type("ConcurrentOriginHandler", (_OriginHandler,), {"state": state})
        origin_server = _LocalServer(origin_handler)
        proxy_handler = type(
            "ConcurrentProxyHandler",
            (_ProxyHandler,),
            {"state": state, "origin_port": origin_server.port},
        )
        proxy_server = _LocalServer(proxy_handler)
        origin_url = f"http://{_LOOPBACK}:{origin_server.port}"
        self._install_network_guard()

        connection = open_database(self._tmp_path / "concurrent.sqlite3")
        registry = RegistryRepository(connection)
        runtime = RuntimeRepository(connection)
        registry.create_user("owner-concurrent", 3)
        registry.create_delivery_target(
            "target-concurrent", "owner-concurrent", "unused-local-address"
        )
        monitor_ids: list[str] = []
        for suffix in ("a", "b"):
            spec = MonitorSpec.model_validate(
                {
                    "schema_version": 1,
                    "owner_id": "owner-concurrent",
                    "name": f"Concurrent monitor {suffix}",
                    "target_url": f"{origin_url}/concurrent-{suffix}",
                    "source_adapter": "scrapling",
                    "fetch_strategy": "dynamic",
                    "extract": {
                        "item_scope": "main.listing",
                        "fields": {
                            "title": {"selector": "h1", "type": "text"},
                            "price": {"selector": ".price", "type": "krw"},
                        },
                    },
                    "validators": {"min_items": 1, "max_items": 1},
                    "rules": [{"kind": "new_item"}],
                }
            )
            monitor_id = registry.create_monitor(spec, created_by="owner-concurrent")
            connection.execute(
                "UPDATE monitors SET next_run_at = ? WHERE id = ?",
                (NOW.isoformat(), monitor_id),
            )
            monitor_ids.append(monitor_id)
        leases = runtime.claim_due(worker_id="worker-concurrent", now=NOW)
        assert len(leases) == 2
        adapter = ScraplingSourceAdapter(
            url_policy=_FixtureOriginPolicy(origin_url),  # type: ignore[arg-type]
            rate_limiter=HostRateLimiter(minimum_interval_seconds=0),
            egress_proxy_url=f"http://{_LOOPBACK}:{proxy_server.port}",
            clock=lambda: NOW,
        )
        scenario = _ConcurrentScenario(
            connection=connection,
            registry=registry,
            runtime=runtime,
            runner=MonitorRunner(
                registry=registry,
                runtime=runtime,
                adapters=DefaultAdapterRegistry(
                    scrapling=adapter,
                    official_api=_ForbiddenAdapter(),
                ),
                clock=_FixedClock(),
                worker_id="worker-concurrent",
            ),
            monitor_id=monitor_ids[0],
            lease=leases[0],
            origin_url=origin_url,
            state=state,
            origin_server=origin_server,
            proxy_server=proxy_server,
            monitor_ids=(monitor_ids[0], monitor_ids[1]),
            leases=(leases[0], leases[1]),
        )
        self._scenarios.append(scenario)
        return scenario

    def session_monitor(self) -> _SessionScenario:
        _require_browser_assets()
        state = _FixtureState()
        origin_handler = type("SessionOriginHandler", (_OriginHandler,), {"state": state})
        origin_server = _LocalServer(origin_handler)
        proxy_handler = type(
            "SessionProxyHandler",
            (_ProxyHandler,),
            {"state": state, "origin_port": origin_server.port},
        )
        proxy_server = _LocalServer(proxy_handler)
        origin_url = f"http://{_LOOPBACK}:{origin_server.port}"
        self._install_network_guard()

        vault_root = self._tmp_path / "session-vault"
        workspace_root = self._tmp_path / "profile-workspaces"
        vault = CredentialVault(vault_root, key=os.urandom(32))
        profile_store = BrowserProfileStore(vault, materialization_root=workspace_root)
        policy = _FixtureOriginPolicy(origin_url)
        login_target = asyncio.run(policy.validate(f"{origin_url}/login"))
        bootstrap_profile(
            profile_store,
            "local-profile",
            login_target,
            runner=DynamicFetcher.fetch,
            egress_proxy_url=f"http://{_LOOPBACK}:{proxy_server.port}",
            page_action=_bootstrap_noop,
            operator_timeout_seconds=10,
        )
        bootstrapped_cookie_rows = _count_profile_cookie_rows(
            profile_store,
            "local-profile",
        )

        database_path = self._tmp_path / "session.sqlite3"
        connection = open_database(database_path)
        registry = RegistryRepository(connection)
        runtime = RuntimeRepository(connection)
        registry.create_user("owner-session", 4)
        registry.create_delivery_target("target-session", "owner-session", "unused-local-address")
        base_spec = {
            "schema_version": 1,
            "owner_id": "owner-session",
            "target_url": f"{origin_url}/protected",
            "source_adapter": "scrapling",
            "fetch_strategy": "dynamic",
            "extract": {
                "item_scope": "main.listing",
                "fields": {
                    "title": {"selector": "h1", "type": "text"},
                    "price": {"selector": ".price", "type": "krw"},
                },
            },
            "validators": {"min_items": 1, "max_items": 1},
            "rules": [{"kind": "new_item"}],
        }
        unauthenticated_spec = MonitorSpec.model_validate(
            {**base_spec, "name": "Unauthenticated local session monitor"}
        )
        profiled_spec = MonitorSpec.model_validate(
            {
                **base_spec,
                "name": "Profiled local session monitor",
                "auth_profile_ref": "local-profile",
            }
        )
        unauthenticated_id = registry.create_monitor(
            unauthenticated_spec,
            created_by="owner-session",
        )
        profiled_id = registry.create_monitor(profiled_spec, created_by="owner-session")
        for monitor_id in (unauthenticated_id, profiled_id):
            connection.execute(
                "UPDATE monitors SET next_run_at = ? WHERE id = ?",
                (NOW.isoformat(), monitor_id),
            )
        leases = runtime.claim_due(worker_id="worker-session", now=NOW)
        leases_by_id = {lease.monitor_id: lease for lease in leases}
        assert leases_by_id.keys() == {unauthenticated_id, profiled_id}
        adapter = ScraplingSourceAdapter(
            url_policy=policy,  # type: ignore[arg-type]
            rate_limiter=HostRateLimiter(minimum_interval_seconds=0),
            egress_proxy_url=f"http://{_LOOPBACK}:{proxy_server.port}",
            profile_provider=profile_store,
            clock=lambda: NOW,
        )
        scenario = _SessionScenario(
            connection=connection,
            registry=registry,
            runtime=runtime,
            runner=MonitorRunner(
                registry=registry,
                runtime=runtime,
                adapters=DefaultAdapterRegistry(
                    scrapling=adapter,
                    official_api=_ForbiddenAdapter(),
                ),
                clock=_FixedClock(),
                worker_id="worker-session",
            ),
            monitor_id=unauthenticated_id,
            lease=leases_by_id[unauthenticated_id],
            origin_url=origin_url,
            state=state,
            origin_server=origin_server,
            proxy_server=proxy_server,
            unauthenticated_lease=leases_by_id[unauthenticated_id],
            profiled_monitor_id=profiled_id,
            profiled_lease=leases_by_id[profiled_id],
            profile_store=profile_store,
            vault=vault,
            workspace_root=workspace_root,
            vault_root=vault_root,
            database_path=database_path,
            baseline_child_pids=frozenset(
                process.pid
                for process in multiprocessing.active_children()
                if process.pid is not None
            ),
            adapter=adapter,
            bootstrapped_cookie_rows=bootstrapped_cookie_rows,
        )
        self._scenarios.append(scenario)
        return scenario

    def _install_network_guard(self) -> None:
        real_getaddrinfo = socket.getaddrinfo
        real_connect = socket.socket.connect

        def guarded_getaddrinfo(host: object, *args: object, **kwargs: object):
            if host not in {_LOOPBACK, "localhost", b"127.0.0.1", b"localhost"}:
                raise AssertionError("non-loopback DNS resolution attempted")
            return real_getaddrinfo(host, *args, **kwargs)

        def guarded_connect(sock: socket.socket, address: object):
            if isinstance(address, str) and sock.family == socket.AF_UNIX:
                return real_connect(sock, address)
            if not isinstance(address, tuple) or not address or address[0] != _LOOPBACK:
                raise AssertionError("non-loopback socket target attempted")
            return real_connect(sock, address)

        self._monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
        self._monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    def close(self) -> None:
        for scenario in reversed(self._scenarios):
            scenario.proxy_server.close()
            scenario.origin_server.close()
            if scenario.trap_server is not None:
                scenario.trap_server.close()
            scenario.connection.close()
            close_extra = getattr(scenario, "close_extra", None)
            if callable(close_extra):
                close_extra()


def _require_browser_assets() -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
    except Exception:
        executable = Path("/browser-assets-unavailable")
    if not executable.is_file():
        pytest.fail(
            "Scrapling browser assets are required; run "
            "`/Volumes/DEV_CACHE/WorkSpace/rental-housing-monitor/.venv/bin/scrapling "
            "install --force`"
        )


@pytest.fixture
def integration_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_IntegrationHarness]:
    harness = _IntegrationHarness(tmp_path=tmp_path, monkeypatch=monkeypatch)
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture(autouse=True)
def fail_fast_telegram_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TelegramClient, "send", _forbid_telegram_send)
