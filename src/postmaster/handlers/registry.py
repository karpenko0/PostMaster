"""Регистрация Telegram-обработчиков в боте.

Сервисы передаются в регистрацию явно, поэтому обработчики не создают зависимости сами.
На этапе SPEC-002 зарегистрирована команда /start. Остальные команды добавятся в SPEC-017…019.
"""

from telebot.async_telebot import AsyncTeleBot

from postmaster.handlers.start import register_start_handler
from postmaster.services.user_service import UserService


def register_handlers(bot: AsyncTeleBot, *, user_service: UserService) -> None:
    """Регистрирует все обработчики бота."""
    register_start_handler(bot, user_service)
