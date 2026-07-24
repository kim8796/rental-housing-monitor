from __future__ import annotations

from pathlib import Path

import pytest

from personal_monitor.config import BillingSettings, ConfigurationError, Settings


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "PERSONAL_MONITOR_TELEGRAM_BOT_TOKEN": "123456:private-token",
        "PERSONAL_MONITOR_TELEGRAM_USER_ID": "123456",
        "PERSONAL_MONITOR_TELEGRAM_COMMAND_CHAT_ID": "-100123",
        "PERSONAL_MONITOR_TELEGRAM_DELIVERY_CHAT_ID": "987654",
        "PERSONAL_MONITOR_MASTER_KEY_PATH": str(tmp_path / "master.key"),
        "PERSONAL_MONITOR_CODEX_SOCKET": str(tmp_path / "codex.sock"),
        "PERSONAL_MONITOR_EGRESS_PROXY": "http://user:password@proxy.example:8080",
    }


def test_settings_loads_required_values_and_safe_defaults(tmp_path: Path) -> None:
    settings = Settings.from_env(_environment(tmp_path))

    assert settings.telegram_user_id == 123456
    assert settings.command_chat_id == -100123
    assert settings.delivery_chat_id == 987654
    assert settings.database_path == Path("/srv/personal-monitor/db/monitor.db")
    assert settings.timezone.key == "Asia/Seoul"
    assert settings.data_go_kr_service_key is None
    assert "private-token" not in repr(settings)
    assert "password" not in repr(settings)


def test_optional_data_go_kr_key_is_loaded_without_repr_or_error_leakage(
    tmp_path: Path,
) -> None:
    secret = "encoded-service-key%2Fprivate"
    environment = _environment(tmp_path)
    environment["PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY"] = secret

    settings = Settings.from_env(environment)

    assert settings.data_go_kr_service_key == secret
    assert secret not in repr(settings)
    assert "data_go_kr_service_key=<redacted>" in repr(settings)

    for malformed in (" leading-space", "trailing-space ", "line\nbreak"):
        environment["PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY"] = malformed
        with pytest.raises(ConfigurationError) as caught:
            Settings.from_env(environment)
        assert str(caught.value) == ("PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY: value is invalid")
        assert malformed not in str(caught.value)
        assert secret not in repr(caught.value)


def test_blank_optional_data_go_kr_key_is_treated_as_unconfigured(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment["PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY"] = ""
    environment["DATA_GO_KR_SERVICE_KEY"] = "legacy-runner-secret"

    assert Settings.from_env(environment).data_go_kr_service_key is None


def test_billing_settings_are_optional_or_loaded_as_one_validated_unit(tmp_path: Path) -> None:
    assert Settings.from_env(_environment(tmp_path)).billing is None
    environment = _environment(tmp_path)
    environment["PERSONAL_MONITOR_BILLING_PROJECT_ID"] = "local-social-native-wlk-0720"
    environment["PERSONAL_MONITOR_BILLING_DATASET_ID"] = "billing_monitor"
    environment["PERSONAL_MONITOR_BILLING_MAXIMUM_BYTES"] = "100000000"

    settings = Settings.from_env(environment)

    assert settings.billing == BillingSettings(
        project_id="local-social-native-wlk-0720",
        dataset_id="billing_monitor",
        maximum_bytes_billed=100_000_000,
    )
    assert "local-social-native-wlk-0720" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("PERSONAL_MONITOR_BILLING_PROJECT_ID", "INVALID_PROJECT"),
        ("PERSONAL_MONITOR_BILLING_DATASET_ID", "bad-dataset"),
        ("PERSONAL_MONITOR_BILLING_MAXIMUM_BYTES", "0"),
        ("PERSONAL_MONITOR_BILLING_MAXIMUM_BYTES", "1000000001"),
    ),
)
def test_invalid_billing_settings_fail_closed(tmp_path: Path, name: str, value: str) -> None:
    environment = _environment(tmp_path)
    environment["PERSONAL_MONITOR_BILLING_PROJECT_ID"] = "local-social-native-wlk-0720"
    environment["PERSONAL_MONITOR_BILLING_DATASET_ID"] = "billing_monitor"
    environment[name] = value

    with pytest.raises(ConfigurationError) as caught:
        Settings.from_env(environment)

    assert name in str(caught.value)
    assert value not in str(caught.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PERSONAL_MONITOR_TELEGRAM_USER_ID", "not-a-number"),
        ("PERSONAL_MONITOR_TELEGRAM_USER_ID", "01"),
        ("PERSONAL_MONITOR_TELEGRAM_USER_ID", "0"),
        ("PERSONAL_MONITOR_TELEGRAM_COMMAND_CHAT_ID", "+1"),
        ("PERSONAL_MONITOR_TELEGRAM_COMMAND_CHAT_ID", "-0"),
        ("PERSONAL_MONITOR_MASTER_KEY_PATH", "relative/key"),
        ("PERSONAL_MONITOR_MASTER_KEY_PATH", "/tmp//private-key"),
        ("PERSONAL_MONITOR_EGRESS_PROXY", "http://user:private@"),
        ("PERSONAL_MONITOR_TIMEZONE", "Invalid/Private"),
    ],
)
def test_settings_errors_name_only_the_variable(tmp_path: Path, name: str, value: str) -> None:
    environment = _environment(tmp_path)
    environment[name] = value

    with pytest.raises(ConfigurationError) as caught:
        Settings.from_env(environment)

    message = str(caught.value)
    assert name in message
    assert value not in message
    assert "private-token" not in message
    assert "password" not in message


def test_settings_requires_every_required_value_without_leaking_neighbors(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    environment["PERSONAL_MONITOR_TELEGRAM_BOT_TOKEN"] = ""

    with pytest.raises(ConfigurationError) as caught:
        Settings.from_env(environment)

    assert str(caught.value) == ("PERSONAL_MONITOR_TELEGRAM_BOT_TOKEN: required value is missing")
    assert "proxy.example" not in str(caught.value)
