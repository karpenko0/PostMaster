"""MyPostsService: список PENDING-публикаций пользователя (SPEC-017, PM-04).

Сервис формирует текстовый список собственных PENDING-постов текущего пользователя,
отсортированных по времени публикации. Время форматируется в часовой пояс пользователя
(Post.timezone), тип публикации отображается человекочитаемо.

PM-04 расширяет сервис структурированным представлением: list_view() возвращает
список Post с признаком усечения (постов > 20), а per-post рендеринг выполняет
handler вместе с inline-кнопками отмены/переноса.

Не работает с Telegram API — только с PostRepository и UserService. Финальное
отображение пользователю делает handler.
"""

import logging
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo

from postmaster.domain.post import Post, PublicationType
from postmaster.services.user_service import UserService
from postmaster.utils.timezone_format import format_local_datetime

logger = logging.getLogger(__name__)


class PostListing(Protocol):
    """Порт для чтения постов пользователя."""

    async def list_pending_by_user(self, user_id: int) -> list[Post]: ...


class UserResolver(Protocol):
    """Порт для получения часового пояса пользователя."""

    async def get_by_telegram_id(self, telegram_user_id: int) -> object | None: ...


EMPTY_LIST = "У тебя пока нет запланированных публикаций."
HEADER = "Твои запланированные публикации:\n"
TRUNCATION_NOTICE = "Показаны первые 20 публикаций; остальные не отображены."

# Сколько постов показывать в одном сообщении /my_posts (PM-04 §3.2).
MY_POSTS_LIMIT = 20

# Текстовые ярлыки типа публикации (PM-04 §3.1).
_PUBLICATION_TYPE_LABELS: dict[PublicationType, str] = {
    PublicationType.TELEGRAM: "Публикация в канал",
    PublicationType.REMINDER: "Напоминание",
}

# Краткие ярлыки для обратной совместимости с SPEC-017.
_PUBLICATION_TYPE_SHORT: dict[PublicationType, str] = {
    PublicationType.TELEGRAM: "публикация в канал",
    PublicationType.REMINDER: "напоминание",
}


@dataclass(frozen=True, slots=True)
class MyPostsView:
    """Структурированное представление списка публикаций пользователя (PM-04)."""

    posts: tuple[Post, ...]
    hidden: int  # Сколько постов не показано из-за лимита MY_POSTS_LIMIT.

    @property
    def truncated(self) -> bool:
        return self.hidden > 0

    @property
    def is_empty(self) -> bool:
        return not self.posts


class MyPostsService:
    """Формирует список PENDING-публикаций пользователя (SPEC-017, PM-04)."""

    def __init__(
        self,
        post_repository: PostListing,
        user_service: UserService,
    ) -> None:
        self._posts = post_repository
        self._users = user_service

    async def render_list(self, telegram_user_id: int) -> str:
        """Возвращает текстовый список PENDING-публикаций пользователя (SPEC-017).

        Сохранён для обратной совместимости с тестами SPEC-017: новый handler /my_posts
        использует list_view() и рендерит блоки с inline-кнопками (PM-04).
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

        lines = ["Ваши запланированные публикации:\n"]
        for index, post in enumerate(posts, start=1):
            local_time = format_local_datetime(post.scheduled_at_utc, post.timezone)
            label = _PUBLICATION_TYPE_SHORT.get(
                post.publication_type, post.publication_type.value
            )
            lines.append(
                f"{index}. ID {post.id} — {local_time} ({label})\n"
            )
        return "".join(lines).rstrip() + "\n"

    async def list_view(self, telegram_user_id: int) -> MyPostsView:
        """Возвращает структурированный список PENDING-публикаций (PM-04).

        Применяет лимит MY_POSTS_LIMIT к видимым постам; остаток указывается в
        ``hidden``. Если пользователь не зарегистрирован — список пустой и
        усечение отсутствует.
        """
        user = await self._users.get_by_telegram_id(telegram_user_id)
        if user is None:
            logger.info(
                "my_posts.view: unknown telegram_user_id=%s — empty view",
                telegram_user_id,
            )
            return MyPostsView(posts=(), hidden=0)

        posts = await self._posts.list_pending_by_user(user.id)
        visible = posts[:MY_POSTS_LIMIT]
        hidden = max(0, len(posts) - MY_POSTS_LIMIT)
        return MyPostsView(posts=tuple(visible), hidden=hidden)


def render_post_block(post: Post) -> str:
    """PM-04 §3.1: рендер одного поста для сообщения с inline-кнопками.

    Помимо «Пост #N» сохраняет подстроку «ID N» для обратной совместимости
    с существующими тестами SPEC-017.
    """
    local = post.scheduled_at_utc.astimezone(ZoneInfo(post.timezone))
    date = f"{local.day:02d}.{local.month:02d}.{local.year:04d}"
    clock = f"{local.hour:02d}:{local.minute:02d}"
    type_label = _PUBLICATION_TYPE_LABELS.get(
        post.publication_type, post.publication_type.value
    )
    lines = [
        f"Пост #{post.id} (ID {post.id})",
        f"Тип: {type_label}",
        f"Дата: {date}",
        f"Время: {clock}",
    ]
    if post.publication_type == PublicationType.TELEGRAM and post.target_id:
        lines.append(f"Канал: {post.target_id}")
    if post.caption:
        lines.append(f"Подпись: {post.caption}")
    return "\n".join(lines)
