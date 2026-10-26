"""Обработчик команды /help (SPEC-019).

Handler отправляет пользователю справку о командах и workflow бота.
Команда работает в любом состоянии FSM — handler не читает и не меняет состояние.

Текст справки описывает только существующие функции (BR-019-01).
"""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from postmaster.utils.logging_config import EVENT_COMMAND_HELP, log_event

logger = logging.getLogger(__name__)

# Текст справки (BR-019-01: описывает только существующие команды).
HELP_TEXT = (
    "📖 PostMaster — помощник для отложенных публикаций в Telegram.\n\n"
    "Доступные команды:\n"
    "/start — начать создание публикации\n"
    "/my_posts — список ваших запланированных публикаций\n"
    "/cancel <post_id> — отменить запланированную публикацию\n"
    "/help — эта справка\n\n"
    "Как создать публикацию:\n"
    "1. Отправьте /start\n"
    "2. Пришлите фотографию\n"
    "3. Укажите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\n\n"
    "Формат даты и времени: ДД.ММ.ГГГГ ЧЧ:ММ\n"
    "Например: 25.12.2026 18:30\n\n"
    "Время указывается в вашем часовом поясе. После наступления времени бот "
    "автоматически опубликует фото в канале или отправит вам напоминание."
)


def register_help_handler(bot: AsyncTeleBot) -> None:
    """Регистрирует обработчик /help в боте.

    Должен регистрироваться ДО обработчиков текста (SPEC-003), иначе команда
    попадёт в DialogService.receive_text как обычный текст.
    """

    @bot.message_handler(commands=["help"], chat_types=["private"])
    async def handle_help(message: Message) -> None:
        sender = message.from_user
        telegram_user_id = sender.id if sender is not None else None
        log_event(
            logger,
            EVENT_COMMAND_HELP,
            command="help",
            telegram_user_id=telegram_user_id,
        )
        await bot.send_message(message.chat.id, HELP_TEXT)
