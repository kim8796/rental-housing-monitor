from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from contextlib import suppress
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
MAX_MEDIA_FILE_ID_CHARS: Final = 512
MAX_PHOTO_SIZES: Final = 20
MAX_MEDIA_CAPTION_CHARS: Final = 1024
MAX_MEDIA_OBJECT_KEYS: Final = 32
MAX_MEDIA_FILE_SIZE: Final = 2**50
MAX_MEDIA_DURATION: Final = 10 * 365 * 24 * 60 * 60
MAX_MEDIA_DIMENSION: Final = 100_000
MAX_MEDIA_FILE_NAME_CHARS: Final = 255
MAX_MEDIA_MIME_TYPE_CHARS: Final = 127
MAX_MEDIA_TEXT_CHARS: Final = 512
_HTTP_CORE_SUPPRESS_LEVEL: Final = logging.CRITICAL + 1

_INVALID: Final = object()
_UNSUPPORTED: Final = object()
_TELEGRAM_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9:_-]+")
_TELEGRAM_ENDPOINTS: Final = frozenset(
    {
        "answerCallbackQuery",
        "editMessageText",
        "getUpdates",
        "getWebhookInfo",
        "sendMessage",
    }
)
_UNSUPPORTED_MEDIA_FIELDS: Final = frozenset(
    {"animation", "audio", "document", "photo", "sticker", "video", "video_note", "voice"}
)
_SAFE_CONTENT_ENCODINGS: Final = frozenset({b"deflate", b"gzip", b"identity"})


class _NoOpHttpcoreLogger(logging.Logger):
    """A permanently inert logger that preserves references held by httpcore modules."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == "__class__":
            raise AttributeError("httpcore logger class is sealed")
        if name == "level":
            value = _HTTP_CORE_SUPPRESS_LEVEL
        elif name == "disabled":
            value = True
        elif name in {"filters", "handlers"}:
            value = ()
        elif name == "propagate":
            value = False
        super().__setattr__(name, value)

    def seal(self) -> None:
        object.__setattr__(self, "level", _HTTP_CORE_SUPPRESS_LEVEL)
        object.__setattr__(self, "disabled", True)
        object.__setattr__(self, "filters", ())
        object.__setattr__(self, "handlers", ())
        object.__setattr__(self, "propagate", False)
        self._cache.clear()

    def setLevel(self, _level: object) -> None:
        self.seal()

    def addFilter(self, _filter: object) -> None:
        return None

    def removeFilter(self, _filter: object) -> None:
        return None

    def addHandler(self, _handler: object) -> None:
        return None

    def removeHandler(self, _handler: object) -> None:
        return None

    def isEnabledFor(self, _level: int) -> bool:
        return False

    def getEffectiveLevel(self) -> int:
        return _HTTP_CORE_SUPPRESS_LEVEL

    def hasHandlers(self) -> bool:
        return False

    def _log(self, *_args: object, **_kwargs: object) -> None:
        return None

    def handle(self, _record: logging.LogRecord) -> None:
        return None

    def callHandlers(self, _record: logging.LogRecord) -> None:
        return None


def _seal_httpcore_logger(logger: logging.Logger) -> bool:
    try:
        if type(logger) is not _NoOpHttpcoreLogger:
            logger.__class__ = _NoOpHttpcoreLogger
        logger.seal()
        return (
            type(logger) is _NoOpHttpcoreLogger
            and logger.level == _HTTP_CORE_SUPPRESS_LEVEL
            and logger.disabled
            and logger.filters == ()
            and logger.handlers == ()
            and not logger.propagate
            and not logger.isEnabledFor(logging.CRITICAL + 100)
        )
    except Exception:
        return False


class _HttpcoreSealingManager(logging.Manager):
    """Preserve stdlib logger creation while sealing the httpcore namespace."""

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"__class__", "getLogger", "loggerDict"}:
            raise AttributeError("logging manager boundary is sealed")
        super().__setattr__(name, value)

    def getLogger(self, name: str) -> logging.Logger:
        logger = super().getLogger(name)
        if (name == "httpcore" or name.startswith("httpcore.")) and not _seal_httpcore_logger(
            logger
        ):
            raise RuntimeError("httpcore logger sealing failed")
        return logger


_LOGGING_MANAGER_ANCHOR: Final = logging.Logger.manager
_LOGGING_LOGGER_DICT_ANCHOR: Final = _LOGGING_MANAGER_ANCHOR.loggerDict


def _install_httpcore_logger_boundary() -> bool:
    try:
        manager = _LOGGING_MANAGER_ANCHOR
        if logging.Logger.manager is not manager or logging.root.manager is not manager:
            return False
        if type(manager) is logging.Manager:
            manager.__class__ = _HttpcoreSealingManager
        return (
            type(manager) is _HttpcoreSealingManager
            and manager.loggerDict is _LOGGING_LOGGER_DICT_ANCHOR
            and "getLogger" not in manager.__dict__
            and type(manager).getLogger is _HttpcoreSealingManager.getLogger
        )
    except Exception:
        return False


def _suppress_httpcore_diagnostics() -> bool:
    """Permanently seal every registered httpcore diagnostic origin."""
    try:
        if not _install_httpcore_logger_boundary():
            return False
        parent = _LOGGING_MANAGER_ANCHOR.getLogger("httpcore")
        candidates = [parent]
        candidates.extend(
            candidate
            for name, candidate in list(_LOGGING_LOGGER_DICT_ANCHOR.items())
            if name.startswith("httpcore.") and isinstance(candidate, logging.Logger)
        )
        if not all(_seal_httpcore_logger(candidate) for candidate in candidates):
            return False
        return all(
            type(candidate) is _NoOpHttpcoreLogger
            and not candidate.isEnabledFor(logging.CRITICAL + 100)
            for name, candidate in list(_LOGGING_LOGGER_DICT_ANCHOR.items())
            if (name == "httpcore" or name.startswith("httpcore."))
            and isinstance(candidate, logging.Logger)
        )
    except Exception:
        return False


# httpcore renders raw response headers and reason phrases inside the transport,
# before this module can sanitize a response. Its registered diagnostic origins
# therefore remain permanent no-op loggers for the process; application and HTTPX
# request logs remain live.
_HTTPCORE_DIAGNOSTICS_SUPPRESSED: Final = _suppress_httpcore_diagnostics()


class _SanitizingTransport(httpx.AsyncBaseTransport):
    __slots__ = ("_inner", "_inner_anchor")

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_inner_anchor", inner)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("Telegram transport composition is sealed")

    def integrity_ok(self) -> bool:
        try:
            inner = self._inner
            return inner is self._inner_anchor and type(inner) in {
                httpx.AsyncHTTPTransport,
                httpx.MockTransport,
            }
        except Exception:
            return False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.integrity_ok():
            raise RuntimeError("Telegram transport integrity failure")
        raw_request = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        try:
            inner_response = await self._inner_anchor.handle_async_request(raw_request)
            try:
                return httpx.Response(
                    inner_response.status_code,
                    headers=_safe_response_headers(inner_response),
                    stream=inner_response.stream,
                    extensions={
                        "http_version": b"HTTP/1.1",
                        "reason_phrase": b"Telegram",
                    },
                )
            except BaseException:
                with suppress(BaseException):
                    await inner_response.aclose()
                raise
        finally:
            request.url = _sanitized_request_url(request.url)

    async def aclose(self) -> None:
        await self._inner_anchor.aclose()


def _sanitized_request_url(value: httpx.URL) -> httpx.URL:
    endpoint = value.path.rpartition("/")[2]
    if endpoint not in _TELEGRAM_ENDPOINTS:
        endpoint = "request"
    return httpx.URL(f"https://api.telegram.org/bot-redacted/{endpoint}")


def _safe_response_headers(response: httpx.Response) -> list[tuple[bytes, bytes]]:
    try:
        raw_headers = response.headers.raw
        encodings = [
            value
            for name, value in raw_headers
            if isinstance(name, bytes) and name.lower() == b"content-encoding"
        ]
    except Exception:
        raise _UnsafeResponseMetadata from None
    if not encodings:
        return []
    if len(encodings) != 1 or not isinstance(encodings[0], bytes):
        raise _UnsafeResponseMetadata
    encoding = encodings[0].lower()
    if encoding not in _SAFE_CONTENT_ENCODINGS:
        raise _UnsafeResponseMetadata
    return [(b"content-encoding", encoding)]


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
        "_inner_transport_anchor",
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
        if token is None or _TELEGRAM_TOKEN_RE.fullmatch(token) is None:
            raise ValueError("invalid Telegram bot token")
        if transport is not None and type(transport) is not httpx.MockTransport:
            raise ValueError("invalid Telegram test transport")
        inner_transport: httpx.AsyncBaseTransport = (
            transport if transport is not None else httpx.AsyncHTTPTransport()
        )
        if not _suppress_httpcore_diagnostics():
            raise TelegramApiError("Telegram logging integrity failure")
        wrapped_transport = _SanitizingTransport(inner_transport)
        client = httpx.AsyncClient(transport=wrapped_transport, trust_env=False)
        hooks = MappingProxyType({"request": (), "response": ()})
        mounts = MappingProxyType(dict(client._mounts))
        object.__setattr__(client, "_event_hooks", hooks)
        object.__setattr__(client, "_mounts", mounts)
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_client_anchor", client)
        object.__setattr__(self, "_hooks_anchor", hooks)
        object.__setattr__(self, "_inner_transport_anchor", inner_transport)
        object.__setattr__(self, "_mounts_anchor", mounts)
        object.__setattr__(self, "_transport_anchor", wrapped_transport)
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
                and type(client._transport) is _SanitizingTransport
                and client._transport.integrity_ok()
                and client._transport._inner_anchor is self._inner_transport_anchor
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
        chat = _chat_identifier(chat_id)
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
        chat = _chat_identifier(chat_id)
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
        if not _HTTPCORE_DIAGNOSTICS_SUPPRESSED or not _suppress_httpcore_diagnostics():
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


class _UnsafeResponseMetadata(Exception):
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


def _chat_identifier(value: object) -> str | None:
    if type(value) is int:
        if value != 0 and abs(value) <= MAX_IDENTIFIER:
            return str(value)
        return None
    if type(value) is not str or not value.isascii():
        return None
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    if not digits.isdigit() or digits.startswith("0"):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    if number == 0 or abs(number) > MAX_IDENTIFIER:
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
    if (
        not _is_int_in_range(message_id, minimum=1, maximum=MAX_IDENTIFIER)
        or type(chat_id) is not int
        or _chat_identifier(chat_id) is None
        or not _is_int_in_range(from_user_id, minimum=1, maximum=MAX_IDENTIFIER)
    ):
        return None
    if "text" not in value:
        return _UNSUPPORTED if _is_valid_unsupported_media_message(value) else None
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


def _is_valid_unsupported_media_message(value: Mapping[str, object]) -> bool:
    media_fields = _UNSUPPORTED_MEDIA_FIELDS.intersection(value)
    if len(media_fields) != 1:
        return False
    if "caption" in value:
        caption = _bounded_string(
            value.get("caption"),
            min_chars=1,
            max_chars=MAX_MEDIA_CAPTION_CHARS,
            max_bytes=MAX_MEDIA_CAPTION_CHARS * 4,
            allow_layout_controls=True,
        )
        if caption is None:
            return False

    field = next(iter(media_fields))
    media = value.get(field)
    if field == "photo":
        return _is_valid_photo_sizes(media)
    validators = {
        "animation": _is_valid_animation,
        "audio": _is_valid_audio,
        "document": _is_valid_document,
        "sticker": _is_valid_sticker,
        "video": _is_valid_video,
        "video_note": _is_valid_video_note,
        "voice": _is_valid_voice,
    }
    return validators[field](media)


def _is_valid_photo_sizes(value: object) -> bool:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PHOTO_SIZES:
        return False
    return all(_is_valid_photo_size(item) for item in value)


def _is_valid_photo_size(value: object) -> bool:
    allowed = {"file_id", "file_unique_id", "width", "height", "file_size"}
    return (
        _is_bounded_media_mapping(value, allowed)
        and _has_valid_media_file_ids(value)
        and _required_media_int(value, "width", minimum=1, maximum=MAX_MEDIA_DIMENSION)
        and _required_media_int(value, "height", minimum=1, maximum=MAX_MEDIA_DIMENSION)
        and _optional_media_int(value, "file_size", minimum=0, maximum=MAX_MEDIA_FILE_SIZE)
    )


def _is_valid_animation(value: object) -> bool:
    allowed = {
        "file_id",
        "file_unique_id",
        "width",
        "height",
        "duration",
        "thumbnail",
        "file_name",
        "mime_type",
        "file_size",
    }
    return (
        _is_bounded_media_mapping(value, allowed)
        and _has_valid_media_file_ids(value)
        and _required_media_int(value, "width", minimum=1, maximum=MAX_MEDIA_DIMENSION)
        and _required_media_int(value, "height", minimum=1, maximum=MAX_MEDIA_DIMENSION)
        and _required_media_int(value, "duration", minimum=0, maximum=MAX_MEDIA_DURATION)
        and _optional_thumbnail(value)
        and _optional_media_file_name(value)
        and _optional_media_mime_type(value)
        and _optional_media_int(value, "file_size", minimum=0, maximum=MAX_MEDIA_FILE_SIZE)
    )


def _is_valid_audio(value: object) -> bool:
    allowed = {
        "file_id",
        "file_unique_id",
        "duration",
        "performer",
        "title",
        "file_name",
        "mime_type",
        "file_size",
        "thumbnail",
    }
    return (
        _is_bounded_media_mapping(value, allowed)
        and _has_valid_media_file_ids(value)
        and _required_media_int(value, "duration", minimum=0, maximum=MAX_MEDIA_DURATION)
        and _optional_media_text(value, "performer")
        and _optional_media_text(value, "title")
        and _optional_media_file_name(value)
        and _optional_media_mime_type(value)
        and _optional_media_int(value, "file_size", minimum=0, maximum=MAX_MEDIA_FILE_SIZE)
        and _optional_thumbnail(value)
    )


def _is_valid_document(value: object) -> bool:
    allowed = {
        "file_id",
        "file_unique_id",
        "thumbnail",
        "file_name",
        "mime_type",
        "file_size",
    }
    return (
        _is_bounded_media_mapping(value, allowed)
        and _has_valid_media_file_ids(value)
        and _optional_thumbnail(value)
        and _optional_media_file_name(value)
        and _optional_media_mime_type(value)
        and _optional_media_int(value, "file_size", minimum=0, maximum=MAX_MEDIA_FILE_SIZE)
    )


def _is_valid_sticker(value: object) -> bool:
    allowed = {
        "file_id",
        "file_unique_id",
        "type",
        "width",
        "height",
        "is_animated",
        "is_video",
        "thumbnail",
        "emoji",
        "set_name",
        "premium_animation",
        "mask_position",
        "custom_emoji_id",
        "needs_repainting",
        "file_size",
    }
    sticker_type = value.get("type") if isinstance(value, dict) else None
    return (
        _is_bounded_media_mapping(value, allowed)
        and _has_valid_media_file_ids(value)
        and type(sticker_type) is str
        and sticker_type in {"regular", "mask", "custom_emoji"}
        and _required_media_int(value, "width", minimum=1, maximum=MAX_MEDIA_DIMENSION)
        and _required_media_int(value, "height", minimum=1, maximum=MAX_MEDIA_DIMENSION)
        and type(value.get("is_animated")) is bool
        and type(value.get("is_video")) is bool
        and _optional_thumbnail(value)
        and _optional_media_text(value, "emoji", max_chars=32)
        and _optional_media_text(value, "set_name")
        and _optional_media_file(value, "premium_animation")
        and _optional_mask_position(value)
        and _optional_media_text(value, "custom_emoji_id")
        and _optional_media_bool(value, "needs_repainting")
        and _optional_media_int(value, "file_size", minimum=0, maximum=MAX_MEDIA_FILE_SIZE)
    )


def _is_valid_video(value: object) -> bool:
    allowed = {
        "file_id",
        "file_unique_id",
        "width",
        "height",
        "duration",
        "thumbnail",
        "cover",
        "start_timestamp",
        "file_name",
        "mime_type",
        "file_size",
        "supports_streaming",
    }
    if not (
        _is_bounded_media_mapping(value, allowed)
        and _has_valid_media_file_ids(value)
        and _required_media_int(value, "width", minimum=1, maximum=MAX_MEDIA_DIMENSION)
        and _required_media_int(value, "height", minimum=1, maximum=MAX_MEDIA_DIMENSION)
        and _required_media_int(value, "duration", minimum=0, maximum=MAX_MEDIA_DURATION)
        and _optional_thumbnail(value)
        and _optional_photo_sizes(value, "cover")
        and _optional_media_file_name(value)
        and _optional_media_mime_type(value)
        and _optional_media_int(value, "file_size", minimum=0, maximum=MAX_MEDIA_FILE_SIZE)
        and _optional_media_bool(value, "supports_streaming")
    ):
        return False
    duration = value["duration"]
    return _optional_media_int(value, "start_timestamp", minimum=0, maximum=duration)


def _is_valid_video_note(value: object) -> bool:
    allowed = {
        "file_id",
        "file_unique_id",
        "length",
        "duration",
        "thumbnail",
        "file_size",
    }
    return (
        _is_bounded_media_mapping(value, allowed)
        and _has_valid_media_file_ids(value)
        and _required_media_int(value, "length", minimum=1, maximum=MAX_MEDIA_DIMENSION)
        and _required_media_int(value, "duration", minimum=0, maximum=MAX_MEDIA_DURATION)
        and _optional_thumbnail(value)
        and _optional_media_int(value, "file_size", minimum=0, maximum=MAX_MEDIA_FILE_SIZE)
    )


def _is_valid_voice(value: object) -> bool:
    allowed = {"file_id", "file_unique_id", "duration", "mime_type", "file_size"}
    return (
        _is_bounded_media_mapping(value, allowed)
        and _has_valid_media_file_ids(value)
        and _required_media_int(value, "duration", minimum=0, maximum=MAX_MEDIA_DURATION)
        and _optional_media_mime_type(value)
        and _optional_media_int(value, "file_size", minimum=0, maximum=MAX_MEDIA_FILE_SIZE)
    )


def _is_bounded_media_mapping(value: object, allowed: set[str]) -> bool:
    return (
        isinstance(value, dict)
        and 1 <= len(value) <= MAX_MEDIA_OBJECT_KEYS
        and set(value).issubset(allowed)
    )


def _has_valid_media_file_ids(value: Mapping[str, object]) -> bool:
    return _is_valid_media_file_id(value.get("file_id")) and _is_valid_media_file_id(
        value.get("file_unique_id")
    )


def _required_media_int(
    value: Mapping[str, object], key: str, *, minimum: int, maximum: int
) -> bool:
    return _is_int_in_range(value.get(key), minimum=minimum, maximum=maximum)


def _optional_media_int(
    value: Mapping[str, object], key: str, *, minimum: int, maximum: int
) -> bool:
    return key not in value or _is_int_in_range(value[key], minimum=minimum, maximum=maximum)


def _optional_media_bool(value: Mapping[str, object], key: str) -> bool:
    return key not in value or type(value[key]) is bool


def _optional_media_text(
    value: Mapping[str, object], key: str, *, max_chars: int = MAX_MEDIA_TEXT_CHARS
) -> bool:
    return key not in value or (
        _bounded_string(
            value[key],
            min_chars=1,
            max_chars=max_chars,
            max_bytes=max_chars * 4,
            allow_layout_controls=False,
        )
        is not None
    )


def _optional_media_file_name(value: Mapping[str, object]) -> bool:
    return _optional_media_text(value, "file_name", max_chars=MAX_MEDIA_FILE_NAME_CHARS)


def _optional_media_mime_type(value: Mapping[str, object]) -> bool:
    if "mime_type" not in value:
        return True
    mime_type = _bounded_string(
        value["mime_type"],
        min_chars=1,
        max_chars=MAX_MEDIA_MIME_TYPE_CHARS,
        max_bytes=MAX_MEDIA_MIME_TYPE_CHARS,
        allow_layout_controls=False,
    )
    return mime_type is not None and mime_type.isascii()


def _optional_thumbnail(value: Mapping[str, object]) -> bool:
    return "thumbnail" not in value or _is_valid_photo_size(value["thumbnail"])


def _optional_photo_sizes(value: Mapping[str, object], key: str) -> bool:
    return key not in value or _is_valid_photo_sizes(value[key])


def _optional_media_file(value: Mapping[str, object], key: str) -> bool:
    if key not in value:
        return True
    item = value[key]
    allowed = {"file_id", "file_unique_id", "file_size"}
    return (
        _is_bounded_media_mapping(item, allowed)
        and _has_valid_media_file_ids(item)
        and _optional_media_int(item, "file_size", minimum=0, maximum=MAX_MEDIA_FILE_SIZE)
    )


def _optional_mask_position(value: Mapping[str, object]) -> bool:
    if "mask_position" not in value:
        return True
    position = value["mask_position"]
    allowed = {"point", "x_shift", "y_shift", "scale"}
    if not _is_bounded_media_mapping(position, allowed) or set(position) != allowed:
        return False
    point = position["point"]
    if type(point) is not str or point not in {"forehead", "eyes", "mouth", "chin"}:
        return False
    return all(
        _is_finite_number(position[key], minimum=-100_000, maximum=100_000)
        for key in ("x_shift", "y_shift")
    ) and _is_finite_number(position["scale"], minimum=0, maximum=100_000)


def _is_finite_number(value: object, *, minimum: float, maximum: float) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and minimum <= value <= maximum


def _is_valid_media_file_id(value: object) -> bool:
    return (
        _bounded_string(
            value,
            min_chars=1,
            max_chars=MAX_MEDIA_FILE_ID_CHARS,
            max_bytes=MAX_MEDIA_FILE_ID_CHARS * 4,
            allow_layout_controls=False,
        )
        is not None
    )


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
    if (
        type(chat_id) is not int
        or _chat_identifier(chat_id) is None
        or not _is_int_in_range(message_id, minimum=1, maximum=MAX_IDENTIFIER)
    ):
        return None
    return CallbackQuery(callback_id, from_user_id, chat_id, message_id, data)
