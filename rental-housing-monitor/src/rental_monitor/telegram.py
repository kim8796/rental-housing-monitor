from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from rental_monitor.collectors.base import request_with_retry
from rental_monitor.models import Announcement

TELEGRAM_MESSAGE_LIMIT = 4096


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, client: httpx.Client, bot_token: str, chat_id: str) -> None:
        self.client = client
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id

    def send(self, text: str) -> int:
        last_message_id: int | None = None
        for chunk in split_message(text):
            try:
                response = request_with_retry(
                    self.client,
                    "POST",
                    self._url,
                    data={
                        "chat_id": self.chat_id,
                        "text": chunk,
                        "disable_web_page_preview": "true",
                    },
                    timeout=20,
                )
            except httpx.HTTPError as error:
                raise TelegramError(
                    f"Telegram HTTP 요청 실패: {type(error).__name__}"
                ) from error
            try:
                payload: Any = response.json()
            except ValueError as error:
                raise TelegramError("Telegram 응답이 JSON이 아닙니다") from error
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                description = payload.get("description", "알 수 없는 Telegram API 오류") if isinstance(payload, dict) else "잘못된 응답 구조"
                raise TelegramError(str(description))
            result = payload.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
                raise TelegramError("Telegram 응답에 message_id가 없습니다")
            last_message_id = result["message_id"]
        if last_message_id is None:
            raise TelegramError("빈 메시지는 전송할 수 없습니다")
        return last_message_id


def format_announcement(announcement: Announcement) -> str:
    return "\n".join(
        (
            "🏠 신규 임대주택 공고",
            f"공고 제목: {announcement.title}",
            f"기관: {announcement.agency.value}",
            f"지역: {announcement.region}",
            f"공급유형: {announcement.housing_type.value}",
            f"대상: {announcement.target}",
            f"공고일: {announcement.announcement_date.isoformat()}",
            f"접수기간: {_format_period(announcement.application_start_date, announcement.application_end_date)}",
            f"URL: {announcement.url}",
        )
    )


def split_message(text: str, *, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _format_period(start: date | None, end: date | None) -> str:
    if start is None and end is None:
        return "정보 없음"
    if start is None:
        return f"정보 없음 ~ {end.isoformat()}"
    if end is None:
        return f"{start.isoformat()} ~ 정보 없음"
    return f"{start.isoformat()} ~ {end.isoformat()}"
