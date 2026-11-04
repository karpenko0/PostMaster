"""Обработчик команды /my_posts (SPEC-017).

Handler:
1. Получает telegram_user_id из message.from_user.
2. Вызывает MyPostsService.render_list(telegram_user_id).
3. Отправляет результат пользователю.

Handler не обращается к repositories и database — только к сервису.
Команда работает только в личных чатах (решение D14 плана SPEC-003).
"""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from postmaster.services.my_posts_service import MyPostsService
from postmaster.utils.logging_config import (
    EVENT_COMMAND_FAILED,
    EVENT_COMMAND_MY_POSTS,
    log_event,
)

logger = logging.getLogger(__name__)

MY_POSTS_FAILED = "Не удалось получить список публикаций. Попробуйте позже."


def register_my_posts_handler(
    bot: AsyncTeleBot,
    my_posts_service: MyPostsService,
) -> None:
    """Регистрирует обработчик /my_posts в боте."""

    @bot.message_handler(commands=["my_posts"], chat_types=["private"])
    async def handle_my_posts(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        try:
            text = await my_posts_service.render_list(sender.id)
        except Exception:
            log_event(
                logger,
                EVENT_COMMAND_FAILED,
                level=logging.ERROR,
                command="my_posts",
                telegram_user_id=sender.id,
            )
            logger.exception(
                "my_posts failed telegram_user_id=%s",
                sender.id,
            )
            await bot.send_message(message.chat.id, MY_POSTS_FAILED)
            return
        log_event(
            logger,
            EVENT_COMMAND_MY_POSTS,
            command="my_posts",
            telegram_user_id=sender.id,
        )
        await bot.send_message(message.chat.id, text)
