from datetime import date
from pathlib import Path

import httpx
import pytest

from rental_monitor.collectors.base import ParserStructureError
from rental_monitor.collectors.sh import SHCollector, parse_sh_detail, parse_sh_list
from rental_monitor.models import Agency, HousingType

FIXTURES = Path(__file__).parent / "fixtures"


def html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_sh_list_uses_seq_and_excludes_follow_up_posts() -> None:
    candidates = parse_sh_list(html("sh_list.html"))

    assert [item.source_id for item in candidates] == ["298109"]
    assert candidates[0].announcement_date == date(2025, 12, 31)


def test_sh_detail_extracts_target_and_application_period() -> None:
    candidate = parse_sh_list(html("sh_list.html"))[0]

    notice = parse_sh_detail(html("sh_detail.html"), candidate)

    assert notice.agency is Agency.SH
    assert notice.housing_type is HousingType.HAPPY
    assert notice.target == "대학생, 청년, 신혼부부, 고령자, 주거급여수급자"
    assert notice.application_start_date == date(2026, 1, 16)
    assert notice.application_end_date == date(2026, 1, 20)


def test_sh_missing_rows_without_empty_marker_is_structure_error() -> None:
    with pytest.raises(ParserStructureError, match="SH"):
        parse_sh_list("<html><body>changed</body></html>")


def test_sh_collector_calls_only_official_list_and_candidate_detail() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        body = html("sh_detail.html") if "view.do" in request.url.path else html("sh_list.html")
        return httpx.Response(200, text=body, request=request)

    collector = SHCollector(httpx.Client(transport=httpx.MockTransport(handler)))

    assert len(collector.collect()) == 1
    assert len(urls) == 2
    assert all("i-sh.co.kr" in url for url in urls)
