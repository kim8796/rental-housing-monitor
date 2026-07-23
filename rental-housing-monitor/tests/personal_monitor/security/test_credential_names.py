from __future__ import annotations

import pytest

from personal_monitor.security.credential_names import is_sensitive_credential_name
from tests.credential_alias_cases import (
    BENIGN_CREDENTIAL_LIKE_KEYS,
    SENSITIVE_KEY_VARIANTS,
)


@pytest.mark.parametrize("value", SENSITIVE_KEY_VARIANTS)
def test_every_canonical_credential_name_variant_is_sensitive(value: str) -> None:
    assert is_sensitive_credential_name(value)


@pytest.mark.parametrize("value", BENIGN_CREDENTIAL_LIKE_KEYS)
def test_noncanonical_credential_like_names_remain_safe(value: str) -> None:
    assert not is_sensitive_credential_name(value)
