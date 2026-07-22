from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

type Scalar = str | int | float | bool | None

_TRACKING_QUERY_KEYS = frozenset(
    {"_ga", "_gl", "dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid"}
)


def _immutable_mapping(values: Mapping[str, Scalar]) -> Mapping[str, Scalar]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class ObservedItem:
    item_id: str = field(repr=False)
    fields: Mapping[str, Scalar] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", _immutable_mapping(self.fields))


@dataclass(frozen=True, slots=True)
class SourceWarning:
    source: str
    stage: str
    detail: str


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    monitor_id: str
    items: tuple[ObservedItem, ...]
    observed_at: datetime
    source_hash: str
    source_status: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[SourceWarning, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "source_status", MappingProxyType(dict(self.source_status)))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True, slots=True)
class Change:
    item_id: str = field(repr=False)
    is_new: bool
    removed: bool
    changed_fields: Mapping[str, tuple[Scalar, Scalar]] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_fields", MappingProxyType(dict(self.changed_fields)))


def content_hash(value: object) -> str:
    """Return a SHA-256 hash of canonical JSON data."""
    canonical = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def stable_item_id(fields: Mapping[str, Scalar]) -> str:
    """Choose a deterministic identity from source data, URL, or item fields."""
    source_id = fields.get("source_id")
    if source_id not in (None, ""):
        return f"source:{source_id}"

    url = fields.get("url")
    if isinstance(url, str) and url:
        return sha256(_normalized_url(url).encode("utf-8")).hexdigest()

    core_fields = {key: value for key, value in fields.items() if key not in {"source_id", "url"}}
    return content_hash(core_fields)


def diff_items(previous: Sequence[ObservedItem], current: Sequence[ObservedItem]) -> list[Change]:
    """Compare item snapshots and return new, removed, and changed items in stable order."""
    previous_by_id = {item.item_id: item for item in previous}
    current_by_id = {item.item_id: item for item in current}
    changes: list[Change] = []

    for item_id in sorted(previous_by_id.keys() | current_by_id.keys()):
        old_item = previous_by_id.get(item_id)
        new_item = current_by_id.get(item_id)
        if old_item is None:
            assert new_item is not None
            changes.append(
                Change(
                    item_id=item_id,
                    is_new=True,
                    removed=False,
                    changed_fields={
                        key: (None, value) for key, value in sorted(new_item.fields.items())
                    },
                )
            )
        elif new_item is None:
            changes.append(
                Change(
                    item_id=item_id,
                    is_new=False,
                    removed=True,
                    changed_fields={
                        key: (value, None) for key, value in sorted(old_item.fields.items())
                    },
                )
            )
        else:
            changed_fields = {
                key: (old_item.fields.get(key), new_item.fields.get(key))
                for key in sorted(old_item.fields.keys() | new_item.fields.keys())
                if old_item.fields.get(key) != new_item.fields.get(key)
            }
            if changed_fields:
                changes.append(
                    Change(
                        item_id=item_id,
                        is_new=False,
                        removed=False,
                        changed_fields=changed_fields,
                    )
                )

    return changes


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _normalized_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return url

    netloc = host.casefold()
    try:
        port = parts.port
    except ValueError:
        return url
    if port is not None and not (
        (parts.scheme.casefold() == "http" and port == 80)
        or (parts.scheme.casefold() == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_query_key(key)
    ]
    return urlunsplit(
        (
            parts.scheme.casefold(),
            netloc,
            parts.path or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def _is_tracking_query_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized.startswith("utm_") or normalized in _TRACKING_QUERY_KEYS
