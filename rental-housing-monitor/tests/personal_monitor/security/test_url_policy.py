from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from personal_monitor.engine.errors import ErrorClass
from personal_monitor.security.url_policy import PolicyError, UrlPolicy, is_public_address


class FakeResolver:
    def __init__(
        self,
        answers: Sequence[str] = ("93.184.216.34",),
        *,
        error: Exception | None = None,
    ) -> None:
        self.answers = answers
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, hostname: str, port: int) -> Sequence[str]:
        self.calls.append((hostname, port))
        if self.error is not None:
            raise self.error
        return self.answers


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/pub",
        "https:///missing-host",
        "http://localhost/admin",
        "http://LOCALHOST./admin",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://169.254.169.254/computeMetadata/v1/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "https://user:pass@example.com/",
        "https://user@example.com/",
        "https://:pass@example.com/",
        "https://example.com:8443/",
        "https://example.com:0/",
        "https://example.com:/",
        "https://example.com:not-a-port/",
        "https://example.com:99999/",
        "https://example.com\x00.evil/",
        "https://example.com\n.evil/",
        "https://example.com\\@127.0.0.1/",
        "https://under_score.example/",
        "https://-leading.example/",
        "https://trailing-.example/",
        "https://double..dot.example/",
        "https://example.com../",
        "https://2130706433/",
        "https://0177.0.0.1/",
        "https://0x7f000001/",
        "https://127.1/",
    ],
)
def test_url_policy_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(PolicyError):
        asyncio.run(UrlPolicy(FakeResolver()).validate(url))


def test_url_policy_normalizes_target_and_pins_all_dns_answers() -> None:
    resolver = FakeResolver((" 93.184.216.34 ", "2606:2800:220:1:248:1893:25c8:1946"))

    target = asyncio.run(
        UrlPolicy(resolver).validate("HTTPS://Example.COM.:443/notices?q=1#section")
    )

    assert target.normalized_url == "https://example.com/notices?q=1"
    assert target.hostname == "example.com"
    assert target.port == 443
    assert target.addresses == frozenset({"93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"})
    assert resolver.calls == [("example.com", 443)]


def test_url_policy_canonicalizes_an_idna_hostname() -> None:
    resolver = FakeResolver()

    target = asyncio.run(UrlPolicy(resolver).validate("https://서울.kr/공고"))

    assert target.hostname == "xn--2i4bq6h.kr"
    assert target.normalized_url == "https://xn--2i4bq6h.kr/공고"
    assert resolver.calls == [("xn--2i4bq6h.kr", 443)]


@pytest.mark.parametrize(
    "answers",
    [
        (),
        ("93.184.216.34", "10.0.0.1"),
        ("not-an-ip",),
        ("",),
        ("fe80::1",),
        ("224.0.0.1",),
    ],
)
def test_url_policy_rejects_empty_invalid_or_non_public_dns_answers(
    answers: Sequence[str],
) -> None:
    with pytest.raises(PolicyError, match="DNS"):
        asyncio.run(UrlPolicy(FakeResolver(answers)).validate("https://example.com"))


def test_url_policy_converts_dns_failure_to_safe_policy_error() -> None:
    with pytest.raises(PolicyError, match="DNS resolution failed") as caught:
        asyncio.run(
            UrlPolicy(FakeResolver(error=OSError("resolver secret"))).validate(
                "https://example.com"
            )
        )

    assert caught.value.error_class is ErrorClass.POLICY
    assert caught.value.stage == "url_policy"
    assert "resolver secret" not in caught.value.safe_detail


def test_url_policy_validates_public_ip_literal_without_trusting_dns() -> None:
    resolver = FakeResolver(("10.0.0.7",))

    target = asyncio.run(UrlPolicy(resolver).validate("https://93.184.216.34/path"))

    assert target.addresses == frozenset({"93.184.216.34"})
    assert resolver.calls == []


@pytest.mark.parametrize(
    "address",
    [
        "100.64.0.1",  # shared carrier-grade NAT space
        "::ffff:100.64.0.1",  # mapped non-global IPv4
        "fec0::1",  # deprecated IPv6 site-local space
        "192.0.2.1",  # documentation-only IPv4
        "2001:db8::1",  # documentation-only IPv6
        "198.18.0.1",  # benchmark network
    ],
)
def test_non_global_addresses_are_never_public_targets(address: str) -> None:
    assert is_public_address(address) is False


def test_redirect_number_five_is_allowed_and_number_six_is_rejected() -> None:
    policy = UrlPolicy(FakeResolver())

    target = asyncio.run(policy.validate_redirect("https://example.com/next", redirect_count=5))
    assert target.normalized_url == "https://example.com/next"

    with pytest.raises(PolicyError, match="redirect"):
        asyncio.run(policy.validate_redirect("https://example.com/six", redirect_count=6))


def test_redirect_count_must_be_non_negative() -> None:
    with pytest.raises(PolicyError, match="redirect"):
        asyncio.run(
            UrlPolicy(FakeResolver()).validate_redirect(
                "https://example.com/next", redirect_count=-1
            )
        )


def test_dns_rebinding_peer_is_rejected() -> None:
    target = asyncio.run(
        UrlPolicy(FakeResolver(("93.184.216.34",))).validate("https://example.com")
    )

    with pytest.raises(PolicyError, match="peer"):
        UrlPolicy.validate_peer(target, "10.0.0.7")
    with pytest.raises(PolicyError, match="peer"):
        UrlPolicy.validate_peer(target, "93.184.216.35")


def test_peer_address_is_normalized_before_matching_pinned_dns_answer() -> None:
    target = asyncio.run(
        UrlPolicy(FakeResolver(("2606:2800:220:1:248:1893:25c8:1946",))).validate(
            "https://example.com"
        )
    )

    UrlPolicy.validate_peer(target, "[2606:2800:0220:0001:0248:1893:25c8:1946]")


def test_public_ipv4_mapped_peer_matches_its_pinned_ipv4_answer() -> None:
    target = asyncio.run(
        UrlPolicy(FakeResolver(("93.184.216.34",))).validate("https://example.com")
    )

    UrlPolicy.validate_peer(target, "::ffff:93.184.216.34")


@pytest.mark.parametrize("peer", ["", "not-an-ip", "[::1]", "[broken"])
def test_invalid_or_non_public_peer_is_rejected(peer: str) -> None:
    target = asyncio.run(
        UrlPolicy(FakeResolver(("93.184.216.34",))).validate("https://example.com")
    )

    with pytest.raises(PolicyError, match="peer"):
        UrlPolicy.validate_peer(target, peer)
