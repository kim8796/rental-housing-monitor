from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import httpx

from rental_monitor.collectors.base import ParserStructureError, request_with_retry
from rental_monitor.filters import classify_housing_type, is_recruitment_title, normalize_region
from rental_monitor.models import Agency, Announcement, HousingType, canonical_key

LH_API_URL = "https://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"
_REGION_CODES = ("11", "41")
_NOTICE_CATEGORY_CODES = ("06", "13")
_PAGE_SIZE = 100


class LHCollector:
    agency = Agency.LH

    def __init__(
        self,
        client: httpx.Client,
        service_key: str,
        *,
        today: Callable[[], date] = date.today,
        lookback_days: int = 90,
    ) -> None:
        self.client = client
        self.service_key = service_key
        self.today = today
        self.lookback_days = lookback_days

    def collect(self) -> list[Announcement]:
        collected: dict[str, Announcement] = {}
        end = self.today()
        start = end - timedelta(days=self.lookback_days)
        for region_code in _REGION_CODES:
            for category_code in _NOTICE_CATEGORY_CODES:
                page = 1
                while True:
                    response = request_with_retry(
                        self.client,
                        "GET",
                        LH_API_URL,
                        params={
                            "ServiceKey": self.service_key,
                            "PG_SZ": str(_PAGE_SIZE),
                            "PAGE": str(page),
                            "UPP_AIS_TP_CD": category_code,
                            "CNP_CD": region_code,
                            "PAN_NT_ST_DT": start.strftime("%Y.%m.%d"),
                            "CLSG_DT": end.strftime("%Y.%m.%d"),
                        },
                        timeout=20,
                    )
                    try:
                        payload = response.json()
                    except ValueError as error:
                        raise ParserStructureError(
                            Agency.LH, "JSON 파싱", "JSON 응답이 아닙니다"
                        ) from error
                    notices, total = parse_lh_response(payload)
                    for notice in notices:
                        collected[canonical_key(notice)] = notice
                    if page >= max(1, math.ceil(total / _PAGE_SIZE)):
                        break
                    page += 1
        return list(collected.values())


def parse_lh_response(payload: object) -> tuple[list[Announcement], int]:
    if isinstance(payload, list):
        payload = next(
            (item for item in payload if isinstance(item, dict) and "dsList" in item),
            payload,
        )
    if not isinstance(payload, dict) or "dsList" not in payload:
        raise ParserStructureError(Agency.LH, "응답 구조", "필수 dsList 경로가 없습니다")

    header = _first_mapping(payload.get("resHeader"))
    if header and str(header.get("SS_CODE", "Y")).upper() not in {"Y", "00"}:
        message = str(header.get("ERR_MSG") or header.get("RS_MSG") or "공식 API 오류")
        raise ParserStructureError(Agency.LH, "API 응답", message)

    rows = payload["dsList"]
    if not isinstance(rows, list):
        raise ParserStructureError(Agency.LH, "응답 구조", "dsList가 배열이 아닙니다")
    if not rows:
        return [], 0

    notices: list[Announcement] = []
    total = _parse_int(_require(rows[0], "ALL_CNT"))
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise ParserStructureError(Agency.LH, "목록 파싱", "공고 행이 객체가 아닙니다")
        title = _require(raw_row, "PAN_NM")
        raw_type = _require(raw_row, "AIS_TP_CD_NM")
        raw_region = _require(raw_row, "CNP_CD_NM")
        region = normalize_region(raw_region)
        housing_type = classify_housing_type(title, raw_type)
        if region is None or housing_type is None or not is_recruitment_title(title):
            continue
        notices.append(
            Announcement(
                source_id=str(
                    raw_row.get("PAN_ID") or _pan_id_from_url(_require(raw_row, "DTL_URL"))
                ),
                title=title,
                agency=Agency.LH,
                region=region,
                housing_type=housing_type,
                target=_target_for(housing_type),
                announcement_date=_parse_date(_require(raw_row, "PAN_NT_ST_DT")),
                application_start_date=None,
                application_end_date=None,
                url=_require(raw_row, "DTL_URL"),
            )
        )
    return notices, total


def _first_mapping(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return None


def _require(row: object, name: str) -> str:
    if not isinstance(row, dict) or not str(row.get(name, "")).strip():
        raise ParserStructureError(Agency.LH, "목록 파싱", f"필수 필드 {name}이 없습니다")
    return str(row[name]).strip()


def _parse_int(value: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise ParserStructureError(Agency.LH, "목록 파싱", "ALL_CNT가 숫자가 아닙니다") from error


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.replace(".", "-"))
    except ValueError as error:
        raise ParserStructureError(
            Agency.LH, "날짜 파싱", f"지원하지 않는 날짜: {value}"
        ) from error


def _pan_id_from_url(url: str) -> str:
    match = re.search(r"(?:PAN_ID[:=]|panId=)([A-Za-z0-9-]+)", url)
    if not match:
        raise ParserStructureError(Agency.LH, "목록 파싱", "PAN_ID를 찾을 수 없습니다")
    return match.group(1)


def _target_for(housing_type: HousingType) -> str:
    return {
        HousingType.HAPPY: "청년·신혼부부 등 행복주택 대상자",
        HousingType.NATIONAL: "국민임대 입주자격 충족자",
        HousingType.NEWLYWED_PURCHASE: "신혼부부·예비신혼부부·신생아 가구",
    }[housing_type]
