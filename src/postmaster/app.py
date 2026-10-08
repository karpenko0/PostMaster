"""Точка входа PostMaster (AC-03): сборка приложения, жизненный цикл и сигналы ОС.

Запуск: ``python -m postmaster`` или команда ``postmaster`` после установки пакета.
Коды завершения: 0 — штатная остановка по сигналу, 1 — ошибка во время запуска или работы,
2 — некорректная конфигурация.
"""

import asyncio
import contextlib
import logging
import signal
import sys

from sqlalchemy.ext.asyncio import AsyncEngine
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException

from postmaster.bot.dialog_store import TelebotDialogStore
from postmaster.bot.factory import close_bot_session, create_bot
from postmaster.bot.transport import PollingTransport, Transport
from postmaster.config import ConfigError, Settings, load_settings
from postmaster.database.engine import (
    check_database,
    create_database_engine,
    create_session_factory,
)
from postmaster.database.schema import create_schema
from postmaster.handlers.registry import register_handlers
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.dialog_service import DialogService
from postmaster.services.post_service import PostService
from postmaster.services.scheduler_service import SchedulerService
from postmaster.services.user_service import UserService
from postmaster.utils.logging_config import configure_logging

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2

# Коды ответа Telegram, которые означают, что токен отвергнут.
_TOKEN_REJECTED_CODES = frozenset({401, 404})


class StartupError(RuntimeError):
    """Приложение не может запуститься. Текст ошибки не содержит секретов."""


class Application:
    """Собирает компоненты и управляет их жизненным циклом.

    Зависимости передаются явно, поэтому тесты подменяют их без сети и без файлов.
    """

    def __init__(
        self,
        *,
        bot: AsyncTeleBot,
        engine: AsyncEngine,
        scheduler: SchedulerService,
        transport: Transport,
    ) -> None:
        self._bot = bot
        self._engine = engine
        self._scheduler = scheduler
        self._transport = transport
        self._closed = False

    @property
    def bot(self) -> AsyncTeleBot:
        """Бот приложения. Нужен тестам и диагностике."""
        return self._bot

    @classmethod
    def from_settings(cls, settings: Settings) -> "Application":
        """Создаёт боевую конфигурацию из настроек. Сеть на этом шаге не используется."""
        engine = create_database_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        user_service = UserService(
            UserRepository(session_factory),
            default_timezone=settings.default_timezone,
        )
        bot = create_bot(settings.bot_token.get_secret_value())
        dialog_service = DialogService(
            TelebotDialogStore(bot),
            user_service=user_service,
            post_service=PostService(),
        )
        register_handlers(bot, user_service=user_service, dialog_service=dialog_service)
        return cls(
            bot=bot,
            engine=engine,
            scheduler=SchedulerService(),
            transport=PollingTransport(bot),
        )

    async def startup(self) -> None:
        """Проверяет доступность БД, создаёт недостающие таблицы и запускает Scheduler."""
        try:
            await check_database(self._engine)
            await create_schema(self._engine)
        except Exception:
            raise StartupError(
                "база данных недоступна или не готова, проверьте DATABASE_URL"
            ) from None
        self._scheduler.start()

    async def run(self) -> None:
        """Запускает приложение и работает до вызова stop(). Ресурсы освобождаются всегда."""
        try:
            await self.startup()
            await self._verify_token()
            logger.info("PostMaster запущен")
            await self._transport.run()
        finally:
            await self.shutdown()

    async def stop(self) -> None:
        """Запрашивает остановку. Безопасно вызывать в любой момент, в том числе до run()."""
        await self._transport.stop()

    async def shutdown(self) -> None:
        """Останавливает Scheduler и закрывает пул соединений. Повторный вызов ничего не делает."""
        if self._closed:
            return
        self._closed = True
        self._scheduler.shutdown()
        await close_bot_session()
        await self._engine.dispose()
        logger.info("PostMaster остановлен")

    async def _verify_token(self) -> None:
        """Проверяет токен запросом getMe.

        Текст исключений не выводится: сетевые ошибки содержат URL с токеном.
        """
        try:
            me = await self._bot.get_me()
        except ApiTelegramException as exc:
            if exc.error_code in _TOKEN_REJECTED_CODES:
                raise StartupError("Telegram отклонил BOT_TOKEN, проверьте его значение") from None
            raise StartupError(f"Telegram вернул ошибку {exc.error_code}") from None
        except Exception:
            raise StartupError("Telegram API недоступен, проверьте сеть") from None
        logger.info("Бот подключён: @%s", me.username)


async def _serve(settings: Settings) -> int:
    """Запускает приложение и возвращает код завершения. Остановка идёт по SIGINT/SIGTERM."""
    try:
        app = Application.from_settings(settings)
    except Exception:
        logger.exception("Не удалось подготовить приложение")
        return EXIT_RUNTIME_ERROR

    stop_requested = asyncio.Event()
    _install_signal_handlers(stop_requested)
    stopper = asyncio.create_task(_stop_when_requested(stop_requested, app))
    try:
        await app.run()
    except StartupError as exc:
        logger.error("Запуск невозможен: %s", exc)
        return EXIT_RUNTIME_ERROR
    except Exception:
        logger.exception("Приложение завершилось с ошибкой")
        return EXIT_RUNTIME_ERROR
    finally:
        stopper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stopper
    return EXIT_OK


def _install_signal_handlers(stop_requested: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_requested.set)
        except NotImplementedError:
            # Windows не поддерживает обработчики сигналов в event loop.
            # Ctrl+C тогда прерывает asyncio.run, и run() освобождает ресурсы в finally.
            return


async def _stop_when_requested(stop_requested: asyncio.Event, app: Application) -> None:
    await stop_requested.wait()
    logger.info("Получен сигнал остановки")
    await app.stop()


def main() -> None:
    """Точка входа консольной команды ``postmaster``."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG_ERROR) from None

    configure_logging(settings.log_level, secrets=[settings.bot_token.get_secret_value()])
    try:
        exit_code = asyncio.run(_serve(settings))
    except KeyboardInterrupt:
        exit_code = EXIT_OK
    raise SystemExit(exit_code)
