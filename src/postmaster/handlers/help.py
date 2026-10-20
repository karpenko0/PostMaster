"""Обработчик команды /help (SPEC-019)."""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "PostMaster помогает запланировать публикацию фотографии.\n\n"
    "Как создать публикацию:\n"
    "1. Отправьте фотографию.\n"
    "2. Укажите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.\n"
    "3. Дождитесь подтверждения.\n\n"
    "Пример даты: 07.10.2026 15:00\n\n"
    "В режиме напоминания бот пришлёт вам фотографию и напоминание.\n"
    "В режиме Telegram бот опубликует фотографию в указанном Telegram-канале.\n\n"
    "Команды:\n"
    "/start — начать работу\n"
    "/my_posts — показать запланированные публикации\n"
    "/cancel <post_id> — отменить публикацию, например /cancel 42\n"
    "/help — показать эту справку"
)


def register_help_handler(bot: AsyncTeleBot) -> None:
    """Регистрирует справку в личном чате; handler не читает и не меняет FSM."""

    @bot.message_handler(commands=["help"], chat_types=["private"])
    async def handle_help(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        logger.info("Help requested telegram_user_id=%s", sender.id)
        try:
            await bot.send_message(message.chat.id, HELP_TEXT)
        except Exception as exc:
            logger.error(
                "Help message send failed telegram_user_id=%s error_type=%s",
                sender.id,
                type(exc).__name__,
            )
