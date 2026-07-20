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
        request_with_retry(client, "GET", "https://official.example/notices", sleeper=lambda _: None)

    assert attempts == 3


def test_client_error_is_not_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        request_with_retry(client, "GET", "https://official.example/notices", sleeper=lambda _: None)

    assert attempts == 1
