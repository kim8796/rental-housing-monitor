from __future__ import annotations

from rental_monitor.models import HousingType

_FOLLOW_UP_TERMS = ("당첨", "계약결과", "계약 결과", "서류심사 대상자", "예비자 발표")
_NEWLYWED_TERMS = ("신혼", "신생아")


def is_recruitment_title(title: str) -> bool:
    compact = " ".join(title.split())
    if "모집" not in compact:
        return False
    if any(term in compact for term in _FOLLOW_UP_TERMS) and "정정공고" not in compact:
        return False
    return True


def classify_housing_type(title: str, raw_type: str) -> HousingType | None:
    combined = f"{raw_type} {title}".replace(" ", "")
    if "매입임대" in combined and any(term in combined for term in _NEWLYWED_TERMS):
        return HousingType.NEWLYWED_PURCHASE
    if "행복주택" in combined:
        return HousingType.HAPPY
    if "국민임대" in combined:
        return HousingType.NATIONAL
    return None


def normalize_region(raw_region: str) -> str | None:
    regions: list[str] = []
    if "서울" in raw_region:
        regions.append("서울특별시")
    if "경기" in raw_region:
        regions.append("경기도")
    return ", ".join(regions) or None
