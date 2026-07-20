from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

import httpx
from bs4 import BeautifulSoup, Tag

from rental_monitor.collectors.base import ParserStructureError, request_with_retry
from rental_monitor.collectors.sh import _default_target, _extract_application_period
from rental_monitor.filters import classify_housing_type, is_recruitment_title, normalize_region
from rental_monitor.models import Agency, Announcement, canonical_key

GH_RENTAL_LIST_URL = "https://apply.gh.or.kr/sb/sr/sr7150/selectPbancRentHouseList.do"
GH_PURCHASE_LIST_URL = "https://apply.gh.or.kr/sb/sr/sr7155/selectPbancRentHouseList.do"
GH_RENTAL_DETAIL_URL = (
    "https://apply.gh.or.kr/sb/sr/sr7150/selectPbancDetailView.do?pbancNo={source_id}"
)
GH_PURCHASE_DETAIL_URL = (
    "https://apply.gh.or.kr/sb/sr/sr7155/selectPbancDetailView.do?pbancNo={source_id}"
)


@dataclass(frozen=True, slots=True)
class GHListItem:
    source_id: str
    title: str
    raw_type: str
    raw_region: str
    announcement_date: date
    application_end_date: date | None
    url: str


class GHCollector:
    agency = Agency.GH

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def collect(self) -> list[Announcement]:
        candidates: dict[str, GHListItem] = {}
        for source_kind, url in (
            ("rental", GH_RENTAL_LIST_URL),
            ("purchase", GH_PURCHASE_LIST_URL),
        ):
            response = request_with_retry(self.client, "GET", url, timeout=20)
            for candidate in parse_gh_list(response.text, source_kind=source_kind):
                candidates[candidate.source_id] = candidate

        notices: dict[str, Announcement] = {}
        for candidate in candidates.values():
            response = request_with_retry(self.client, "GET", candidate.url, timeout=20)
            notice = parse_gh_detail(response.text, candidate)
            if notice is not None:
                notices[canonical_key(notice)] = notice
        return list(notices.values())


def parse_gh_list(html: str, *, source_kind: Literal["rental", "purchase"]) -> list[GHListItem]:
    soup = BeautifulSoup(html, "html.parser")
    table = next(
        (
            candidate
            for candidate in soup.find_all("table")
            if {"유형", "공고명", "지역", "게시일", "마감일"}.issubset(_headers(candidate))
        ),
        None,
    )
    if table is None:
        if re.search(r"총게시물\s*:\s*0", soup.get_text(" ", strip=True)):
            return []
        raise ParserStructureError(Agency.GH, "목록 파싱", "청약공고 표를 찾지 못했습니다")

    headers = [cell.get_text(" ", strip=True) for cell in table.select("thead th")]
    index = {name: position for position, name in enumerate(headers)}
    rows = table.select("tbody tr")
    if not rows:
        return []
    candidates: list[GHListItem] = []
    parsed_rows = 0
    for row in rows:
        # GH's live markup omits several closing </td> tags. Recursive lookup
        # preserves the browser-repaired semantic column order.
        cells = row.find_all("td")
        if len(cells) <= max(index.values()):
            continue
        link = cells[index["공고명"]].find("a", attrs={"data-pbancno": True})
        if link is None:
            continue
        source_id = str(link.get("data-pbancno", "")).strip()
        if not source_id:
            continue
        parsed_rows += 1
        title = link.get_text(" ", strip=True)
        raw_type = str(link.get("data-biztynm") or cells[index["유형"]].get_text(" ", strip=True))
        raw_region = cells[index["지역"]].get_text(" ", strip=True)
        if _gh_region(raw_region) is None or not is_recruitment_title(title):
            continue
        if source_kind == "rental" and classify_housing_type(title, raw_type) is None:
            continue
        end_text = cells[index["마감일"]].get_text(" ", strip=True)
        detail_template = (
            GH_RENTAL_DETAIL_URL if source_kind == "rental" else GH_PURCHASE_DETAIL_URL
        )
        candidates.append(
            GHListItem(
                source_id=source_id,
                title=title,
                raw_type=raw_type,
                raw_region=raw_region,
                announcement_date=_parse_date(cells[index["게시일"]].get_text(" ", strip=True)),
                application_end_date=_optional_date(end_text),
                url=detail_template.format(source_id=source_id),
            )
        )
    if parsed_rows == 0:
        raise ParserStructureError(Agency.GH, "목록 파싱", "공고 행의 pbancNo를 찾지 못했습니다")
    return candidates


def parse_gh_detail(html: str, candidate: GHListItem) -> Announcement | None:
    soup = BeautifulSoup(html, "html.parser")
    values = _label_values(soup)
    if "공고일" not in values:
        raise ParserStructureError(Agency.GH, "상세 파싱", "공고일 필드를 찾지 못했습니다")
    content = soup.select_one("#sub_content") or soup
    text = content.get_text("\n", strip=True)
    raw_type = values.get("유형", candidate.raw_type)
    classification_text = candidate.title
    if "매입임대" in raw_type.replace(" ", ""):
        classification_text = f"{classification_text} {values.get('공고문', '')}"
    housing_type = classify_housing_type(classification_text, raw_type)
    if housing_type is None:
        return None
    region = _gh_region(candidate.raw_region)
    if region is None:
        return None
    start, end = _extract_application_period(text, Agency.GH)
    if start is None:
        end = candidate.application_end_date
    return Announcement(
        source_id=candidate.source_id,
        title=candidate.title,
        agency=Agency.GH,
        region=region,
        housing_type=housing_type,
        target=_default_target(housing_type),
        announcement_date=_parse_date(values["공고일"]),
        application_start_date=start,
        application_end_date=end,
        url=candidate.url,
    )


def _label_values(soup: BeautifulSoup) -> dict[str, str]:
    values: dict[str, str] = {}
    for heading in soup.find_all("th"):
        sibling = heading.find_next_sibling("td")
        if sibling is not None:
            values[heading.get_text(" ", strip=True)] = sibling.get_text(" ", strip=True)
    return values


def _headers(table: Tag) -> set[str]:
    return {cell.get_text(" ", strip=True) for cell in table.select("thead th")}


def _parse_date(value: str) -> date:
    match = re.search(r"(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", value)
    if not match:
        raise ParserStructureError(Agency.GH, "날짜 파싱", f"지원하지 않는 날짜: {value}")
    return date(*(int(part) for part in match.groups()))


def _optional_date(value: str) -> date | None:
    if not re.search(r"\d{4}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2}", value):
        return None
    return _parse_date(value)


def _gh_region(raw_region: str) -> str | None:
    normalized = normalize_region(raw_region)
    if normalized is not None:
        return normalized
    if raw_region.strip() and raw_region.strip() != "-":
        return "경기도"
    return None
