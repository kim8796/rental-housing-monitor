import logging

import httpx
import pytest

from rental_monitor.collectors.base import request_with_retry


def test_transient_http_errors_are_retried_three_times() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        request_with_retry(
            client, "GET", "https://official.example/notices", sleeper=lambda _: None
        )

    assert attempts == 3


def test_client_error_is_not_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        request_with_retry(
            client, "GET", "https://official.example/notices", sleeper=lambda _: None
        )

    assert attempts == 1


def test_retry_attempt_is_logged_without_request_url(caplog: pytest.LogCaptureFixture) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        status = 503 if attempts == 1 else 200
        return httpx.Response(status, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        request_with_retry(
            client,
            "GET",
            "https://official.example/notices?ServiceKey=secret",
            sleeper=lambda _: None,
        )

    assert "attempt=1/3" in caplog.text
    assert "secret" not in caplog.text
