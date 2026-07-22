from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from urllib.parse import urljoin, urlsplit

import httpx

from personal_monitor.engine.errors import ErrorClass, FetchError, MonitorError
from personal_monitor.security.url_policy import ResolvedTarget, has_unsafe_url_characters

MONITOR_USER_AGENT = "personal-monitor/0.1"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_JSON_HEADERS = {
    "User-Agent": MONITOR_USER_AGENT,
    "Accept": "application/json",
    "Accept-Encoding": "identity",
}
_ROBOTS_HEADERS = {
    "User-Agent": MONITOR_USER_AGENT,
    "Accept": "text/plain",
    "Accept-Encoding": "identity",
}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class PolicyHttpResponse:
    final_url: str = field(repr=False)
    status: int
    headers: MappingProxyType[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    redirect_location: str | None = field(default=None, repr=False)
    peer_ip: str | None = field(default=None, repr=False)


class BoundedPolicyHttpClient:
    """A proxy-required, GET-only HTTP client with two fixed request shapes."""

    def __init__(self, *, egress_proxy_url: str | None) -> None:
        self._proxy_url = _required_proxy(egress_proxy_url)
        self._test_transport: httpx.AsyncBaseTransport | None = None

    @classmethod
    def for_test(
        cls,
        *,
        egress_proxy_url: str,
        transport: httpx.AsyncBaseTransport,
    ) -> BoundedPolicyHttpClient:
        if not isinstance(transport, httpx.AsyncBaseTransport):
            raise TypeError("test transport must be an httpx async transport")
        instance = cls(egress_proxy_url=egress_proxy_url)
        instance._test_transport = transport
        return instance

    async def get_json(self, target: ResolvedTarget) -> PolicyHttpResponse:
        return await self._get(target, headers=_JSON_HEADERS, require_json=True)

    async def get_robots(self, target: ResolvedTarget) -> PolicyHttpResponse:
        return await self._get(target, headers=_ROBOTS_HEADERS, require_json=False)

    async def _get(
        self,
        target: ResolvedTarget,
        *,
        headers: dict[str, str],
        require_json: bool,
    ) -> PolicyHttpResponse:
        if not isinstance(target, ResolvedTarget):
            raise TypeError("target must be an approved ResolvedTarget")
        timeout = httpx.Timeout(30.0, connect=10.0)
        kwargs: dict[str, object] = {
            "timeout": timeout,
            "follow_redirects": False,
        }
        if self._test_transport is None:
            kwargs["proxy"] = self._proxy_url
        else:
            kwargs["transport"] = self._test_transport
        try:
            async with httpx.AsyncClient(**kwargs) as client:  # type: ignore[arg-type]
                request = httpx.Request(
                    "GET",
                    target.normalized_url,
                    headers=headers,
                    extensions={
                        "timeout": {
                            "connect": 10.0,
                            "read": 30.0,
                            "write": 30.0,
                            "pool": 30.0,
                        }
                    },
                )
                async with asyncio.timeout(30.0):
                    response = await client.send(request, stream=True, follow_redirects=False)
                    try:
                        return await _read_response(response, require_json=require_json)
                    finally:
                        await response.aclose()
        except FetchError:
            raise
        except MonitorError:
            raise
        except httpx.TimeoutException:
            raise FetchError(
                ErrorClass.TRANSIENT_NETWORK,
                "fetch timed out",
                status=0,
            ) from None
        except TimeoutError:
            raise FetchError(
                ErrorClass.TRANSIENT_NETWORK,
                "fetch timed out",
                status=0,
            ) from None
        except httpx.HTTPError:
            raise FetchError(
                ErrorClass.TRANSIENT_NETWORK,
                "fetch failed",
                status=0,
            ) from None


async def _read_response(
    response: httpx.Response,
    *,
    require_json: bool,
) -> PolicyHttpResponse:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise _policy_error("response exceeds the 10 MiB decompressed size limit")

    status = response.status_code
    if isinstance(status, bool) or not 100 <= status <= 599:
        raise _policy_error("response status is missing or invalid")
    final_url = _safe_absolute_url(str(response.url), "response final URL is invalid")
    content_types = response.headers.get_list("content-type")
    locations = response.headers.get_list("location")
    retry_after = _retry_after(response.headers.get_list("retry-after"))
    retained: dict[str, str] = {}
    for name, values in (
        ("content-type", content_types),
        ("location", locations),
        ("retry-after", response.headers.get_list("retry-after")),
    ):
        if len(values) == 1:
            retained[name] = values[0]

    redirect_location = None
    if status in _REDIRECT_STATUSES:
        if len(locations) != 1 or not locations[0] or "," in locations[0]:
            raise _policy_error("navigation redirect requires exactly one Location header")
        raw_location = locations[0]
        if has_unsafe_url_characters(raw_location):
            raise _policy_error("navigation redirect Location is invalid")
        redirect_location = _safe_absolute_url(
            urljoin(final_url, raw_location),
            "navigation redirect Location is invalid",
        )
    elif require_json:
        _raise_for_status(status, retry_after)
        if len(content_types) != 1 or "," in content_types[0]:
            raise _policy_error("response Content-Type is missing or duplicated")
        media_type = content_types[0].split(";", 1)[0].strip().casefold()
        if media_type != "application/json" and not (
            media_type.startswith("application/") and media_type.endswith("+json")
        ):
            raise _policy_error("official endpoint did not return JSON")

    return PolicyHttpResponse(
        final_url=final_url,
        status=status,
        headers=MappingProxyType(retained),
        body=bytes(body),
        redirect_location=redirect_location,
        # httpx exposes the connected proxy socket here, not an origin peer
        # attested by the policy proxy. Treat it as unavailable.
        peer_ip=None,
    )


def _raise_for_status(status: int, retry_after: float | None) -> None:
    if status in {401, 403}:
        raise FetchError(
            ErrorClass.AUTHENTICATION,
            "authentication was rejected",
            status=status,
            retry_after_seconds=retry_after,
        )
    if status == 429 or 500 <= status <= 599:
        raise FetchError(
            ErrorClass.TRANSIENT_NETWORK,
            "remote service is temporarily unavailable",
            status=status,
            retry_after_seconds=retry_after,
        )
    if 400 <= status <= 499:
        raise FetchError(
            ErrorClass.POLICY,
            "remote service rejected the request",
            status=status,
            retry_after_seconds=retry_after,
        )


def _retry_after(values: list[str]) -> float | None:
    if len(values) != 1:
        return None
    raw = values[0].strip()
    if raw.isascii() and raw.isdigit() and len(raw) <= 10:
        return float(int(raw))
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if value.tzinfo is None:
        return None
    # Date-form Retry-After is intentionally not accepted here: the adapter has
    # no wall clock at this transport boundary, so it cannot calculate safely.
    return None


def _required_proxy(value: str | None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("an egress proxy is required")
    if has_unsafe_url_characters(value):
        raise ValueError("egress proxy URL is invalid")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("egress proxy URL is invalid") from None
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not parts.hostname
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError("egress proxy URL is invalid")
    return value


def _safe_absolute_url(value: str, detail: str) -> str:
    if not value or has_unsafe_url_characters(value):
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


def _policy_error(detail: str) -> MonitorError:
    return MonitorError(ErrorClass.POLICY, "fetch", detail)
