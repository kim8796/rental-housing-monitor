from datetime import date

import httpx
import pytest

from rental_monitor.models import Agency, Announcement, HousingType
from rental_monitor.telegram import (
    TelegramClient,
    TelegramError,
    format_announcement,
    split_message,
)


def notice() -> Announcement:
    return Announcement(
        source_id="801",
        title="다산 국민임대주택 예비입주자 모집공고",
        agency=Agency.GH,
        region="경기도",
        housing_type=HousingType.NATIONAL,
        target="국민임대 입주자격 충족자",
        announcement_date=date(2026, 7, 10),
        application_start_date=date(2026, 7, 18),
        application_end_date=date(2026, 7, 20),
        url="https://apply.gh.or.kr/official/801",
    )


def test_formatted_notice_contains_all_required_fields() -> None:
    text = format_announcement(notice())

    for label in ("공고 제목", "기관", "지역", "공급유형", "대상", "공고일", "접수기간", "URL"):
        assert f"{label}:" in text
    assert "2026-07-18 ~ 2026-07-20" in text


def test_missing_application_period_is_explicit() -> None:
    item = notice()
    item = Announcement(
        **{
            field: getattr(item, field)
            for field in item.__dataclass_fields__
            if field not in {"application_start_date", "application_end_date"}
        },
        application_start_date=None,
        application_end_date=None,
    )
    assert "접수기간: 정보 없음" in format_announcement(item)


def test_message_split_never_exceeds_telegram_limit() -> None:
    chunks = split_message(("공고\n" * 1500).strip(), limit=4096)

    assert len(chunks) > 1
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == ("공고" * 1500)


def test_telegram_returns_last_message_id_after_all_chunks() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode())
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": len(sent)}}, request=request
        )

    client = TelegramClient(httpx.Client(transport=httpx.MockTransport(handler)), "token", "42")

    assert client.send("x" * 5000) == 2
    assert len(sent) == 2


def test_telegram_ok_false_raises_without_exposing_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "description": "chat not found"}, request=request
        )

    client = TelegramClient(
        httpx.Client(transport=httpx.MockTransport(handler)), "secret-token", "42"
    )

    with pytest.raises(TelegramError, match="chat not found") as caught:
        client.send("hello")
    assert "secret-token" not in str(caught.value)
