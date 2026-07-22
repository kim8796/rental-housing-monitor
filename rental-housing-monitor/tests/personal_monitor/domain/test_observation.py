from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import pytest

from personal_monitor.domain.observation import (
    Change,
    ObservationBatch,
    ObservedItem,
    SourceWarning,
    content_hash,
    diff_items,
    stable_item_id,
)


def test_item_id_prefers_source_id_then_normalized_url() -> None:
    assert stable_item_id({"source_id": "A-7", "url": "https://example.com/a"}) == "source:A-7"
    assert stable_item_id({"url": "https://EXAMPLE.com/a?utm_source=x"}) == stable_item_id(
        {"url": "https://example.com/a"}
    )


def test_item_id_falls_back_to_canonical_core_fields() -> None:
    fields = {"title": "Home", "price": 99_000}
    expected = sha256(b'{"price":99000,"title":"Home"}').hexdigest()
    assert stable_item_id(fields) == expected


def test_content_hash_uses_compact_sorted_utf8_json() -> None:
    assert content_hash({"title": "서울", "price": 99_000}) == sha256(
        '{"price":99000,"title":"서울"}'.encode()
    ).hexdigest()


def test_diff_reports_changed_fields_in_item_id_order() -> None:
    previous = [
        ObservedItem(item_id="p2", fields={"status": "available"}),
        ObservedItem(item_id="p1", fields={"price": 120_000}),
    ]
    current = [
        ObservedItem(item_id="p3", fields={"title": "New home"}),
        ObservedItem(item_id="p1", fields={"price": 99_000}),
    ]

    changes = diff_items(previous, current)

    assert [(change.item_id, change.is_new, change.removed) for change in changes] == [
        ("p1", False, False),
        ("p2", False, True),
        ("p3", True, False),
    ]
    assert changes[0].changed_fields == {"price": (120_000, 99_000)}
    assert changes[1].changed_fields == {"status": ("available", None)}
    assert changes[2].changed_fields == {"title": (None, "New home")}


def test_observation_value_mappings_are_defensively_immutable() -> None:
    fields = {"price": 99_000}
    statuses = {"source-a": "ok"}
    changed_fields = {"price": (120_000, 99_000)}
    item = ObservedItem(item_id="p1", fields=fields)
    batch = ObservationBatch(
        monitor_id="monitor-1",
        items=(item,),
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
        source_hash="source-hash",
        source_status=statuses,
        warnings=(SourceWarning(source="source-a", stage="fetch", detail="slow"),),
    )
    change = Change(item_id="p1", is_new=False, removed=False, changed_fields=changed_fields)

    fields["price"] = 1
    statuses["source-a"] = "failed"
    changed_fields["price"] = (1, 2)

    assert item.fields == {"price": 99_000}
    assert batch.source_status == {"source-a": "ok"}
    assert change.changed_fields == {"price": (120_000, 99_000)}
    with pytest.raises(TypeError):
        item.fields["price"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        batch.source_status["source-a"] = "failed"  # type: ignore[index]
    with pytest.raises(TypeError):
        change.changed_fields["price"] = (1, 2)  # type: ignore[index]
