from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import personal_monitor.adapters.rental_housing as rental_module
from personal_monitor.adapters.rental_housing import (
    RENTAL_ANNOUNCEMENT_FIELDS,
    RENTAL_ITEM_SCOPE,
    RentalHousingAdapter,
)
from personal_monitor.domain.spec import MonitorSpec
from personal_monitor.engine.errors import ErrorClass, MonitorError
from rental_monitor.collectors.base import ParserStructureError
from rental_monitor.collectors.gh import GHCollector
from rental_monitor.collectors.lh import LHCollector
from rental_monitor.collectors.sh import SHCollector
from rental_monitor.models import Agency, Announcement, HousingType, canonical_key

FIXTURES = Path(__file__).parents[2] / "fixtures"
NOW = datetime(2026, 7, 23, 3, 4, 5, tzinfo=UTC)


def rental_spec(**overrides: object) -> MonitorSpec:
    payload: dict[str, object] = {
        "schema_version": 1,
        "owner_id": "telegram-user:7",
        "name": "서울·경기 임대주택",
        "target_url": "https://apply.lh.or.kr/",
        "source_adapter": "python_plugin",
        "adapter_ref": "rental_housing",
        "fetch_strategy": "http",
        "schedule": "13 12 * * *",
        "timezone": "Asia/Seoul",
        "extract": {
            "item_scope": RENTAL_ITEM_SCOPE,
            "fields": {
                name: field.model_dump(mode="json")
                for name, field in RENTAL_ANNOUNCEMENT_FIELDS.items()
            },
        },
        "validators": {"min_items": 0, "max_items": 10_000},
        "rules": [{"kind": "new_item"}],
        "notify_on_no_change": True,
    }
    payload.update(overrides)
    return MonitorSpec.model_validate(payload)


def announcement(
    agency: Agency,
    source_id: str = "1",
    *,
    title: str | None = None,
) -> Announcement:
    return Announcement(
        source_id=source_id,
        title=title or f"{agency.value} 행복주택 입주자 모집",
        agency=agency,
        region="서울특별시" if agency is not Agency.GH else "경기도",
        housing_type=HousingType.HAPPY,
        target="청년",
        announcement_date=date(2026, 7, 20),
        application_start_date=date(2026, 7, 25),
        application_end_date=date(2026, 7, 30),
        url=f"https://example.com/{agency.value}/{source_id}",
    )


class FakeCollector:
    def __init__(self, agency: Agency, result: object) -> None:
        self.agency = agency
        self.result = result
        self.calls = 0

    def collect(self) -> object:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def adapter_for(
    lh: object,
    sh: object,
    gh: object,
    *,
    clock=lambda: NOW,
) -> RentalHousingAdapter:
    return RentalHousingAdapter.from_collectors(
        (
            FakeCollector(Agency.LH, lh),
            FakeCollector(Agency.SH, sh),
            FakeCollector(Agency.GH, gh),
        ),
        clock=clock,
    )


def test_all_success_preserves_exact_identity_fields_and_deterministic_order() -> None:
    notices = [
        announcement(Agency.LH, "30"),
        announcement(Agency.SH, "20"),
        announcement(Agency.GH, "10"),
    ]
    batch = asyncio.run(
        adapter_for(notices[:1], notices[1:2], notices[2:]).fetch("m", rental_spec())
    )

    assert batch.monitor_id == "m"
    assert batch.observed_at == NOW
    assert batch.source_status == {"LH": "ok", "SH": "ok", "GH": "ok"}
    assert batch.warnings == ()
    assert [item.item_id for item in batch.items] == sorted(
        f"announcement:{canonical_key(notice)}" for notice in notices
    )
    item = next(item for item in batch.items if item.item_id.endswith("LH:30"))
    assert dict(item.fields) == {
        "source_id": "30",
        "title": "LH 행복주택 입주자 모집",
        "agency": "LH",
        "region": "서울특별시",
        "housing_type": "행복주택",
        "target": "청년",
        "announcement_date": "2026-07-20",
        "application_start_date": "2026-07-25",
        "application_end_date": "2026-07-30",
        "url": "https://example.com/LH/30",
    }


def test_parser_failure_is_isolated_in_agency_order() -> None:
    batch = asyncio.run(
        adapter_for(
            [announcement(Agency.LH)],
            ParserStructureError(Agency.SH, "목록 파싱", "공고 행 없음"),
            [announcement(Agency.GH)],
        ).fetch("m", rental_spec())
    )

    assert batch.source_status == {"LH": "ok", "SH": "failed", "GH": "ok"}
    assert [(warning.source, warning.stage, warning.detail) for warning in batch.warnings] == [
        ("SH", "목록 파싱", "공고 행 없음")
    ]
    assert {item.fields["agency"] for item in batch.items} == {"LH", "GH"}


def test_unexpected_exception_never_leaks_its_secret_bearing_message() -> None:
    class TransportExplosion(RuntimeError):
        pass

    secret = "token=top-secret&cookie=session-secret"
    batch = asyncio.run(adapter_for([], TransportExplosion(secret), []).fetch("m", rental_spec()))

    assert batch.source_status == {"LH": "ok", "SH": "failed", "GH": "ok"}
    assert batch.warnings[0].stage == "collection"
    assert batch.warnings[0].detail == "TransportExplosion"
    assert secret not in repr(batch)
    assert "top-secret" not in batch.source_hash


def test_all_failed_and_zero_item_success_are_distinct_and_deterministic() -> None:
    def failure(agency: Agency) -> ParserStructureError:
        return ParserStructureError(agency, "수집", "실패")

    failed = asyncio.run(
        adapter_for(failure(Agency.LH), failure(Agency.SH), failure(Agency.GH)).fetch(
            "m", rental_spec()
        )
    )
    empty = asyncio.run(adapter_for([], [], []).fetch("m", rental_spec()))

    assert failed.items == ()
    assert failed.source_status == {"LH": "failed", "SH": "failed", "GH": "failed"}
    assert [warning.source for warning in failed.warnings] == ["LH", "SH", "GH"]
    assert empty.items == ()
    assert empty.source_status == {"LH": "ok", "SH": "ok", "GH": "ok"}
    assert failed.source_hash != empty.source_hash


def test_identical_duplicates_are_stable_but_conflicts_fail_closed() -> None:
    original = announcement(Agency.LH, "same")
    duplicate = announcement(Agency.LH, "same")
    first = asyncio.run(adapter_for([original, duplicate], [], []).fetch("m", rental_spec()))
    second = asyncio.run(adapter_for([duplicate, original], [], []).fetch("m", rental_spec()))
    assert first.items == second.items
    assert first.source_hash == second.source_hash

    conflict = announcement(Agency.LH, "same", title="다른 제목")
    with pytest.raises(MonitorError, match="conflicting duplicate") as caught:
        asyncio.run(adapter_for([original, conflict], [], []).fetch("m", rental_spec()))
    assert caught.value.error_class is ErrorClass.VALIDATION
    assert "다른 제목" not in repr(caught.value)


def test_hash_covers_status_and_warnings_but_not_observation_time() -> None:
    notices = [announcement(Agency.LH)]
    one = asyncio.run(adapter_for(notices, [], [], clock=lambda: NOW).fetch("m", rental_spec()))
    later = asyncio.run(
        adapter_for(notices, [], [], clock=lambda: NOW + timedelta(days=1)).fetch(
            "m", rental_spec()
        )
    )
    failed = asyncio.run(
        adapter_for(
            notices,
            ParserStructureError(Agency.SH, "목록", "변경"),
            [],
            clock=lambda: NOW,
        ).fetch("m", rental_spec())
    )
    assert one.source_hash == later.source_hash
    assert one.source_hash != failed.source_hash


@pytest.mark.parametrize(
    "bad_clock",
    [
        lambda: datetime(2026, 7, 23),
        lambda: datetime(2026, 7, 23, tzinfo=timezone(timedelta(hours=9))),
        lambda: object(),
    ],
)
def test_clock_must_return_a_non_hostile_aware_utc_datetime(bad_clock) -> None:
    with pytest.raises(MonitorError, match="UTC clock") as caught:
        asyncio.run(adapter_for([], [], [], clock=bad_clock).fetch("m", rental_spec()))
    assert caught.value.error_class is ErrorClass.VALIDATION


def test_incompatible_spec_is_rejected_before_collector_factory_is_touched() -> None:
    touched = False

    def factory():
        nonlocal touched
        touched = True
        raise AssertionError("must not be called")

    adapter = RentalHousingAdapter(factory, clock=lambda: NOW)
    incompatible = rental_spec(
        source_adapter="official_api",
        adapter_ref="json_get",
    )
    with pytest.raises(MonitorError) as caught:
        asyncio.run(adapter.fetch("m", incompatible))
    assert caught.value.error_class is ErrorClass.POLICY
    assert touched is False

    fields = {
        name: field.model_dump(mode="json")
        for name, field in RENTAL_ANNOUNCEMENT_FIELDS.items()
        if name != "url"
    }
    with pytest.raises(MonitorError) as caught:
        asyncio.run(
            adapter.fetch(
                "m",
                rental_spec(extract={"item_scope": RENTAL_ITEM_SCOPE, "fields": fields}),
            )
        )
    assert caught.value.error_class is ErrorClass.VALIDATION
    assert touched is False


def test_wrong_agency_and_malformed_result_are_isolated_without_raw_object_leaks() -> None:
    wrong = announcement(Agency.GH, "wrong")
    batch = asyncio.run(
        adapter_for([wrong], {"secret": "credential"}, []).fetch("m", rental_spec())
    )

    assert batch.items == ()
    assert batch.source_status == {"LH": "failed", "SH": "failed", "GH": "ok"}
    assert [warning.detail for warning in batch.warnings] == [
        "invalid collector result",
        "invalid collector result",
    ]
    assert "credential" not in repr(batch)


def test_cancellation_propagates_instead_of_becoming_a_warning() -> None:
    cancelled = adapter_for([], asyncio.CancelledError(), [])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled.fetch("m", rental_spec()))


def test_injected_collectors_are_not_closed() -> None:
    class CloseAware(FakeCollector):
        def close(self) -> None:
            raise AssertionError("injected collector must not be closed")

    adapter = RentalHousingAdapter.from_collectors(
        (
            CloseAware(Agency.LH, []),
            CloseAware(Agency.SH, []),
            CloseAware(Agency.GH, []),
        ),
        clock=lambda: NOW,
    )
    asyncio.run(adapter.fetch("m", rental_spec()))


def test_production_factory_uses_exact_clients_and_closes_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[object] = []
    ssl_context = object()

    class FakeClient:
        def __init__(self, **options: object) -> None:
            self.options = options
            self.closed = False
            clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *unused: object) -> None:
            self.closed = True

    class EmptyCollector:
        def __init__(self, agency: Agency, *unused: object) -> None:
            self.agency = agency

        def collect(self) -> list[Announcement]:
            return []

    monkeypatch.setattr(rental_module.httpx, "Client", FakeClient)
    monkeypatch.setattr(rental_module, "build_gh_ssl_context", lambda: ssl_context)
    monkeypatch.setattr(
        rental_module,
        "LHCollector",
        lambda client, key: EmptyCollector(Agency.LH, client, key),
    )
    monkeypatch.setattr(
        rental_module,
        "SHCollector",
        lambda client: EmptyCollector(Agency.SH, client),
    )
    monkeypatch.setattr(
        rental_module,
        "GHCollector",
        lambda client: EmptyCollector(Agency.GH, client),
    )

    batch = asyncio.run(
        RentalHousingAdapter.production("encoded-key", clock=lambda: NOW).fetch("m", rental_spec())
    )

    assert batch.source_status == {"LH": "ok", "SH": "ok", "GH": "ok"}
    assert len(clients) == 2
    assert clients[0].options == {
        "follow_redirects": True,
        "headers": {"User-Agent": "rental-housing-monitor/0.1 (+official-notice-checker)"},
    }
    assert clients[1].options == {**clients[0].options, "verify": ssl_context}
    assert all(client.closed for client in clients)


def test_real_collectors_run_offline_through_all_six_unchanged_fixtures() -> None:
    lh_payload = json.loads((FIXTURES / "lh_notices.json").read_text(encoding="utf-8"))
    sh_list = (FIXTURES / "sh_list.html").read_text(encoding="utf-8")
    sh_detail = (FIXTURES / "sh_detail.html").read_text(encoding="utf-8")
    gh_rental = (FIXTURES / "gh_rental_list.html").read_text(encoding="utf-8")
    gh_purchase = (FIXTURES / "gh_purchase_list.html").read_text(encoding="utf-8")
    gh_detail = (FIXTURES / "gh_detail.html").read_text(encoding="utf-8")

    def common_handler(request: httpx.Request) -> httpx.Response:
        if "apis.data.go.kr" in request.url.host:
            return httpx.Response(200, json=lh_payload, request=request)
        if "view.do" in request.url.path:
            return httpx.Response(200, text=sh_detail, request=request)
        return httpx.Response(200, text=sh_list, request=request)

    def gh_handler(request: httpx.Request) -> httpx.Response:
        if "DetailView" in request.url.path:
            return httpx.Response(200, text=gh_detail, request=request)
        body = gh_purchase if "sr7155" in request.url.path else gh_rental
        return httpx.Response(200, text=body, request=request)

    common = httpx.Client(transport=httpx.MockTransport(common_handler))
    gh = httpx.Client(transport=httpx.MockTransport(gh_handler))
    try:
        adapter = RentalHousingAdapter.from_collectors(
            (
                LHCollector(common, "offline-key", today=lambda: date(2026, 7, 20)),
                SHCollector(common),
                GHCollector(gh),
            ),
            clock=lambda: NOW,
        )
        batch = asyncio.run(adapter.fetch("rental-housing-seoul-gyeonggi", rental_spec()))
    finally:
        common.close()
        gh.close()

    assert batch.source_status == {"LH": "ok", "SH": "ok", "GH": "ok"}
    assert {item.fields["agency"] for item in batch.items} == {"LH", "SH", "GH"}
    assert len(batch.items) == 5
