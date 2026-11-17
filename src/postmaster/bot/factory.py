"""Создание клиента Telegram Bot API на pyTelegramBotAPI (асинхронный вариант)."""

from telebot import asyncio_helper
from telebot.async_telebot import AsyncTeleBot, ExceptionHandler
from telebot.asyncio_storage import StateMemoryStorage
from telebot.asyncio_storage.base_storage import StateStorageBase


def create_bot(
    token: str,
    *,
    state_storage: StateStorageBase | None = None,
    exception_handler: ExceptionHandler | None = None,
) -> AsyncTeleBot:
    """Создаёт асинхронного бота. Токен передаётся явно и не записывается в лог.

    Хранилище состояний создаётся для каждого бота. Общее хранилище по умолчанию
    смешивало бы состояния разных экземпляров (решение D9 плана SPEC-002).

    PM-06 / BUG-07: приложение передаёт персистентное хранилище (БД), поэтому состояние
    диалога переживает перезапуск. Значение по умолчанию — прежнее хранилище в памяти,
    чтобы отдельные сценарии (тесты, диагностика) не требовали БД.

    ``exception_handler`` нужен приложению, чтобы объяснить в логе конфликт Long
    Polling (409): без него причина «бот молчит» не видна (см. ``bot.transport``).
    """
    return AsyncTeleBot(
        token,
        state_storage=state_storage or StateMemoryStorage(),
        exception_handler=exception_handler,
    )


async def close_bot_session() -> None:
    """Закрывает HTTP-сессию pyTelegramBotAPI, если она была создана.

    Сессия создаётся при первом запросе к Telegram. Если запросов не было, закрывать нечего.
    """
    session = asyncio_helper.session_manager.session
    if session is None or session.closed:
        return
    await session.close()
