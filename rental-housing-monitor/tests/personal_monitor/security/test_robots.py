from __future__ import annotations

from datetime import UTC, datetime

import pytest

from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.security.robots import RobotsPolicy
from personal_monitor.security.url_policy import PolicyError

CHECKED_AT = datetime(2026, 7, 22, 3, 13, tzinfo=UTC)


def test_robots_disallow_has_no_override_and_converts_to_policy_error() -> None:
    policy = RobotsPolicy.from_text(
        "User-agent: *\nDisallow: /private\n",
        "https://example.com/robots.txt",
        checked_at=CHECKED_AT,
    )

    decision = policy.check("personal-monitor", "https://example.com/private/a")

    assert decision.allowed is False
    assert decision.checked_at == CHECKED_AT
    with pytest.raises(MonitorError, match="robots.txt disallows this path") as caught:
        decision.require_allowed()
    assert caught.value.error_class is ErrorClass.POLICY
    assert caught.value.stage == "robots"


def test_robots_allow_reports_crawl_delay() -> None:
    policy = RobotsPolicy.from_text(
        "User-agent: personal-monitor\nCrawl-delay: 25\nDisallow:\n",
        "https://example.com/robots.txt",
        checked_at=CHECKED_AT,
    )

    decision = policy.check("personal-monitor", "https://example.com/public")

    assert decision.allowed is True
    assert decision.crawl_delay_seconds == 25.0
    decision.require_allowed()


def test_robots_fetch_failure_is_fail_open_without_claimed_crawl_delay() -> None:
    policy = RobotsPolicy.from_fetch_failure(
        "https://example.com/robots.txt", checked_at=CHECKED_AT
    )

    decision = policy.check("personal-monitor", "https://example.com/private")

    assert decision.allowed is True
    assert decision.crawl_delay_seconds is None
    assert decision.checked_at == CHECKED_AT
    assert decision.policy_fetched is False


def test_robots_success_records_that_policy_was_fetched() -> None:
    policy = RobotsPolicy.from_text(
        "User-agent: *\nAllow: /\n",
        "https://example.com/robots.txt",
        checked_at=CHECKED_AT,
    )

    assert policy.check("personal-monitor", "https://example.com/").policy_fetched is True


def test_robots_policy_cannot_be_applied_to_a_different_origin() -> None:
    policy = RobotsPolicy.from_text(
        "User-agent: *\nAllow: /\n",
        "https://example.com/robots.txt",
        checked_at=CHECKED_AT,
    )

    with pytest.raises(PolicyError, match="origin"):
        policy.check("personal-monitor", "https://other.example/path")


def test_robots_origin_treats_unicode_and_punycode_hosts_as_equivalent() -> None:
    policy = RobotsPolicy.from_text(
        "User-agent: *\nAllow: /\n",
        "https://faß.de/robots.txt",
        checked_at=CHECKED_AT,
    )

    decision = policy.check("personal-monitor", "https://xn--fa-hia.de/path")

    assert decision.allowed is True


@pytest.mark.parametrize(
    "robots_url",
    [
        "https://\ud800.example/robots.txt",
        "https://under_score.example/robots.txt",
        "https://example.com\x00.evil/robots.txt",
        "https://example.com\\evil/robots.txt",
    ],
)
def test_robots_origin_rejects_malformed_or_ambiguous_hostname(robots_url: str) -> None:
    with pytest.raises(PolicyError, match="robots"):
        RobotsPolicy.from_text(
            "User-agent: *\nAllow: /\n",
            robots_url,
            checked_at=CHECKED_AT,
        )


@pytest.mark.parametrize(
    "robots_url",
    [
        "https://example.com:0/robots.txt",
        "https://example.com:/robots.txt",
        "https://example.com:not-a-port/robots.txt",
    ],
)
def test_robots_origin_rejects_zero_empty_or_malformed_port(robots_url: str) -> None:
    with pytest.raises(PolicyError, match="robots"):
        RobotsPolicy.from_text(
            "User-agent: *\nAllow: /\n",
            robots_url,
            checked_at=CHECKED_AT,
        )


def test_robots_requires_timezone_aware_checked_at() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        RobotsPolicy.from_text(
            "User-agent: *\nAllow: /\n",
            "https://example.com/robots.txt",
            checked_at=datetime(2026, 7, 22, 3, 13),
        )
