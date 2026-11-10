"""Обработчик команды /my_posts (SPEC-017, PM-04).

PM-04 расширяет handler:
1. Получает структурированный список PENDING-публикаций через MyPostsService.list_view.
2. Отправляет заголовок.
3. Для каждого поста (до 20) отправляет отдельное сообщение с блоком деталей
   и inline-клавиатурой «Отменить» / «Перенести» (PM-04 §3.1).
4. Если есть скрытые посты — отправляет предупреждение (PM-04 §3.2).
5. Если публикаций нет — отправляет EMPTY_LIST (PM-04 §3.2).

Handler не обращается к repositories и database — только к сервису.
Команда работает только в личных чатах (решение D14 плана SPEC-003).
"""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from postmaster.handlers.inline_keyboards import build_post_actions_keyboard
from postmaster.services.my_posts_service import (
    EMPTY_LIST,
    HEADER,
    TRUNCATION_NOTICE,
    MyPostsService,
    render_post_block,
)
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
        chat_id = message.chat.id
        try:
            view = await my_posts_service.list_view(sender.id)
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
            await bot.send_message(chat_id, MY_POSTS_FAILED)
            return

        if view.is_empty:
            await bot.send_message(chat_id, EMPTY_LIST)
        else:
            await bot.send_message(chat_id, HEADER.rstrip())
            for post in view.posts:
                await bot.send_message(
                    chat_id,
                    render_post_block(post),
                    reply_markup=build_post_actions_keyboard(post.id),
                )
            if view.truncated:
                await bot.send_message(chat_id, TRUNCATION_NOTICE)

        log_event(
            logger,
            EVENT_COMMAND_MY_POSTS,
            command="my_posts",
            telegram_user_id=sender.id,
            shown=len(view.posts),
            hidden=view.hidden,
        )
