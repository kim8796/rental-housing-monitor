from __future__ import annotations

import pytest

from tests.personal_monitor.adapters._helpers import (
    install_policy_test_boundaries,
    reset_test_boundaries,
)


@pytest.fixture(autouse=True)
def sealed_test_boundaries(monkeypatch: pytest.MonkeyPatch):
    reset_test_boundaries()
    install_policy_test_boundaries(monkeypatch)
    yield
    reset_test_boundaries()
