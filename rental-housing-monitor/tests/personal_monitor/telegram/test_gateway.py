from __future__ import annotations

import asyncio
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from personal_monitor.control.actions import PendingActionService
from personal_monitor.storage import open_database
from personal_monitor.telegram import CallbackQuery, TelegramMessage, TelegramUpdate
from personal_monitor.telegram.gateway import ControlRequest, TelegramGateway

NOW = datetime(2026, 7, 23, tzinfo=UTC)
OWNER = "telegram-user:7"


class FakeApi:
    def __init__(self) -> None:
        self.answers: list[tuple[str, str | None, bool]] = []

    async def answer_callback(
        self, callback_query_id: str, *, text: str | None = None, show_alert: bool = False
    ) -> None:
        self.answers.append((callback_query_id, text, show_alert))


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.error: BaseException | None = None

    async def route(self, value: object) -> None:
        self.calls.append(value)
        if self.error is not None:
            raise self.error


def _connection(path: str | Path = ":memory:") -> sqlite3.Connection:
    connection = open_database(path)
    connection.execute(
        "INSERT INTO users(id, telegram_user_id, status, created_at) VALUES (?, 7, 'active', ?)",
        (OWNER, NOW.isoformat()),
    )
    return connection


@pytest.fixture
def gateway_parts() -> tuple[
    TelegramGateway,
    FakeRouter,
    FakeApi,
    PendingActionService,
    sqlite3.Connection,
]:
    connection = _connection()
    actions = PendingActionService(connection)
    router = FakeRouter()
    api = FakeApi()
    gateway = TelegramGateway(
        allowed_user_id=7,
        command_chat_id=42,
        router=router,
        actions=actions,
        api=api,
    )
    yield gateway, router, api, actions, connection
    connection.close()


def _message(*, user_id: int = 7, chat_id: int = 42, text: str = "모니터해줘") -> TelegramUpdate:
    return TelegramUpdate(1, TelegramMessage(31, chat_id, user_id, text), None)


def _callback(data: str, *, user_id: int = 7, chat_id: int = 42) -> TelegramUpdate:
    return TelegramUpdate(2, None, CallbackQuery("cb-1", user_id, chat_id, 31, data))


def run(value: object) -> object:
    return asyncio.run(value)  # type: ignore[arg-type]


def test_authorized_natural_language_routes_exactly_once_with_redacted_request(
    gateway_parts, caplog: pytest.LogCaptureFixture
) -> None:
    gateway, router, api, _, connection = gateway_parts
    text = "이 상품을 모니터해줘"

    with caplog.at_level(logging.INFO):
        run(gateway.handle_update(_message(text=text)))

    assert len(router.calls) == 1
    request = router.calls[0]
    assert request == ControlRequest(OWNER, "42", text)
    assert text not in repr(request)
    assert text not in caplog.text
    assert text not in "\n".join(connection.iterdump())
    assert api.answers == []
    with pytest.raises(FrozenInstanceError):
        request.text = "changed"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "update",
    [
        _message(user_id=999),
        _message(chat_id=999),
        _message(user_id=-7),
        _message(chat_id=-42),
        TelegramUpdate(3, None, None),
        _callback("confirm:" + "x" * 32, user_id=999),
        _callback("confirm:" + "x" * 32, chat_id=999),
    ],
)
def test_unauthorized_or_unsupported_updates_touch_no_downstream_boundary(
    gateway_parts, update: TelegramUpdate
) -> None:
    gateway, router, api, _, connection = gateway_parts

    run(gateway.handle_update(update))

    assert router.calls == []
    assert api.answers == []
    assert connection.execute("SELECT count(*) FROM pending_actions").fetchone()[0] == 0


def test_unauthorized_log_contains_only_bounded_user_shape(
    gateway_parts, caplog: pytest.LogCaptureFixture
) -> None:
    gateway, _, _, _, _ = gateway_parts
    text = "private-message"
    update = _message(user_id=999, chat_id=123456, text=text)

    with caplog.at_level(logging.WARNING):
        run(gateway.handle_update(update))

    assert "unauthorized_telegram_user_id=999" in caplog.text
    assert text not in caplog.text
    assert "123456" not in caplog.text
    assert "allowed" not in caplog.text


def test_confirm_consumes_before_routing_one_immutable_action(gateway_parts) -> None:
    gateway, router, api, actions, connection = gateway_parts
    pending = actions.create(OWNER, "create", {"version_id": "v1"}, now=NOW)

    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))

    assert len(router.calls) == 1
    action = router.calls[0]
    assert action.action == "create"
    assert dict(action.payload) == {"version_id": "v1"}
    assert pending.token not in repr(action)
    assert connection.execute("SELECT consumed_at FROM pending_actions").fetchone()[0] is not None
    assert api.answers == [("cb-1", "처리되었습니다", False)]


def test_cancel_consumes_without_routing(gateway_parts) -> None:
    gateway, router, api, actions, _ = gateway_parts
    pending = actions.create(OWNER, "delete", {"monitor_id": "m1"}, now=NOW)

    run(gateway.handle_update(_callback(pending.cancel_callback), now=NOW))
    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))

    assert router.calls == []
    assert api.answers == [("cb-1", "취소되었습니다", False)]


@pytest.mark.parametrize(
    "data",
    [
        "",
        "confirm:",
        "unknown:" + "x" * 32,
        "CONFIRM:" + "x" * 32,
        "confirm:" + "x" * 31,
        "confirm:" + "x" * 33,
        "confirm:" + "x" * 31 + "+",
        "confirm:" + "가" * 32,
        "confirm:" + "x" * 64,
        "confirm:" + "x" * 32 + ":extra",
    ],
)
def test_malformed_callbacks_fail_without_lookup_or_response(
    gateway_parts, data: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, router, api, actions, _ = gateway_parts

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("action lookup must not happen")

    monkeypatch.setattr(type(actions), "consume", forbidden)
    run(gateway.handle_update(_callback(data), now=NOW))

    assert router.calls == []
    assert api.answers == []


def test_unknown_valid_token_and_replay_produce_no_response_or_dispatch(gateway_parts) -> None:
    gateway, router, api, actions, _ = gateway_parts
    pending = actions.create(OWNER, "create", {}, now=NOW)

    run(gateway.handle_update(_callback("confirm:" + "x" * 32), now=NOW))
    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))
    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))

    assert len(router.calls) == 1
    assert api.answers == [("cb-1", "처리되었습니다", False)]


def test_callback_identity_is_checked_before_action_lookup(
    gateway_parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, router, api, actions, _ = gateway_parts

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("action lookup must not happen")

    monkeypatch.setattr(type(actions), "consume", forbidden)
    run(gateway.handle_update(_callback("confirm:" + "x" * 32, user_id=999), now=NOW))
    run(gateway.handle_update(_callback("confirm:" + "x" * 32, chat_id=999), now=NOW))

    assert router.calls == []
    assert api.answers == []


def test_router_failure_does_not_resurrect_consumed_action(gateway_parts) -> None:
    gateway, router, api, actions, _ = gateway_parts
    pending = actions.create(OWNER, "create", {}, now=NOW)
    router.error = RuntimeError("router-private-error")

    with pytest.raises(RuntimeError, match="router-private-error"):
        run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))
    router.error = None
    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))

    assert len(router.calls) == 1
    assert api.answers == []


def test_router_cancellation_is_preserved_and_action_stays_consumed(gateway_parts) -> None:
    gateway, router, api, actions, _ = gateway_parts
    pending = actions.create(OWNER, "create", {}, now=NOW)
    router.error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))
    router.error = None
    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))

    assert len(router.calls) == 1
    assert api.answers == []


def test_same_token_confirmation_race_dispatches_exactly_once(tmp_path: Path) -> None:
    path = tmp_path / "gateway-actions.db"
    setup_connection = _connection(path)
    pending = PendingActionService(setup_connection).create(
        OWNER, "delete", {"monitor_id": "m1"}, now=NOW
    )
    setup_connection.close()
    first_router, second_router = FakeRouter(), FakeRouter()
    first_api, second_api = FakeApi(), FakeApi()
    boundaries = ((first_router, first_api), (second_router, second_api))
    barrier = Barrier(2)

    def handle(boundary: tuple[FakeRouter, FakeApi]) -> None:
        connection = open_database(path)
        try:
            router, api = boundary
            gateway = TelegramGateway(7, 42, router, PendingActionService(connection), api)
            barrier.wait()
            run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(handle, boundaries))

    assert len(first_router.calls) + len(second_router.calls) == 1
    assert len(first_api.answers) + len(second_api.answers) == 1


@pytest.mark.parametrize(
    "values",
    [
        (True, 42),
        (7, True),
        (0, 42),
        (7, 0),
        (2**63, 42),
        (7, 2**63),
    ],
)
def test_gateway_rejects_invalid_exact_type_configuration(
    gateway_parts, values: tuple[object, object]
) -> None:
    _, router, api, actions, _ = gateway_parts

    with pytest.raises(ValueError, match="invalid Telegram gateway configuration"):
        TelegramGateway(values[0], values[1], router, actions, api)  # type: ignore[arg-type]


def test_gateway_composition_is_sealed_and_repr_redacted(gateway_parts) -> None:
    gateway, router, _, _, _ = gateway_parts

    assert repr(gateway) == "<TelegramGateway redacted>"
    with pytest.raises(AttributeError, match="sealed"):
        gateway.router = router  # type: ignore[attr-defined]


@pytest.mark.parametrize("text", ["", " \n", "bad\x00", "x" * 4097])
def test_direct_invalid_message_text_is_dropped(gateway_parts, text: str) -> None:
    gateway, router, api, _, _ = gateway_parts

    run(gateway.handle_update(_message(text=text)))

    assert router.calls == []
    assert api.answers == []
