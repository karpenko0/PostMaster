"""Адаптер AsyncTeleBot к интерфейсам TelegramSender/TelegramReminderSender (SPEC-010)."""

from telebot.async_telebot import AsyncTeleBot


class BotTelegramSender:
    """Реализация TelegramSender через pyTelegramBotAPI."""

    def __init__(self, bot: AsyncTeleBot) -> None:
        self._bot = bot

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> None:
        await self._bot.send_photo(chat_id, photo_file_id, caption=caption)


class BotTelegramReminderSender:
    """Реализация TelegramReminderSender через pyTelegramBotAPI."""

    def __init__(self, bot: AsyncTeleBot) -> None:
        self._bot = bot

    async def send_message(self, *, chat_id: int, text: str) -> None:
        await self._bot.send_message(chat_id, text)
