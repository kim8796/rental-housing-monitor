from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime

import pytest

from personal_monitor.billing import (
    BillingAggregate,
    BillingRepository,
    CreditGrant,
    ProjectSpend,
)
from personal_monitor.storage import open_database

NOW = datetime(2026, 7, 24, 3, 10, tzinfo=UTC)


def grant() -> CreditGrant:
    return CreditGrant(
        id="free-trial",
        name="Free Trial",
        original_micros=460_418_000_000,
        baseline_remaining_micros=455_463_260_000,
        starts_on=date(2026, 7, 8),
        ends_on=date(2026, 10, 8),
        baseline_as_of=NOW,
    )


def aggregate(*, consumed: int, recent: int = 0) -> BillingAggregate:
    return BillingAggregate(
        observed_at=NOW,
        promotion_consumed_micros=consumed,
        recent_7d_consumed_micros=recent,
        projects=(
            ProjectSpend("project-a", "주 프로젝트", 4_000_000_000),
            ProjectSpend("project-b", "보조 프로젝트", 500_000_000),
        ),
    )


def test_register_grant_persists_console_baseline_as_initial_snapshot() -> None:
    connection = open_database(":memory:")
    repository = BillingRepository(connection)

    snapshot = repository.register_grant(grant())

    assert snapshot.remaining_micros == 455_463_260_000
    assert snapshot.used_micros == 4_954_740_000
    assert snapshot.remaining_basis_points == 9_892
    assert snapshot.source == "console"
    assert snapshot.projects == ()
    assert repository.latest_snapshot("free-trial") == snapshot
    assert "455463260000" not in repr(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.remaining_micros = 0  # type: ignore[misc]


def test_first_export_sync_calibrates_then_later_sync_applies_only_increment() -> None:
    connection = open_database(":memory:")
    repository = BillingRepository(connection)
    repository.register_grant(grant())

    first = repository.record_aggregate("free-trial", aggregate(consumed=4_960_000_000))
    second = repository.record_aggregate(
        "free-trial",
        aggregate(consumed=5_960_000_000, recent=1_400_000_000),
    )

    assert first.remaining_micros == 455_463_260_000
    assert second.remaining_micros == 454_463_260_000
    assert second.used_micros == 5_954_740_000
    assert second.daily_burn_micros == 200_000_000
    assert second.projected_exhaustion_on == date(2032, 10, 13)
    assert [(item.project_id, item.cost_micros) for item in second.projects] == [
        ("project-a", 4_000_000_000),
        ("project-b", 500_000_000),
    ]


def test_alert_claim_is_atomic_and_deduplicated_per_grant_and_key() -> None:
    connection = open_database(":memory:")
    repository = BillingRepository(connection)
    repository.register_grant(grant())

    assert repository.claim_alert("free-trial", "remaining:30", now=NOW)
    assert not repository.claim_alert("free-trial", "remaining:30", now=NOW)
    assert repository.claim_alert("free-trial", "expiry:30", now=NOW)
    assert connection.execute("SELECT count(*) FROM billing_alerts").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("original", "remaining", "starts_on", "ends_on"),
    (
        (0, 1, date(2026, 7, 8), date(2026, 10, 8)),
        (100, 101, date(2026, 7, 8), date(2026, 10, 8)),
        (100, 99, date(2026, 10, 8), date(2026, 7, 8)),
    ),
)
def test_invalid_credit_grants_are_rejected_before_storage(
    original: int,
    remaining: int,
    starts_on: date,
    ends_on: date,
) -> None:
    connection = open_database(":memory:")

    with pytest.raises(ValueError):
        CreditGrant(
            "free-trial",
            "Free Trial",
            original,
            remaining,
            starts_on,
            ends_on,
            NOW,
        )

    assert connection.execute("SELECT count(*) FROM billing_credit_grants").fetchone()[0] == 0
