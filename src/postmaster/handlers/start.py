"""Обработчик команды /start (SPEC-002: US-01, US-02, AC-03, AC-04; SPEC-003: BR-01).

Обработчик берёт отправителя из сообщения, регистрирует пользователя через UserService,
отвечает приветствием и переводит диалог в WAITING_PHOTO через DialogService. Правила находятся
в сервисах (BR-01). Обработчик не обращается к repositories и database. Команда работает только
в личных чатах (решение D14 плана SPEC-003).
"""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from postmaster.services.dialog_service import DialogService
from postmaster.services.user_service import UserService
from postmaster.utils.logging_config import EVENT_COMMAND_FAILED, EVENT_COMMAND_START, log_event

logger = logging.getLogger(__name__)

START_GREETING = (
    "Здравствуйте, {first_name}! Я PostMaster, помогу запланировать публикацию.\n\n"
    "Отправьте фотографию, которую хотите опубликовать."
)
START_GREETING_NO_NAME = (
    "Здравствуйте! Я PostMaster, помогу запланировать публикацию.\n\n"
    "Отправьте фотографию, которую хотите опубликовать."
)
START_FAILED = "Не удалось обработать /start. Попробуйте ещё раз немного позже."


def register_start_handler(
    bot: AsyncTeleBot,
    user_service: UserService,
    dialog_service: DialogService,
) -> None:
    """Регистрирует обработчик /start в боте."""

    @bot.message_handler(commands=["start"], chat_types=["private"])
    async def handle_start(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        try:
            await user_service.get_or_create(sender.id, sender.username, sender.first_name)
        except Exception as exc:
            # Ошибка SQLAlchemy может включать username/first_name в тексте и аргументах SQL.
            # Логируем только тип, не traceback и не строку исключения (ERR-007-01, §50).
            log_event(
                logger,
                EVENT_COMMAND_FAILED,
                level=logging.ERROR,
                command="start",
                telegram_user_id=sender.id,
                error_type=type(exc).__name__,
            )
            await bot.send_message(message.chat.id, START_FAILED)
            return
        log_event(
            logger,
            EVENT_COMMAND_START,
            command="start",
            telegram_user_id=sender.id,
        )
        await bot.send_message(
            message.chat.id,
            START_GREETING.format(first_name=sender.first_name)
            if sender.first_name
            else START_GREETING_NO_NAME,
        )
        # Диалог переходит в WAITING_PHOTO после приветствия: пользователь ждёт фото после ответа.
        await dialog_service.start(sender.id, message.chat.id)
