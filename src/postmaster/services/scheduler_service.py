"""Обёртка над APScheduler (SPEC-010).

Scheduler работает в event loop приложения. Его задания вызывают прикладные сервисы,
а не Telegram API напрямую (BR-04). SPEC-009 регистрирует задания по post_id;
исполнение публикации — SPEC-010/014/015.
"""

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from postmaster.repositories.post_repository import PostRepository

logger = logging.getLogger(__name__)


class SchedulerService:
    """Запускает и останавливает Scheduler вместе с приложением (US-02)."""

    def __init__(self, post_repository: PostRepository | None = None) -> None:
        # Все времена в БД хранятся в UTC (SPEC-006), поэтому Scheduler работает в UTC.
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._posts = post_repository

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        """Одноразовое задание по id; публикация/доставка появится в следующих SPEC.

        На запуске задача перечитывает БД, а не держит Post в памяти. Пока Publisher не
        реализован, она оставляет PENDING для будущего recovery, не заявляя об отправке.
        """
        if run_at.tzinfo is None or run_at.utcoffset() != timedelta(0):
            raise ValueError("run_at должен быть aware UTC")
        if self._posts is None:
            raise RuntimeError("Scheduler требует PostRepository для чтения задания")
        if not self.running:
            raise RuntimeError("Scheduler не запущен")
        self._scheduler.add_job(
            self._on_post_due,
            trigger="date",
            run_date=run_at.astimezone(UTC),
            args=(post_id,),
            id=f"post:{post_id}",
            replace_existing=True,
        )

    async def _on_post_due(self, post_id: int) -> None:
        assert self._posts is not None
        post = await self._posts.get_by_id(post_id)
        # Исполнение, обработка пропущенных задач и доставка — SPEC-010/014/015.
        logger.warning(
            "Post due but execution is not implemented post_id=%s exists=%s",
            post_id,
            post is not None,
        )

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
