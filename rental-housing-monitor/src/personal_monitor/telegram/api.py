from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
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

_INVALID: Final = object()
_SAFE_DESCRIPTION_RE: Final = re.compile(
    r"(?:https?://|www\.|token|authorization|bearer|cookie|secret|credential|"
    r"api[-_ ]?key|header|chat[-_ ]?id|callback|message|\d)",
    re.IGNORECASE,
)


class _TelegramUrlRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.args, tuple):
            return True
        redacted: list[object] = []
        changed = False
        for value in record.args:
            if isinstance(value, httpx.URL) and value.host == "api.telegram.org":
                redacted.append("https://api.telegram.org/<redacted>")
                changed = True
            else:
                redacted.append(value)
        if changed:
            record.args = tuple(redacted)
        return True


_HTTPX_LOGGER = logging.getLogger("httpx")
if not any(isinstance(item, _TelegramUrlRedactionFilter) for item in _HTTPX_LOGGER.filters):
    _HTTPX_LOGGER.addFilter(_TelegramUrlRedactionFilter())


class TelegramApiError(RuntimeError):
    __slots__ = ()

    def __repr__(self) -> str:
        return "TelegramApiError(<redacted>)"


class TelegramApi:
    __slots__ = ("_base_url", "_client")

    def __init__(self, client: httpx.AsyncClient, bot_token: str) -> None:
        token = _bounded_string(
            bot_token,
            min_chars=1,
            max_chars=512,
            max_bytes=2048,
            allow_layout_controls=False,
        )
        if token is None or not token.strip():
            raise ValueError("invalid Telegram bot token")
        self._client = client
        self._base_url = f"https://api.telegram.org/bot{token}"

    def __repr__(self) -> str:
        return "<TelegramApi redacted>"

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
        except httpx.HTTPError:
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
            description = _safe_description(payload.get("description"))
            if description is not None:
                raise TelegramApiError(f"Telegram API request failed: {description}")
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
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _INVALID


def _safe_description(value: object) -> str | None:
    description = _bounded_string(
        value,
        min_chars=1,
        max_chars=160,
        max_bytes=640,
        allow_layout_controls=False,
    )
    if description is None or _SAFE_DESCRIPTION_RE.search(description):
        return None
    return description


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
        return TelegramUpdate(update_id, message, None)
    if has_callback:
        callback = _parse_callback(value["callback_query"])
        if callback is None:
            return None
        return TelegramUpdate(update_id, None, callback)
    return TelegramUpdate(update_id, None, None)


def _parse_message(value: object) -> TelegramMessage | None:
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


def _parse_callback(value: object) -> CallbackQuery | None:
    if not isinstance(value, dict):
        return None
    sender = value.get("from")
    message = value.get("message")
    if not isinstance(sender, dict) or not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None

    from_user_id = sender.get("id")
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if not all(
        _is_int_in_range(item, minimum=1, maximum=MAX_IDENTIFIER)
        for item in (from_user_id, chat_id, message_id)
    ):
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
    return CallbackQuery(callback_id, from_user_id, chat_id, message_id, data)
