"""MyPostsService: список PENDING-публикаций пользователя (SPEC-017).

Сервис формирует текстовый список собственных PENDING-постов текущего пользователя,
отсортированных по времени публикации. Время форматируется в часовой пояс пользователя
(Post.timezone), тип публикации отображается человекочитаемо.

Не работает с Telegram API — только с PostRepository и UserService. Финальное
отображение пользователю делает handler.
"""

import logging
from typing import Protocol

from postmaster.domain.post import Post, PublicationType
from postmaster.services.user_service import UserService
from postmaster.utils.timezone_format import format_local_datetime

logger = logging.getLogger(__name__)


class PostListing(Protocol):
    """Порт для чтения постов пользователя.

    Задаётся отдельно от PostRepository, чтобы не зависеть от всего репозитория
    и упростить подмену в тестах.
    """

    async def list_pending_by_user(self, user_id: int) -> list[Post]: ...


class UserResolver(Protocol):
    """Порт для получения часового пояса пользователя."""

    async def get_by_telegram_id(self, telegram_user_id: int) -> object | None: ...


EMPTY_LIST = "У вас нет запланированных публикаций."
HEADER = "Ваши запланированные публикации:\n\n"
ITEM_TEMPLATE = "{index}. ID {post_id} — {local_time} ({publication_type})\n"

_PUBLICATION_TYPE_LABELS: dict[PublicationType, str] = {
    PublicationType.TELEGRAM: "публикация в канал",
    PublicationType.REMINDER: "напоминание",
}


class MyPostsService:
    """Формирует список PENDING-публикаций пользователя (SPEC-017)."""

    def __init__(
        self,
        post_repository: PostListing,
        user_service: UserService,
    ) -> None:
        self._posts = post_repository
        self._users = user_service

    async def render_list(self, telegram_user_id: int) -> str:
        """Возвращает текстовый список PENDING-публикаций пользователя.

        Пользователь определяется по telegram_user_id (стабильный идентификатор).
        Если пользователь не зарегистрирован — возвращается EMPTY_LIST.
        Чужие посты никогда не возвращаются: фильтрация по user_id в SQL.
        """
        user = await self._users.get_by_telegram_id(telegram_user_id)
        if user is None:
            logger.info(
                "my_posts: unknown telegram_user_id=%s — returning empty list",
                telegram_user_id,
            )
            return EMPTY_LIST

        posts = await self._posts.list_pending_by_user(user.id)
        if not posts:
            return EMPTY_LIST

        return self._format_list(posts)

    @staticmethod
    def _format_list(posts: list[Post]) -> str:
        """Форматирует список постов в текст для отправки пользователю."""
        lines = [HEADER]
        for index, post in enumerate(posts, start=1):
            local_time = format_local_datetime(post.scheduled_at_utc, post.timezone)
            label = _PUBLICATION_TYPE_LABELS.get(post.publication_type, post.publication_type.value)
            lines.append(
                ITEM_TEMPLATE.format(
                    index=index,
                    post_id=post.id,
                    local_time=local_time,
                    publication_type=label,
                )
            )
        return "".join(lines).rstrip() + "\n"
