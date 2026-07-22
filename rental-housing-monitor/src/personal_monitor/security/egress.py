from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from urllib.parse import urlsplit

from personal_monitor.security.url_policy import has_unsafe_url_characters

HTTP_EGRESS_CONCURRENCY = 4
HTTP_EGRESS_GATE = threading.BoundedSemaphore(HTTP_EGRESS_CONCURRENCY)
_IDENTITY_KEY = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True, repr=False)
class EgressProxyIdentity:
    _digest: bytes = field(repr=False)

    def matches(self, other: object) -> bool:
        return isinstance(other, EgressProxyIdentity) and hmac.compare_digest(
            self._digest,
            other._digest,
        )

    def __repr__(self) -> str:
        return "<egress-proxy-identity>"


@dataclass(frozen=True, slots=True, repr=False)
class EgressProxyPolicy:
    _url: str = field(repr=False)
    _digest: bytes = field(repr=False)

    @classmethod
    def from_url(cls, value: str | None) -> EgressProxyPolicy:
        url = _validate_egress_proxy(value)
        digest = hmac.digest(_IDENTITY_KEY, url.encode("utf-8"), sha256)
        return cls(url, digest)

    @property
    def url(self) -> str:
        if not self.is_valid:
            raise ValueError("egress proxy policy integrity check failed")
        return self._url

    @property
    def identity(self) -> EgressProxyIdentity:
        if not self.is_valid:
            raise ValueError("egress proxy policy integrity check failed")
        return EgressProxyIdentity(self._digest)

    @property
    def is_valid(self) -> bool:
        try:
            url = _validate_egress_proxy(self._url)
        except ValueError:
            return False
        expected = hmac.digest(_IDENTITY_KEY, url.encode("utf-8"), sha256)
        return hmac.compare_digest(self._digest, expected)

    def has_same_provenance(self, other: object) -> bool:
        return self is other and self.is_valid


def _validate_egress_proxy(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("an egress proxy is required")
    if value != value.strip() or has_unsafe_url_characters(value):
        raise ValueError("egress proxy URL is invalid")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("egress proxy URL is invalid") from None
    if (
        parts.scheme.casefold() not in {"http", "https"}
        or not parts.hostname
        or parts.netloc.rsplit("@", 1)[-1].endswith(":")
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError("egress proxy URL is invalid")
    return value


def require_egress_proxy(value: str | None) -> tuple[str, EgressProxyIdentity]:
    policy = EgressProxyPolicy.from_url(value)
    return policy.url, policy.identity


@asynccontextmanager
async def hold_http_egress_slot():
    acquired = False
    try:
        while not acquired:
            acquired = HTTP_EGRESS_GATE.acquire(blocking=False)
            if not acquired:
                await asyncio.sleep(0.01)
        yield
    finally:
        if acquired:
            HTTP_EGRESS_GATE.release()
