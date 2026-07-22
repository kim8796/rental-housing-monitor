from __future__ import annotations

import asyncio


def test_static_monitor_runs_real_pipeline_and_deduplicates(integration_harness) -> None:
    scenario = integration_harness.static_monitor()

    first = asyncio.run(scenario.run())
    second = asyncio.run(scenario.run_again())

    assert (first.status, first.matched_count, first.warning_count) == ("success", 1, 0)
    assert (second.status, second.matched_count, second.warning_count) == ("success", 0, 0)
    assert [dict(item.fields) for item in scenario.runtime.load_items(scenario.monitor_id)] == [
        {"price": 129000, "title": "Local keyboard"}
    ]
    rows = scenario.connection.execute(
        "SELECT target_id, payload_json FROM outbox ORDER BY created_at, id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (
            "target-local",
            '{"text":"모니터 조건에 맞는 변경이 감지되었습니다.\\n'
            f'종류: new_item\\n출처: {scenario.origin_url}/static"}}',
        )
    ]
    assert scenario.origin_requests == ["/robots.txt", "/static", "/robots.txt", "/static"]
    assert scenario.telegram_calls == []
    scenario.assert_local_only_and_clean()
