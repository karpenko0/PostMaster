"""Транспорт получения обновлений Telegram (BR-05, п. 19 SPEC-001).

MVP работает через Long Polling. Переход на Webhook — это новая реализация протокола
Transport. Обработчики и сервисы при этом не меняются.
"""

import asyncio
import logging
from typing import Protocol, runtime_checkable

from telebot.async_telebot import AsyncTeleBot

logger = logging.getLogger(__name__)

POLLING_TIMEOUT_SECONDS = 20


@runtime_checkable
class Transport(Protocol):
    """Источник обновлений. run() работает до остановки, stop() её запрашивает."""

    async def run(self) -> None: ...

    async def stop(self) -> None: ...


class PollingTransport:
    """Long Polling через AsyncTeleBot.

    pyTelegramBotAPI 4.x не предоставляет публичного метода остановки. Опрос
    останавливается отменой задачи. Библиотека перехватывает CancelledError внутри
    цикла, закрывает HTTP-сессию в блоке finally и возвращает управление.
    """

    def __init__(self, bot: AsyncTeleBot, timeout: int = POLLING_TIMEOUT_SECONDS) -> None:
        self._bot = bot
        self._timeout = timeout
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = False

    async def run(self) -> None:
        """Запускает опрос и ждёт его завершения. Если stop() уже вызван, сразу возвращается."""
        if self._stop_requested:
            return
        self._task = asyncio.create_task(
            self._bot.polling(non_stop=True, timeout=self._timeout),
            name="telegram-long-polling",
        )
        logger.info("Long Polling запущен")
        await self._task

    async def stop(self) -> None:
        """Запрашивает остановку опроса. Метод не выбрасывает исключений опроса."""
        self._stop_requested = True
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.wait([task])
        logger.info("Long Polling остановлен")
