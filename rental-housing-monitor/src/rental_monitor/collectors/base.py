from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from rental_monitor.models import Agency, Announcement

logger = logging.getLogger(__name__)


class Collector(Protocol):
    agency: Agency

    def collect(self) -> list[Announcement]: ...


class CollectorError(RuntimeError):
    def __init__(self, agency: Agency, stage: str, message: str) -> None:
        self.agency = agency
        self.stage = stage
        self.detail = message
        super().__init__(f"{agency.value} {stage}: {message}")


class ParserStructureError(CollectorError):
    pass


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    attempts: int = 3,
    **kwargs: object,
) -> httpx.Response:
    for attempt in range(attempts):
        status_code: int | None = None
        try:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            retryable = error.response.status_code == 429 or error.response.status_code >= 500
            if not retryable or attempt == attempts - 1:
                raise
            error_type = type(error).__name__
        except (httpx.TimeoutException, httpx.TransportError) as error:
            if attempt == attempts - 1:
                raise
            error_type = type(error).__name__
        logger.warning(
            "HTTP 재시도 attempt=%d/%d error_type=%s status=%s",
            attempt + 1,
            attempts,
            error_type,
            status_code if status_code is not None else "transport",
        )
        sleeper(0.5 * (2**attempt))
    raise RuntimeError("unreachable retry state")
