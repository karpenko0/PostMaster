"""Обёртка над APScheduler (SPEC-010, SPEC-013).

Scheduler работает в event loop приложения. Его задания вызывают прикладные сервисы,
а не Telegram API напрямую (BR-04). При подключённом PublicationService выполнение
делегируется в publication_service.execute(post_id) с защитой через atomic claim (SPEC-013 §19).
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from postmaster.repositories.post_repository import PostRepository

logger = logging.getLogger(__name__)


class PostExecutionService(Protocol):
    async def execute(self, post_id: int) -> object: ...


class SchedulerService:
    """Запускает и останавливает Scheduler вместе с приложением (US-02)."""

    def __init__(
        self,
        post_repository: PostRepository | None = None,
        publication_service: PostExecutionService | None = None,
    ) -> None:
        # Все времена в БД хранятся в UTC (SPEC-006), поэтому Scheduler работает в UTC.
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._posts = post_repository
        self._publication_service = publication_service

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        """Одноразовое задание по id; при срабатывании запускает централизованное выполнение."""
        if run_at.tzinfo is None or run_at.utcoffset() != timedelta(0):
            raise ValueError("run_at должен быть aware UTC")
        if self._posts is None and self._publication_service is None:
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
        if self._publication_service is not None:
            await self._publication_service.execute(post_id)
            return
        assert self._posts is not None
        post = await self._posts.get_by_id(post_id)
        # Scheduler не меняет статус самостоятельно; публикация/claim принадлежат service.
        logger.warning(
            "Post due but execution is not implemented post_id=%s exists=%s",
            post_id,
            post is not None,
        )

    def remove_post(self, post_id: int) -> bool:
        """Удаляет date-job поста; отсутствие job — штатный исход (SPEC-018 §17–19)."""
        try:
            self._scheduler.remove_job(f"post:{post_id}")
        except JobLookupError:
            logger.info("Scheduler job not found post_id=%s", post_id)
            return False
        logger.info("Scheduler job removed post_id=%s", post_id)
        return True

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
