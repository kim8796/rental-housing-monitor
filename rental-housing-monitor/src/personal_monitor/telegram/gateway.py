from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol

from personal_monitor.control.actions import ActionDenied, ConsumedAction, PendingActionService
from personal_monitor.telegram.types import CallbackQuery, TelegramMessage, TelegramUpdate

_LOGGER = logging.getLogger(__name__)
_MAX_IDENTIFIER: Final = 2**63 - 1
_TOKEN_RE: Final = re.compile(r"(confirm|cancel|edit):([A-Za-z0-9_-]{32})\Z")


@dataclass(frozen=True, slots=True, repr=False)
class ControlRequest:
    owner_id: str
    chat_id: str
    text: str

    def __post_init__(self) -> None:
        if (
            type(self.owner_id) is not str
            or re.fullmatch(r"telegram-user:[1-9][0-9]{0,18}", self.owner_id) is None
            or type(self.chat_id) is not str
            or not self.chat_id.isascii()
            or not self.chat_id.isdigit()
            or not 1 <= int(self.chat_id) <= _MAX_IDENTIFIER
            or not _valid_text(self.text)
        ):
            raise ValueError("invalid control request")

    def __repr__(self) -> str:
        return "<ControlRequest redacted>"


class ControlRouter(Protocol):
    async def route(self, value: ControlRequest | ConsumedAction) -> object | None: ...


class CallbackApi(Protocol):
    async def answer_callback(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None: ...

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: tuple[tuple[object, ...], ...] | None = None,
        disable_web_page_preview: bool = True,
    ) -> str: ...


class TelegramGateway:
    __slots__ = (
        "_actions",
        "_actions_anchor",
        "_allowed_user_id_anchor",
        "_allowed_user_id",
        "_api",
        "_answer_callback_anchor",
        "_api_anchor",
        "_command_chat_id",
        "_command_chat_id_anchor",
        "_consume_anchor",
        "_route_anchor",
        "_send_message_anchor",
        "_router",
        "_router_anchor",
    )

    def __init__(
        self,
        allowed_user_id: int,
        command_chat_id: int,
        router: ControlRouter,
        actions: PendingActionService,
        api: CallbackApi,
    ) -> None:
        configuration_valid = (
            _valid_identifier(allowed_user_id)
            and _valid_identifier(command_chat_id)
            and type(actions) is PendingActionService
        )
        route = None
        consume = None
        answer_callback = None
        send_message = None
        capture_failed = not configuration_valid
        if configuration_valid:
            try:
                route = router.route
                answer_callback = api.answer_callback
                consume = actions.consume
                if not all(callable(item) for item in (route, consume, answer_callback)):
                    capture_failed = True
                candidate_send = getattr(api, "send_message", None)
                if callable(candidate_send):
                    send_message = candidate_send
            except Exception:
                capture_failed = True
        if capture_failed:
            raise ValueError("invalid Telegram gateway configuration") from None
        object.__setattr__(self, "_allowed_user_id", allowed_user_id)
        object.__setattr__(self, "_allowed_user_id_anchor", allowed_user_id)
        object.__setattr__(self, "_command_chat_id", command_chat_id)
        object.__setattr__(self, "_command_chat_id_anchor", command_chat_id)
        object.__setattr__(self, "_router", router)
        object.__setattr__(self, "_router_anchor", router)
        object.__setattr__(self, "_route_anchor", route)
        object.__setattr__(self, "_actions", actions)
        object.__setattr__(self, "_actions_anchor", actions)
        object.__setattr__(self, "_consume_anchor", consume)
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_api_anchor", api)
        object.__setattr__(self, "_answer_callback_anchor", answer_callback)
        object.__setattr__(self, "_send_message_anchor", send_message)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("TelegramGateway composition is sealed")

    def __repr__(self) -> str:
        return "<TelegramGateway redacted>"

    async def handle_update(self, update: TelegramUpdate, *, now: datetime | None = None) -> None:
        if type(update) is not TelegramUpdate:
            return
        if (update.message is None) == (update.callback_query is None):
            return

        if update.message is not None:
            message = update.message
            if type(message) is not TelegramMessage:
                return
            if not self._config_integrity_ok():
                return
            if not self._authorized(message.from_user_id, message.chat_id):
                return
            try:
                request = ControlRequest(
                    owner_id=f"telegram-user:{message.from_user_id}",
                    chat_id=str(message.chat_id),
                    text=message.text,
                )
            except ValueError:
                return
            if not self._integrity_ok():
                return
            reply = await self._route_anchor(request)
            await self._deliver_reply(message.chat_id, reply)
            return

        callback = update.callback_query
        if callback is None or type(callback) is not CallbackQuery:
            return
        if not self._config_integrity_ok():
            return
        if (
            not self._authorized(callback.from_user_id, callback.chat_id)
            or not _valid_callback_id(callback.id)
            or not _valid_identifier(callback.message_id)
        ):
            return
        parsed = _parse_callback(callback.data)
        if parsed is None:
            return
        if not self._integrity_ok():
            return
        operation, token = parsed
        owner_id = f"telegram-user:{callback.from_user_id}"
        consumed_at = datetime.now(UTC) if now is None else now
        try:
            action = self._consume_anchor(
                token,
                owner_id,
                now=consumed_at,
                operation="edit" if operation == "edit" else "confirm",
            )
        except ActionDenied:
            return
        if operation == "cancel":
            await self._answer_callback_anchor(
                callback.id,
                text="취소되었습니다",
                show_alert=False,
            )
            return
        reply = await self._route_anchor(action)
        await self._deliver_reply(callback.chat_id, reply)
        await self._answer_callback_anchor(
            callback.id,
            text="수정 안내를 보냈습니다" if operation == "edit" else "처리되었습니다",
            show_alert=False,
        )

    async def _deliver_reply(self, chat_id: int, value: object) -> None:
        from personal_monitor.control.messages import ControlReply

        if value is None:
            return
        if type(value) is not ControlReply or self._send_message_anchor is None:
            return
        try:
            reply = ControlReply(value.text, value.buttons)
        except Exception:
            return
        await self._send_message_anchor(
            chat_id,
            reply.text,
            buttons=reply.buttons or None,
            disable_web_page_preview=True,
        )

    def _authorized(self, user_id: object, chat_id: object) -> bool:
        if (
            _valid_identifier(user_id)
            and _valid_identifier(chat_id)
            and user_id == self._allowed_user_id_anchor
            and chat_id == self._command_chat_id_anchor
        ):
            return True
        safe_user_id = user_id if _valid_log_identifier(user_id) else 0
        _LOGGER.warning("unauthorized_telegram_user_id=%d", safe_user_id)
        return False

    def _config_integrity_ok(self) -> bool:
        try:
            allowed_user_id = self._allowed_user_id
            allowed_user_id_anchor = self._allowed_user_id_anchor
            command_chat_id = self._command_chat_id
            command_chat_id_anchor = self._command_chat_id_anchor
            if not all(
                type(value) is int
                for value in (
                    allowed_user_id,
                    allowed_user_id_anchor,
                    command_chat_id,
                    command_chat_id_anchor,
                )
            ):
                return False
            if (
                allowed_user_id is not allowed_user_id_anchor
                or command_chat_id is not command_chat_id_anchor
            ):
                return False
            return _valid_identifier(allowed_user_id) and _valid_identifier(command_chat_id)
        except Exception:
            return False

    def _integrity_ok(self) -> bool:
        try:
            return (
                self._router is self._router_anchor
                and self._actions is self._actions_anchor
                and self._api is self._api_anchor
                and self._allowed_user_id is self._allowed_user_id_anchor
                and self._command_chat_id is self._command_chat_id_anchor
                and type(self._actions_anchor) is PendingActionService
                and _callable_still_attached(self._route_anchor, self._router_anchor, "route")
                and _callable_still_attached(
                    self._consume_anchor,
                    self._actions_anchor,
                    "consume",
                )
                and _callable_still_attached(
                    self._answer_callback_anchor,
                    self._api_anchor,
                    "answer_callback",
                )
                and (
                    self._send_message_anchor is None
                    or _callable_still_attached(
                        self._send_message_anchor,
                        self._api_anchor,
                        "send_message",
                    )
                )
            )
        except Exception:
            return False


def _valid_identifier(value: object) -> bool:
    return type(value) is int and 1 <= value <= _MAX_IDENTIFIER


def _valid_log_identifier(value: object) -> bool:
    return type(value) is int and -_MAX_IDENTIFIER <= value <= _MAX_IDENTIFIER


def _callable_still_attached(captured: object, owner: object, name: str) -> bool:
    try:
        current = getattr(owner, name)
    except Exception:
        return False
    if not callable(captured) or not callable(current):
        return False
    if current is captured:
        return True
    return (
        getattr(captured, "__self__", None) is owner
        and getattr(current, "__self__", None) is owner
        and getattr(captured, "__func__", None) is getattr(current, "__func__", None)
    )


def _valid_callback_id(value: object) -> bool:
    if type(value) is not str or not 1 <= len(value) <= 256:
        return False
    try:
        if len(value.encode("utf-8", errors="strict")) > 1024:
            return False
    except UnicodeEncodeError:
        return False
    return not any(unicodedata.category(character).startswith("C") for character in value)


def _valid_text(value: object) -> bool:
    if type(value) is not str or not 1 <= len(value) <= 4096 or not value.strip():
        return False
    try:
        if len(value.encode("utf-8", errors="strict")) > 16_384:
            return False
    except UnicodeEncodeError:
        return False
    return not any(
        unicodedata.category(character).startswith("C") and character not in "\n\r\t"
        for character in value
    )


def _parse_callback(value: object) -> tuple[str, str] | None:
    if type(value) is not str or len(value) not in {37, 39, 40}:
        return None
    matched = _TOKEN_RE.fullmatch(value)
    if matched is None:
        return None
    return matched.group(1), matched.group(2)
