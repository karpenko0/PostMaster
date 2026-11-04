"""SPEC-025: Логирование.

Проверки §8:
- events: канонические события startup, post_create, scheduler, recovery, claim,
  publish, retry, commands и errors логируются в структурированном виде;
- error_code/post_id: события публикации содержат post_id, ошибки — error_code;
- token redaction: токен не попадает в лог, в том числе в значениях полей;
- caption protection: полный caption не логируется.

§7: логирование не меняет бизнес-state.
"""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.publishers import DefaultPublisherResolver
from postmaster.publishers.telegram_errors import TelegramErrorMapper
from postmaster.publishers.telegram_publisher import TelegramChannelPublisher
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import (
    PublicationError,
    PublicationService,
    RetryPolicy,
)
from postmaster.services.user_service import UserService
from postmaster.utils.logging_config import (
    EVENT_COMMAND_CANCEL,
    EVENT_COMMAND_START,
    EVENT_STARTUP,
    MASK,
    caption_for_log,
    log_event,
    truncate_for_log,
)

TEST_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789"
RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


# === Формат структурированного события ====================================


def test_log_event_format(caplog: pytest.LogCaptureFixture) -> None:
    """Событие логируется как 'event=NAME key=value key=value'."""
    logger = logging.getLogger("postmaster.test")

    with caplog.at_level(logging.INFO, logger="postmaster.test"):
        log_event(logger, "demo_event", post_id=7, user_id=3)

    assert "event=demo_event post_id=7 user_id=3" in caplog.text


def test_log_event_first_token_is_event(caplog: pytest.LogCaptureFixture) -> None:
    """Первый токен строки — всегда 'event=NAME' (фиксированный формат для grep)."""
    logger = logging.getLogger("postmaster.test")

    with caplog.at_level(logging.INFO, logger="postmaster.test"):
        log_event(logger, "demo_event", post_id=7)

    message = caplog.records[0].getMessage()
    assert message.split()[0] == "event=demo_event"


def test_log_event_respects_level(caplog: pytest.LogCaptureFixture) -> None:
    """Уровень события задаётся явно: WARNING/ERROR (SPEC-025 §4)."""
    logger = logging.getLogger("postmaster.test")

    with caplog.at_level(logging.INFO, logger="postmaster.test"):
        log_event(logger, "warn_event", level=logging.WARNING)
        log_event(logger, "err_event", level=logging.ERROR)

    levels = {r.getMessage().split()[0]: r.levelno for r in caplog.records}
    assert levels["event=warn_event"] == logging.WARNING
    assert levels["event=err_event"] == logging.ERROR


def test_log_event_accepts_explicit_fields_mapping(caplog: pytest.LogCaptureFixture) -> None:
    """Поля можно передать словарём fields=."""
    logger = logging.getLogger("postmaster.test")

    with caplog.at_level(logging.INFO, logger="postmaster.test"):
        log_event(logger, "demo_event", fields={"post_id": 1}, attempt=2)

    assert "event=demo_event post_id=1 attempt=2" in caplog.text


# === События жизненного цикла приложения ===================================


async def test_startup_event_emitted(caplog: pytest.LogCaptureFixture) -> None:
    """При старте приложения логируется событие startup (SPEC-025 §1)."""
    from postmaster.app import Application

    logger = logging.getLogger("postmaster.app")
    with caplog.at_level(logging.INFO, logger="postmaster.app"):
        log_event(logger, EVENT_STARTUP)

    assert "event=startup" in caplog.text
    assert Application is not None


# === Post create / claim / publish (repository) ============================


@pytest.fixture
async def post_repo(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, UserService]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        yield PostRepository(factory), UserService(user_repo, default_timezone="UTC")
    finally:
        await engine.dispose()


async def _make_user(user_service: UserService, telegram_user_id: int) -> int:
    user = await user_service.get_or_create(
        telegram_user_id=telegram_user_id,
        username=None,
        first_name=f"User{telegram_user_id}",
    )
    return user.id


async def _create_post(repo: PostRepository, user_id: int) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id=f"file_{user_id}",
        telegram_file_unique_id=f"unique_{user_id}",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    return post.id


async def test_post_created_event_contains_post_id(
    post_repo, caplog: pytest.LogCaptureFixture
) -> None:
    """post_create содержит post_id и user_id (SPEC-025 §8)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 100)

    with caplog.at_level(logging.INFO, logger="postmaster.repositories.post_repository"):
        post_id = await _create_post(repo, user_id)

    assert "event=post_created" in caplog.text
    assert f"post_id={post_id}" in caplog.text
    assert f"user_id={user_id}" in caplog.text


async def test_claim_event_contains_post_id(post_repo, caplog: pytest.LogCaptureFixture) -> None:
    """claim содержит post_id и новый статус."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 101)
    post_id = await _create_post(repo, user_id)

    with caplog.at_level(logging.INFO, logger="postmaster.repositories.post_repository"):
        await repo.claim_for_processing(post_id)

    assert "event=claim" in caplog.text
    assert f"post_id={post_id}" in caplog.text
    assert "status=PROCESSING" in caplog.text


async def test_publish_ok_event_contains_post_id(
    post_repo, caplog: pytest.LogCaptureFixture
) -> None:
    """publish_ok содержит post_id."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 102)
    post_id = await _create_post(repo, user_id)
    await repo.claim_for_processing(post_id)

    with caplog.at_level(logging.INFO, logger="postmaster.repositories.post_repository"):
        await repo.update_status(post_id, PostStatus.PUBLISHED, published_at=RUN_AT)

    assert "event=publish_ok" in caplog.text
    assert f"post_id={post_id}" in caplog.text


async def test_post_failed_event_is_error_level(
    post_repo, caplog: pytest.LogCaptureFixture
) -> None:
    """post_failed логируется на уровне ERROR (SPEC-025 §4)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 103)
    post_id = await _create_post(repo, user_id)
    await repo.claim_for_processing(post_id)

    with caplog.at_level(logging.INFO, logger="postmaster.repositories.post_repository"):
        await repo.update_status(post_id, PostStatus.FAILED, error_message="boom")

    records = [r for r in caplog.records if "event=post_failed" in r.getMessage()]
    assert records
    assert records[0].levelno == logging.ERROR
    assert f"post_id={post_id}" in records[0].getMessage()


# === Scheduler ==============================================================


async def test_scheduler_start_and_stop_events(caplog: pytest.LogCaptureFixture) -> None:
    """Scheduler логирует события запуска и остановки (SPEC-025 §1)."""
    from postmaster.services.scheduler_service import SchedulerService

    service = SchedulerService()
    with caplog.at_level(logging.INFO, logger="postmaster.services.scheduler_service"):
        service.start()
        service.shutdown()

    assert "event=scheduler_start" in caplog.text
    assert "event=scheduler_stop" in caplog.text


# === Recovery ==============================================================


async def test_recovery_events(post_repo, caplog: pytest.LogCaptureFixture) -> None:
    """Recovery логирует события начала и завершения (SPEC-025 §1)."""
    from postmaster.services.recovery_service import RecoveryService

    repo, user_service = post_repo
    await _make_user(user_service, 104)

    class _Scheduler:
        def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
            return None

    class _Missed:
        async def handle(self, post_id: int):
            return None

    service = RecoveryService(
        repository=repo, scheduler=_Scheduler(), missed_post_handler=_Missed()
    )
    with caplog.at_level(logging.INFO, logger="postmaster.services.recovery_service"):
        await service.recover()

    assert "event=recovery_start" in caplog.text
    assert "event=recovery_done" in caplog.text
    assert "rescheduled=0" in caplog.text


# === Publish / retry / error_code =========================================


class _RecordingSender:
    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self._fail = fail_with
        self.calls: list[dict] = []

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        self.calls.append({"chat_id": chat_id, "caption": caption})
        if self._fail is not None:
            raise self._fail
        return {"message_id": 1}


class _FakeTelegramException(Exception):
    def __init__(self, error_code: int, description: str = "") -> None:
        self.error_code = error_code
        super().__init__(description)


async def _prepare_telegram_post(repo: PostRepository, user_id: int) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id=f"file_{user_id}",
        telegram_file_unique_id=f"unique_{user_id}",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )
    return post.id


async def test_publication_events_on_success(post_repo, caplog: pytest.LogCaptureFixture) -> None:
    """Успешная публикация: publication_start → publication_ok (SPEC-025 §1)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 110)
    post_id = await _prepare_telegram_post(repo, user_id)
    publisher = TelegramChannelPublisher(_RecordingSender())
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver)

    with caplog.at_level(logging.INFO):
        await service.publish(post_id)

    assert "event=publication_start" in caplog.text
    assert "event=publication_ok" in caplog.text
    assert f"post_id={post_id}" in caplog.text


async def test_publication_error_event_has_error_code(
    post_repo, caplog: pytest.LogCaptureFixture
) -> None:
    """Ошибка публикации содержит error_code (SPEC-025 §8)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 111)
    post_id = await _prepare_telegram_post(repo, user_id)
    publisher = TelegramChannelPublisher(
        _RecordingSender(fail_with=_FakeTelegramException(400, "Bad Request: chat not found"))
    )
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, error_mapper=TelegramErrorMapper())

    with caplog.at_level(logging.INFO):
        with pytest.raises(PublicationError):
            await service.publish(post_id)

    assert "event=publish_error" in caplog.text
    assert "error_code=PERMISSION" in caplog.text
    assert "event=publication_attempt_failed" in caplog.text
    assert "error_code=PERMISSION" in caplog.text


async def test_retry_event_emitted_with_delay(post_repo, caplog: pytest.LogCaptureFixture) -> None:
    """Повтор логируется отдельным событием retry с задержкой (SPEC-025 §1)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 112)
    post_id = await _prepare_telegram_post(repo, user_id)
    publisher = TelegramChannelPublisher(_RecordingSender(fail_with=TimeoutError("timeout")))
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        repo,
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
        error_mapper=TelegramErrorMapper(),
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(PublicationError):
            await service.publish(post_id)

    assert "event=retry" in caplog.text
    assert f"post_id={post_id}" in caplog.text
    assert "delay_seconds=0" in caplog.text


async def test_publish_telegram_event_contains_post_id(
    post_repo, caplog: pytest.LogCaptureFixture
) -> None:
    """Publisher логирует событие публикации с post_id."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 113)
    post_id = await _prepare_telegram_post(repo, user_id)
    publisher = TelegramChannelPublisher(_RecordingSender())
    post = await repo.get_by_id(post_id)
    assert post is not None
    object.__setattr__(post, "status", PostStatus.PROCESSING)

    with caplog.at_level(logging.INFO, logger="postmaster.publishers.telegram_publisher"):
        await publisher.publish(post)

    assert "event=publish_telegram" in caplog.text
    assert f"post_id={post_id}" in caplog.text


# === Команды ===============================================================


def test_command_events_defined() -> None:
    """Команды используют единый словарь событий (SPEC-025 §1)."""
    assert EVENT_COMMAND_START == "command_start"
    assert EVENT_COMMAND_CANCEL == "command_cancel"


# === Token redaction =======================================================


def test_token_redacted_in_event_values(capsys) -> None:
    """Токен в значении поля редактируется форматтером (SPEC-025 §7, §8)."""
    from postmaster.utils.logging_config import configure_logging

    logger = logging.getLogger("postmaster.test.redaction")
    configure_logging("INFO", secrets=[TEST_TOKEN])

    log_event(logger, "demo_event", token=TEST_TOKEN, post_id=1)

    captured = capsys.readouterr()
    assert TEST_TOKEN not in captured.out
    assert MASK in captured.out


def test_token_redacted_by_pattern_in_event_values(caplog: pytest.LogCaptureFixture) -> None:
    """Токен в формате Bot API редактируется даже без списка секретов."""
    from postmaster.utils.logging_config import RedactingFormatter

    record = logging.LogRecord(
        "postmaster.test.pattern",
        logging.INFO,
        __file__,
        1,
        f"event=demo_event url=https://api.telegram.org/bot{TEST_TOKEN}/getMe",
        None,
        None,
    )

    rendered = RedactingFormatter().format(record)

    assert TEST_TOKEN not in rendered
    assert MASK in rendered


async def test_file_id_not_logged_in_events(post_repo, caplog: pytest.LogCaptureFixture) -> None:
    """file_id фотографии не попадает в лог (SPEC-025 §7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 120)
    secret_file_id = "AgAC_secret_file_id_value_12345"
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id=secret_file_id,
        telegram_file_unique_id="unique_secret_12345",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )

    with caplog.at_level(logging.INFO):
        assert post.id > 0

    assert secret_file_id not in caplog.text
    assert "unique_secret_12345" not in caplog.text


# === Caption protection ====================================================


def test_caption_for_log_returns_dash_for_none() -> None:
    """None-caption представлен как '-' (SPEC-025 §7)."""
    assert caption_for_log(None) == "-"


def test_caption_for_log_truncates_long_text() -> None:
    """Длинный caption обрезается и не попадает в лог целиком."""
    caption = "A" * 500
    result = caption_for_log(caption)
    assert len(result) < len(caption)
    assert result.endswith("...")
    assert caption not in result


def test_caption_for_log_keeps_short_text() -> None:
    """Короткий caption остаётся без изменений."""
    assert caption_for_log("Hi") == "Hi"


def test_truncate_for_log_respects_limit() -> None:
    """truncate_for_log уважает limit."""
    assert truncate_for_log("abcdef", limit=3) == "abc..."
    assert truncate_for_log("ab", limit=3) == "ab"
    assert truncate_for_log("abcdef", limit=0) == "..."


async def test_caption_never_logged_in_full(post_repo, caplog: pytest.LogCaptureFixture) -> None:
    """Полный caption не попадает в лог при создании поста (SPEC-025 §7).

    Проверяются записи логгеров приложения (``postmaster.*``). Отладочные
    сообщения сторонних библиотек (aiosqlite печатает SQL с параметрами)
    не относятся к логированию приложения.
    """
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 121)
    secret_caption = "СЕКРЕТНЫЙ-CAPTION-ТЕКСТ-ДЛИННЫЙ-ЧТОБЫ-ПОПАСТЬ-В-ЛОГ"

    with caplog.at_level(logging.DEBUG):
        post = await repo.create_post(
            user_id=user_id,
            telegram_file_id="file_121",
            telegram_file_unique_id="unique_121",
            scheduled_at_utc=RUN_AT,
            timezone="UTC",
            publication_type=PublicationType.REMINDER,
            target_id=None,
            caption=secret_caption,
        )

    assert post.caption == secret_caption
    app_records = [r for r in caplog.records if r.name.startswith("postmaster")]
    app_text = "\n".join(r.getMessage() for r in app_records)
    assert secret_caption not in app_text


async def test_photo_service_does_not_log_caption(caplog: pytest.LogCaptureFixture) -> None:
    """PhotoService не логирует caption и бинарные данные (SPEC-025 §7)."""
    from postmaster.services.photo_service import PhotoService

    class _Size:
        def __init__(self) -> None:
            self.file_id = "file-1"
            self.file_unique_id = "file-1-uniq"
            self.width = 1280
            self.height = 960
            self.file_size = 150000

    with caplog.at_level(logging.DEBUG):
        PhotoService().handle_photo(1, [_Size()], caption="мой-секретный-caption")

    assert "мой-секретный-caption" not in caplog.text


# === Логирование не меняет бизнес-state ====================================


async def test_logging_does_not_change_state(post_repo, caplog: pytest.LogCaptureFixture) -> None:
    """Логирование не меняет статус поста и не создаёт записей (SPEC-025 §7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, 130)
    post_id = await _prepare_telegram_post(repo, user_id)
    publisher = TelegramChannelPublisher(_RecordingSender())
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver)

    with caplog.at_level(logging.DEBUG):
        result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert len(await repo.list_pending()) == 0
    assert len(await repo.list_pending_by_user(user_id)) == 0
    # События не создают дополнительных записей.
    assert len(await repo.list_pending()) == 0


async def test_log_event_helper_is_side_effect_free(caplog: pytest.LogCaptureFixture) -> None:
    """Хелпер log_event только пишет в лог и ничего не меняет вокруг себя."""
    logger = logging.getLogger("postmaster.test.sideeffect")
    before = list(caplog.records)

    with caplog.at_level(logging.INFO, logger="postmaster.test.sideeffect"):
        log_event(logger, "noop_event", value=1)

    assert len(caplog.records) == len(before) + 1
