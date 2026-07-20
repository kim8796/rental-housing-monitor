from pathlib import Path

import pytest

from rental_monitor.config import ConfigurationError, Settings

REQUIRED = ("DATA_GO_KR_SERVICE_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def clear_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)


def test_missing_secret_names_are_reported_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_required(monkeypatch)
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "private-api-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")

    with pytest.raises(ConfigurationError) as caught:
        Settings.from_env()

    message = str(caught.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "TELEGRAM_CHAT_ID" in message
    assert "private-api-key" not in message


def test_settings_read_required_and_default_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_required(monkeypatch)
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "api-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.delenv("LOG_PATH", raising=False)

    settings = Settings.from_env()

    assert settings.database_path == Path("data/announcements.db")
    assert settings.log_path == Path("logs/monitor.log")
    assert settings.telegram_delivery_target == "telegram-default"


def test_blank_value_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_required(monkeypatch)
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", " ")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    with pytest.raises(ConfigurationError, match="DATA_GO_KR_SERVICE_KEY"):
        Settings.from_env()
