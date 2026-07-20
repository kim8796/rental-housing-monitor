from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    data_go_kr_service_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    database_path: Path
    log_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        required = {
            "DATA_GO_KR_SERVICE_KEY": os.getenv("DATA_GO_KR_SERVICE_KEY", "").strip(),
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError(f"필수 환경변수가 없습니다: {', '.join(missing)}")
        return cls(
            data_go_kr_service_key=required["DATA_GO_KR_SERVICE_KEY"],
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=required["TELEGRAM_CHAT_ID"],
            database_path=Path(os.getenv("DATABASE_PATH", "data/announcements.db")),
            log_path=Path(os.getenv("LOG_PATH", "logs/monitor.log")),
        )
