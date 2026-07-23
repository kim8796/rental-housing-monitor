from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import Any

import httpx
import pytest

import personal_monitor.telegram.api as api_module
from personal_monitor.telegram import (
    CallbackQuery,
    InlineButton,
    TelegramApi,
    TelegramApiError,
    TelegramMessage,
    TelegramUpdate,
)

TOKEN = "unit-test-bot-token"
MAX_RESPONSE_BYTES = 1024 * 1024


def message_json(text: str = "감시해줘") -> dict[str, object]:
    return {
        "message_id": 31,
        "chat": {"id": 42, "ignored": "chat-secret"},
        "from": {"id": 7, "ignored": "user-secret"},
        "text": text,
        "ignored": "response-secret",
    }


def callback_json(data: str = "confirm:opaque") -> dict[str, object]:
    return {
        "id": "callback-query-id",
        "from": {"id": 7},
        "message": {"message_id": 31, "chat": {"id": 42}},
        "data": data,
        "ignored": "response-secret",
    }


class TelegramHarness:
    def __init__(
        self,
        responses: list[httpx.Response | BaseException | Callable[[httpx.Request], httpx.Response]],
        *,
        token: str = TOKEN,
    ) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if not self.responses:
                raise AssertionError("unexpected HTTP request")
            queued = self.responses.pop(0)
            if isinstance(queued, BaseException):
                if isinstance(queued, httpx.RequestError):
                    queued._request = request
                raise queued
            if callable(queued):
                return queued(request)
            queued.request = request
            return queued

        self.transport = httpx.MockTransport(handler)
        self.api = TelegramApi(token, transport=self.transport)

    async def close(self) -> None:
        await self.api.aclose()


def json_response(payload: object, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def request_json(request: httpx.Request) -> dict[str, Any]:
    parsed = json.loads(request.content)
    assert isinstance(parsed, dict)
    return parsed


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_get_updates_sends_bounded_poll_query_and_parses_supported_updates() -> None:
    harness = TelegramHarness(
        [
            json_response(
                {
                    "ok": True,
                    "result": [
                        {"update_id": 17, "message": message_json()},
                        {"update_id": 18, "callback_query": callback_json()},
                        {"update_id": 19, "edited_message": {"ignored": True}},
                    ],
                }
            )
        ]
    )

    updates = run(harness.api.get_updates(offset=10, timeout=30))
    run(harness.close())

    assert updates == [
        TelegramUpdate(17, TelegramMessage(31, 42, 7, "감시해줘"), None),
        TelegramUpdate(
            18,
            None,
            CallbackQuery("callback-query-id", 7, 42, 31, "confirm:opaque"),
        ),
        TelegramUpdate(19, None, None),
    ]
    request = harness.requests[0]
    assert request.method == "GET"
    assert request.url.path.endswith("/getUpdates")
    assert dict(request.url.params) == {
        "offset": "10",
        "timeout": "30",
        "allowed_updates": '["message","callback_query"]',
    }
    assert request.extensions["timeout"] == {
        "connect": 35.0,
        "read": 35.0,
        "write": 35.0,
        "pool": 35.0,
    }


def test_parsed_updates_are_frozen_copies_with_redacted_representations() -> None:
    raw = {"update_id": 17, "message": message_json("private-message")}
    harness = TelegramHarness([json_response({"ok": True, "result": [raw]})])

    update = run(harness.api.get_updates(offset=0, timeout=0))[0]
    raw["update_id"] = 999
    message = raw["message"]
    assert isinstance(message, dict)
    message["text"] = "mutated"
    run(harness.close())

    assert update.update_id == 17
    assert update.message is not None
    assert update.message.text == "private-message"
    with pytest.raises(FrozenInstanceError):
        update.message.text = "changed"  # type: ignore[misc]
    assert "private-message" not in repr(update)
    assert "42" not in repr(update)


@pytest.mark.parametrize(
    "payload",
    [
        {"update_id": True},
        {"update_id": -1},
        {"update_id": 1, "message": None},
        {"update_id": 1, "message": {}},
        {"update_id": 1, "message": {**message_json(), "message_id": True}},
        {"update_id": 1, "message": {**message_json(), "message_id": 0}},
        {"update_id": 1, "message": {**message_json(), "chat": {"id": False}}},
        {"update_id": 1, "message": {**message_json(), "from": {"id": -1}}},
        {"update_id": 1, "message": {**message_json(), "text": 4}},
        {"update_id": 1, "message": {**message_json(), "text": "bad\x00text"}},
        {"update_id": 1, "message": {**message_json(), "text": "x" * 4097}},
        {"update_id": 1, "callback_query": None},
        {"update_id": 1, "callback_query": {}},
        {"update_id": 1, "callback_query": {**callback_json(), "id": ""}},
        {"update_id": 1, "callback_query": {**callback_json(), "data": ""}},
        {"update_id": 1, "callback_query": {**callback_json(), "data": "가" * 22}},
        {"update_id": 1, "callback_query": {**callback_json(), "data": "bad\x01"}},
        {
            "update_id": 1,
            "message": message_json(),
            "callback_query": callback_json(),
        },
    ],
)
def test_malformed_supported_update_shapes_fail_with_one_fixed_error(payload: object) -> None:
    harness = TelegramHarness([json_response({"ok": True, "result": [payload]})])

    with pytest.raises(TelegramApiError) as caught:
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())

    assert str(caught.value) == "invalid Telegram response shape"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("offset", "timeout"),
    [(-1, 30), (True, 30), (2**63, 30), (0, -1), (0, 31), (0, False), (0, 1.5)],
)
def test_poll_bounds_fail_before_network_access(offset: object, timeout: object) -> None:
    harness = TelegramHarness([])

    with pytest.raises(TelegramApiError, match="invalid Telegram request"):
        run(harness.api.get_updates(offset=offset, timeout=timeout))  # type: ignore[arg-type]
    run(harness.close())

    assert harness.requests == []


def test_too_many_updates_fail_closed() -> None:
    result = [{"update_id": index} for index in range(101)]
    harness = TelegramHarness([json_response({"ok": True, "result": result})])

    with pytest.raises(TelegramApiError, match="invalid Telegram response shape"):
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())


def test_send_message_splits_unbroken_and_korean_text_in_order_and_returns_final_id() -> None:
    harness = TelegramHarness(
        [
            json_response({"ok": True, "result": {"message_id": 88}}),
            json_response({"ok": True, "result": {"message_id": 89}}),
            json_response({"ok": True, "result": {"message_id": 90}}),
        ]
    )
    text = "가" * 4096 + "\n" + "x" * 4097

    result = run(harness.api.send_message("42", text))
    run(harness.close())

    assert result == "90"
    payloads = [request_json(request) for request in harness.requests]
    assert [payload["text"] for payload in payloads] == ["가" * 4096, "x" * 4096, "x"]
    assert all(payload["chat_id"] == "42" for payload in payloads)
    assert all(payload["disable_web_page_preview"] is True for payload in payloads)
    assert all(request.method == "POST" for request in harness.requests)
    assert all(
        request.headers["content-type"] == "application/json" for request in harness.requests
    )
    assert all(
        request.extensions["timeout"]
        == {"connect": 20.0, "read": 20.0, "write": 20.0, "pool": 20.0}
        for request in harness.requests
    )


def test_inline_keyboard_has_canonical_structure_and_copies_mutable_rows() -> None:
    rows = [[InlineButton("등록", "confirm:opaque")]]
    harness = TelegramHarness([json_response({"ok": True, "result": {"message_id": 88}})])

    result = run(harness.api.send_message(42, "미리보기", buttons=rows))
    rows[0].append(InlineButton("취소", "cancel:opaque"))
    run(harness.close())

    assert result == "88"
    assert request_json(harness.requests[0]) == {
        "chat_id": "42",
        "text": "미리보기",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[{"text": "등록", "callback_data": "confirm:opaque"}]]
        },
    }
    with pytest.raises(FrozenInstanceError):
        rows[0][0].text = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "button",
    [
        InlineButton("", "valid"),
        InlineButton("bad\x00", "valid"),
        InlineButton("x" * 65, "valid"),
        InlineButton("valid", ""),
        InlineButton("valid", "bad\x01"),
        InlineButton("valid", "가" * 22),
        InlineButton("valid", "bad\ud800"),
    ],
)
def test_invalid_inline_buttons_fail_before_network_access(button: InlineButton) -> None:
    harness = TelegramHarness([])

    with pytest.raises(TelegramApiError, match="invalid Telegram request"):
        run(harness.api.send_message(42, "preview", buttons=[[button]]))
    run(harness.close())

    assert harness.requests == []


def test_button_collection_bounds_fail_before_network_access() -> None:
    harness = TelegramHarness([])
    rows = [[InlineButton(str(index), f"callback:{index}")] for index in range(9)]

    with pytest.raises(TelegramApiError, match="invalid Telegram request"):
        run(harness.api.send_message(42, "preview", buttons=rows))
    run(harness.close())

    assert harness.requests == []


@pytest.mark.parametrize("text", ["", "   \n\t", "bad\x00text", "bad\ud800", "x" * 40961])
def test_invalid_outgoing_message_fails_before_network_access(text: str) -> None:
    harness = TelegramHarness([])

    with pytest.raises(TelegramApiError, match="invalid Telegram request"):
        run(harness.api.send_message(42, text))
    run(harness.close())

    assert harness.requests == []


def test_edit_message_accepts_one_chunk_and_returns_edited_id() -> None:
    harness = TelegramHarness([json_response({"ok": True, "result": {"message_id": 31}})])

    result = run(
        harness.api.edit_message(
            42,
            31,
            "수정된 미리보기",
            buttons=((InlineButton("확인", "confirm:opaque"),),),
        )
    )
    run(harness.close())

    assert result == "31"
    assert request_json(harness.requests[0]) == {
        "chat_id": "42",
        "message_id": "31",
        "text": "수정된 미리보기",
        "reply_markup": {
            "inline_keyboard": [[{"text": "확인", "callback_data": "confirm:opaque"}]]
        },
    }


@pytest.mark.parametrize("text", ["", " \n", "x" * 4097])
def test_edit_rejects_empty_or_multi_chunk_text_before_network(text: str) -> None:
    harness = TelegramHarness([])

    with pytest.raises(TelegramApiError, match="invalid Telegram request"):
        run(harness.api.edit_message(42, 31, text))
    run(harness.close())

    assert harness.requests == []


def test_answer_callback_requires_boolean_success() -> None:
    harness = TelegramHarness([json_response({"ok": True, "result": True})])

    assert run(harness.api.answer_callback("callback-id", text="완료", show_alert=True)) is None
    run(harness.close())

    assert request_json(harness.requests[0]) == {
        "callback_query_id": "callback-id",
        "text": "완료",
        "show_alert": True,
    }


@pytest.mark.parametrize(
    ("callback_id", "text", "show_alert"),
    [
        ("", None, False),
        ("bad\x00", None, False),
        ("x" * 257, None, False),
        ("valid", "x" * 201, False),
        ("valid", "bad\x01", False),
        ("valid", None, 1),
    ],
)
def test_invalid_callback_answer_fails_before_network(
    callback_id: str, text: str | None, show_alert: object
) -> None:
    harness = TelegramHarness([])

    with pytest.raises(TelegramApiError, match="invalid Telegram request"):
        run(
            harness.api.answer_callback(
                callback_id,
                text=text,
                show_alert=show_alert,  # type: ignore[arg-type]
            )
        )
    run(harness.close())

    assert harness.requests == []


def test_answer_callback_rejects_non_boolean_result() -> None:
    harness = TelegramHarness([json_response({"ok": True, "result": 1})])

    with pytest.raises(TelegramApiError, match="invalid Telegram response shape"):
        run(harness.api.answer_callback("callback-id"))
    run(harness.close())


def test_webhook_preflight_accepts_only_an_empty_url() -> None:
    harness = TelegramHarness([json_response({"ok": True, "result": {"url": ""}})])

    assert run(harness.api.ensure_webhook_disabled()) is None
    run(harness.close())

    assert harness.requests[0].method == "GET"
    assert harness.requests[0].url.path.endswith("/getWebhookInfo")


@pytest.mark.parametrize("result", [{"url": "https://hook.invalid/private"}, {}, {"url": None}])
def test_webhook_preflight_rejects_nonempty_or_malformed_state_without_leaking_it(
    result: object,
) -> None:
    harness = TelegramHarness([json_response({"ok": True, "result": result})])

    with pytest.raises(TelegramApiError, match="Telegram webhook preflight failed") as caught:
        run(harness.api.ensure_webhook_disabled())
    run(harness.close())

    assert "hook.invalid" not in str(caught.value)
    assert "hook.invalid" not in repr(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, content=b"server-body-secret"),
        httpx.Response(200, content=b"not-json-response-secret"),
        httpx.Response(200, content=b'\xff{"ok":true,"result":[]}'),
        json_response([]),
        json_response({"ok": True}),
        json_response({"ok": 1, "result": []}),
        json_response({"ok": True, "result": {}}),
    ],
)
def test_invalid_http_or_response_envelope_fails_without_retaining_response(
    response: httpx.Response,
) -> None:
    harness = TelegramHarness([response])

    with pytest.raises(TelegramApiError) as caught:
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())

    combined = f"{caught.value!s} {caught.value!r}"
    assert "server-body-secret" not in combined
    assert "not-json-response-secret" not in combined
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_oversized_response_body_is_rejected_at_one_mib_boundary() -> None:
    harness = TelegramHarness([httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))])

    with pytest.raises(TelegramApiError, match="Telegram response too large"):
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("offline"), httpx.ReadTimeout("timed out")],
)
def test_network_errors_are_replaced_without_token_bearing_cause_or_context(
    error: httpx.RequestError,
) -> None:
    harness = TelegramHarness([error])

    with pytest.raises(TelegramApiError, match="Telegram request failed") as caught:
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())

    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_api_descriptions_are_always_replaced_with_fixed_error() -> None:
    safe = TelegramHarness([json_response({"ok": False, "description": "chat not found"})])
    with pytest.raises(TelegramApiError) as safe_error:
        run(safe.api.get_updates(offset=0, timeout=30))
    run(safe.close())
    assert str(safe_error.value) == "Telegram API request failed"

    secret = "Authorization: Bearer abc https://bad.invalid/?token=response-secret chat 42"
    malicious = TelegramHarness([json_response({"ok": False, "description": secret})])
    with pytest.raises(TelegramApiError) as caught:
        run(malicious.api.get_updates(offset=0, timeout=30))
    run(malicious.close())

    assert str(caught.value) == "Telegram API request failed"
    assert secret not in repr(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "result": {}},
        {"ok": True, "result": {"message_id": True}},
        {"ok": True, "result": {"message_id": 0}},
        {"ok": True, "result": {"message_id": "88"}},
    ],
)
def test_send_rejects_missing_or_invalid_message_id(payload: object) -> None:
    harness = TelegramHarness([json_response(payload)])

    with pytest.raises(TelegramApiError, match="invalid Telegram response shape"):
        run(harness.api.send_message(42, "hello"))
    run(harness.close())


def test_api_and_errors_have_fixed_secret_safe_representations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response_secret = "private-response-body"
    harness = TelegramHarness(
        [json_response({"ok": False, "description": f"token={response_secret}"})]
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(TelegramApiError) as caught:
        run(harness.api.send_message(42, "private-message"))
    run(harness.close())

    assert repr(harness.api) == "<TelegramApi redacted>"
    assert repr(caught.value) == "TelegramApiError(<redacted>)"
    exposed = " ".join((str(caught.value), repr(caught.value), caplog.text))
    for secret in (TOKEN, response_secret, "private-message", "42"):
        assert secret not in exposed


@pytest.mark.parametrize("token", ["", "   ", "bad\x00token", "bad\ud800"])
def test_constructor_rejects_invalid_token_without_exposing_value(token: str) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))

    with pytest.raises(ValueError) as caught:
        TelegramApi(token, transport=transport)

    if token:
        assert token not in str(caught.value)
        assert token not in repr(caught.value)


class _ObservingLogFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def filter(self, record: logging.LogRecord) -> bool:
        self.messages.append(record.getMessage())
        return True


def test_review_preexisting_httpx_filter_never_observes_tokenized_url() -> None:
    logger = logging.getLogger("httpx")
    original_filters = list(logger.filters)
    original_level = logger.level
    observer = _ObservingLogFilter()
    logger.filters.insert(0, observer)
    logger.setLevel(logging.INFO)
    harness = TelegramHarness([json_response({"ok": True, "result": []})])
    try:
        assert run(harness.api.get_updates(offset=0, timeout=30)) == []
        run(harness.close())
        assert observer.messages
        assert all(TOKEN not in message for message in observer.messages)
        assert any("/bot-redacted/getUpdates" in message for message in observer.messages)
    finally:
        logger.filters[:] = original_filters
        logger.setLevel(original_level)


def test_review_request_hook_exception_is_replaced_without_secret_context() -> None:
    hook_calls = 0

    async def leaking_hook(request: httpx.Request) -> None:
        nonlocal hook_calls
        hook_calls += 1
        raise RuntimeError(f"hook saw {request.url}")

    harness = TelegramHarness([])
    object.__setattr__(
        harness.api._client,
        "_event_hooks",
        {"request": [leaking_hook], "response": []},
    )

    with pytest.raises(TelegramApiError, match="Telegram client integrity failure") as caught:
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())

    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert hook_calls == 0
    assert harness.requests == []


def test_review_runtime_transport_exception_is_replaced_without_secret_context() -> None:
    harness = TelegramHarness([RuntimeError(f"transport leaked {TOKEN}")])

    with pytest.raises(TelegramApiError, match="Telegram request failed") as caught:
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())

    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_review_caller_owned_async_client_is_not_an_accepted_constructor_boundary() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    try:
        with pytest.raises(TypeError):
            TelegramApi(client, TOKEN)
    finally:
        run(client.aclose())


def test_review_post_construction_client_swap_fails_before_swapped_transport() -> None:
    harness = TelegramHarness([])
    swapped_requests: list[httpx.Request] = []

    def swapped_handler(request: httpx.Request) -> httpx.Response:
        swapped_requests.append(request)
        return json_response({"ok": True, "result": []})

    swapped = httpx.AsyncClient(transport=httpx.MockTransport(swapped_handler))
    object.__setattr__(harness.api, "_client", swapped)
    with pytest.raises(TelegramApiError, match="Telegram client integrity failure"):
        run(harness.api.get_updates(offset=0, timeout=30))
    run(swapped.aclose())
    run(harness.close())

    assert swapped_requests == []


def test_review_ok_false_never_returns_even_ordinary_description() -> None:
    harness = TelegramHarness(
        [json_response({"ok": False, "description": "ordinary alphabetic failure"})]
    )

    with pytest.raises(TelegramApiError) as caught:
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())

    assert str(caught.value) == "Telegram API request failed"
    assert repr(caught.value) == "TelegramApiError(<redacted>)"


def test_review_huge_json_integer_is_replaced_with_fixed_response_error() -> None:
    body = b'{"ok":true,"result":[{"update_id":' + b"9" * 5000 + b"}]}"
    harness = TelegramHarness([httpx.Response(200, content=body)])

    with pytest.raises(TelegramApiError, match="invalid Telegram response shape") as caught:
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_review_deep_json_is_rejected_before_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"[" * 400_000 + b"0" + b"]" * 400_000
    harness = TelegramHarness([httpx.Response(200, content=body)])
    decoder_called = False

    def forbidden_decoder(*_args: object, **_kwargs: object) -> object:
        nonlocal decoder_called
        decoder_called = True
        raise AssertionError("deep JSON reached decoder")

    monkeypatch.setattr(api_module.json, "loads", forbidden_decoder)
    with pytest.raises(TelegramApiError, match="invalid Telegram response shape"):
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())

    assert decoder_called is False


def test_review_photo_message_and_inline_callback_advance_offset_as_unsupported() -> None:
    photo_message = {
        "message_id": 31,
        "chat": {"id": 42},
        "from": {"id": 7},
        "photo": [{"file_id": "opaque", "width": 1, "height": 1}],
    }
    inline_callback = {
        "id": "callback-query-id",
        "from": {"id": 7},
        "data": "confirm:opaque",
        "inline_message_id": "inline-message-id",
    }
    harness = TelegramHarness(
        [
            json_response(
                {
                    "ok": True,
                    "result": [
                        {"update_id": 70, "message": photo_message},
                        {"update_id": 71, "callback_query": inline_callback},
                        {"update_id": 72, "message": message_json("supported")},
                    ],
                }
            )
        ]
    )

    updates = run(harness.api.get_updates(offset=70, timeout=30))
    run(harness.close())

    assert updates == [
        TelegramUpdate(70, None, None),
        TelegramUpdate(71, None, None),
        TelegramUpdate(72, TelegramMessage(31, 42, 7, "supported"), None),
    ]


def test_review_api_owns_exact_client_and_exposes_only_mock_transport_seam() -> None:
    transport = httpx.MockTransport(lambda _request: json_response({"ok": True, "result": []}))
    api = TelegramApi(TOKEN, transport=transport)

    assert type(api._client) is httpx.AsyncClient
    assert api._client.event_hooks == {"request": (), "response": ()}
    with pytest.raises(TypeError):
        TelegramApi(TOKEN, event_hooks={"request": []})  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="invalid Telegram test transport"):
        TelegramApi(TOKEN, transport=object())  # type: ignore[arg-type]
    run(api.aclose())


def test_review_api_composition_rejects_normal_attribute_swap() -> None:
    harness = TelegramHarness([])
    with pytest.raises(AttributeError, match="sealed"):
        harness.api._client = object()  # type: ignore[assignment]
    run(harness.close())


def test_review_logger_filter_container_is_not_a_request_dependency() -> None:
    logger = logging.getLogger("httpx")
    original_filters = logger.filters
    harness = TelegramHarness([json_response({"ok": True, "result": []})])
    logger.filters = tuple(reversed(original_filters))  # type: ignore[assignment]
    try:
        assert run(harness.api.get_updates(offset=0, timeout=30)) == []
        assert len(harness.requests) == 1
    finally:
        logger.filters = original_filters
        run(harness.close())


def test_review_concurrent_clients_redact_each_token_before_other_filters() -> None:
    logger = logging.getLogger("httpx")
    original_filters = list(logger.filters)
    original_level = logger.level
    observer = _ObservingLogFilter()
    logger.filters.insert(0, observer)
    logger.setLevel(logging.INFO)
    first_token = "first-concurrent-token"
    second_token = "second-concurrent-token"
    first = TelegramHarness([json_response({"ok": True, "result": []})], token=first_token)
    second = TelegramHarness([json_response({"ok": True, "result": []})], token=second_token)

    async def exercise() -> None:
        await asyncio.gather(
            first.api.get_updates(offset=0, timeout=30),
            second.api.get_updates(offset=0, timeout=30),
        )
        await first.close()
        await second.close()

    try:
        run(exercise())
        assert observer.messages
        assert all(first_token not in message for message in observer.messages)
        assert all(second_token not in message for message in observer.messages)
        assert sum("/bot-redacted/getUpdates" in item for item in observer.messages) == 2
    finally:
        logger.filters[:] = original_filters
        logger.setLevel(original_level)


@pytest.mark.parametrize(
    "description",
    [
        f"token echo {TOKEN}",
        "message echo private payload",
        "callback echo confirm opaque",
        "chat echo private address",
    ],
)
def test_review_api_failure_descriptions_are_never_public(description: str) -> None:
    harness = TelegramHarness([json_response({"ok": False, "description": description})])

    with pytest.raises(TelegramApiError) as caught:
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())

    assert str(caught.value) == "Telegram API request failed"
    assert description not in repr(caught.value)


def test_review_json_nesting_scanner_ignores_escaped_string_brackets() -> None:
    ignored = '\\"' + "[" * 1000 + "}" * 1000
    body = json.dumps(
        {"ok": True, "result": [{"update_id": 90, "ignored": ignored}]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    harness = TelegramHarness([httpx.Response(200, content=body)])

    assert run(harness.api.get_updates(offset=90, timeout=30)) == [TelegramUpdate(90, None, None)]
    run(harness.close())


@pytest.mark.parametrize(
    "payload",
    [
        {
            "update_id": 80,
            "message": {"message_id": 31, "chat": {"id": 42}, "photo": []},
        },
        {
            "update_id": 81,
            "callback_query": {
                "id": "callback-query-id",
                "from": {"id": 7},
                "data": "confirm:opaque",
                "inline_message_id": "",
            },
        },
        {
            "update_id": 82,
            "callback_query": {
                "id": "callback-query-id",
                "from": {"id": True},
                "data": "confirm:opaque",
                "inline_message_id": "inline-id",
            },
        },
    ],
)
def test_review_malformed_unsupported_subtypes_remain_strictly_rejected(
    payload: object,
) -> None:
    harness = TelegramHarness([json_response({"ok": True, "result": [payload]})])

    with pytest.raises(TelegramApiError, match="invalid Telegram response shape"):
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())


def test_final_review_filter_installed_in_flight_observes_only_sanitized_url() -> None:
    logger = logging.getLogger("httpx")
    original_filters = list(logger.filters)
    original_level = logger.level
    observer = _ObservingLogFilter()

    def install_filter(_request: httpx.Request) -> httpx.Response:
        logger.filters.insert(0, observer)
        return json_response({"ok": True, "result": []})

    logger.setLevel(logging.INFO)
    harness = TelegramHarness([install_filter])
    try:
        assert run(harness.api.get_updates(offset=0, timeout=30)) == []
        run(harness.close())
        assert observer.messages
        assert all(TOKEN not in message for message in observer.messages)
        assert any("/bot-redacted/getUpdates" in message for message in observer.messages)
        assert all("allowed_updates" not in message for message in observer.messages)
    finally:
        logger.filters[:] = original_filters
        logger.setLevel(original_level)


def test_final_review_concurrent_in_flight_filter_changes_never_observe_tokens() -> None:
    logger = logging.getLogger("httpx")
    original_filters = list(logger.filters)
    original_level = logger.level
    observer = _ObservingLogFilter()

    def install_filter(_request: httpx.Request) -> httpx.Response:
        if observer in logger.filters:
            logger.filters.remove(observer)
        logger.filters.insert(0, observer)
        return json_response({"ok": True, "result": []})

    logger.setLevel(logging.INFO)
    first_token = "first-race-token"
    second_token = "second-race-token"
    first = TelegramHarness([install_filter], token=first_token)
    second = TelegramHarness([install_filter], token=second_token)

    async def exercise() -> None:
        await asyncio.gather(
            first.api.get_updates(offset=0, timeout=30),
            second.api.get_updates(offset=0, timeout=30),
        )
        await first.close()
        await second.close()

    try:
        run(exercise())
        assert observer.messages
        assert all(first_token not in message for message in observer.messages)
        assert all(second_token not in message for message in observer.messages)
        assert all("allowed_updates" not in message for message in observer.messages)
    finally:
        logger.filters[:] = original_filters
        logger.setLevel(original_level)


@pytest.mark.parametrize(
    "token",
    [
        "raw/slash",
        "raw?query",
        "raw#fragment",
        "raw\\backslash",
        "percent%2Fslash",
        "percent%3Fquery",
        "percent%23fragment",
        "dot.segment",
        "space token",
        "nonascii-토큰",
    ],
)
def test_final_review_token_rejects_path_query_and_encoding_delimiters(token: str) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    api: TelegramApi | None = None
    try:
        with pytest.raises(ValueError, match="invalid Telegram bot token") as caught:
            api = TelegramApi(token, transport=transport)
        assert token not in str(caught.value)
        assert token not in repr(caught.value)
    finally:
        if api is not None:
            run(api.aclose())


def test_final_review_transport_wrapper_and_inner_identity_are_sealed() -> None:
    harness = TelegramHarness([])
    wrapper = harness.api._transport_anchor
    replacement_calls = 0

    def replacement_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal replacement_calls
        replacement_calls += 1
        return json_response({"ok": True, "result": []})

    replacement = httpx.MockTransport(replacement_handler)
    with pytest.raises(AttributeError, match="sealed"):
        wrapper._inner = replacement  # type: ignore[attr-defined]
    object.__setattr__(wrapper, "_inner", replacement)

    with pytest.raises(TelegramApiError, match="Telegram client integrity failure"):
        run(harness.api.get_updates(offset=0, timeout=30))
    run(harness.close())

    assert replacement_calls == 0
    assert harness.requests == []


def unsupported_message(**content: object) -> dict[str, object]:
    return {
        "message_id": 31,
        "chat": {"id": 42},
        "from": {"id": 7},
        **content,
    }


@pytest.mark.parametrize(
    "message",
    [
        unsupported_message(),
        unsupported_message(made_up={"value": "unknown"}),
        unsupported_message(photo=[]),
        unsupported_message(photo={"file_id": "not-a-list"}),
        unsupported_message(photo=[{}]),
        unsupported_message(
            photo=[{"file_id": f"photo-{index}", "width": 1, "height": 1} for index in range(21)]
        ),
        unsupported_message(photo=[{"file_id": "x" * 513, "width": 1, "height": 1}]),
        unsupported_message(video={}),
        unsupported_message(video="not-an-object"),
        unsupported_message(
            photo=[{"file_id": "photo-id", "width": 1, "height": 1}],
            caption="x" * 1025,
        ),
    ],
)
def test_final_review_malformed_or_unknown_no_text_message_is_rejected(
    message: object,
) -> None:
    harness = TelegramHarness(
        [json_response({"ok": True, "result": [{"update_id": 100, "message": message}]})]
    )

    with pytest.raises(TelegramApiError, match="invalid Telegram response shape"):
        run(harness.api.get_updates(offset=100, timeout=30))
    run(harness.close())


@pytest.mark.parametrize(
    "message",
    [
        unsupported_message(
            photo=[
                {"file_id": "photo-small", "width": 320, "height": 240},
                {"file_id": "photo-large", "width": 1280, "height": 960},
            ],
            caption="사진 설명",
        ),
        unsupported_message(
            video={
                "file_id": "video-id",
                "width": 1280,
                "height": 720,
                "duration": 5,
            }
        ),
        unsupported_message(document={"file_id": "document-id"}),
    ],
)
def test_final_review_valid_bounded_media_message_advances_offset(message: object) -> None:
    harness = TelegramHarness(
        [json_response({"ok": True, "result": [{"update_id": 101, "message": message}]})]
    )

    assert run(harness.api.get_updates(offset=101, timeout=30)) == [TelegramUpdate(101, None, None)]
    run(harness.close())


def test_final_review_cancellation_propagates_and_owned_transport_still_closes() -> None:
    close_calls = 0

    async def cancel_handler(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    transport = httpx.MockTransport(cancel_handler)
    original_close = transport.aclose

    async def recording_close() -> None:
        nonlocal close_calls
        close_calls += 1
        await original_close()

    object.__setattr__(transport, "aclose", recording_close)
    api = TelegramApi(TOKEN, transport=transport)

    with pytest.raises(asyncio.CancelledError):
        run(api.get_updates(offset=0, timeout=30))
    run(api.aclose())

    assert close_calls == 1
