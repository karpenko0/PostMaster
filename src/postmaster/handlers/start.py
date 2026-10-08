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

logger = logging.getLogger(__name__)

START_GREETING = (
    "Здравствуйте, {first_name}! Я PostMaster, помогу запланировать публикацию.\n\n"
    "Отправьте фотографию, которую хотите опубликовать."
)


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
        except Exception:
            # Данные не меняются, состояние тоже (§7 п. 4). Ответа пользователю нет (решение D5).
            logger.exception("Не удалось зарегистрировать пользователя при /start")
            return
        await bot.send_message(
            message.chat.id,
            START_GREETING.format(first_name=sender.first_name),
        )
        # Диалог переходит в WAITING_PHOTO после приветствия: пользователь ждёт фото после ответа.
        await dialog_service.start(sender.id, message.chat.id)
