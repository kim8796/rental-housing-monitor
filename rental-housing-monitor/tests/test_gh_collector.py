from datetime import date
from pathlib import Path

import httpx
import pytest

from rental_monitor.collectors.base import ParserStructureError
from rental_monitor.collectors.gh import GHCollector, parse_gh_detail, parse_gh_list
from rental_monitor.models import Agency, HousingType

FIXTURES = Path(__file__).parent / "fixtures"


def html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_gh_rental_list_maps_semantic_columns() -> None:
    candidates = parse_gh_list(html("gh_rental_list.html"), source_kind="rental")

    assert [item.source_id for item in candidates] == ["801", "788"]
    assert candidates[0].raw_type == "국민임대"
    assert candidates[0].announcement_date == date(2026, 7, 10)


def test_gh_detail_identifies_newlywed_purchase_and_period() -> None:
    candidate = parse_gh_list(html("gh_purchase_list.html"), source_kind="purchase")[0]

    notice = parse_gh_detail(html("gh_detail.html"), candidate)

    assert notice is not None
    assert notice.agency is Agency.GH
    assert notice.housing_type is HousingType.NEWLYWED_PURCHASE
    assert notice.application_start_date == date(2026, 5, 25)
    assert notice.application_end_date == date(2026, 6, 12)


def test_gh_global_navigation_does_not_override_official_national_type() -> None:
    candidate = parse_gh_list(html("gh_rental_list.html"), source_kind="rental")[0]

    notice = parse_gh_detail(html("gh_national_detail.html"), candidate)

    assert notice is not None
    assert notice.housing_type is HousingType.NATIONAL


def test_gh_missing_table_is_structure_error() -> None:
    with pytest.raises(ParserStructureError, match="GH"):
        parse_gh_list("<html>changed</html>", source_kind="rental")


def test_gh_collector_merges_duplicate_notice_from_two_lists() -> None:
    rental_with_duplicate = html("gh_rental_list.html").replace(
        'data-pbancNo="801"', 'data-pbancNo="792"', 1
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "selectPbancDetailView" in request.url.path:
            body = html("gh_detail.html")
        elif "sr7155" in request.url.path:
            body = html("gh_purchase_list.html")
        else:
            body = rental_with_duplicate
        return httpx.Response(200, text=body, request=request)

    notices = GHCollector(httpx.Client(transport=httpx.MockTransport(handler))).collect()

    assert [notice.source_id for notice in notices].count("792") == 1
