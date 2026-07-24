from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from personal_monitor.ai.contracts import (
    IntentKind,
    IntentRequest,
    IntentResult,
    PlanRequest,
    PlanResult,
)
from personal_monitor.control.actions import PendingActionService
from personal_monitor.control.intents import IntentRouter, OwnedMonitorSummary
from personal_monitor.control.planner import MonitorPlanner, ProbeResult
from personal_monitor.control.service import ControlService
from personal_monitor.domain.observation import ObservationBatch, ObservedItem
from personal_monitor.domain.spec import FetchStrategy, MonitorSpec
from personal_monitor.engine.outbox import OutboxWorker
from personal_monitor.engine.runner import MonitorRunner
from personal_monitor.scraping.document import SourceDocument
from personal_monitor.security.robots import RobotsDecision
from personal_monitor.security.url_policy import ResolvedTarget
from personal_monitor.storage import RegistryRepository, RuntimeRepository, open_database
from personal_monitor.telegram.gateway import TelegramGateway
from personal_monitor.telegram.types import CallbackQuery, TelegramMessage, TelegramUpdate
from tests.personal_monitor.control.test_planner import IdSource

OWNER_ID = "telegram-user:7"
USER_ID = 7
CHAT_ID = 777
TARGET_URL = "https://example.com/products"
NOW = datetime(2026, 7, 23, 3, 0, tzinfo=UTC)
DELIVERY_NOW = datetime(2099, 1, 1, tzinfo=UTC)
MESSAGE = f"{TARGET_URL} 가격이 10만원 아래면 알려줘"
HTML = b"<main class='listing'><h1>Safe keyboard</h1><span class='price'>88,000</span></main>"


def planned_spec() -> MonitorSpec:
    return MonitorSpec.model_validate(
        {
            "schema_version": 1,
            "owner_id": OWNER_ID,
            "name": "키보드 가격",
            "target_url": TARGET_URL,
            "source_adapter": "scrapling",
            "adapter_ref": None,
            "fetch_strategy": "auto",
            "schedule": "0 */6 * * *",
            "timezone": "Asia/Seoul",
            "extract": {
                "item_scope": "main.listing",
                "fields": {
                    "title": {"selector": "h1", "type": "text"},
                    "price": {"selector": ".price", "type": "krw"},
                },
            },
            "validators": {"min_items": 1, "max_items": 3},
            "rules": [
                {
                    "kind": "numeric_threshold",
                    "field": "price",
                    "operator": "lt",
                    "value": 100000,
                }
            ],
            "notify_on_no_change": False,
            "auth_profile_ref": None,
        }
    )


class MonitorProvider:
    def __init__(self, registry: RegistryRepository) -> None:
        self.registry = registry

    def list_monitors(self, owner_id: str) -> tuple[OwnedMonitorSummary, ...]:
        return tuple(
            OwnedMonitorSummary(row.owner_id, row.id, row.name, row.status.value)
            for row in self.registry.list_monitors(owner_id)
        )


class FakeCodex:
    def __init__(self) -> None:
        self.calls: list[tuple[IntentRequest | PlanRequest, str, str]] = []

    async def run(
        self,
        request: IntentRequest | PlanRequest,
        *,
        model: str,
        effort: str,
    ) -> object:
        self.calls.append((request, model, effort))
        if type(request) is IntentRequest:
            return IntentResult(
                kind=IntentKind.CREATE,
                target_monitor_ids=[],
                target_url=TARGET_URL,
                condition_text="가격이 10만원 아래",
                schedule_text=None,
                clarification=None,
                confidence=0.98,
            )
        if type(request) is PlanRequest:
            return PlanResult(spec=planned_spec(), explanation="고정 계획 결과")
        raise AssertionError("unexpected fake Codex contract")


class FakePolicy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def validate(self, url: str) -> ResolvedTarget:
        self.calls.append(url)
        return ResolvedTarget(url, "example.com", 443, frozenset({"93.184.216.34"}))


class FakeProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ResolvedTarget]] = []

    async def probe(self, owner_id: str, target: ResolvedTarget) -> ProbeResult:
        self.calls.append((owner_id, target))
        return ProbeResult(
            target=target,
            document=SourceDocument(
                final_url=target.normalized_url,
                status=200,
                content_type="text/html",
                headers={"content-type": "text/html"},
                body=HTML,
                strategy=FetchStrategy.HTTP,
            ),
            robots=RobotsDecision(True, None, NOW, True),
        )


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int | str, str, object]] = []
        self.answers: list[tuple[str, str | None, bool]] = []

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: tuple[tuple[object, ...], ...] | None = None,
        disable_web_page_preview: bool = True,
    ) -> str:
        assert disable_web_page_preview is True
        self.messages.append((chat_id, text, buttons))
        return f"telegram-{len(self.messages)}"

    async def answer_callback(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        self.answers.append((callback_query_id, text, show_alert))


@dataclass(frozen=True)
class FixedClock:
    def now(self) -> datetime:
        return NOW


class AISentinel:
    def __init__(self) -> None:
        object.__setattr__(self, "_accesses", 0)

    @property
    def access_count(self) -> int:
        return object.__getattribute__(self, "_accesses")

    def __getattribute__(self, name: str) -> object:
        if name in {"_accesses", "access_count"}:
            return object.__getattribute__(self, name)
        accesses = object.__getattribute__(self, "_accesses")
        object.__setattr__(self, "_accesses", accesses + 1)
        raise AssertionError("scheduled runtime touched AI")

    def __call__(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("scheduled runtime called AI")


class DeterministicAdapter:
    async def fetch(self, monitor_id: str, _spec: MonitorSpec) -> ObservationBatch:
        return ObservationBatch(
            monitor_id=monitor_id,
            items=(ObservedItem("listing-1", {"title": "Safe keyboard", "price": 88000}),),
            observed_at=NOW,
            source_hash="fixture-source-hash",
        )


class ScheduledAdapters:
    def __init__(self, sentinel: AISentinel) -> None:
        self.adapter = DeterministicAdapter()
        self._scheduled_ai_sentinel = sentinel

    def resolve(self, _kind: object, _adapter_ref: str | None) -> DeterministicAdapter:
        return self.adapter


class RecordingSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def send(self, address: str, payload: dict[str, object]) -> str:
        self.calls.append((address, payload))
        return f"delivery-{len(self.calls)}"


class HealthSink:
    async def emit_once(self, _dedupe_key: str, _payload: dict[str, object]) -> None:
        raise AssertionError("healthy delivery emitted an operator event")


@dataclass
class Harness:
    connection: sqlite3.Connection
    registry: RegistryRepository
    runtime: RuntimeRepository
    codex: FakeCodex
    policy: FakePolicy
    probe: FakeProbe
    telegram: FakeTelegram
    gateway: TelegramGateway

    def close(self) -> None:
        self.connection.close()


def harness() -> Harness:
    connection = open_database(":memory:")
    registry = RegistryRepository(connection)
    runtime = RuntimeRepository(connection)
    registry.create_user(OWNER_ID, USER_ID)
    registry.create_delivery_target("delivery-target", OWNER_ID, str(CHAT_ID))
    actions = PendingActionService(connection)
    codex = FakeCodex()
    policy = FakePolicy()
    probe = FakeProbe()
    planner = MonitorPlanner(
        policy,
        probe,
        codex,
        actions,
        id_source=IdSource(),
        now_source=lambda: NOW,
    )
    router = IntentRouter(MonitorProvider(registry), codex)
    control = ControlService(router, registry, planner, actions, now_source=lambda: NOW)
    telegram = FakeTelegram()
    gateway = TelegramGateway(USER_ID, CHAT_ID, control, actions, telegram)
    return Harness(connection, registry, runtime, codex, policy, probe, telegram, gateway)


def text_update(*, user_id: int = USER_ID, chat_id: int = CHAT_ID) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=1,
        message=TelegramMessage(10, chat_id, user_id, MESSAGE),
        callback_query=None,
    )


def callback_update(
    callback: str,
    *,
    user_id: int = USER_ID,
    chat_id: int = CHAT_ID,
    update_id: int = 2,
) -> TelegramUpdate:
    return TelegramUpdate(
        update_id=update_id,
        message=None,
        callback_query=CallbackQuery(
            f"callback-{update_id}",
            user_id,
            chat_id,
            10,
            callback,
        ),
    )


def test_full_onboarding_confirmation_run_delivery_and_deduplication() -> None:
    state = harness()
    try:
        asyncio.run(state.gateway.handle_update(text_update(), now=NOW))

        assert len(state.telegram.messages) == 1
        preview_chat, preview_text, raw_buttons = state.telegram.messages[0]
        assert preview_chat == CHAT_ID
        assert "모니터: 키보드 가격" in preview_text
        assert "현재 가격: 88,000원" in preview_text
        assert state.connection.execute("SELECT count(*) FROM monitors").fetchone()[0] == 0
        assert isinstance(raw_buttons, tuple)
        callbacks = tuple(button.callback_data for button in raw_buttons[0])
        token = callbacks[0].split(":", 1)[1]
        assert callbacks == (f"confirm:{token}", f"edit:{token}", f"cancel:{token}")
        assert re.fullmatch(r"[A-Za-z0-9_-]{32}", token)
        assert [(model, effort) for _, model, effort in state.codex.calls] == [
            ("gpt-5.6-terra", "medium"),
            ("gpt-5.6-terra", "medium"),
        ]

        asyncio.run(state.gateway.handle_update(callback_update(callbacks[0]), now=NOW))

        monitor = state.connection.execute(
            "SELECT id, status, active_version_id FROM monitors"
        ).fetchone()
        assert monitor is not None
        assert monitor["status"] == "active"
        version = state.connection.execute(
            "SELECT version_number, approved_by FROM monitor_versions WHERE id = ?",
            (monitor["active_version_id"],),
        ).fetchone()
        assert tuple(version) == (1, OWNER_ID)
        assert state.telegram.answers == [("callback-2", "처리되었습니다", False)]

        sentinel = AISentinel()
        adapters = ScheduledAdapters(sentinel)
        runner = MonitorRunner(
            registry=state.registry,
            runtime=state.runtime,
            adapters=adapters,
            clock=FixedClock(),
            worker_id="runtime-worker",
        )
        sender = RecordingSender()
        outbox = OutboxWorker(
            runtime=state.runtime,
            registry=state.registry,
            sender=sender,
            health_sink=HealthSink(),
            worker_id="outbox-worker",
        )
        state.connection.execute(
            "UPDATE monitors SET next_run_at = ? WHERE id = ?",
            (NOW.isoformat(), monitor["id"]),
        )
        first_lease = state.runtime.claim_due(worker_id="runtime-worker", now=NOW)[0]
        first = asyncio.run(runner.run(first_lease))
        delivered = asyncio.run(outbox.drain_once(now=DELIVERY_NOW))

        assert first.matched_count == 1
        assert delivered == 1
        assert len(sender.calls) == 1
        assert state.connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
        assert state.connection.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1

        state.connection.execute(
            "UPDATE monitors SET next_run_at = ? WHERE id = ?",
            (NOW.isoformat(), monitor["id"]),
        )
        second_lease = state.runtime.claim_due(worker_id="runtime-worker", now=NOW)[0]
        second = asyncio.run(runner.run(second_lease))
        delivered_again = asyncio.run(outbox.drain_once(now=DELIVERY_NOW))

        assert second.matched_count == 0
        assert delivered_again == 0
        assert len(sender.calls) == 1
        assert state.connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
        assert sentinel.access_count == 0

        messages_before_replay = len(state.telegram.messages)
        asyncio.run(
            state.gateway.handle_update(
                callback_update(callbacks[0], update_id=3),
                now=NOW,
            )
        )
        assert len(state.telegram.messages) == messages_before_replay
        assert state.connection.execute("SELECT count(*) FROM monitors").fetchone()[0] == 1
        assert state.connection.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1
    finally:
        state.close()


def test_unauthorized_user_and_wrong_chat_stop_before_sql_or_external_boundaries() -> None:
    state = harness()
    statements: list[str] = []
    state.connection.set_trace_callback(statements.append)
    try:
        fake_callback = "confirm:" + "A" * 32
        asyncio.run(state.gateway.handle_update(text_update(user_id=8), now=NOW))
        asyncio.run(state.gateway.handle_update(text_update(chat_id=778), now=NOW))
        asyncio.run(
            state.gateway.handle_update(
                callback_update(fake_callback, user_id=8, update_id=3),
                now=NOW,
            )
        )
        asyncio.run(
            state.gateway.handle_update(
                callback_update(fake_callback, chat_id=778, update_id=4),
                now=NOW,
            )
        )

        assert state.codex.calls == []
        assert state.policy.calls == []
        assert state.probe.calls == []
        assert state.telegram.messages == []
        assert state.telegram.answers == []
        assert statements == []
    finally:
        state.connection.set_trace_callback(None)
        state.close()
