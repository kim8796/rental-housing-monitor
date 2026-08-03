import pytest

from rental_monitor.filters import (
    classify_housing_type,
    is_recruitment_title,
    normalize_region,
)
from rental_monitor.models import HousingType


def test_newlywed_purchase_requires_purchase_and_newlywed_terms() -> None:
    result = classify_housing_type(
        title="신혼·신생아 매입임대 입주자 모집",
        raw_type="매입임대",
    )
    assert result is HousingType.NEWLYWED_PURCHASE


def test_general_purchase_rental_is_excluded() -> None:
    assert classify_housing_type("기존주택 매입임대 모집", "매입임대") is None


def test_happy_and_national_rental_are_classified() -> None:
    assert classify_housing_type("입주자 모집", "행복주택") is HousingType.HAPPY
    assert classify_housing_type("입주자 모집", "국민임대") is HousingType.NATIONAL


def test_follow_up_result_post_is_excluded() -> None:
    assert is_recruitment_title("행복주택 당첨자 발표") is False
    assert is_recruitment_title("행복주택 입주자 모집공고") is True


@pytest.mark.parametrize(
    "title",
    (
        "[2일차 동호선정 완료] 신혼·신생아 매입임대주택 입주대기자 모집공고",
        "행복주택 입주자 모집공고 서류심사대상자 발표 및 서류제출 안내",
        "국민임대주택 예비입주자 모집공고 예비당첨자 발표",
    ),
)
def test_operational_follow_up_posts_are_not_new_recruitment(title: str) -> None:
    assert is_recruitment_title(title) is False


def test_corrected_recruitment_notice_is_included() -> None:
    assert is_recruitment_title("[정정공고] 행복주택 추가모집") is True


def test_only_seoul_and_gyeonggi_are_normalized() -> None:
    assert normalize_region("서울") == "서울특별시"
    assert normalize_region("경기도 수원시") == "경기도"
    assert normalize_region("서울특별시, 경기도") == "서울특별시, 경기도"
    assert normalize_region("부산광역시") is None
