"""Обёртка над APScheduler (SPEC-010).

Scheduler работает в event loop приложения. Его задания вызывают PublicationService,
а не Telegram API напрямую (BR-04). SPEC-009 регистрирует задания по post_id;
при срабатывании задания PublicationService выполняет публикацию.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from postmaster.domain.post import PostNotFoundError, PostStatus
from postmaster.repositories.post_repository import PostRepository
from postmaster.utils.logging_config import (
    EVENT_SCHEDULER_JOB_ADDED,
    EVENT_SCHEDULER_JOB_REMOVED,
    EVENT_SCHEDULER_START,
    EVENT_SCHEDULER_STOP,
    log_event,
)

logger = logging.getLogger(__name__)


class PublicationExecutor(Protocol):
    """Порт выполнения публикации. Реализация — PublicationService."""

    async def publish(self, post_id: int): ...


class SchedulerService:
    """Запускает и останавливает Scheduler вместе с приложением (US-02)."""

    def __init__(
        self,
        post_repository: PostRepository | None = None,
        publication_executor: PublicationExecutor | None = None,
    ) -> None:
        # Все времена в БД хранятся в UTC (SPEC-006), поэтому Scheduler работает в UTC.
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._posts = post_repository
        self._executor = publication_executor

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        """Одноразовое задание по id; при срабатывании вызывает PublicationExecutor.

        На запуске задача передаёт только post_id в PublicationService, который
        перечитывает БД. Это позволяет не хранить объект Post в памяти Scheduler.
        """
        if run_at.tzinfo is None or run_at.utcoffset() != timedelta(0):
            raise ValueError("run_at должен быть aware UTC")
        if self._posts is None or self._executor is None:
            raise RuntimeError("Scheduler требует PostRepository и PublicationExecutor")
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
        log_event(
            logger,
            EVENT_SCHEDULER_JOB_ADDED,
            post_id=post_id,
            run_at=run_at.astimezone(UTC).isoformat(),
        )

    def remove_job(self, post_id: int) -> bool:
        """Удаляет задание поста из Scheduler. Возвращает True, если задание было.

        Вызывается после успешной отмены публикации (SPEC-018 §1). Ошибка удаления
        не должна возвращать пост в PENDING: отмена уже зафиксирована в БД.
        """
        if not self.running:
            logger.info("Scheduler не запущен, задание не удалено post_id=%s", post_id)
            return False
        try:
            removed = self._scheduler.remove_job(f"post:{post_id}")
        except Exception:
            logger.exception("Не удалось удалить задание Scheduler post_id=%s", post_id)
            return False
        if removed:
            log_event(
                logger,
                EVENT_SCHEDULER_JOB_REMOVED,
                level=logging.INFO,
                post_id=post_id,
            )
        return removed

    async def _on_post_due(self, post_id: int) -> None:
        assert self._posts is not None and self._executor is not None
        post = await self._posts.get_by_id(post_id)
        if post is None:
            logger.warning("Post not found at execution time post_id=%s", post_id)
            return
        if post.status != PostStatus.PENDING:
            logger.info(
                "Post already processed post_id=%s status=%s",
                post_id,
                post.status,
            )
            return
        try:
            await self._executor.publish(post_id)
        except PostNotFoundError:
            logger.warning("Post disappeared before execution post_id=%s", post_id)
        except Exception:
            logger.exception("Publication execution failed post_id=%s", post_id)

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def start(self) -> None:
        """Запускает Scheduler. Вызывать из работающего event loop."""
        self._scheduler.start()
        log_event(logger, EVENT_SCHEDULER_START)

    def shutdown(self) -> None:
        """Останавливает Scheduler, если он запущен. Повторный вызов безопасен."""
        if not self.running:
            return
        self._scheduler.shutdown(wait=False)
        log_event(logger, EVENT_SCHEDULER_STOP)
