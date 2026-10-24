"""CancelService: отмена PENDING-публикации пользователем (SPEC-018).

Сервис координирует атомарную отмену:
1. PostRepository.cancel_post(post_id, user_id) — атомарный UPDATE own PENDING→CANCELLED.
   rowcount=1 — единственное условие успешной отмены. Чужие посты, отсутствующие
   посты и посты не в PENDING возвращают False — статус не меняется.
2. SchedulerService.remove_job(post_id) — удаление задания из планировщика.
   Ошибка удаления НЕ возвращает пост в PENDING: отмена уже зафиксирована в БД.
3. Запись из БД не удаляется.

Не работает с Telegram API — только с PostRepository и Scheduler.
"""

import logging
from typing import Protocol

from postmaster.repositories.post_repository import PostRepository

logger = logging.getLogger(__name__)


class SchedulerJobRemover(Protocol):
    """Порт удаления задания Scheduler (SPEC-018 §1)."""

    def remove_job(self, post_id: int) -> bool: ...


class CancelError(Exception):
    """Ошибка отмены публикации (SPEC-018 §7)."""


class PostNotFoundForUserError(CancelError):
    """Пост не найден среди PENDING пользователя (чужой или не PENDING)."""


class CancelService:
    """Отменяет PENDING-публикацию пользователя (SPEC-018)."""

    def __init__(
        self,
        post_repository: PostRepository,
        scheduler: SchedulerJobRemover,
    ) -> None:
        self._posts = post_repository
        self._scheduler = scheduler

    async def cancel(self, *, post_id: int, user_id: int) -> None:
        """Атомарно отменяет публикацию и удаляет задание из Scheduler.

        Raises:
            PostNotFoundForUserError: пост не найден среди PENDING пользователя
                (не существует, чужой или уже не PENDING).
        """
        cancelled = await self._posts.cancel_post(post_id=post_id, user_id=user_id)
        if not cancelled:
            logger.info(
                "Cancel rejected post_id=%s user_id=%s: not own PENDING",
                post_id,
                user_id,
            )
            raise PostNotFoundForUserError(
                f"публикация {post_id} не найдена среди ваших ожидающих публикаций"
            )

        # БД уже зафиксировала отмену — ошибка удаления задания не должна
        # возвращать пост в PENDING (§7 SPEC-018).
        try:
            self._scheduler.remove_job(post_id)
        except Exception:
            logger.exception(
                "Scheduler cleanup failed post_id=%s (cancel already in DB)",
                post_id,
            )
        logger.info("Post cancelled post_id=%s user_id=%s", post_id, user_id)
