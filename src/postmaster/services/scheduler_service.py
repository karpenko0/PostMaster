"""Обёртка над APScheduler (SPEC-010).

Scheduler работает в event loop приложения. Его задания вызывают прикладные сервисы,
а не Telegram API напрямую (BR-04). Регистрация заданий появится в SPEC-009 и SPEC-010.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)


class SchedulerService:
    """Запускает и останавливает Scheduler вместе с приложением (US-02)."""

    def __init__(self) -> None:
        # Все времена в БД хранятся в UTC (SPEC-006), поэтому Scheduler работает в UTC.
        self._scheduler = AsyncIOScheduler(timezone="UTC")

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def start(self) -> None:
        """Запускает Scheduler. Вызывать из работающего event loop."""
        self._scheduler.start()
        logger.info("Scheduler запущен")

    def shutdown(self) -> None:
        """Останавливает Scheduler, если он запущен. Повторный вызов безопасен."""
        if not self.running:
            return
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler остановлен")
