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

from postmaster.domain.post import Post, PostStatus
from postmaster.repositories.post_repository import PostRepository

logger = logging.getLogger(__name__)


class SchedulerJobRescheduler(Protocol):
    """Порт обновления/пересоздания задания Scheduler (PM-04 §6)."""

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None: ...


class RescheduleError(Exception):
    """Ошибка переноса публикации (PM-04 §5.2)."""


class PostNotPendingError(RescheduleError):
    """Пост не найден среди PENDING пользователя (чужой, отсутствует или не PENDING)."""


class RescheduleResult:
    """Итог переноса: сам пост и признак согласованности Scheduler (PM-06 / BUG-04).

    ``scheduler_synced is False`` означает: время в БД обновлено, но задание Scheduler
    не удалось перерегистрировать. Вызывающая сторона обязана сообщить об этом и/или
    выполнить повторную синхронизацию — ошибка не скрывается успешным ответом.
    """

    __slots__ = ("post", "scheduler_synced")

    def __init__(self, post: Post, *, scheduler_synced: bool) -> None:
        self.post = post
        self.scheduler_synced = scheduler_synced

    def __repr__(self) -> str:  # pragma: no cover — диагностика
        return f"RescheduleResult(post_id={self.post.id}, synced={self.scheduler_synced})"


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
        """Переносит публикацию и обновляет задание; возвращает Post (совместимость)."""
        result = await self.reschedule_with_status(
            post_id=post_id,
            user_id=user_id,
            scheduled_at_utc=scheduled_at_utc,
        )
        return result.post

    async def check_pending(self, *, post_id: int, user_id: int) -> Post | None:
        """Проверяет владельца и статус до запуска диалога переноса (PM-06 §10).

        Возвращает пост только если он принадлежит пользователю и всё ещё PENDING.
        Проверка read-only: FSM и БД не меняются. Финальная проверка всё равно
        выполняется атомарным UPDATE при вводе даты, поэтому подмена callback data
        между проверкой и переносом безопасна.
        """
        post = await self._posts.get_user_post(post_id, user_id)
        if post is None or post.status != PostStatus.PENDING:
            return None
        return post

    async def reschedule_with_status(
        self,
        *,
        post_id: int,
        user_id: int,
        scheduled_at_utc: datetime,
    ) -> RescheduleResult:
        """Атомарно переносит публикацию и синхронизирует Scheduler.

        БД фиксируется первой; ошибка Scheduler не откатывает БД (PM-04 §6.1), но и не
        скрывается: возвращается ``scheduler_synced=False`` и выполняется одна повторная
        попытка синхронизации. Данные в БД остаются источником истины, а задание
        Scheduler дополнительно проверяет своё время при срабатывании (BUG-04).

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
            raise PostNotPendingError("публикация не найдена среди ваших ожидающих публикаций")

        synced = False
        for attempt in (1, 2):
            try:
                self._scheduler.schedule_post(post_id=post.id, run_at=post.scheduled_at_utc)
            except Exception:
                logger.exception(
                    "Scheduler reschedule failed post_id=%s attempt=%s (DB already updated)",
                    post.id,
                    attempt,
                )
                continue
            synced = True
            break
        if not synced:
            logger.error(
                "Scheduler out of sync after reschedule post_id=%s scheduled_at_utc=%s",
                post.id,
                post.scheduled_at_utc,
            )
        logger.info(
            "Post rescheduled post_id=%s user_id=%s scheduled_at_utc=%s scheduler_synced=%s",
            post.id,
            user_id,
            post.scheduled_at_utc,
            synced,
        )
        return RescheduleResult(post, scheduler_synced=synced)
