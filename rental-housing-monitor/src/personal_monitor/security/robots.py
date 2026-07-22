from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from personal_monitor.engine.errors import ErrorClass, MonitorError
from personal_monitor.security.url_policy import PolicyError


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    allowed: bool
    crawl_delay_seconds: float | None
    checked_at: datetime
    policy_fetched: bool

    def require_allowed(self) -> None:
        if not self.allowed:
            raise MonitorError(
                ErrorClass.POLICY,
                "robots",
                "robots.txt disallows this path",
            )


class RobotsPolicy:
    def __init__(
        self,
        *,
        robots_url: str,
        parser: RobotFileParser | None,
        checked_at: datetime,
    ) -> None:
        _require_aware(checked_at)
        self._robots_url = robots_url
        self._origin = _origin(robots_url)
        self._parser = parser
        self._checked_at = checked_at

    @classmethod
    def from_text(
        cls,
        body: str,
        robots_url: str,
        *,
        checked_at: datetime | None = None,
    ) -> RobotsPolicy:
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(body.splitlines())
        return cls(
            robots_url=robots_url,
            parser=parser,
            checked_at=checked_at or datetime.now(UTC),
        )

    @classmethod
    def from_fetch_failure(
        cls,
        robots_url: str,
        *,
        checked_at: datetime | None = None,
    ) -> RobotsPolicy:
        return cls(
            robots_url=robots_url,
            parser=None,
            checked_at=checked_at or datetime.now(UTC),
        )

    def check(self, user_agent: str, url: str) -> RobotsDecision:
        if _origin(url) != self._origin:
            raise PolicyError("robots policy origin does not match target", stage="robots")
        if self._parser is None:
            return RobotsDecision(
                allowed=True,
                crawl_delay_seconds=None,
                checked_at=self._checked_at,
                policy_fetched=False,
            )
        crawl_delay = self._parser.crawl_delay(user_agent)
        return RobotsDecision(
            allowed=self._parser.can_fetch(user_agent, url),
            crawl_delay_seconds=float(crawl_delay) if crawl_delay is not None else None,
            checked_at=self._checked_at,
            policy_fetched=True,
        )


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.casefold()
        hostname = (parts.hostname or "").rstrip(".").casefold()
        if parts.netloc.rsplit("@", 1)[-1].endswith(":"):
            raise ValueError("empty port")
        port = parts.port if parts.port is not None else (443 if scheme == "https" else 80)
    except ValueError:
        raise PolicyError("robots URL is malformed", stage="robots") from None
    if scheme not in {"http", "https"} or not hostname:
        raise PolicyError("robots URL must be absolute http(s)", stage="robots")
    if port <= 0:
        raise PolicyError("robots URL port is invalid", stage="robots")
    return scheme, hostname, port


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")
