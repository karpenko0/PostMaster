"""Отмена собственной ожидающей публикации (SPEC-018)."""

import logging
from enum import StrEnum
from typing import Protocol

from postmaster.domain.post import Post, PostStatus
from postmaster.repositories.post_repository import PostRepository
from postmaster.services.user_service import UserService

logger = logging.getLogger(__name__)


class PostScheduler(Protocol):
    def remove_post(self, post_id: int) -> bool: ...


class CancelOutcome(StrEnum):
    """Публичные исходы cancel-flow; чужие и несуществующие записи не различаются."""

    CANCELLED = "CANCELLED"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    NOT_CANCELLABLE = "POST_NOT_CANCELLABLE"
    PROCESSING = "PROCESSING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    ALREADY_CANCELLED = "ALREADY_CANCELLED"


class PostCancellationService:
    """Атомарно отменяет собственный PENDING и затем best-effort снимает job Scheduler."""

    def __init__(
        self,
        user_service: UserService,
        post_repository: PostRepository,
        scheduler_service: PostScheduler,
    ) -> None:
        self._users = user_service
        self._posts = post_repository
        self._scheduler = scheduler_service

    async def cancel(self, telegram_user_id: int, post_id: int) -> CancelOutcome:
        """Атомарно выполняет PENDING → CANCELLED и возвращает безопасный результат."""
        user = await self._users.get_by_telegram_id(telegram_user_id)
        if user is None:
            logger.info(
                "Post cancel rejected telegram_user_id=%s post_id=%s", telegram_user_id, post_id
            )
            return CancelOutcome.USER_NOT_FOUND

        logger.info("Cancel requested user_id=%s post_id=%s", user.id, post_id)
        if await self._posts.cancel_pending_post(post_id, user.id):
            logger.info("Post cancelled user_id=%s post_id=%s", user.id, post_id)
            try:
                self._scheduler.remove_post(post_id)
            except Exception as exc:
                # БД — источник истины; остаточный job не пройдёт atomic claim в PublicationService.
                logger.warning(
                    "Scheduler job removal failed post_id=%s error_type=%s",
                    post_id,
                    type(exc).__name__,
                )
            return CancelOutcome.CANCELLED

        # Читаем состояние только для выбора понятного ответа. Право на изменение уже
        # проверено единственным условным UPDATE по id, владельцу и PENDING.
        post = await self._posts.get_by_id(post_id)
        if post is None or post.user_id != user.id:
            logger.info("Post cancel rejected user_id=%s post_id=%s", user.id, post_id)
            return CancelOutcome.NOT_CANCELLABLE

        outcome = self._outcome_for_status(post)
        logger.info(
            "Post cancel rejected user_id=%s post_id=%s status=%s", user.id, post_id, post.status
        )
        return outcome

    @staticmethod
    def _outcome_for_status(post: Post) -> CancelOutcome:
        return {
            PostStatus.PROCESSING: CancelOutcome.PROCESSING,
            PostStatus.PUBLISHED: CancelOutcome.PUBLISHED,
            PostStatus.FAILED: CancelOutcome.FAILED,
            PostStatus.CANCELLED: CancelOutcome.ALREADY_CANCELLED,
        }.get(post.status, CancelOutcome.NOT_CANCELLABLE)
