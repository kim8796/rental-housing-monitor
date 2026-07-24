from __future__ import annotations

import asyncio

import pytest


@pytest.mark.browser
def test_auto_strategy_uses_real_browser_when_javascript_inserts_price(
    integration_harness,
) -> None:
    scenario = integration_harness.dynamic_monitor()

    result = asyncio.run(scenario.run())
    scenario.assert_proxy_ledger_consistent()

    assert (result.status, result.matched_count, result.warning_count) == ("success", 1, 0)
    assert scenario.strategy_trace == ["http", "dynamic"]
    assert [dict(item.fields) for item in scenario.runtime.load_items(scenario.monitor_id)] == [
        {"price": 87000, "title": "Browser keyboard"}
    ]
    assert scenario.outbox_count == 1
    assert scenario.pending_outbox_count == 1
    assert scenario.delivery_count == 0
    assert scenario.route_count("/dynamic") == 2
    scenario.assert_browser_proxy_trap()
    scenario.assert_local_only_and_clean()


@pytest.mark.browser
def test_two_dynamic_monitors_share_one_process_wide_browser_slot(
    integration_harness,
) -> None:
    scenario = integration_harness.concurrent_dynamic_monitors()

    async def exercise():
        running = asyncio.create_task(scenario.run_both())
        assert await asyncio.to_thread(scenario.first_browser_entered.wait, 5)
        assert scenario.browser_gate_is_held()
        scenario.release_first_browser()
        return await running

    results = asyncio.run(exercise())
    scenario.assert_proxy_ledger_consistent()

    assert [(result.status, result.matched_count) for result in results] == [
        ("success", 1),
        ("success", 1),
    ]
    assert scenario.max_active_browser_requests == 1
    assert scenario.route_count("/concurrent-a") == 1
    assert scenario.route_count("/concurrent-b") == 1
    assert scenario.observation_count == 2
    assert scenario.outbox_count == 2
    assert scenario.pending_outbox_count == 2
    assert scenario.delivery_count == 0
    scenario.assert_local_only_and_clean()
