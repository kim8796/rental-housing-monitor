from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class Agency(StrEnum):
    LH = "LH"
    SH = "SH"
    GH = "GH"


class HousingType(StrEnum):
    HAPPY = "행복주택"
    NATIONAL = "국민임대"
    NEWLYWED_PURCHASE = "신혼부부 매입임대"


@dataclass(frozen=True, slots=True)
class Announcement:
    source_id: str | None
    title: str
    agency: Agency
    region: str
    housing_type: HousingType
    target: str
    announcement_date: date
    application_start_date: date | None
    application_end_date: date | None
    url: str

    def __post_init__(self) -> None:
        for field_name in ("title", "region", "target", "url"):
            value = getattr(self, field_name)
            if not value.strip():
                raise ValueError(f"{field_name} must not be blank")


_TRACKING_QUERY_NAMES = {
    "fbclid",
    "gclid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_NAMES
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            urlencode(sorted(query)),
            "",
        )
    )


def canonical_key(announcement: Announcement) -> str:
    if announcement.source_id and announcement.source_id.strip():
        return f"{announcement.agency.value}:{announcement.source_id.strip()}"
    digest = hashlib.sha256(normalize_url(announcement.url).encode()).hexdigest()
    return f"{announcement.agency.value}:url:{digest}"
