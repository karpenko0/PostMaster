"""RescheduleService: перенос PENDING-публикации пользователем (PM-04).

Сервис координирует атомарный перенос:
1. Проверка владельца и статуса PENDING через PostRepository.reschedule_post.
   rowcount=1 — единственное условие успешного переноса. Чужие посты, отсутствующие
   посты и посты не в PENDING возвращают None — БД остаётся неизменной.
2. SchedulerService.schedule_post(post_id, run_at) с replace_existing=True —
   обновляет существующее задание новым временем. Ошибка НЕ возвращает пост
   к прежнему времени: БД уже содержит новый scheduled_at_utc (источник истины),
   а Recovery при следующем старте перерегистрирует задачу.
3. Поля типа публикации, фото, caption и target_id остаются неизменными —
   обновляется только scheduled_at_utc и updated_at.

Не работает с Telegram API — только с PostRepository и Scheduler.
"""

import logging
from datetime import datetime
from typing import Protocol

from postmaster.domain.post import Post
from postmaster.repositories.post_repository import PostRepository

logger = logging.getLogger(__name__)


class SchedulerJobRescheduler(Protocol):
    """Порт обновления/пересоздания задания Scheduler (PM-04 §6)."""

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None: ...


class RescheduleError(Exception):
    """Ошибка переноса публикации (PM-04 §5.2)."""


class PostNotPendingError(RescheduleError):
    """Пост не найден среди PENDING пользователя (чужой, отсутствует или не PENDING)."""


class RescheduleService:
    """Переносит PENDING-публикацию пользователя на новое UTC-время (PM-04)."""

    def __init__(
        self,
        post_repository: PostRepository,
        scheduler: SchedulerJobRescheduler,
    ) -> None:
        self._posts = post_repository
        self._scheduler = scheduler

    async def reschedule(
        self,
        *,
        post_id: int,
        user_id: int,
        scheduled_at_utc: datetime,
    ) -> Post:
        """Атомарно переносит публикацию и обновляет задание в Scheduler.

        БД фиксируется первой; ошибка Scheduler не откатывает БД — следующий Recovery
        восстановит согласованность (PM-04 §6.1).

        Raises:
            PostNotPendingError: пост не найден среди PENDING пользователя
                (не существует, чужой или уже не PENDING).
        """
        post = await self._posts.reschedule_post(
            post_id=post_id,
            user_id=user_id,
            scheduled_at_utc=scheduled_at_utc,
        )
        if post is None:
            logger.info(
                "Reschedule rejected post_id=%s user_id=%s: not own PENDING",
                post_id,
                user_id,
            )
            raise PostNotPendingError(
                "публикация не найдена среди ваших ожидающих публикаций"
            )

        # БД уже содержит новое время — ошибка Scheduler не должна возвращать
        # пост к прежнему расписанию (§6.1 SPEC-PM-04). Recovery при следующем
        # старте перерегистрирует задание по актуальному scheduled_at_utc.
        try:
            self._scheduler.schedule_post(
                post_id=post.id, run_at=post.scheduled_at_utc
            )
        except Exception:
            logger.exception(
                "Scheduler reschedule failed post_id=%s (DB already updated)",
                post.id,
            )
        logger.info(
            "Post rescheduled post_id=%s user_id=%s scheduled_at_utc=%s",
            post.id,
            user_id,
            post.scheduled_at_utc,
        )
        return post
