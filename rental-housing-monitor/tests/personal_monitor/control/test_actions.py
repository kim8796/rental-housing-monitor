from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

import personal_monitor.control.actions as actions_module
from personal_monitor.control.actions import (
    ActionDenied,
    ConsumedAction,
    PendingActionService,
)
from personal_monitor.storage import open_database

NOW = datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
OWNER = "telegram-user:7"


def _database(path: str | Path = ":memory:") -> sqlite3.Connection:
    connection = open_database(path)
    connection.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) VALUES (?, 7, 'active', ?)",
        (OWNER, NOW.isoformat()),
    )
    connection.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) "
        "VALUES ('telegram-user:8', 8, 'active', ?)",
        (NOW.isoformat(),),
    )
    return connection


@pytest.fixture
def connection() -> sqlite3.Connection:
    value = _database()
    yield value
    value.close()


@pytest.fixture
def actions(connection: sqlite3.Connection) -> PendingActionService:
    return PendingActionService(connection)


def test_create_stores_only_hash_canonical_payload_and_exact_expiry(
    actions: PendingActionService, connection: sqlite3.Connection
) -> None:
    pending = actions.create(OWNER, "create", {"version_id": "v1", "count": 1}, now=NOW)

    assert len(pending.token) == 32
    assert pending.token.isascii()
    assert all(character.isalnum() or character in "-_" for character in pending.token)
    assert pending.confirm_callback == f"confirm:{pending.token}"
    assert pending.cancel_callback == f"cancel:{pending.token}"
    assert len(pending.confirm_callback.encode()) < 64
    assert pending.expires_at == NOW + timedelta(minutes=10)
    row = connection.execute("SELECT * FROM pending_actions").fetchone()
    assert row["token_hash"] == hashlib.sha256(pending.token.encode()).hexdigest()
    assert row["payload_json"] == '{"count":1,"version_id":"v1"}'
    assert row["expires_at"] == "2026-07-23T00:10:00+00:00"
    assert pending.token not in "".join(str(value) for value in row)
    assert pending.token not in repr(pending)
    assert "v1" not in repr(pending)


def test_confirmation_is_requester_bound_single_use_and_immutable(
    actions: PendingActionService,
) -> None:
    pending = actions.create(OWNER, "create", {"version_id": "v1", "nested": [1]}, now=NOW)

    with pytest.raises(ActionDenied, match="pending action denied") as wrong_owner:
        actions.consume(pending.token, "telegram-user:8", now=NOW)
    result = actions.consume(pending.token, OWNER, now=NOW)

    assert result.action == "create"
    assert dict(result.payload) == {"nested": (1,), "version_id": "v1"}
    assert pending.token not in repr(result)
    assert "v1" not in repr(result)
    assert wrong_owner.value.__cause__ is None
    with pytest.raises(TypeError):
        result.payload["new"] = "value"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.action = "delete"  # type: ignore[misc]
    with pytest.raises(ActionDenied, match="pending action denied"):
        actions.consume(pending.token, OWNER, now=NOW)


def test_public_action_cannot_be_registered_through_module_or_instance_state(
    actions: PendingActionService,
) -> None:
    forged = ConsumedAction("delete", {"monitor_id": "m1"}, OWNER)

    assert not hasattr(actions_module, "_register_consumed_action")
    assert not hasattr(actions_module, "_claim_consumed_action")
    assert not hasattr(actions_module, "_discard_consumed_action")
    assert not hasattr(actions_module, "_make_consumed_action_registry")
    service_slots = {
        slot
        for service_type in PendingActionService.__mro__
        for slot in getattr(service_type, "__slots__", ())
    }
    assert service_slots == {
        "_connection",
        "_connection_anchor",
        "_token_source",
        "_token_source_anchor",
    }
    assert set(ConsumedAction.__slots__) == {
        "action",
        "payload",
        "owner_id",
        "operation",
    }
    assert actions.claim(forged) is False
    assert actions.discard(forged) is False


def test_wrong_owner_does_not_consume_valid_action(actions: PendingActionService) -> None:
    pending = actions.create(OWNER, "delete", {"monitor_id": "m1"}, now=NOW)

    with pytest.raises(ActionDenied):
        actions.consume(pending.token, "telegram-user:8", now=NOW)

    assert actions.consume(pending.token, OWNER, now=NOW).action == "delete"


@pytest.mark.parametrize(
    "token",
    ["", "short", "x" * 31, "x" * 33, "가" * 32, "bad+token" + "x" * 23, True, None],
)
def test_malformed_and_unknown_tokens_fail_uniformly_without_sql_details(
    actions: PendingActionService, token: object
) -> None:
    with pytest.raises(ActionDenied) as caught:
        actions.consume(token, OWNER, now=NOW)  # type: ignore[arg-type]

    assert str(caught.value) == "pending action denied"
    assert repr(caught.value) == "ActionDenied(<redacted>)"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_expired_action_fails_and_is_not_returned(actions: PendingActionService) -> None:
    pending = actions.create(OWNER, "update", {"version_id": "v2"}, now=NOW)

    with pytest.raises(ActionDenied):
        actions.consume(pending.token, OWNER, now=NOW + timedelta(minutes=10))


def test_aware_non_utc_times_normalize_consistently(actions: PendingActionService) -> None:
    korea = datetime(2026, 7, 23, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    pending = actions.create(OWNER, "pause", {"monitor_id": "m1"}, now=korea)

    consumed = actions.consume(
        pending.token,
        OWNER,
        now=datetime(2026, 7, 23, 9, 9, tzinfo=timezone(timedelta(hours=9))),
    )

    assert pending.expires_at == datetime(2026, 7, 23, 0, 10, tzinfo=UTC)
    assert consumed.action == "pause"


def test_naive_times_fail_before_writes(actions: PendingActionService) -> None:
    with pytest.raises(ValueError, match="invalid pending action"):
        actions.create(OWNER, "create", {}, now=datetime(2026, 7, 23))
    assert actions.connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0

    pending = actions.create(OWNER, "create", {}, now=NOW)
    with pytest.raises(ActionDenied):
        actions.consume(pending.token, OWNER, now=datetime(2026, 7, 23))


def test_unrepresentable_expiry_and_broken_timezone_fail_without_writes(
    actions: PendingActionService,
) -> None:
    class BrokenTimezone(tzinfo):
        def utcoffset(self, value: datetime | None) -> Any:
            raise RuntimeError("private-time-secret")

        def dst(self, value: datetime | None) -> Any:
            return None

        def tzname(self, value: datetime | None) -> str:
            return "broken"

    bad_time = datetime(2026, 7, 23, tzinfo=BrokenTimezone())
    for now in (datetime.max.replace(tzinfo=UTC), bad_time):
        with pytest.raises(ValueError, match="invalid pending action") as caught:
            actions.create(OWNER, "create", {}, now=now)
        assert "private-time-secret" not in str(caught.value)

    assert actions.connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0


def test_hostile_mapping_fails_with_fixed_error_before_writes(
    actions: PendingActionService,
) -> None:
    class HostileMapping(dict[str, object]):
        def items(self):  # type: ignore[no-untyped-def, override]
            raise RuntimeError("private-payload-secret")

    with pytest.raises(ValueError, match="invalid pending action") as caught:
        actions.create(OWNER, "create", HostileMapping(), now=NOW)

    assert "private-payload-secret" not in str(caught.value)
    assert actions.connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "owner,action,payload",
    [
        ("", "create", {}),
        ("telegram-user:0", "create", {}),
        (OWNER, "unknown", {}),
        (OWNER, "create", []),
        (OWNER, "create", {"x": float("nan")}),
        (OWNER, "create", {"x": "v" * 4097}),
        (OWNER, "create", {"x": [[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]}),
        (OWNER, "create", {"x" * 129: "v"}),
    ],
)
def test_create_bounds_owner_action_and_payload_without_writes(
    actions: PendingActionService,
    owner: object,
    action: object,
    payload: object,
) -> None:
    with pytest.raises(ValueError, match="invalid pending action") as caught:
        actions.create(owner, action, payload, now=NOW)  # type: ignore[arg-type]

    if owner:
        assert str(owner) not in str(caught.value)
    assert actions.connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "column,value",
    [
        ("expires_at", "not-a-time"),
        ("expires_at", "2026-07-23T00:10:00"),
        ("payload_json", "not-json"),
        ("payload_json", "[]"),
        ("payload_json", '{"x":NaN}'),
        ("action", "unknown"),
    ],
)
def test_corrupt_persisted_values_fail_closed(
    actions: PendingActionService,
    connection: sqlite3.Connection,
    column: str,
    value: str,
) -> None:
    pending = actions.create(OWNER, "create", {"version_id": "v1"}, now=NOW)
    connection.execute(f"UPDATE pending_actions SET {column} = ?", (value,))

    with pytest.raises(ActionDenied) as caught:
        actions.consume(pending.token, OWNER, now=NOW)

    assert str(caught.value) == "pending action denied"
    assert connection.execute("SELECT consumed_at FROM pending_actions").fetchone()[0] is None


def test_failed_state_transition_rolls_back_consumption(
    actions: PendingActionService, connection: sqlite3.Connection
) -> None:
    pending = actions.create(OWNER, "create", {"version_id": "v1"}, now=NOW)
    connection.execute(
        "CREATE TRIGGER reject_action_consume BEFORE UPDATE OF consumed_at ON pending_actions "
        "BEGIN SELECT RAISE(ABORT, 'private-trigger-secret'); END"
    )

    with pytest.raises(ActionDenied) as caught:
        actions.consume(pending.token, OWNER, now=NOW)

    assert "private-trigger-secret" not in str(caught.value)
    assert connection.execute("SELECT consumed_at FROM pending_actions").fetchone()[0] is None


def test_create_inside_caller_transaction_does_not_commit_it(
    actions: PendingActionService, connection: sqlite3.Connection
) -> None:
    connection.execute("BEGIN")
    pending = actions.create(OWNER, "create", {}, now=NOW)

    assert connection.in_transaction
    connection.rollback()
    assert (
        connection.execute(
            "SELECT 1 FROM pending_actions WHERE token_hash = ?",
            (hashlib.sha256(pending.token.encode()).hexdigest(),),
        ).fetchone()
        is None
    )


def test_consume_rejects_caller_transaction_without_committing_or_consuming(
    actions: PendingActionService, connection: sqlite3.Connection
) -> None:
    pending = actions.create(OWNER, "create", {}, now=NOW)
    connection.execute("BEGIN")
    connection.execute("UPDATE users SET status='paused' WHERE id=?", (OWNER,))

    with pytest.raises(ActionDenied):
        actions.consume(pending.token, OWNER, now=NOW)

    assert connection.in_transaction
    connection.rollback()
    assert actions.consume(pending.token, OWNER, now=NOW).action == "create"


def test_two_real_connections_racing_yield_exactly_one_success(tmp_path: Path) -> None:
    path = tmp_path / "actions.db"
    setup_connection = _database(path)
    pending = PendingActionService(setup_connection).create(
        OWNER, "delete", {"monitor_id": "m1"}, now=NOW
    )
    setup_connection.close()
    barrier = Barrier(2)

    def consume(_worker: int) -> str:
        connection = open_database(path)
        try:
            service = PendingActionService(connection)
            barrier.wait()
            try:
                return service.consume(pending.token, OWNER, now=NOW).action
            except ActionDenied:
                return "denied"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, (1, 2)))

    assert sorted(results) == ["delete", "denied"]


def test_service_composition_is_sealed_and_repr_is_redacted(
    actions: PendingActionService, connection: sqlite3.Connection
) -> None:
    assert actions.connection is connection
    assert repr(actions) == "<PendingActionService redacted>"
    with pytest.raises(AttributeError, match="sealed"):
        actions.connection = _database()  # type: ignore[misc]
    with pytest.raises(ValueError, match="invalid pending action storage"):
        PendingActionService(object())  # type: ignore[arg-type]


def test_service_anchors_cryptographic_randomness_source(
    actions: PendingActionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    attacker_token = "x" * 32
    monkeypatch.setattr(actions_module.secrets, "token_urlsafe", lambda _size: attacker_token)

    pending = actions.create(OWNER, "create", {}, now=NOW)

    assert pending.token != attacker_token


def test_consumed_action_cannot_be_constructed_with_mutable_or_invalid_values() -> None:
    with pytest.raises(ValueError, match="invalid consumed action"):
        ConsumedAction("unknown", {}, OWNER)
