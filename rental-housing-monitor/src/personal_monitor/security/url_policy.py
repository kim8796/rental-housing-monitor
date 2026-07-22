from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from personal_monitor.engine.errors import ErrorClass, MonitorError

ALLOWED_PORTS = frozenset({80, 443})
BLOCKED_HOSTS = frozenset({"localhost", "metadata.google.internal"})
MAX_REDIRECTS = 5
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_LEGACY_NUMERIC_COMPONENT = re.compile(r"(?:[0-9]+|0x[0-9a-f]+)\Z")


class Resolver(Protocol):
    def resolve(self, hostname: str, port: int) -> Awaitable[Iterable[str]]: ...


class PolicyError(MonitorError):
    """A safe-to-report rejection at an outbound network policy boundary."""

    def __init__(self, safe_detail: str, *, stage: str = "url_policy") -> None:
        super().__init__(ErrorClass.POLICY, stage, safe_detail)


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    normalized_url: str
    hostname: str
    port: int
    addresses: frozenset[str]


def is_public_address(value: str) -> bool:
    address = _parse_address(value)
    if (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.is_site_local:
            return False
        if address.ipv4_mapped is not None and not address.ipv4_mapped.is_global:
            return False
    return True


class UrlPolicy:
    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    async def validate(self, url: str) -> ResolvedTarget:
        parts, hostname, port = _parse_target(url)

        literal = _try_parse_address(hostname)
        if literal is not None:
            normalized_addresses = frozenset({_canonical_address(literal)})
        else:
            try:
                answers = await self._resolver.resolve(hostname, port)
                normalized_addresses = _normalize_answers(answers)
            except PolicyError:
                raise
            except Exception:
                raise PolicyError("DNS resolution failed") from None

        if not normalized_addresses or any(
            not is_public_address(value) for value in normalized_addresses
        ):
            raise PolicyError("DNS resolved to an empty, invalid, or non-public address set")

        return ResolvedTarget(
            normalized_url=_normalize_url(parts, hostname, port),
            hostname=hostname,
            port=port,
            addresses=normalized_addresses,
        )

    async def validate_redirect(self, url: str, *, redirect_count: int) -> ResolvedTarget:
        if (
            isinstance(redirect_count, bool)
            or not isinstance(redirect_count, int)
            or redirect_count < 0
            or redirect_count > MAX_REDIRECTS
        ):
            raise PolicyError(f"redirect count must be between 0 and {MAX_REDIRECTS}")
        return await self.validate(url)

    @staticmethod
    def validate_peer(target: ResolvedTarget, peer_ip: str) -> None:
        try:
            peer = _parse_peer(peer_ip)
        except ValueError:
            raise PolicyError("connected peer is invalid or non-public") from None
        normalized = _canonical_address(peer)
        if not is_public_address(normalized) or normalized not in target.addresses:
            raise PolicyError("connected peer is not in the approved DNS address set")


def _parse_target(url: str) -> tuple[SplitResult, str, int]:
    if not isinstance(url, str):
        raise PolicyError("target URL must be a string")
    if _has_unsafe_raw_character(url):
        raise PolicyError("target URL contains an unsafe character")
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.casefold()
        hostname_value = parts.hostname
        username = parts.username
        password = parts.password
        port = parts.port
    except ValueError:
        raise PolicyError("target URL is malformed") from None

    if scheme not in {"http", "https"} or not hostname_value:
        raise PolicyError("only absolute http(s) URLs are allowed")
    if username is not None or password is not None:
        raise PolicyError("URL userinfo is not allowed")

    hostname = _normalize_hostname(hostname_value)
    if hostname in BLOCKED_HOSTS:
        raise PolicyError("target host is blocked")

    if _has_explicit_empty_port(parts.netloc):
        raise PolicyError("target port is malformed")
    resolved_port = port if port is not None else (443 if scheme == "https" else 80)
    if resolved_port not in ALLOWED_PORTS:
        raise PolicyError("target port is blocked")

    literal = _try_parse_address(hostname)
    if literal is not None and not is_public_address(literal.compressed):
        raise PolicyError("target IP address is non-public")

    return parts._replace(scheme=scheme), hostname, resolved_port


def _normalize_hostname(value: str) -> str:
    folded = value.casefold()
    if folded.endswith(".."):
        raise PolicyError("target hostname is malformed")
    hostname = folded[:-1] if folded.endswith(".") else folded
    if not hostname or _has_unsafe_raw_character(hostname):
        raise PolicyError("target hostname is malformed")
    if "%" in hostname:
        raise PolicyError("scoped IP addresses are not allowed")
    if _try_parse_address(hostname) is not None:
        return hostname
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise PolicyError("target hostname is malformed") from None
    if len(ascii_hostname) > 253 or any(
        _DNS_LABEL.fullmatch(label) is None for label in ascii_hostname.split(".")
    ):
        raise PolicyError("target hostname is malformed")
    if all(
        _LEGACY_NUMERIC_COMPONENT.fullmatch(label) is not None
        for label in ascii_hostname.split(".")
    ):
        raise PolicyError("legacy numeric IP spellings are not allowed")
    return ascii_hostname


def _normalize_answers(answers: Iterable[str]) -> frozenset[str]:
    try:
        return frozenset(_canonical_address(_parse_address(answer)) for answer in answers)
    except (TypeError, ValueError):
        raise PolicyError("DNS returned an invalid address") from None


def _parse_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if not isinstance(value, str):
        raise ValueError("IP address must be text")
    candidate = value.strip()
    if not candidate or "%" in candidate:
        raise ValueError("IP address is empty or scoped")
    return ipaddress.ip_address(candidate)


def _try_parse_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return _parse_address(value)
    except ValueError:
        return None


def _parse_peer(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    return _parse_address(candidate)


def _canonical_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> str:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.compressed
    return address.compressed


def _has_unsafe_raw_character(value: str) -> bool:
    return "\\" in value or any(
        unicodedata.category(character).startswith("C") for character in value
    )


def _has_explicit_empty_port(netloc: str) -> bool:
    authority = netloc.rsplit("@", 1)[-1]
    return authority.endswith(":")


def _normalize_url(parts: SplitResult, hostname: str, port: int) -> str:
    default_port = 443 if parts.scheme == "https" else 80
    display_hostname = (
        f"[{hostname}]" if _try_parse_address(hostname) and ":" in hostname else hostname
    )
    netloc = display_hostname if port == default_port else f"{display_hostname}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
