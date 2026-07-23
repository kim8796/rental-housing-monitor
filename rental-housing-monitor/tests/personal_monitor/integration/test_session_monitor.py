from __future__ import annotations

import asyncio

import pytest


@pytest.mark.browser
def test_encrypted_profile_bootstrap_reuses_session_without_plaintext(
    integration_harness,
) -> None:
    scenario = integration_harness.session_monitor()
    assert scenario.bootstrapped_cookie_rows == 1

    unauthenticated = asyncio.run(scenario.run_without_profile())
    scenario.assert_proxy_ledger_consistent()
    assert (unauthenticated.status, unauthenticated.matched_count) == ("failed", 0)
    assert scenario.observation_count == 0
    assert scenario.outbox_count == 0
    assert scenario.pending_outbox_count == 0
    assert scenario.delivery_count == 0
    scenario.assert_profile_runtime_clean()

    first = asyncio.run(scenario.run_profiled())
    scenario.assert_proxy_ledger_consistent()
    assert (first.status, first.matched_count) == ("success", 1), (
        scenario.safe_run_diagnostics,
        scenario.unauthenticated_requests,
        scenario.authenticated_requests,
    )
    assert scenario.observation_count == 1
    assert scenario.outbox_count == 1
    assert scenario.pending_outbox_count == 1
    assert scenario.delivery_count == 0
    scenario.assert_profile_runtime_clean()

    second = asyncio.run(scenario.run_profiled_again())
    scenario.assert_proxy_ledger_consistent()
    assert (second.status, second.matched_count) == ("success", 0)
    assert scenario.observation_count == 1
    assert scenario.outbox_count == 1
    assert scenario.pending_outbox_count == 1
    assert scenario.delivery_count == 0
    assert scenario.unauthenticated_requests == 1
    assert scenario.authenticated_requests == 2
    assert scenario.forwarded_protected_cookie_requests == 2
    scenario.assert_profile_runtime_clean()
    scenario.assert_sensitive_material_absent()
    scenario.assert_local_only_and_clean()
