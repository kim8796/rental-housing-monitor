from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from personal_monitor.security.egress import EgressProxyPolicy

_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]{0,18}\Z")
_SIGNED_INTEGER = re.compile(r"-?[1-9][0-9]{0,18}\Z")
_MAX_TELEGRAM_ID = 2**63 - 1


class ConfigurationError(RuntimeError):
    """A deployment configuration error whose text is safe for operator logs."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ConfigurationError(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class Settings:
    telegram_bot_token: str
    telegram_user_id: int
    command_chat_id: int
    delivery_chat_id: int
    master_key_path: Path
    codex_socket: Path
    egress_proxy: str
    database_path: Path
    profiles_root: Path
    diagnostics_root: Path
    adaptive_root: Path
    log_path: Path
    timezone: ZoneInfo
    backup_status_path: Path

    def __repr__(self) -> str:
        return (
            "Settings(telegram_bot_token=<redacted>, telegram_user_id=<redacted>, "
            "command_chat_id=<redacted>, delivery_chat_id=<redacted>, "
            "master_key_path=<redacted>, codex_socket=<redacted>, "
            "egress_proxy=<redacted>, database_path=<redacted>, "
            "profiles_root=<redacted>, diagnostics_root=<redacted>, "
            "adaptive_root=<redacted>, log_path=<redacted>, timezone=<redacted>, "
            "backup_status_path=<redacted>)"
        )

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if environment is None else environment
        token = _required_text(values, "PERSONAL_MONITOR_TELEGRAM_BOT_TOKEN")
        user_id = _positive_id(values, "PERSONAL_MONITOR_TELEGRAM_USER_ID")
        command_chat_id = _signed_id(values, "PERSONAL_MONITOR_TELEGRAM_COMMAND_CHAT_ID")
        delivery_chat_id = _signed_id(values, "PERSONAL_MONITOR_TELEGRAM_DELIVERY_CHAT_ID")
        master_key_path = _required_path(values, "PERSONAL_MONITOR_MASTER_KEY_PATH")
        codex_socket = _required_path(values, "PERSONAL_MONITOR_CODEX_SOCKET")
        proxy = _required_text(values, "PERSONAL_MONITOR_EGRESS_PROXY")
        _validate_proxy(proxy, "PERSONAL_MONITOR_EGRESS_PROXY")
        database_path = _optional_path(
            values,
            "PERSONAL_MONITOR_DATABASE_PATH",
            "/srv/personal-monitor/db/monitor.db",
        )
        profiles_root = _optional_path(
            values,
            "PERSONAL_MONITOR_PROFILES_ROOT",
            "/run/personal-monitor-profiles",
        )
        diagnostics_root = _optional_path(
            values,
            "PERSONAL_MONITOR_DIAGNOSTICS_ROOT",
            "/srv/personal-monitor/diagnostics",
        )
        adaptive_root = _optional_path(
            values,
            "PERSONAL_MONITOR_ADAPTIVE_ROOT",
            "/srv/personal-monitor/adaptive",
        )
        log_path = _optional_path(
            values,
            "PERSONAL_MONITOR_LOG_PATH",
            "/srv/personal-monitor/logs/monitor.jsonl",
        )
        backup_status_path = _optional_path(
            values,
            "PERSONAL_MONITOR_BACKUP_STATUS_PATH",
            "/srv/personal-monitor/backup-status.json",
        )
        timezone_name = values.get("PERSONAL_MONITOR_TIMEZONE", "Asia/Seoul")
        if not isinstance(timezone_name, str) or not timezone_name:
            raise ConfigurationError("PERSONAL_MONITOR_TIMEZONE: value must be a valid timezone")
        try:
            timezone = ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError):
            raise ConfigurationError(
                "PERSONAL_MONITOR_TIMEZONE: value must be a valid timezone"
            ) from None
        return cls(
            telegram_bot_token=token,
            telegram_user_id=user_id,
            command_chat_id=command_chat_id,
            delivery_chat_id=delivery_chat_id,
            master_key_path=master_key_path,
            codex_socket=codex_socket,
            egress_proxy=proxy,
            database_path=database_path,
            profiles_root=profiles_root,
            diagnostics_root=diagnostics_root,
            adaptive_root=adaptive_root,
            log_path=log_path,
            timezone=timezone,
            backup_status_path=backup_status_path,
        )


def _required_text(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{name}: required value is missing")
    if len(value) > 4096 or any(ord(character) < 32 for character in value):
        raise ConfigurationError(f"{name}: value is invalid")
    return value


def _positive_id(values: Mapping[str, str], name: str) -> int:
    value = _required_text(values, name)
    if _POSITIVE_INTEGER.fullmatch(value) is None:
        raise ConfigurationError(f"{name}: value must be a positive decimal integer")
    result = int(value)
    if result > _MAX_TELEGRAM_ID:
        raise ConfigurationError(f"{name}: value is outside the Telegram integer range")
    return result


def _signed_id(values: Mapping[str, str], name: str) -> int:
    value = _required_text(values, name)
    if _SIGNED_INTEGER.fullmatch(value) is None:
        raise ConfigurationError(f"{name}: value must be a signed Telegram integer")
    result = int(value)
    if abs(result) > _MAX_TELEGRAM_ID:
        raise ConfigurationError(f"{name}: value is outside the Telegram integer range")
    return result


def _required_path(values: Mapping[str, str], name: str) -> Path:
    value = _required_text(values, name)
    return _validated_path(value, name)


def _optional_path(values: Mapping[str, str], name: str, default: str) -> Path:
    value = values.get(name, default)
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{name}: value must be an absolute normalized path")
    return _validated_path(value, name)


def _validated_path(value: str, name: str) -> Path:
    try:
        path = Path(value)
        normalized = Path(os.path.normpath(value))
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name}: value must be an absolute normalized path") from None
    if (
        "\x00" in value
        or not path.is_absolute()
        or path != normalized
        or value != os.path.normpath(value)
        or value.endswith("/")
        or len(os.fsencode(value)) > 4096
    ):
        raise ConfigurationError(f"{name}: value must be an absolute normalized path")
    return path


def _validate_proxy(value: str, name: str) -> None:
    try:
        EgressProxyPolicy.from_url(value)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name}: value must be a valid proxy URL") from None
