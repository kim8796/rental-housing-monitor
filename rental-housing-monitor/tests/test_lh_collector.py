import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from rental_monitor.collectors.base import ParserStructureError
from rental_monitor.collectors.lh import LHCollector, parse_lh_response
from rental_monitor.models import Agency, HousingType

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_lh_response_normalizes_official_fields() -> None:
    notices, total = parse_lh_response(fixture("lh_notices.json"))

    assert total == 1
    assert len(notices) == 1
    assert notices[0].agency is Agency.LH
    assert notices[0].housing_type is HousingType.HAPPY
    assert notices[0].announcement_date == date(2026, 7, 20)
    assert notices[0].source_id == "2016122300001530"


def test_lh_schema_change_names_lh_in_error() -> None:
    with pytest.raises(ParserStructureError, match="LH"):
        parse_lh_response({"unexpected": []})


def test_lh_api_error_is_not_treated_as_empty_result() -> None:
    with pytest.raises(ParserStructureError, match="인증키"):
        parse_lh_response({"resHeader": [{"SS_CODE": "N", "ERR_MSG": "인증키 오류"}], "dsList": []})


def test_collector_queries_both_regions_and_notice_categories() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"resHeader": [{"SS_CODE": "Y"}], "dsList": []},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    collector = LHCollector(client, "encoded-key", today=lambda: date(2026, 7, 20))

    assert collector.collect() == []
    pairs = {(r.url.params["CNP_CD"], r.url.params["UPP_AIS_TP_CD"]) for r in requests}
    assert pairs == {("11", "06"), ("11", "13"), ("41", "06"), ("41", "13")}
    assert all(r.url.params["PAN_NT_ST_DT"] == "2026.04.21" for r in requests)
    assert all(r.url.params["CLSG_DT"] == "2026.07.20" for r in requests)


def test_collector_deduplicates_same_notice_across_queries() -> None:
    body = fixture("lh_notices.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    collector = LHCollector(client, "key", today=lambda: date(2026, 7, 20))

    assert len(collector.collect()) == 1
