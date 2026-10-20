"""Запросы пользовательских публикаций (SPEC-017)."""

import logging

from postmaster.domain.post import Post
from postmaster.repositories.post_repository import PostRepository
from postmaster.services.user_service import UserService

logger = logging.getLogger(__name__)


class PostQueryService:
    """Возвращает ожидающие публикации только зарегистрированного пользователя."""

    def __init__(self, user_service: UserService, post_repository: PostRepository) -> None:
        self._users = user_service
        self._posts = post_repository

    async def get_pending_posts_for_user(self, telegram_user_id: int) -> list[Post] | None:
        """Возвращает список PENDING-постов или None, если пользователь не найден.

        Фильтрация по внутреннему users.id и статусу выполняется в SQL-запросе
        PostRepository, не после загрузки данных в Telegram handler.
        """
        user = await self._users.get_by_telegram_id(telegram_user_id)
        if user is None:
            logger.info("My posts requested user_not_found telegram_user_id=%s", telegram_user_id)
            return None

        logger.info("My posts requested user_id=%s", user.id)
        posts = await self._posts.list_pending_by_user(user.id)
        # Сохраняем детерминированную сортировку и для альтернативных реализаций репозитория.
        ordered = sorted(posts, key=lambda post: (post.scheduled_at_utc, post.id))
        logger.info("My posts returned user_id=%s count=%s", user.id, len(ordered))
        return ordered
