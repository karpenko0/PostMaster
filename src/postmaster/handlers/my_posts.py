"""Обработчик /my_posts: список собственных ожидающих публикаций (SPEC-017)."""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from postmaster.domain.post import Post, PublicationType
from postmaster.domain.timezone import convert_utc_to_local
from postmaster.services.post_query_service import PostQueryService

logger = logging.getLogger(__name__)

USER_NOT_FOUND_TEXT = "Не удалось найти данные пользователя. Используйте /start."
POSTS_LOAD_FAILED_TEXT = "Не удалось загрузить запланированные публикации.\nПопробуйте позже."
NO_PENDING_POSTS_TEXT = "У вас нет запланированных публикаций."
MY_POSTS_HEADER = "Ваши запланированные публикации:\n\n"
_TYPE_LABELS = {
    PublicationType.REMINDER: "Напоминание",
    PublicationType.TELEGRAM: "Telegram",
}
# Оставляем запас до ограничения Telegram в 4096 символов на сообщение.
_MAX_MESSAGE_LENGTH = 3500


def format_post(post: Post) -> str:
    """Форматирует обязательные поля Post, показывая время в часовом поясе записи."""
    local = convert_utc_to_local(post.scheduled_at_utc, post.timezone)
    label = _TYPE_LABELS[post.publication_type]
    return f"#{post.id}\n📅 {local:%d.%m.%Y %H:%M}\n📌 Тип: {label}"


def format_post_messages(posts: list[Post]) -> list[str]:
    """Формирует одно или несколько сообщений, не нарушая порядок публикаций."""
    if not posts:
        return [NO_PENDING_POSTS_TEXT]

    messages: list[str] = []
    current = MY_POSTS_HEADER
    for post in posts:
        item = f"{format_post(post)}\n\n"
        if len(current) + len(item) > _MAX_MESSAGE_LENGTH and current != MY_POSTS_HEADER:
            messages.append(current.rstrip())
            current = ""
        current += item
    if current:
        messages.append(current.rstrip())
    return messages


def register_my_posts_handler(bot: AsyncTeleBot, post_query_service: PostQueryService) -> None:
    """Регистрирует команду /my_posts только для личных чатов."""

    @bot.message_handler(commands=["my_posts"], chat_types=["private"])
    async def handle_my_posts(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        try:
            posts = await post_query_service.get_pending_posts_for_user(sender.id)
        except Exception as exc:
            logger.error(
                "My posts loading failed telegram_user_id=%s error_type=%s",
                sender.id,
                type(exc).__name__,
            )
            await bot.send_message(message.chat.id, POSTS_LOAD_FAILED_TEXT)
            return

        if posts is None:
            await bot.send_message(message.chat.id, USER_NOT_FOUND_TEXT)
            return

        for text in format_post_messages(posts):
            await bot.send_message(message.chat.id, text)
