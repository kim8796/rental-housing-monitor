from __future__ import annotations

from dataclasses import dataclass


class _RedactedRepr:
    __slots__ = ()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class TelegramMessage(_RedactedRepr):
    message_id: int
    chat_id: int
    from_user_id: int
    text: str


@dataclass(frozen=True, slots=True, repr=False)
class CallbackQuery(_RedactedRepr):
    id: str
    from_user_id: int
    chat_id: int
    message_id: int
    data: str


@dataclass(frozen=True, slots=True, repr=False)
class TelegramUpdate(_RedactedRepr):
    update_id: int
    message: TelegramMessage | None
    callback_query: CallbackQuery | None


@dataclass(frozen=True, slots=True, repr=False)
class InlineButton(_RedactedRepr):
    text: str
    callback_data: str
