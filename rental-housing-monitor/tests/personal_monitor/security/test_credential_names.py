from __future__ import annotations

import pytest

import personal_monitor.security.credential_names as credential_names
from tests.credential_alias_cases import (
    BENIGN_COMPOUND_FIELD_NAMES,
    BENIGN_CREDENTIAL_LIKE_KEYS,
    SENSITIVE_COMPOUND_FIELD_NAMES,
    SENSITIVE_KEY_VARIANTS,
)


@pytest.mark.parametrize("value", SENSITIVE_KEY_VARIANTS)
def test_every_canonical_credential_name_variant_is_sensitive(value: str) -> None:
    assert credential_names.is_sensitive_credential_name(value)


@pytest.mark.parametrize("value", BENIGN_CREDENTIAL_LIKE_KEYS)
def test_noncanonical_credential_like_names_remain_safe(value: str) -> None:
    assert not credential_names.is_sensitive_credential_name(value)


@pytest.mark.parametrize("value", SENSITIVE_COMPOUND_FIELD_NAMES)
def test_structured_compound_fields_find_canonical_credential_tokens(value: str) -> None:
    assert credential_names.is_sensitive_compound_field_name(value)


@pytest.mark.parametrize("value", BENIGN_COMPOUND_FIELD_NAMES)
def test_noncredential_compound_fields_remain_safe(value: str) -> None:
    assert not credential_names.is_sensitive_compound_field_name(value)


@pytest.mark.parametrize("value", SENSITIVE_COMPOUND_FIELD_NAMES)
def test_compound_field_names_are_not_exact_credential_query_names(value: str) -> None:
    assert not credential_names.is_sensitive_credential_name(value)
