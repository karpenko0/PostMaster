"""Транспорт получения обновлений Telegram (BR-05, п. 19 SPEC-001).

MVP работает через Long Polling. Переход на Webhook — это новая реализация протокола
Transport. Обработчики и сервисы при этом не меняются.
"""

import asyncio
import logging
from typing import Protocol, runtime_checkable

from telebot.async_telebot import AsyncTeleBot, ExceptionHandler
from telebot.asyncio_helper import ApiTelegramException

from postmaster.utils.logging_config import log_event

logger = logging.getLogger(__name__)

POLLING_TIMEOUT_SECONDS = 20

# Telegram отвечает этим кодом, когда токен занят webhook или другим экземпляром.
CONFLICT_ERROR_CODE = 409
# Полный текст конфликта содержит описание конфликта, но не секретов.
CONFLICT_EVENT = "polling_conflict"


class PollingConflictAlerter(ExceptionHandler):
    """Объясняет в логе конфликт Long Polling вместо бесконечных ошибок библиотеки.

    Библиотека при 409 Conflict не падает: она пишет ERROR и повторяет запросы с
    нарастающей паузой (до 60 с). Снаружи бот «запущен, но не отвечает», а по логу
    причина не видна. Здесь конфликт логируется один раз, чтобы диагноз был
    очевиден, а остальные ошибки библиотека обрабатывает как обычно.

    Проверка webhook на старте (``Application._check_transport_conflicts``) ловит
    первый случай. Второй — занятый другим процессом токен — проявляется только
    здесь: пробный ``getUpdates`` недопустим, он сам вычитывает апдейты.
    """

    def __init__(self) -> None:
        self._reported = False

    async def handle(self, exception: BaseException) -> bool:
        if not isinstance(exception, ApiTelegramException):
            return False
        if exception.error_code != CONFLICT_ERROR_CODE or self._reported:
            return False
        self._reported = True
        log_event(
            logger,
            CONFLICT_EVENT,
            level=logging.ERROR,
            error_code=CONFLICT_ERROR_CODE,
        )
        logger.error(
            "Long Polling получает 409 Conflict: у бота задан webhook или токен уже "
            "используется другим процессом. Пока конфликт не устранён, сообщения "
            "не обрабатываются. Проверьте getWebhookInfo и запущенные копии бота."
        )
        return False


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
