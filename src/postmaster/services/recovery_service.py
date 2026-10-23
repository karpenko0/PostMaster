"""RecoveryService: восстановление заданий Scheduler после перезапуска (SPEC-011).

При старте приложения APScheduler не содержит заданий — они существовали только в памяти.
RecoveryService читает все PENDING-посты из БД и:

1. Посты с scheduled_at_utc в будущем — регистрирует новое задание в Scheduler.
2. Посты с scheduled_at_utc в прошлом (overdue) — передаёт missed handler (SPEC-012).

Сервис не импортирует telebot (BR-04) и работает только через репозиторий и Scheduler.
"""

import logging
from datetime import datetime
from typing import Protocol

from postmaster.domain.post import Post, PostStatus
from postmaster.repositories.post_repository import PostRepository
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)


class SchedulerGateway(Protocol):
    """Порт регистрации задания в Scheduler."""

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None: ...


class MissedPostGateway(Protocol):
    """Порт обработки пропущенных публикаций. Реализация — MissedPostHandler (SPEC-012)."""

    async def handle(self, post_id: int): ...


class RecoveryService:
    """Восстанавливает задания Scheduler по PENDING-постам из БД."""

    def __init__(
        self,
        repository: PostRepository,
        scheduler: SchedulerGateway,
        missed_post_handler: MissedPostGateway,
    ) -> None:
        self._repository = repository
        self._scheduler = scheduler
        self._missed = missed_post_handler

    async def recover(self) -> "RecoveryReport":
        """Читает PENDING-посты и восстанавливает задания или передаёт overdue в missed handler.

        Returns:
            RecoveryReport со статистикой: количество восстановленных задач,
            переданных в missed handler и список ошибок.
        """
        logger.info("Recovery started")
        posts = await self._repository.list_pending()
        report = RecoveryReport()
        now = utc_now()
        for post in posts:
            is_overdue = post.scheduled_at_utc <= now
            try:
                await self._recover_post(post, now)
            except Exception as exc:
                logger.error("Recovery failed for post_id=%s error=%s", post.id, type(exc).__name__)
                report.errors.append({"post_id": post.id, "error": type(exc).__name__})
                continue
            if is_overdue:
                report.missed += 1
            else:
                report.rescheduled += 1
        logger.info(
            "Recovery completed rescheduled=%s missed=%s errors=%s",
            report.rescheduled,
            report.missed,
            len(report.errors),
        )
        return report

    async def _recover_post(self, post: Post, now: datetime) -> None:
        """Восстанавливает один пост: reschedule или передача в missed handler."""
        if post.status != PostStatus.PENDING:
            # Запись могла измениться между чтением и обработкой.
            logger.info(
                "Skipping non-PENDING post during recovery post_id=%s status=%s",
                post.id,
                post.status,
            )
            return
        if post.scheduled_at_utc <= now:
            # Время публикации прошло пока приложение было выключено.
            # Обработка пропущенных публикаций — отдельный модуль (SPEC-012).
            logger.info(
                "Post is overdue, delegating to missed handler post_id=%s scheduled_at_utc=%s",
                post.id,
                post.scheduled_at_utc,
            )
            await self._missed.handle(post.id)
            return
        # Время ещё не пришло — регистрируем задание в Scheduler.
        logger.info(
            "Rescheduling post during recovery post_id=%s scheduled_at_utc=%s",
            post.id,
            post.scheduled_at_utc,
        )
        self._scheduler.schedule_post(post_id=post.id, run_at=post.scheduled_at_utc)


class RecoveryReport:
    """Результат восстановления заданий."""

    def __init__(self) -> None:
        self.rescheduled: int = 0
        self.missed: int = 0
        self.errors: list[dict[str, object]] = []
