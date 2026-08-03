from __future__ import annotations

import asyncio
import http.client
import inspect

from personal_monitor.engine.runner import MonitorRunner
from rental_monitor.telegram import TelegramClient


def test_integration_installs_fail_fast_telegram_boundary() -> None:
    assert getattr(TelegramClient.send, "_integration_fail_fast", False)


def test_fixture_origin_rejects_requests_without_proxy_capability(
    integration_harness,
) -> None:
    scenario = integration_harness.static_monitor()
    connection = http.client.HTTPConnection("127.0.0.1", scenario.origin_server.port)
    try:
        connection.request("GET", "/static")
        response = connection.getresponse()
        response.read()
    finally:
        connection.close()

    assert response.status == 403
    with scenario.state.lock:
        assert scenario.state.direct_origin_rejections == 1
        assert scenario.state.forwarded_paths == []
        assert scenario.state.requests == []


def test_static_monitor_runs_real_pipeline_and_deduplicates(integration_harness) -> None:
    scenario = integration_harness.static_monitor()

    first = asyncio.run(scenario.run())
    scenario.assert_proxy_ledger_consistent()
    second = asyncio.run(scenario.run_again())
    scenario.assert_proxy_ledger_consistent()

    assert (first.status, first.matched_count, first.warning_count) == ("success", 1, 0)
    assert (second.status, second.matched_count, second.warning_count) == ("success", 0, 0)
    assert [dict(item.fields) for item in scenario.runtime.load_items(scenario.monitor_id)] == [
        {"price": 129000, "title": "Local keyboard"}
    ]
    rows = scenario.connection.execute(
        "SELECT target_id, payload_json, status FROM outbox ORDER BY created_at, id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
            (
                "target-local",
                '{"text":"모니터 조건에 맞는 변경이 감지되었습니다.\\n'
                "종류: 신규 항목\\n항목: Local keyboard\\n"
                f'출처: {scenario.origin_url}/static"}}',
                "pending",
            )
    ]
    assert scenario.pending_outbox_count == 1
    assert scenario.delivery_count == 0
    runner_source = inspect.getsource(MonitorRunner)
    assert "DeliverySender" not in runner_source
    assert "TelegramClient" not in runner_source
    assert all(
        "sender" not in name.lower() and "telegram" not in name.lower()
        for name in vars(scenario.runner)
    )
    assert scenario.origin_requests == [
        "/robots.txt",
        "/static?view=private",
        "/robots.txt",
        "/static?view=private",
    ]
    assert not hasattr(scenario, "telegram_calls")
    scenario.assert_local_only_and_clean()
