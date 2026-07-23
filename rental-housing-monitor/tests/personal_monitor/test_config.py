from __future__ import annotations

from pathlib import Path

import pytest

from personal_monitor.config import ConfigurationError, Settings


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
    assert "private-token" not in repr(settings)
    assert "password" not in repr(settings)


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
