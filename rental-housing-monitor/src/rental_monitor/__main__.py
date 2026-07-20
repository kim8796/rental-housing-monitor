from __future__ import annotations

import logging
import sys

import httpx

from rental_monitor.collectors.gh import GHCollector
from rental_monitor.collectors.lh import LHCollector
from rental_monitor.collectors.sh import SHCollector
from rental_monitor.config import ConfigurationError, Settings
from rental_monitor.logging_config import configure_logging
from rental_monitor.repository import AnnouncementRepository
from rental_monitor.runner import MonitorRunner
from rental_monitor.telegram import TelegramClient


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        print(f"설정 오류: {error}", file=sys.stderr)
        return 2

    configure_logging(settings.log_path)
    logger = logging.getLogger(__name__)
    repository = AnnouncementRepository(settings.database_path)
    try:
        with httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": "rental-housing-monitor/0.1 (+official-notice-checker)"},
        ) as client:
            telegram = TelegramClient(
                client,
                settings.telegram_bot_token,
                settings.telegram_chat_id,
            )
            runner = MonitorRunner(
                (
                    LHCollector(client, settings.data_go_kr_service_key),
                    SHCollector(client),
                    GHCollector(client),
                ),
                repository,
                telegram,
                chat_id=settings.telegram_chat_id,
            )
            result = runner.run()
            logger.info("실행 완료 status=%s new_count=%d", result.status, result.new_count)
    except Exception as error:
        logger.error("실행 실패 error_type=%s", type(error).__name__)
        return 1
    finally:
        repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
