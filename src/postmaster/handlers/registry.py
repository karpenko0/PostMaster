"""Регистрация Telegram-обработчиков в боте.

На этапе SPEC-001 обработчиков нет. Команды добавляются в SPEC-002 и SPEC-017…019.
"""

from telebot.async_telebot import AsyncTeleBot


def register_handlers(bot: AsyncTeleBot) -> None:
    """Регистрирует обработчики в боте. На этапе SPEC-001 ничего не регистрирует."""
