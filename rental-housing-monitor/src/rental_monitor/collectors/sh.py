from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import httpx
from bs4 import BeautifulSoup

from rental_monitor.collectors.base import ParserStructureError, request_with_retry
from rental_monitor.filters import classify_housing_type, is_recruitment_title
from rental_monitor.models import Agency, Announcement, HousingType

SH_LIST_URL = (
    "https://www.i-sh.co.kr/app/lay2/program/"
    "S1T294C297/www/brd/m_247/list.do?multi_itm_seq=2"
)
SH_DETAIL_URL = (
    "https://www.i-sh.co.kr/app/lay2/program/"
    "S1T294C297/www/brd/m_247/view.do?multi_itm_seq=2&seq={source_id}"
)


@dataclass(frozen=True, slots=True)
class SHListItem:
    source_id: str
    title: str
    announcement_date: date
    url: str


class SHCollector:
    agency = Agency.SH

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def collect(self) -> list[Announcement]:
        list_response = request_with_retry(self.client, "GET", SH_LIST_URL, timeout=20)
        candidates = parse_sh_list(list_response.text)
        notices: list[Announcement] = []
        for candidate in candidates:
            detail_response = request_with_retry(self.client, "GET", candidate.url, timeout=20)
            notices.append(parse_sh_detail(detail_response.text, candidate))
        return notices


def parse_sh_list(html: str) -> list[SHListItem]:
    soup = BeautifulSoup(html, "html.parser")
    table = next(
        (
            candidate
            for candidate in soup.find_all("table")
            if {"제목", "등록일"}.issubset(_headers(candidate))
        ),
        None,
    )
    if table is None:
        if "등록된 게시물이 없습니다" in soup.get_text(" ", strip=True):
            return []
        raise ParserStructureError(Agency.SH, "목록 파싱", "제목/등록일 표를 찾지 못했습니다")

    headers = [cell.get_text(" ", strip=True) for cell in table.select("thead th")]
    header_index = {name: index for index, name in enumerate(headers)}
    rows = table.select("tbody tr")
    if not rows:
        return []

    candidates: list[SHListItem] = []
    parsed_rows = 0
    for row in rows:
        cells = row.find_all("td", recursive=False)
        if len(cells) <= max(header_index["제목"], header_index["등록일"]):
            continue
        title_cell = cells[header_index["제목"]]
        link = title_cell.find("a", onclick=re.compile(r"getDetailView"))
        if link is None:
            continue
        match = re.search(r"getDetailView\(['\"]?(\d+)", str(link.get("onclick", "")))
        if not match:
            continue
        parsed_rows += 1
        title = link.get_text(" ", strip=True)
        if classify_housing_type(title, title) is None or not is_recruitment_title(title):
            continue
        source_id = match.group(1)
        candidates.append(
            SHListItem(
                source_id=source_id,
                title=title,
                announcement_date=_parse_date(cells[header_index["등록일"]].get_text(" ", strip=True)),
                url=SH_DETAIL_URL.format(source_id=source_id),
            )
        )
    if parsed_rows == 0:
        raise ParserStructureError(Agency.SH, "목록 파싱", "공고 행의 getDetailView ID를 찾지 못했습니다")
    return candidates


def parse_sh_detail(html: str, candidate: SHListItem) -> Announcement:
    soup = BeautifulSoup(html, "html.parser")
    detail = soup.select_one(".detailTable")
    if detail is None:
        raise ParserStructureError(Agency.SH, "상세 파싱", "상세 공고 컨테이너를 찾지 못했습니다")
    text = soup.get_text("\n", strip=True)
    housing_type = classify_housing_type(candidate.title, candidate.title)
    if housing_type is None:
        raise ParserStructureError(Agency.SH, "상세 파싱", "공급유형을 분류할 수 없습니다")
    target_match = re.search(r"신청자격\s*:\s*([^\n■]+)", text)
    target = target_match.group(1).strip() if target_match else _default_target(housing_type)
    application_start, application_end = _extract_application_period(text, Agency.SH)
    return Announcement(
        source_id=candidate.source_id,
        title=candidate.title,
        agency=Agency.SH,
        region="서울특별시",
        housing_type=housing_type,
        target=target,
        announcement_date=candidate.announcement_date,
        application_start_date=application_start,
        application_end_date=application_end,
        url=candidate.url,
    )


def _headers(table: object) -> set[str]:
    return {cell.get_text(" ", strip=True) for cell in table.select("thead th")}


def _parse_date(value: str) -> date:
    match = re.search(r"(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", value)
    if not match:
        raise ParserStructureError(Agency.SH, "날짜 파싱", f"지원하지 않는 날짜: {value}")
    return date(*(int(part) for part in match.groups()))


def _extract_application_period(text: str, agency: Agency) -> tuple[date | None, date | None]:
    label = re.search(
        r"(?:인터넷\s*접수|방문\s*접수|접수처\s*운영기간|접수기간|신청기간|청약기간)\s*:?",
        text,
    )
    if label is None:
        return None, None
    segment = text[label.end() : label.end() + 180]
    matches = re.findall(r"(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", segment)
    if not matches:
        raise ParserStructureError(agency, "접수기간 파싱", "접수기간 라벨 뒤 날짜를 찾지 못했습니다")
    dates = [date(*(int(part) for part in match)) for match in matches[:2]]
    return dates[0], dates[-1]


def _default_target(housing_type: HousingType) -> str:
    return {
        HousingType.HAPPY: "청년·신혼부부 등 행복주택 대상자",
        HousingType.NATIONAL: "국민임대 입주자격 충족자",
        HousingType.NEWLYWED_PURCHASE: "신혼부부·예비신혼부부·신생아 가구",
    }[housing_type]
