from __future__ import annotations

import asyncio
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personal_monitor.control.actions import PendingActionService
from personal_monitor.control.messages import ControlReply
from personal_monitor.storage import open_database
from personal_monitor.telegram import CallbackQuery, InlineButton, TelegramMessage, TelegramUpdate
from personal_monitor.telegram.gateway import ControlRequest, TelegramGateway

NOW = datetime(2026, 7, 23, tzinfo=UTC)
OWNER = "telegram-user:7"


class FakeApi:
    def __init__(self) -> None:
        self.answers: list[tuple[str, str | None, bool]] = []
        self.messages: list[tuple[str, str, object]] = []

    async def answer_callback(
        self, callback_query_id: str, *, text: str | None = None, show_alert: bool = False
    ) -> None:
        self.answers.append((callback_query_id, text, show_alert))

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: object = None,
        disable_web_page_preview: bool = True,
    ) -> str:
        self.messages.append((str(chat_id), text, buttons))
        return "88"


class FakeRouter:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.error: BaseException | None = None

    async def route(self, value: object) -> None:
        self.calls.append(value)
        if self.error is not None:
            raise self.error


class ReplyRouter(FakeRouter):
    async def route(self, value: object) -> ControlReply:
        self.calls.append(value)
        return ControlReply(
            "안전한 처리 결과",
            ((InlineButton("확인", "confirm:" + "z" * 32),),),
        )


class CountingProtocolBoundary:
    def __init__(self, attribute: str) -> None:
        self.attribute = attribute
        self.accesses = 0
        self.call = AsyncMock()
        self.send_call = AsyncMock(return_value="88")

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        if name == "send_message":
            return self.send_call
        if name != self.attribute:
            raise AttributeError(name)
        self.accesses += 1
        return self.call


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


def _callback(
    data: object,
    *,
    user_id: object = 7,
    chat_id: object = 42,
    callback_id: object = "cb-1",
    message_id: object = 31,
) -> TelegramUpdate:
    return TelegramUpdate(
        2,
        None,
        CallbackQuery(
            callback_id,  # type: ignore[arg-type]
            user_id,  # type: ignore[arg-type]
            chat_id,  # type: ignore[arg-type]
            message_id,  # type: ignore[arg-type]
            data,  # type: ignore[arg-type]
        ),
    )


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


def test_gateway_sends_validated_reply_for_messages_and_confirmed_callbacks() -> None:
    connection = _connection()
    actions = PendingActionService(connection)
    router = ReplyRouter()
    api = FakeApi()
    gateway = TelegramGateway(7, 42, router, actions, api)

    run(gateway.handle_update(_message(), now=NOW))
    pending = actions.create(OWNER, "delete", {"owner_id": OWNER}, now=NOW)
    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))

    assert [message[:2] for message in api.messages] == [
        ("42", "안전한 처리 결과"),
        ("42", "안전한 처리 결과"),
    ]
    assert api.answers == [("cb-1", "처리되었습니다", False)]
    connection.close()


def test_gateway_requires_send_message_at_construction() -> None:
    connection = _connection()
    actions = PendingActionService(connection)
    api = SimpleNamespace(answer_callback=AsyncMock())

    with pytest.raises(ValueError, match="invalid Telegram gateway configuration"):
        TelegramGateway(7, 42, FakeRouter(), actions, api)

    connection.close()


def test_confirm_delivery_failure_gets_one_truthful_alert_after_consumption() -> None:
    class FailingSendApi(FakeApi):
        async def send_message(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("delivery-secret")

    connection = _connection()
    actions = PendingActionService(connection)
    api = FailingSendApi()
    gateway = TelegramGateway(7, 42, ReplyRouter(), actions, api)
    pending = actions.create(OWNER, "delete", {"owner_id": OWNER}, now=NOW)

    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))

    assert api.answers == [("cb-1", "결과 전달에 실패했습니다. 모니터 상태를 확인해 주세요.", True)]
    assert connection.execute("SELECT consumed_at FROM pending_actions").fetchone()[0] is not None
    connection.close()


def test_edit_callback_is_requester_bound_single_use_and_dispatched_without_confirmation() -> None:
    connection = _connection()
    actions = PendingActionService(connection)
    router = ReplyRouter()
    api = FakeApi()
    gateway = TelegramGateway(7, 42, router, actions, api)
    pending = actions.create(OWNER, "update", {"owner_id": OWNER}, now=NOW)

    run(gateway.handle_update(_callback(f"edit:{pending.token}"), now=NOW))
    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))

    assert len(router.calls) == 1
    assert router.calls[0].owner_id == OWNER
    assert router.calls[0].operation == "edit"
    assert len(api.messages) == 1
    assert api.answers == [("cb-1", "수정 안내를 보냈습니다", False)]
    connection.close()


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
def test_malformed_callbacks_fail_without_lookup_or_response(gateway_parts, data: str) -> None:
    gateway, router, api, _, connection = gateway_parts
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    run(gateway.handle_update(_callback(data), now=NOW))
    connection.set_trace_callback(None)

    assert router.calls == []
    assert api.answers == []
    assert not any("pending_actions" in statement for statement in statements)


def test_huge_callback_data_is_rejected_before_lookup_or_response(gateway_parts) -> None:
    gateway, router, api, _, connection = gateway_parts
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    run(gateway.handle_update(_callback("x" * 1_000_000), now=NOW))
    connection.set_trace_callback(None)

    assert router.calls == []
    assert api.answers == []
    assert not any("pending_actions" in statement for statement in statements)


def test_unknown_valid_token_and_replay_produce_no_response_or_dispatch(gateway_parts) -> None:
    gateway, router, api, actions, _ = gateway_parts
    pending = actions.create(OWNER, "create", {}, now=NOW)

    run(gateway.handle_update(_callback("confirm:" + "x" * 32), now=NOW))
    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))
    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))

    assert len(router.calls) == 1
    assert api.answers == [("cb-1", "처리되었습니다", False)]


def test_callback_identity_is_checked_before_action_lookup(gateway_parts) -> None:
    gateway, router, api, _, connection = gateway_parts
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    run(gateway.handle_update(_callback("confirm:" + "x" * 32, user_id=999), now=NOW))
    run(gateway.handle_update(_callback("confirm:" + "x" * 32, chat_id=999), now=NOW))
    connection.set_trace_callback(None)

    assert router.calls == []
    assert api.answers == []
    assert not any("pending_actions" in statement for statement in statements)


@pytest.mark.parametrize("operation", ["confirm", "cancel"])
@pytest.mark.parametrize(
    ("callback_id", "message_id"),
    [
        ("", 31),
        ("x" * 257, 31),
        ("bad\x00id", 31),
        ("bad\ud800id", 31),
        (4, 31),
        ("cb-1", True),
        ("cb-1", 0),
        ("cb-1", -1),
        ("cb-1", 2**63),
    ],
)
def test_entire_callback_boundary_is_validated_before_consumption(
    gateway_parts,
    operation: str,
    callback_id: object,
    message_id: object,
) -> None:
    gateway, router, api, actions, connection = gateway_parts
    pending = actions.create(OWNER, "create", {"version_id": "v1"}, now=NOW)
    invalid_data = pending.confirm_callback if operation == "confirm" else pending.cancel_callback

    run(
        gateway.handle_update(
            _callback(invalid_data, callback_id=callback_id, message_id=message_id),
            now=NOW,
        )
    )

    assert connection.execute("SELECT consumed_at FROM pending_actions").fetchone()[0] is None
    assert router.calls == []
    assert api.answers == []

    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))
    assert len(router.calls) == 1
    assert api.answers == [("cb-1", "처리되었습니다", False)]


def test_router_failure_does_not_resurrect_consumed_action(gateway_parts) -> None:
    gateway, router, api, actions, _ = gateway_parts
    pending = actions.create(OWNER, "create", {}, now=NOW)
    router.error = RuntimeError("router-private-error")

    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))
    router.error = None
    run(gateway.handle_update(_callback(pending.confirm_callback), now=NOW))

    assert len(router.calls) == 1
    assert api.answers == [("cb-1", "결과 전달에 실패했습니다. 모니터 상태를 확인해 주세요.", True)]


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


def test_instance_level_async_callables_are_supported(
    gateway_parts,
) -> None:
    _, _, _, actions, _ = gateway_parts
    route = AsyncMock(return_value=None)
    answer = AsyncMock()
    send = AsyncMock(return_value="88")
    router = SimpleNamespace(route=route)
    api = SimpleNamespace(answer_callback=answer, send_message=send)
    gateway = TelegramGateway(7, 42, router, actions, api)

    run(gateway.handle_update(_message()))
    pending = actions.create(OWNER, "delete", {"monitor_id": "m1"}, now=NOW)
    run(gateway.handle_update(_callback(pending.cancel_callback), now=NOW))

    assert route.await_count == 1
    assert route.await_args.args == (ControlRequest(OWNER, "42", "모니터해줘"),)
    answer.assert_awaited_once_with("cb-1", text="취소되었습니다", show_alert=False)


@pytest.mark.parametrize("boundary", ["router", "api"])
def test_hostile_callable_descriptors_raise_one_fixed_constructor_error(
    gateway_parts,
    boundary: str,
) -> None:
    _, router, api, actions, _ = gateway_parts

    class HostileRouter:
        @property
        def route(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("private-router-descriptor-secret")

    class HostileApi:
        @property
        def answer_callback(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("private-api-descriptor-secret")

    selected_router = HostileRouter() if boundary == "router" else router
    selected_api = HostileApi() if boundary == "api" else api

    with pytest.raises(ValueError) as caught:
        TelegramGateway(7, 42, selected_router, actions, selected_api)

    assert str(caught.value) == "invalid Telegram gateway configuration"
    assert "private" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_constructor_retrieves_each_protocol_callable_exactly_once(gateway_parts) -> None:
    _, _, _, actions, _ = gateway_parts

    class CountingRouter:
        def __init__(self) -> None:
            self.accesses = 0
            self.call = AsyncMock()

        @property
        def route(self):  # type: ignore[no-untyped-def]
            self.accesses += 1
            return self.call

    class CountingApi:
        def __init__(self) -> None:
            self.accesses = 0
            self.call = AsyncMock()
            self.send_message = AsyncMock(return_value="88")

        @property
        def answer_callback(self):  # type: ignore[no-untyped-def]
            self.accesses += 1
            return self.call

    router = CountingRouter()
    api = CountingApi()

    TelegramGateway(7, 42, router, actions, api)

    assert router.accesses == 1
    assert api.accesses == 1


def test_unauthorized_and_unsupported_updates_do_not_retrieve_protocol_callables(
    gateway_parts,
) -> None:
    _, _, _, actions, _ = gateway_parts
    router = CountingProtocolBoundary("route")
    api = CountingProtocolBoundary("answer_callback")
    gateway = TelegramGateway(7, 42, router, actions, api)
    assert (router.accesses, api.accesses) == (1, 1)

    run(gateway.handle_update(_message(user_id=999)))
    run(gateway.handle_update(TelegramUpdate(3, None, None)))

    assert (router.accesses, api.accesses) == (1, 1)
    assert router.call.await_count == 0
    assert api.call.await_count == 0


@pytest.mark.parametrize("field", ["_allowed_user_id", "_command_chat_id"])
@pytest.mark.parametrize(
    "mutation",
    [
        "hostile_current",
        "hostile_anchor",
        "wrong_type_current",
        "wrong_type_anchor",
        "replace_current",
        "replace_anchor",
        "delete_current",
        "delete_anchor",
    ],
)
def test_corrupt_config_drops_all_updates_before_authorization_or_protocol_access(
    gateway_parts,
    caplog: pytest.LogCaptureFixture,
    field: str,
    mutation: str,
) -> None:
    _, _, _, actions, connection = gateway_parts
    router = CountingProtocolBoundary("route")
    api = CountingProtocolBoundary("answer_callback")
    gateway = TelegramGateway(7, 42, router, actions, api)
    assert (router.accesses, api.accesses) == (1, 1)

    class HostileEquality:
        def __eq__(self, _other: object) -> bool:
            raise RuntimeError("private-config-equality-secret")

    anchor_field = f"{field}_anchor"
    target = anchor_field if mutation.endswith("anchor") else field
    if mutation.startswith("hostile"):
        object.__setattr__(gateway, target, HostileEquality())
    elif mutation.startswith("wrong_type"):
        object.__setattr__(gateway, target, True)
    elif mutation.startswith("replace"):
        object.__setattr__(gateway, target, 8)
    else:
        object.__delattr__(gateway, target)

    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    with caplog.at_level(logging.WARNING):
        for update in (
            _message(user_id=7),
            _message(user_id=999),
            _callback("confirm:" + "x" * 32, user_id=7),
            _callback("confirm:" + "x" * 32, user_id=999),
        ):
            run(gateway.handle_update(update, now=NOW))
    connection.set_trace_callback(None)

    assert (router.accesses, api.accesses) == (1, 1)
    assert router.call.await_count == 0
    assert api.call.await_count == 0
    assert not any("pending_actions" in statement for statement in statements)
    assert caplog.text == ""


@pytest.mark.parametrize("boundary", ["router", "api"])
def test_changing_callable_descriptor_fails_closed_on_use(gateway_parts, boundary: str) -> None:
    _, stable_router, stable_api, actions, _ = gateway_parts

    class ChangingRouter:
        def __init__(self) -> None:
            self.calls: list[object] = []

        @property
        def route(self):  # type: ignore[no-untyped-def]
            async def changing(value: object) -> None:
                self.calls.append(value)

            return changing

    class ChangingApi:
        def __init__(self) -> None:
            self.calls: list[object] = []

        async def send_message(self, *_args: object, **_kwargs: object) -> str:
            return "88"

        @property
        def answer_callback(self):  # type: ignore[no-untyped-def]
            async def changing(*args: object, **kwargs: object) -> None:
                self.calls.append((args, kwargs))

            return changing

    changing_router = ChangingRouter()
    changing_api = ChangingApi()
    router = changing_router if boundary == "router" else stable_router
    api = changing_api if boundary == "api" else stable_api
    gateway = TelegramGateway(7, 42, router, actions, api)

    run(gateway.handle_update(_message()))

    assert changing_router.calls == []
    assert changing_api.calls == []
    assert stable_router.calls == []
    assert stable_api.answers == []


@pytest.mark.parametrize("text", ["", " \n", "bad\x00", "x" * 4097])
def test_direct_invalid_message_text_is_dropped(gateway_parts, text: str) -> None:
    gateway, router, api, _, _ = gateway_parts

    run(gateway.handle_update(_message(text=text)))

    assert router.calls == []
    assert api.answers == []
