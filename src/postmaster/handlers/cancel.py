"""Обработчик команды /cancel (SPEC-018).

Handler:
1. Парсит post_id из текста команды (после /cancel через пробел).
2. Получает telegram_user_id из message.from_user.
3. Находит внутренний user_id через UserService.
4. Вызывает CancelService.cancel(post_id, user_id).
5. Отправляет подтверждение или ошибку.

Handler не обращается к repositories и database — только к сервисам.
Команда работает только в личных чатах (решение D14 плана SPEC-003).
"""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from postmaster.services.cancel_service import CancelService, PostNotFoundForUserError
from postmaster.services.user_service import UserService
from postmaster.utils.logging_config import (
    EVENT_COMMAND_CANCEL,
    EVENT_COMMAND_FAILED,
    log_event,
)

logger = logging.getLogger(__name__)

CANCEL_USAGE = "Использование: /cancel <post_id> (узнайте ID через /my_posts)"
CANCEL_NOT_FOUND = "Публикация не найдена среди ваших ожидающих. Проверьте ID."
CANCEL_FAILED = "Не удалось отменить публикацию. Попробуйте позже."
CANCEL_OK = "Публикация ID {post_id} отменена."

INVALID_ID_TEXT = "ID публикации должен быть положительным целым числом."


def register_cancel_handler(
    bot: AsyncTeleBot,
    user_service: UserService,
    cancel_service: CancelService,
) -> None:
    """Регистрирует обработчик /cancel в боте."""

    @bot.message_handler(commands=["cancel"], chat_types=["private"])
    async def handle_cancel(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await bot.send_message(message.chat.id, CANCEL_USAGE)
            return
        raw_id = parts[1].strip()
        if not raw_id.isdigit() or int(raw_id) <= 0:
            await bot.send_message(message.chat.id, INVALID_ID_TEXT)
            return
        post_id = int(raw_id)

        try:
            user = await user_service.get_by_telegram_id(sender.id)
            if user is None:
                await bot.send_message(message.chat.id, CANCEL_NOT_FOUND)
                return
            await cancel_service.cancel(post_id=post_id, user_id=user.id)
        except PostNotFoundForUserError:
            await bot.send_message(message.chat.id, CANCEL_NOT_FOUND)
            return
        except Exception:
            log_event(
                logger,
                EVENT_COMMAND_FAILED,
                level=logging.ERROR,
                command="cancel",
                post_id=post_id,
                telegram_user_id=sender.id,
            )
            logger.exception("Cancel failed post_id=%s telegram_user_id=%s", post_id, sender.id)
            await bot.send_message(message.chat.id, CANCEL_FAILED)
            return
        log_event(
            logger,
            EVENT_COMMAND_CANCEL,
            command="cancel",
            post_id=post_id,
            telegram_user_id=sender.id,
        )
        await bot.send_message(message.chat.id, CANCEL_OK.format(post_id=post_id))
