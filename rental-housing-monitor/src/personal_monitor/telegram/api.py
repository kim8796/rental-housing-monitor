from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

import httpx

from rental_monitor.telegram import split_message

from .types import CallbackQuery, InlineButton, TelegramMessage, TelegramUpdate

MAX_RESPONSE_BYTES: Final = 1024 * 1024
MAX_UPDATES: Final = 100
MAX_IDENTIFIER: Final = 2**63 - 1
MAX_INCOMING_MESSAGE_CHARS: Final = 4096
MAX_OUTGOING_MESSAGE_CHARS: Final = 40960
MAX_BUTTON_ROWS: Final = 8
MAX_BUTTONS_PER_ROW: Final = 8
MAX_BUTTON_TEXT_CHARS: Final = 64
MAX_CALLBACK_DATA_BYTES: Final = 64
MAX_CALLBACK_ID_CHARS: Final = 256
MAX_CALLBACK_ANSWER_CHARS: Final = 200
MAX_JSON_NESTING: Final = 64
MAX_JSON_INTEGER_DIGITS: Final = 19

_INVALID: Final = object()
_UNSUPPORTED: Final = object()
_TELEGRAM_BOT_PATH_RE: Final = re.compile(r"^/bot[^/]+(?P<endpoint>/.*)?$")


class _TelegramUrlRedactionFilter(logging.Filter):
    _personal_monitor_telegram_redactor = True

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.args, tuple):
            return True
        redacted: list[object] = []
        changed = False
        for value in record.args:
            replacement = _redacted_telegram_url(value)
            redacted.append(replacement)
            changed = changed or replacement is not value
        if changed:
            record.args = tuple(redacted)
        return True


_HTTPX_LOGGER = logging.getLogger("httpx")
_HTTPX_REDACTOR = _TelegramUrlRedactionFilter()


def _redacted_telegram_url(value: object) -> object:
    if not isinstance(value, httpx.URL) or value.host != "api.telegram.org":
        return value
    matched = _TELEGRAM_BOT_PATH_RE.fullmatch(value.path)
    if matched is None:
        return value
    endpoint = matched.group("endpoint") or ""
    return f"https://api.telegram.org/bot<redacted>{endpoint}"


def _ensure_httpx_redactor_first() -> bool:
    filters = _HTTPX_LOGGER.filters
    if type(filters) is not list:
        return False
    try:
        retained = [
            item
            for item in filters
            if not getattr(item, "_personal_monitor_telegram_redactor", False)
        ]
        filters[:] = [_HTTPX_REDACTOR, *retained]
    except Exception:
        return False
    return bool(filters) and filters[0] is _HTTPX_REDACTOR


_ensure_httpx_redactor_first()


class TelegramApiError(RuntimeError):
    __slots__ = ()

    def __repr__(self) -> str:
        return "TelegramApiError(<redacted>)"


class TelegramApi:
    __slots__ = (
        "_base_url",
        "_client",
        "_client_anchor",
        "_hooks_anchor",
        "_mounts_anchor",
        "_transport_anchor",
    )

    def __init__(
        self,
        bot_token: str,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        token = _bounded_string(
            bot_token,
            min_chars=1,
            max_chars=512,
            max_bytes=2048,
            allow_layout_controls=False,
        )
        if token is None or not token.strip():
            raise ValueError("invalid Telegram bot token")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise ValueError("invalid Telegram test transport")
        client = httpx.AsyncClient(transport=transport, trust_env=False)
        hooks = MappingProxyType({"request": (), "response": ()})
        mounts = MappingProxyType(dict(client._mounts))
        object.__setattr__(client, "_event_hooks", hooks)
        object.__setattr__(client, "_mounts", mounts)
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_client_anchor", client)
        object.__setattr__(self, "_hooks_anchor", hooks)
        object.__setattr__(self, "_mounts_anchor", mounts)
        object.__setattr__(self, "_transport_anchor", client._transport)
        object.__setattr__(self, "_base_url", f"https://api.telegram.org/bot{token}")

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("TelegramApi composition is sealed")

    def __repr__(self) -> str:
        return "<TelegramApi redacted>"

    async def aclose(self) -> None:
        close_failed = False
        try:
            await self._client_anchor.aclose()
        except Exception:
            close_failed = True
        if close_failed:
            raise TelegramApiError("Telegram client close failed") from None

    def _client_integrity_ok(self) -> bool:
        try:
            client = self._client
            return (
                type(client) is httpx.AsyncClient
                and client is self._client_anchor
                and client._event_hooks is self._hooks_anchor
                and client._mounts is self._mounts_anchor
                and client._transport is self._transport_anchor
                and client.event_hooks == {"request": (), "response": ()}
            )
        except Exception:
            return False

    async def get_updates(self, *, offset: int, timeout: int = 30) -> list[TelegramUpdate]:
        if not _is_int_in_range(offset, minimum=0, maximum=MAX_IDENTIFIER):
            raise TelegramApiError("invalid Telegram request")
        if not _is_int_in_range(timeout, minimum=0, maximum=30):
            raise TelegramApiError("invalid Telegram request")

        payload = await self._request(
            "GET",
            "getUpdates",
            params={
                "offset": str(offset),
                "timeout": str(timeout),
                "allowed_updates": '["message","callback_query"]',
            },
            timeout=35.0,
        )
        result = self._result(payload)
        if not isinstance(result, list) or len(result) > MAX_UPDATES:
            raise TelegramApiError("invalid Telegram response shape")

        updates: list[TelegramUpdate] = []
        for item in result:
            parsed = _parse_update(item)
            if parsed is None:
                raise TelegramApiError("invalid Telegram response shape")
            updates.append(parsed)
        return updates

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
        disable_web_page_preview: bool = True,
    ) -> str:
        chat = _positive_identifier(chat_id)
        message = _outgoing_message(text)
        markup = _inline_keyboard(buttons)
        if chat is None or message is None or type(disable_web_page_preview) is not bool:
            raise TelegramApiError("invalid Telegram request")

        chunks = split_message(message)
        last_message_id: str | None = None
        for chunk in chunks:
            body: dict[str, object] = {
                "chat_id": chat,
                "text": chunk,
                "disable_web_page_preview": disable_web_page_preview,
            }
            if markup is not None:
                body["reply_markup"] = markup
            payload = await self._request("POST", "sendMessage", json_body=body, timeout=20.0)
            last_message_id = self._message_id(payload)
        if last_message_id is None:
            raise TelegramApiError("invalid Telegram request")
        return last_message_id

    async def edit_message(
        self,
        chat_id: int | str,
        message_id: int | str,
        text: str,
        *,
        buttons: Sequence[Sequence[InlineButton]] | None = None,
    ) -> str:
        chat = _positive_identifier(chat_id)
        target_message = _positive_identifier(message_id)
        message = _outgoing_message(text)
        markup = _inline_keyboard(buttons)
        if chat is None or target_message is None or message is None:
            raise TelegramApiError("invalid Telegram request")
        chunks = split_message(message)
        if len(chunks) != 1:
            raise TelegramApiError("invalid Telegram request")

        body: dict[str, object] = {
            "chat_id": chat,
            "message_id": target_message,
            "text": chunks[0],
        }
        if markup is not None:
            body["reply_markup"] = markup
        payload = await self._request("POST", "editMessageText", json_body=body, timeout=20.0)
        return self._message_id(payload)

    async def answer_callback(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        callback_id = _bounded_string(
            callback_query_id,
            min_chars=1,
            max_chars=MAX_CALLBACK_ID_CHARS,
            max_bytes=1024,
            allow_layout_controls=False,
        )
        answer_text: str | None = None
        if text is not None:
            answer_text = _bounded_string(
                text,
                min_chars=1,
                max_chars=MAX_CALLBACK_ANSWER_CHARS,
                max_bytes=800,
                allow_layout_controls=True,
            )
        if callback_id is None or (text is not None and answer_text is None):
            raise TelegramApiError("invalid Telegram request")
        if type(show_alert) is not bool:
            raise TelegramApiError("invalid Telegram request")

        body: dict[str, object] = {
            "callback_query_id": callback_id,
            "show_alert": show_alert,
        }
        if answer_text is not None:
            body["text"] = answer_text
        payload = await self._request("POST", "answerCallbackQuery", json_body=body, timeout=20.0)
        if self._result(payload) is not True:
            raise TelegramApiError("invalid Telegram response shape")

    async def ensure_webhook_disabled(self) -> None:
        payload = await self._request("GET", "getWebhookInfo", timeout=20.0)
        result = self._result(payload)
        if not isinstance(result, dict) or result.get("url") != "":
            raise TelegramApiError("Telegram webhook preflight failed")

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        if not self._client_integrity_ok() or self._client.is_closed:
            raise TelegramApiError("Telegram client integrity failure")
        if not _ensure_httpx_redactor_first():
            raise TelegramApiError("Telegram logging integrity failure")

        request_failed = False
        status_code = 0
        body = bytearray()
        try:
            async with self._client.stream(
                method,
                f"{self._base_url}/{endpoint}",
                params=params,
                json=json_body,
                timeout=timeout,
            ) as response:
                status_code = response.status_code
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise _ResponseTooLarge
                    body.extend(chunk)
        except _ResponseTooLarge:
            request_failed = False
            status_code = 200
            body = bytearray()
            response_too_large = True
        except Exception:
            request_failed = True
            response_too_large = False
        else:
            response_too_large = False

        if request_failed:
            raise TelegramApiError("Telegram request failed") from None
        if response_too_large:
            raise TelegramApiError("Telegram response too large") from None
        if not 200 <= status_code < 300:
            raise TelegramApiError("Telegram request failed")

        payload = _decode_json(bytes(body))
        if not isinstance(payload, dict):
            raise TelegramApiError("invalid Telegram response shape")
        return payload

    @staticmethod
    def _result(payload: Mapping[str, Any]) -> Any:
        ok = payload.get("ok", _INVALID)
        if ok is False:
            raise TelegramApiError("Telegram API request failed")
        if ok is not True or "result" not in payload:
            raise TelegramApiError("invalid Telegram response shape")
        return payload["result"]

    def _message_id(self, payload: Mapping[str, Any]) -> str:
        result = self._result(payload)
        if not isinstance(result, dict):
            raise TelegramApiError("invalid Telegram response shape")
        message_id = result.get("message_id")
        if not _is_int_in_range(message_id, minimum=1, maximum=MAX_IDENTIFIER):
            raise TelegramApiError("invalid Telegram response shape")
        return str(message_id)


class _ResponseTooLarge(Exception):
    pass


def _decode_json(body: bytes) -> object:
    try:
        text = body.decode("utf-8", errors="strict")
        if not _json_nesting_is_safe(text):
            return _INVALID
        return json.loads(
            text,
            parse_int=_limited_json_int,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        return _INVALID


def _json_nesting_is_safe(value: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING:
                return False
        elif character in "]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


def _limited_json_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if not digits or len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("invalid JSON integer")
    return int(value)


def _reject_json_constant(_value: str) -> object:
    raise ValueError("invalid JSON constant")


def _is_int_in_range(value: object, *, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _positive_identifier(value: object) -> str | None:
    if type(value) is int:
        if 1 <= value <= MAX_IDENTIFIER:
            return str(value)
        return None
    if type(value) is not str or not value.isascii() or not value.isdigit():
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    if number < 1 or number > MAX_IDENTIFIER:
        return None
    return str(number)


def _bounded_string(
    value: object,
    *,
    min_chars: int,
    max_chars: int,
    max_bytes: int,
    allow_layout_controls: bool,
) -> str | None:
    if type(value) is not str or not min_chars <= len(value) <= max_chars:
        return None
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    if len(encoded) > max_bytes:
        return None
    for character in value:
        if character in "\n\r\t" and allow_layout_controls:
            continue
        if unicodedata.category(character).startswith("C"):
            return None
    return encoded.decode("utf-8")


def _outgoing_message(value: object) -> str | None:
    message = _bounded_string(
        value,
        min_chars=1,
        max_chars=MAX_OUTGOING_MESSAGE_CHARS,
        max_bytes=MAX_OUTGOING_MESSAGE_CHARS * 4,
        allow_layout_controls=True,
    )
    if message is None or not message.strip():
        return None
    return message


def _inline_keyboard(
    buttons: Sequence[Sequence[InlineButton]] | None,
) -> dict[str, list[list[dict[str, str]]]] | None:
    if buttons is None:
        return None
    if isinstance(buttons, (str, bytes)) or not isinstance(buttons, Sequence):
        raise TelegramApiError("invalid Telegram request")
    if not 1 <= len(buttons) <= MAX_BUTTON_ROWS:
        raise TelegramApiError("invalid Telegram request")

    keyboard: list[list[dict[str, str]]] = []
    for row in buttons:
        if isinstance(row, (str, bytes)) or not isinstance(row, Sequence):
            raise TelegramApiError("invalid Telegram request")
        if not 1 <= len(row) <= MAX_BUTTONS_PER_ROW:
            raise TelegramApiError("invalid Telegram request")
        frozen_row: list[dict[str, str]] = []
        for button in row:
            if type(button) is not InlineButton:
                raise TelegramApiError("invalid Telegram request")
            button_text = _bounded_string(
                button.text,
                min_chars=1,
                max_chars=MAX_BUTTON_TEXT_CHARS,
                max_bytes=MAX_BUTTON_TEXT_CHARS * 4,
                allow_layout_controls=False,
            )
            callback_data = _bounded_string(
                button.callback_data,
                min_chars=1,
                max_chars=MAX_CALLBACK_DATA_BYTES,
                max_bytes=MAX_CALLBACK_DATA_BYTES,
                allow_layout_controls=False,
            )
            if button_text is None or callback_data is None:
                raise TelegramApiError("invalid Telegram request")
            frozen_row.append({"text": button_text, "callback_data": callback_data})
        keyboard.append(frozen_row)
    return {"inline_keyboard": keyboard}


def _parse_update(value: object) -> TelegramUpdate | None:
    if not isinstance(value, dict):
        return None
    update_id = value.get("update_id")
    if not _is_int_in_range(update_id, minimum=0, maximum=MAX_IDENTIFIER):
        return None

    has_message = "message" in value
    has_callback = "callback_query" in value
    if has_message and has_callback:
        return None
    if has_message:
        message = _parse_message(value["message"])
        if message is None:
            return None
        if message is _UNSUPPORTED:
            return TelegramUpdate(update_id, None, None)
        assert isinstance(message, TelegramMessage)
        return TelegramUpdate(update_id, message, None)
    if has_callback:
        callback = _parse_callback(value["callback_query"])
        if callback is None:
            return None
        if callback is _UNSUPPORTED:
            return TelegramUpdate(update_id, None, None)
        assert isinstance(callback, CallbackQuery)
        return TelegramUpdate(update_id, None, callback)
    return TelegramUpdate(update_id, None, None)


def _parse_message(value: object) -> TelegramMessage | object | None:
    if not isinstance(value, dict):
        return None
    chat = value.get("chat")
    sender = value.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    message_id = value.get("message_id")
    chat_id = chat.get("id")
    from_user_id = sender.get("id")
    if not all(
        _is_int_in_range(item, minimum=1, maximum=MAX_IDENTIFIER)
        for item in (message_id, chat_id, from_user_id)
    ):
        return None
    if "text" not in value:
        return _UNSUPPORTED
    text = _bounded_string(
        value.get("text"),
        min_chars=1,
        max_chars=MAX_INCOMING_MESSAGE_CHARS,
        max_bytes=MAX_INCOMING_MESSAGE_CHARS * 4,
        allow_layout_controls=True,
    )
    if text is None:
        return None
    return TelegramMessage(message_id, chat_id, from_user_id, text)


def _parse_callback(value: object) -> CallbackQuery | object | None:
    if not isinstance(value, dict):
        return None
    sender = value.get("from")
    if not isinstance(sender, dict):
        return None
    from_user_id = sender.get("id")
    if not _is_int_in_range(from_user_id, minimum=1, maximum=MAX_IDENTIFIER):
        return None
    callback_id = _bounded_string(
        value.get("id"),
        min_chars=1,
        max_chars=MAX_CALLBACK_ID_CHARS,
        max_bytes=1024,
        allow_layout_controls=False,
    )
    data = _bounded_string(
        value.get("data"),
        min_chars=1,
        max_chars=MAX_CALLBACK_DATA_BYTES,
        max_bytes=MAX_CALLBACK_DATA_BYTES,
        allow_layout_controls=False,
    )
    if callback_id is None or data is None:
        return None

    has_message = "message" in value
    has_inline_message = "inline_message_id" in value
    if has_message and has_inline_message:
        return None
    if has_inline_message:
        inline_message_id = _bounded_string(
            value.get("inline_message_id"),
            min_chars=1,
            max_chars=MAX_CALLBACK_ID_CHARS,
            max_bytes=1024,
            allow_layout_controls=False,
        )
        if inline_message_id is None:
            return None
        return _UNSUPPORTED
    if not has_message:
        return None

    message = value.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if not all(
        _is_int_in_range(item, minimum=1, maximum=MAX_IDENTIFIER) for item in (chat_id, message_id)
    ):
        return None
    return CallbackQuery(callback_id, from_user_id, chat_id, message_id, data)
