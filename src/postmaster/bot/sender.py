"""Адаптер AsyncTeleBot к интерфейсам TelegramSender/TelegramReminderSender (SPEC-010)."""

from typing import Any

from telebot.async_telebot import AsyncTeleBot


class BotTelegramSender:
    """Реализация TelegramSender через pyTelegramBotAPI."""

    def __init__(self, bot: AsyncTeleBot) -> None:
        self._bot = bot

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> Any:
        return await self._bot.send_photo(chat_id, photo_file_id, caption=caption)


class BotTelegramReminderSender:
    """Реализация TelegramReminderSender через pyTelegramBotAPI.

    Поддерживает оба метода: send_message и send_photo (SPEC-014).
    """

    def __init__(self, bot: AsyncTeleBot) -> None:
        self._bot = bot

    async def send_message(self, *, chat_id: int, text: str) -> Any:
        return await self._bot.send_message(chat_id, text)

    async def send_photo(
        self, *, chat_id: int, photo_file_id: str, caption: str | None = None
    ) -> Any:
        return await self._bot.send_photo(chat_id, photo_file_id, caption=caption)
