"""Обработчик команды /start (SPEC-002: US-01, US-02, AC-03, AC-04).

Обработчик берёт отправителя из сообщения, вызывает UserService и отвечает приветствием.
Правила регистрации находятся в сервисе (BR-01). Обработчик не обращается к repositories
и database.
"""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from postmaster.domain.user import UserState
from postmaster.services.user_service import UserService

logger = logging.getLogger(__name__)

START_GREETING = (
    "Здравствуйте, {first_name}! Я PostMaster, помогу запланировать публикацию.\n\n"
    "Отправьте фотографию, которую хотите опубликовать."
)


def register_start_handler(bot: AsyncTeleBot, user_service: UserService) -> None:
    """Регистрирует обработчик /start в боте."""

    @bot.message_handler(commands=["start"])
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
        # Состояние ставится после приветствия, потому что пользователь ждёт фото после ответа.
        await bot.set_state(sender.id, UserState.WAITING_PHOTO.value, message.chat.id)
