from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from urllib.parse import urljoin, urlsplit

import httpx

from personal_monitor.engine.errors import ErrorClass, FetchError, MonitorError
from personal_monitor.security.egress import (
    EgressProxyIdentity,
    EgressProxyPolicy,
    hold_http_egress_slot,
)
from personal_monitor.security.url_policy import ResolvedTarget, has_unsafe_url_characters

MONITOR_USER_AGENT = "personal-monitor/0.1"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
TOTAL_TIMEOUT_SECONDS = 30.0
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
_MEDIA_TOKEN = r"[!#$%&'*+\-.^_`|~0-9a-z]+"
_MEDIA_TYPE = re.compile(rf"^({_MEDIA_TOKEN})/({_MEDIA_TOKEN})$")
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class PolicyHttpResponse:
    final_url: str = field(repr=False)
    status: int
    headers: MappingProxyType[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    redirect_location: str | None = field(default=None, repr=False)
    peer_ip: str | None = field(default=None, repr=False)
    retry_after_seconds: float | None = field(default=None, repr=False)


class _BoundedBodyAccumulator:
    def __init__(self, *, max_bytes: int = MAX_RESPONSE_BYTES) -> None:
        self._max_bytes = max_bytes
        self._body = bytearray()

    @property
    def retained_bytes(self) -> int:
        return len(self._body)

    def extend(self, chunk: bytes) -> None:
        if len(chunk) > self._max_bytes - len(self._body):
            raise _policy_error("response exceeds the 10 MiB decompressed size limit")
        self._body.extend(chunk)

    def to_bytes(self) -> bytes:
        return bytes(self._body)


@dataclass(frozen=True, slots=True, init=False)
class BoundedPolicyHttpClient:
    """A proxy-required, GET-only HTTP client with two fixed request shapes."""

    _egress_policy: EgressProxyPolicy = field(repr=False)
    _clock: Clock = field(repr=False)

    def __init__(
        self,
        *,
        egress_proxy_url: str | None,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        object.__setattr__(self, "_egress_policy", EgressProxyPolicy.from_url(egress_proxy_url))
        object.__setattr__(self, "_clock", clock)

    @classmethod
    def _from_egress_policy(
        cls,
        policy: EgressProxyPolicy,
        *,
        clock: Clock,
    ) -> BoundedPolicyHttpClient:
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_egress_policy", policy)
        object.__setattr__(instance, "_clock", clock)
        return instance

    @property
    def proxy_identity(self) -> EgressProxyIdentity:
        return self._egress_policy.identity

    def _uses_egress_policy(self, policy: EgressProxyPolicy) -> bool:
        return self._egress_policy.has_same_provenance(policy)

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
            "proxy": self._egress_policy.url,
        }
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                async with (
                    hold_http_egress_slot(),
                    httpx.AsyncClient(**kwargs) as client,  # type: ignore[arg-type]
                ):
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
                    response = await client.send(request, stream=True, follow_redirects=False)
                    try:
                        return await _read_response(
                            response,
                            require_json=require_json,
                            clock=self._clock,
                        )
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
    clock: Clock,
) -> PolicyHttpResponse:
    content_encodings = response.headers.get_list("content-encoding")
    if len(content_encodings) > 1 or (
        content_encodings
        and ("," in content_encodings[0] or content_encodings[0].strip().casefold() != "identity")
    ):
        raise _policy_error("response Content-Encoding must be identity")
    body = _BoundedBodyAccumulator()
    async for chunk in response.aiter_raw():
        body.extend(chunk)

    status = response.status_code
    if isinstance(status, bool) or not 100 <= status <= 599:
        raise _policy_error("response status is missing or invalid")
    final_url = _safe_absolute_url(str(response.url), "response final URL is invalid")
    content_types = response.headers.get_list("content-type")
    locations = response.headers.get_list("location")
    retry_after = _retry_after(response.headers.get_list("retry-after"), clock=clock)
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
        if not 200 <= status <= 299:
            raise _policy_error("official endpoint returned an invalid terminal status")
        if len(content_types) != 1 or "," in content_types[0]:
            raise _policy_error("response Content-Type is missing or duplicated")
        media_type = content_types[0].split(";", 1)[0].strip().casefold()
        match = _MEDIA_TYPE.fullmatch(media_type)
        if match is None:
            raise _policy_error("official endpoint did not return JSON")
        main_type, subtype = match.groups()
        if main_type != "application" or not (
            subtype == "json" or (subtype.endswith("+json") and subtype != "+json")
        ):
            raise _policy_error("official endpoint did not return JSON")

    return PolicyHttpResponse(
        final_url=final_url,
        status=status,
        headers=MappingProxyType(retained),
        body=body.to_bytes(),
        redirect_location=redirect_location,
        # httpx exposes the connected proxy socket here, not an origin peer
        # attested by the policy proxy. Treat it as unavailable.
        peer_ip=None,
        retry_after_seconds=retry_after,
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


def _retry_after(values: list[str], *, clock: Clock) -> float | None:
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
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise MonitorError(ErrorClass.INTERNAL, "clock", "clock returned an invalid timestamp")
    return max(0.0, (value - now).total_seconds())


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
