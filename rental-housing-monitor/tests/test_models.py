from datetime import date

from rental_monitor.models import Agency, Announcement, HousingType, canonical_key


def make_notice(**overrides: object) -> Announcement:
    values: dict[str, object] = {
        "source_id": "2016122300001530",
        "title": "서울 행복주택 입주자 모집",
        "agency": Agency.LH,
        "region": "서울특별시",
        "housing_type": HousingType.HAPPY,
        "target": "청년, 신혼부부",
        "announcement_date": date(2026, 7, 20),
        "application_start_date": date(2026, 7, 27),
        "application_end_date": date(2026, 7, 29),
        "url": "https://apply.lh.or.kr/detail?panId=123&utm_source=test",
    }
    values.update(overrides)
    return Announcement(**values)  # type: ignore[arg-type]


def test_source_id_makes_stable_agency_scoped_key() -> None:
    assert canonical_key(make_notice()) == "LH:2016122300001530"


def test_normalized_url_hash_is_used_without_source_id() -> None:
    with_tracking = canonical_key(make_notice(source_id=None))
    without_tracking = canonical_key(
        make_notice(source_id=None, url="https://apply.lh.or.kr/detail?panId=123")
    )

    assert with_tracking == without_tracking
    assert with_tracking.startswith("LH:url:")


def test_required_text_fields_cannot_be_blank() -> None:
    try:
        make_notice(title="  ")
    except ValueError as error:
        assert "title" in str(error)
    else:
        raise AssertionError("blank title was accepted")
