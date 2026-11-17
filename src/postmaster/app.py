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
from pathlib import Path

from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException

from postmaster.bot.dialog_store import TelebotDialogStore
from postmaster.bot.factory import close_bot_session, create_bot
from postmaster.bot.transport import PollingTransport, Transport
from postmaster.config import ConfigError, Settings, load_settings
from postmaster.database.backup import BackupError, create_backup
from postmaster.database.engine import (
    check_database,
    create_database_engine,
    create_session_factory,
)
from postmaster.database.health import probe_database
from postmaster.database.schema import create_schema
from postmaster.handlers.registry import register_handlers
from postmaster.publishers.base import BotTelegramClient
from postmaster.publishers.reminder import ReminderPublisher
from postmaster.publishers.resolver import PublisherResolver
from postmaster.publishers.telegram import TelegramPublisher
from postmaster.repositories.dialog_store import DatabaseDialogStore
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.cancel_service import CancelService
from postmaster.services.dialog_service import DialogService
from postmaster.services.health_service import HealthCheck, HealthReporter
from postmaster.services.missed_post_handler import MissedPostHandler
from postmaster.services.my_posts_service import MyPostsService
from postmaster.services.photo_service import PhotoService
from postmaster.services.post_service import PostService
from postmaster.services.publication_service import PublicationService
from postmaster.services.recovery_service import RecoveryService
from postmaster.services.reschedule_service import RescheduleService
from postmaster.services.scheduler_service import SchedulerService
from postmaster.services.user_service import UserService
from postmaster.utils.health import (
    HealthReportCorrupted,
    HealthReportStale,
    read_report,
)
from postmaster.utils.logging_config import (
    EVENT_STARTUP,
    EVENT_STARTUP_ERROR,
    configure_logging,
    log_event,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2

# Коды ответа Telegram, которые означают, что токен отвергнут.
_TOKEN_REJECTED_CODES = frozenset({401, 404})


class StartupError(RuntimeError):
    """Приложение не может запуститься. Текст ошибки не содержит секретов."""


class HybridDialogStore:
    """PM06-R2-007: БД — источник истины, telebot StateMemoryStorage — зеркало.

    Зеркало нужно для совместимости с существующими E2E-тестами/диагностикой,
    которые читают ``bot.get_state``. Все записи идут одной транзакцией в БД;
    после рестарта :meth:`restore` восстанавливает зеркало из БД.
    """

    def __init__(self, memory: TelebotDialogStore, persistent: DatabaseDialogStore) -> None:
        self._memory = memory
        self._persistent = persistent

    async def get_state(self, user_id: int, chat_id: int) -> str | None:
        return await self._persistent.get_state(user_id, chat_id)

    async def get_draft(self, user_id: int, chat_id: int) -> dict[str, str]:
        draft = await self._persistent.get_draft(user_id, chat_id)
        await self._mirror(user_id, chat_id, await self.get_state(user_id, chat_id), draft)
        return draft

    async def set_state(self, user_id: int, chat_id: int, state: str) -> None:
        await self.save(user_id, chat_id, state, await self.get_draft(user_id, chat_id))

    async def set_draft(self, user_id: int, chat_id: int, draft: dict[str, str]) -> None:
        state = await self.get_state(user_id, chat_id)
        await self.save(user_id, chat_id, state or "IDLE", draft)

    async def save(self, user_id: int, chat_id: int, state: str, draft: dict[str, str]) -> None:
        await self._persistent.save(user_id, chat_id, state, draft)
        await self._mirror(user_id, chat_id, state, draft)

    async def _mirror(
        self, user_id: int, chat_id: int, state: str | None, draft: dict[str, str]
    ) -> None:
        if state is None:
            return
        try:
            await self._memory.set_state(user_id, chat_id, state)
            await self._memory.set_draft(user_id, chat_id, draft)
        except Exception:
            logger.debug("Dialog state mirror failed user_id=%s", user_id, exc_info=True)

    def restore_snapshot(self) -> int:
        """Синхронно заполняет telebot-зеркало из БД при создании Application.

        Нужно для диагностики/старых E2E-тестов, которые читают bot.get_state до
        вызова startup(). Источник истины — БД; startup() повторяет restore() уже
        через асинхронный порт, поддерживающий любой SQLAlchemy-драйвер.
        """
        rows = self._persistent.load_all_sync()
        bot = self._memory._bot
        separator = bot.current_states.separator
        prefix = bot.current_states.prefix
        for user_id, chat_id, state, draft in rows:
            key = separator.join((prefix, str(bot.bot_id), str(chat_id), str(user_id)))
            bot.current_states.data[key] = {"state": state, "data": dict(draft)}
        return len(rows)

    async def restore(self) -> int:
        rows = await self._persistent.load_all()
        for user_id, chat_id, state, draft in rows:
            await self._mirror(user_id, chat_id, state, draft)
        if rows:
            logger.info("FSM restored after restart: %s dialogs", len(rows))
        return len(rows)


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
        health_reporter: HealthReporter | None = None,
    ) -> None:
        self._bot = bot
        self._engine = engine
        self._scheduler = scheduler
        self._transport = transport
        self._closed = False
        self._recovery_service: RecoveryService | None = None
        self._missed_post_handler: MissedPostHandler | None = None
        self._dialog_state_store: DatabaseDialogStore | None = None
        self._dialog_service: DialogService | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._health_reporter = health_reporter

    @property
    def bot(self) -> AsyncTeleBot:
        """Бот приложения. Нужен тестам и диагностике."""
        return self._bot

    @classmethod
    def from_settings(cls, settings: Settings) -> "Application":
        """Создаёт боевую конфигурацию из настроек. Сеть на этом шаге не используется."""
        engine = create_database_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        user_repository = UserRepository(session_factory)
        user_service = UserService(
            user_repository,
            default_timezone=settings.default_timezone,
        )
        post_repository = PostRepository(session_factory)
        bot = create_bot(settings.bot_token.get_secret_value(), settings.bot_proxy_url)
        telegram_client = BotTelegramClient(bot)
        reminder_publisher = ReminderPublisher(
            telegram_client=telegram_client,
            user_repository=user_repository,
            secrets=settings.secret_values,
        )
        telegram_publisher = TelegramPublisher(
            telegram_client=telegram_client,
            secrets=settings.secret_values,
        )
        publisher_resolver = PublisherResolver(
            reminder_publisher=reminder_publisher,
            telegram_publisher=telegram_publisher,
        )
        publication_service = PublicationService(
            post_repository,
            publisher_resolver=publisher_resolver,
        )
        scheduler = SchedulerService(post_repository, publication_service)

        # PM06-R2-008: единственная инициализация MissedPostHandler/RecoveryService.
        # Оба пути восстановления используют актуальный PublisherResolver через
        # PublicationService.execute(), поэтому типы публикаций из БД авторитетны.
        class _MissedExecutor:
            async def publish(self, post_id: int) -> bool:
                return await publication_service.execute(post_id)

        missed_post_handler = MissedPostHandler(post_repository, _MissedExecutor())
        recovery_service = RecoveryService(post_repository, scheduler, missed_post_handler)
        reschedule_service = RescheduleService(
            post_repository=post_repository,
            scheduler=scheduler,
            strict_scheduler=True,
        )
        dialog_store = HybridDialogStore(
            TelebotDialogStore(bot), DatabaseDialogStore(session_factory)
        )
        # Восстанавливаем диагностическое telebot-зеркало сразу после сборки.
        dialog_store.restore_snapshot()
        dialog_service = DialogService(
            dialog_store,
            user_service=user_service,
            post_service=PostService(
                post_repository,
                scheduler,
                telegram_target_allowlist=settings.telegram_target_allowlist,
            ),
            reschedule_service=reschedule_service,
            post_repository=post_repository,
        )
        my_posts_service = MyPostsService(
            post_repository=post_repository,
            user_service=user_service,
        )
        cancel_service = CancelService(
            post_repository=post_repository,
            scheduler=scheduler,
        )
        register_handlers(
            bot,
            user_service=user_service,
            dialog_service=dialog_service,
            photo_service=PhotoService(),
            my_posts_service=my_posts_service,
            cancel_service=cancel_service,
        )

        instance = cls(
            bot=bot,
            engine=engine,
            scheduler=scheduler,
            transport=PollingTransport(bot),
            health_reporter=HealthReporter(
                health_check=HealthCheck(
                    database_probe=lambda: probe_database(engine),
                    scheduler_status=scheduler,
                ),
                target=Path(settings.health_file),
            ),
        )
        instance._recovery_service = recovery_service
        instance._missed_post_handler = missed_post_handler
        instance._dialog_state_store = dialog_store._persistent
        instance._dialog_service = dialog_service
        return instance

    async def startup(self) -> None:
        """Проверяет БД, создаёт таблицы, восстанавливает задания и запускает Scheduler."""
        try:
            await check_database(self._engine)
            await create_schema(self._engine)
        except Exception:
            raise StartupError(
                "база данных недоступна или не готова, проверьте DATABASE_URL"
            ) from None
        await self.restore_fsm()
        self._scheduler.start()
        # Восстановление заданий выполняется после запуска Scheduler,
        # чтобы schedule_post мог регистрировать задания.
        if self._recovery_service is not None:
            report = await self._recovery_service.recover()
            if report.errors:
                logger.warning("Recovery completed with %s errors", len(report.errors))
        # Фоновый отчёт health check — после Scheduler и Recovery, чтобы первый
        # отчёт уже отражал запущенный Scheduler (SPEC-029).
        if self._health_reporter is not None:
            await self._health_reporter.start()
        # PM06-R2-003: согласованность БД и APScheduler без обязательного рестарта.
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="scheduler-reconcile"
        )

    async def restore_fsm(self) -> int:
        """PM06-R2-007: восстанавливает state/draft и чистит просроченные записи."""
        if self._dialog_state_store is None:
            return 0
        await self._dialog_state_store.purge_expired_async()
        hybrid = self._dialog_service._store if self._dialog_service is not None else None
        if hybrid is None or not hasattr(hybrid, "restore"):
            return 0
        return await hybrid.restore()

    async def _reconcile_loop(self) -> None:
        interval = getattr(self._scheduler, "reconcile_interval", 30)
        while True:
            await asyncio.sleep(max(1, int(interval)))
            try:
                await self._scheduler.reconcile_pending()
            except Exception:
                logger.exception("Scheduler reconciliation failed")

    async def run(self) -> None:
        """Запускает приложение и работает до вызова stop(). Ресурсы освобождаются всегда."""
        try:
            await self.startup()
            await self._verify_token()
            log_event(logger, EVENT_STARTUP)
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
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None
        if self._health_reporter is not None:
            await self._health_reporter.stop()
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
            code = exc.error_code
            log_event(
                logger,
                EVENT_STARTUP_ERROR,
                level=logging.ERROR,
                error_code=code if code in _TOKEN_REJECTED_CODES else "other",
            )
            if code in _TOKEN_REJECTED_CODES:
                raise StartupError("Telegram отклонил BOT_TOKEN, проверьте его значение") from None
            raise StartupError(f"Telegram вернул ошибку {code}") from None
        except Exception:
            log_event(logger, EVENT_STARTUP_ERROR, level=logging.ERROR, error="network")
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
    """Точка входа консной команды ``postmaster``."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG_ERROR) from None

    configure_logging(settings.log_level, secrets=settings.secret_values)
    try:
        exit_code = asyncio.run(_serve(settings))
    except KeyboardInterrupt:
        exit_code = EXIT_OK
    raise SystemExit(exit_code)


# === Health check CLI (SPEC-029) ============================================


EXIT_HEALTHY = 0
EXIT_UNHEALTHY = 1
EXIT_HEALTH_CONFIG_ERROR = 2


def backup_main(argv: list[str] | None = None) -> int:
    """Точка входа ``python -m postmaster backup``.

    Создаёт резервную копию SQLite и проверяет её читаемость (SPEC-030).

    Возвращает:
    - ``0`` — backup создан и проверен;
    - ``1`` — backup не создан или проверка не пройдена;
    - ``2`` — ошибка конфигурации.
    """
    args = _parse_backup_args(argv)

    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 — ConfigError или OSError от dotenv
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_HEALTH_CONFIG_ERROR

    source = args.source or _database_file_path(settings.database_url)
    if source is None:
        print(
            "Резервное копирование поддерживается только для SQLite (DATABASE_URL)",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY

    destination = args.destination or Path(settings.backup_path)

    try:
        report = create_backup(source, destination)
    except BackupError as exc:
        print(f"Ошибка backup: {exc}", file=sys.stderr)
        return EXIT_UNHEALTHY

    print(f"backup создан: {report.destination}")
    print(f"проверка целостности: {report.integrity}")
    print(f"строк: {report.row_counts}")
    print(f"длительность: {report.duration_seconds:.3f}s")
    return EXIT_HEALTHY


def _parse_backup_args(argv: list[str] | None) -> "_BackupArgs":
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m postmaster backup",
        description="Резервное копирование SQLite (SPEC-030).",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Путь к файлу SQLite (по умолчанию из DATABASE_URL).",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=None,
        help="Путь к файлу backup (по умолчанию из BACKUP_PATH).",
    )
    parsed = parser.parse_args(argv)
    return _BackupArgs(source=parsed.source, destination=parsed.destination)


class _BackupArgs:
    __slots__ = ("source", "destination")

    def __init__(self, *, source, destination) -> None:
        self.source = source
        self.destination = destination


def _database_file_path(database_url: str) -> Path | None:
    """Возвращает путь к файлу SQLite по строке подключения.

    Для не-SQLite баз (PostgreSQL и др.) возвращает None: online backup
    реализован через sqlite3 и к PostgreSQL неприменим.
    """
    try:
        url = make_url(database_url)
    except ArgumentError:
        return None
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database)


def health_main(argv: list[str] | None = None) -> int:
    """Точка входа ``python -m postmaster health``.

    Читает JSON-отчёт, который пишет ``HealthReporter`` в фоне работающего
    приложения. Возвращает:

    - ``0`` — отчёт healthy;
    - ``1`` — unhealthy / устарел / повреждён / отсутствует;
    - ``2`` — ошибка конфигурации.
    """
    args = _parse_health_args(argv)

    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 — ConfigError или OSError от dotenv
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_HEALTH_CONFIG_ERROR

    target = args.health_file or Path(settings.health_file)
    max_age = (
        float(args.max_age_seconds)
        if args.max_age_seconds is not None
        else float(settings.health_max_age_seconds)
    )

    return _check_health_file(target, max_age, args.print_report)


def _parse_health_args(argv: list[str] | None) -> "_HealthArgs":
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m postmaster health",
        description="Health check для PostMaster (SPEC-029).",
    )
    parser.add_argument(
        "--health-file",
        type=Path,
        default=None,
        help="Путь к файлу отчёта (по умолчанию из HEALTH_FILE).",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=None,
        help="Максимальный возраст отчёта в секундах (HEALTH_MAX_AGE_SECONDS).",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="Печатать полный JSON-отчёт вместо краткого summary.",
    )
    parsed = parser.parse_args(argv)
    return _HealthArgs(
        health_file=parsed.health_file,
        max_age_seconds=parsed.max_age_seconds,
        print_report=parsed.print,
    )


class _HealthArgs:
    __slots__ = ("health_file", "max_age_seconds", "print_report")

    def __init__(self, *, health_file, max_age_seconds, print_report) -> None:
        self.health_file = health_file
        self.max_age_seconds = max_age_seconds
        self.print_report = print_report


def _check_health_file(target: Path, max_age_seconds: float, print_report: bool) -> int:
    """Читает отчёт, печатает (опционально), возвращает код завершения."""
    try:
        report = read_report(target, max_age_seconds=max_age_seconds)
    except HealthReportCorrupted as exc:
        print(f"unhealthy: corrupted report ({exc})", file=sys.stderr)
        return EXIT_UNHEALTHY
    except HealthReportStale as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return EXIT_UNHEALTHY

    if report is None:
        print(
            f"unhealthy: отчёт не найден ({target}); приложение не запущено или не успело записать",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY

    if print_report:
        import json

        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(report.summary(), file=sys.stderr if not report.healthy else sys.stdout)

    return EXIT_HEALTHY if report.healthy else EXIT_UNHEALTHY
