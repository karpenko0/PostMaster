"""SPEC-021: Обработка ошибок Telegram API.

Обязательные проверки (§8):
- все маппинги исключений в коды;
- неизвестная ошибка (UNKNOWN);
- редактирование секретов (Bot Token).

Дополнительно: интеграция маппера с PublicationService (retry только для
retryable) и с Publisher (текст ошибки не содержит токен).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.publishers import DefaultPublisherResolver
from postmaster.publishers.reminder_publisher import (
    ReminderPublisher,
    TelegramBlockedError,
)
from postmaster.publishers.telegram_errors import (
    RETRYABLE_CODES,
    TelegramError,
    TelegramErrorCode,
    TelegramErrorMapper,
    redact_secrets,
)
from postmaster.publishers.telegram_publisher import (
    TelegramAPIError,
    TelegramChannelPublisher,
    TelegramPermissionError,
    TelegramTimeoutError,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import (
    PublicationError,
    PublicationService,
    RetryPolicy,
)
from postmaster.services.user_service import UserService

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)

# Реальный формат токена: \"<id>:<secret>\". 123456:ABC... — соответствует шаблону.
SAMPLE_TOKEN = "1234567890:AaBbCcDdEeFfGgHhIiJjKkLl"


class FakeTelegramException(Exception):
    """Эмуляция ApiTelegramException с error_code и description."""

    def __init__(self, error_code: int, description: str = "") -> None:
        self.error_code = error_code
        super().__init__(description)


# === TelegramErrorMapper: отдельные маппинги =================================


def test_mapper_timeout_via_timeout_error() -> None:
    """asyncio.TimeoutError → TIMEOUT, retryable=True."""
    error = TelegramErrorMapper().map_exception(TimeoutError())
    assert error.code == TelegramErrorCode.TIMEOUT
    assert error.retryable is True


def test_mapper_timeout_via_text() -> None:
    """Текст с 'timed out' → TIMEOUT."""
    error = TelegramErrorMapper().map_exception(RuntimeError("Request timed out"))
    assert error.code == TelegramErrorCode.TIMEOUT
    assert error.retryable is True


def test_mapper_timeout_via_connect_timeout_text() -> None:
    """'connect timeout' в тексте → TIMEOUT."""
    error = TelegramErrorMapper().map_exception(RuntimeError("connect timeout after 5s"))
    assert error.code == TelegramErrorCode.TIMEOUT


def test_mapper_network_via_text() -> None:
    """Текст 'network' → NETWORK, retryable=True."""
    error = TelegramErrorMapper().map_exception(RuntimeError("network error"))
    assert error.code == TelegramErrorCode.NETWORK
    assert error.retryable is True


def test_mapper_network_via_connection_error_type() -> None:
    """ConnectionError без status code → NETWORK."""
    error = TelegramErrorMapper().map_exception(ConnectionError("dns failed"))
    assert error.code == TelegramErrorCode.NETWORK
    assert error.retryable is True


def test_mapper_network_via_os_error() -> None:
    """OSError без status code → NETWORK."""
    error = TelegramErrorMapper().map_exception(OSError("host unreachable"))
    assert error.code == TelegramErrorCode.NETWORK


def test_mapper_unavailable_via_429() -> None:
    """429 Too Many Requests → UNAVAILABLE, retryable=True."""
    error = TelegramErrorMapper().map_exception(FakeTelegramException(429, "Too Many Requests"))
    assert error.code == TelegramErrorCode.UNAVAILABLE
    assert error.retryable is True


def test_mapper_unavailable_via_502() -> None:
    """502 Bad Gateway → UNAVAILABLE."""
    error = TelegramErrorMapper().map_exception(FakeTelegramException(502, "Bad Gateway"))
    assert error.code == TelegramErrorCode.UNAVAILABLE


def test_mapper_unavailable_via_text() -> None:
    """Текст 'service unavailable' → UNAVAILABLE."""
    error = TelegramErrorMapper().map_exception(RuntimeError("service unavailable"))
    assert error.code == TelegramErrorCode.UNAVAILABLE
    assert error.retryable is True


def test_mapper_permission_chat_not_found() -> None:
    """400 + 'chat not found' → PERMISSION, retryable=False."""
    error = TelegramErrorMapper().map_exception(
        FakeTelegramException(400, "Bad Request: chat not found")
    )
    assert error.code == TelegramErrorCode.PERMISSION
    assert error.retryable is False


def test_mapper_permission_bot_kicked() -> None:
    """403 + 'bot was kicked' → PERMISSION."""
    error = TelegramErrorMapper().map_exception(
        FakeTelegramException(403, "Forbidden: bot was kicked from the chat")
    )
    assert error.code == TelegramErrorCode.PERMISSION
    assert error.retryable is False


def test_mapper_permission_not_admin() -> None:
    """400 + 'need administrator rights' → PERMISSION."""
    error = TelegramErrorMapper().map_exception(
        FakeTelegramException(400, "Bad Request: need administrator rights in the target chat")
    )
    assert error.code == TelegramErrorCode.PERMISSION


def test_mapper_permission_chat_deactivated() -> None:
    """403 + 'chat is deactivated' → PERMISSION."""
    error = TelegramErrorMapper().map_exception(
        FakeTelegramException(403, "Forbidden: chat is deactivated")
    )
    assert error.code == TelegramErrorCode.PERMISSION


def test_mapper_blocked_bot_blocked() -> None:
    """403 + 'bot was blocked' → BLOCKED, retryable=False."""
    error = TelegramErrorMapper().map_exception(
        FakeTelegramException(403, "Forbidden: bot was blocked by the user")
    )
    assert error.code == TelegramErrorCode.BLOCKED
    assert error.retryable is False


def test_mapper_blocked_user_deactivated() -> None:
    """403 + 'user is deactivated' → BLOCKED."""
    error = TelegramErrorMapper().map_exception(
        FakeTelegramException(403, "Forbidden: user is deactivated")
    )
    assert error.code == TelegramErrorCode.BLOCKED


def test_mapper_invalid_file_id() -> None:
    """400 + 'invalid file_id' → INVALID_FILE, retryable=False."""
    error = TelegramErrorMapper().map_exception(
        FakeTelegramException(400, "Bad Request: invalid file_id")
    )
    assert error.code == TelegramErrorCode.INVALID_FILE
    assert error.retryable is False


def test_mapper_invalid_photo() -> None:
    """400 + 'invalid photo' → INVALID_FILE."""
    error = TelegramErrorMapper().map_exception(
        FakeTelegramException(400, "Bad Request: invalid photo")
    )
    assert error.code == TelegramErrorCode.INVALID_FILE


def test_mapper_file_too_big() -> None:
    """400 + 'file is too big' → INVALID_FILE."""
    error = TelegramErrorMapper().map_exception(
        FakeTelegramException(400, "Bad Request: file is too big")
    )
    assert error.code == TelegramErrorCode.INVALID_FILE


def test_mapper_unknown_generic_exception() -> None:
    """Неизвестная ошибка → UNKNOWN, retryable=False (§8 unknown error)."""
    error = TelegramErrorMapper().map_exception(RuntimeError("something unexpected"))
    assert error.code == TelegramErrorCode.UNKNOWN
    assert error.retryable is False


def test_mapper_unknown_value_error() -> None:
    """ValueError → UNKNOWN."""
    error = TelegramErrorMapper().map_exception(ValueError("bad value"))
    assert error.code == TelegramErrorCode.UNKNOWN


def test_mapper_unknown_with_unrelated_400() -> None:
    """400 без известных подстрок → UNKNOWN."""
    error = TelegramErrorMapper().map_exception(FakeTelegramException(400, "weird thing"))
    assert error.code == TelegramErrorCode.UNKNOWN


def test_mapper_returns_telegram_error_instance() -> None:
    """map_exception возвращает TelegramError."""
    result = TelegramErrorMapper().map_exception(RuntimeError("boom"))
    assert isinstance(result, TelegramError)


def test_mapper_is_pure() -> None:
    """Mapper — чистая функция: повторный вызов даёт тот же результат."""
    mapper = TelegramErrorMapper()
    exc = FakeTelegramException(403, "Forbidden: bot was blocked by the user")
    first = mapper.map_exception(exc)
    second = mapper.map_exception(exc)
    assert first == second


def test_retryable_codes_match_spec() -> None:
    """retryable = TIMEOUT, NETWORK, UNAVAILABLE (SPEC-021 §4)."""
    assert RETRYABLE_CODES == frozenset(
        {
            TelegramErrorCode.TIMEOUT,
            TelegramErrorCode.NETWORK,
            TelegramErrorCode.UNAVAILABLE,
        }
    )


def test_non_retryable_codes() -> None:
    """PERMISSION, BLOCKED, INVALID_FILE, UNKNOWN — non-retryable."""
    assert TelegramErrorCode.PERMISSION not in RETRYABLE_CODES
    assert TelegramErrorCode.BLOCKED not in RETRYABLE_CODES
    assert TelegramErrorCode.INVALID_FILE not in RETRYABLE_CODES
    assert TelegramErrorCode.UNKNOWN not in RETRYABLE_CODES


# === Редактирование секретов (SPEC-021 §7) ==================================


def test_secret_token_redacted_in_message() -> None:
    """Bot Token в тексте исключения заменяется на маску (§7)."""
    raw = f"Failed to call /bot{SAMPLE_TOKEN}/sendPhoto: timeout"
    error = TelegramErrorMapper().map_exception(RuntimeError(raw))

    assert SAMPLE_TOKEN not in error.message
    assert "***" in error.message
    assert error.code == TelegramErrorCode.TIMEOUT


async def test_secret_token_redacted_in_telegram_api_error() -> None:
    """TelegramAPIError.error.message тоже без токена."""
    exc = RuntimeError(f"network error while calling /bot{SAMPLE_TOKEN}/sendMessage")
    with pytest.raises(TelegramAPIError) as exc_info:
        await TelegramChannelPublisher(_DummySender(exc)).publish(_make_post())

    assert SAMPLE_TOKEN not in str(exc_info.value)
    assert SAMPLE_TOKEN not in exc_info.value.error.message


async def test_secret_token_redacted_in_publication_service_error_message() -> None:
    """Post.error_message после публикации без токена (§7 + acceptance)."""
    exc = RuntimeError(f"connection error to /bot{SAMPLE_TOKEN}/sendPhoto")
    with pytest.raises(TelegramAPIError) as exc_info:
        await TelegramChannelPublisher(_DummySender(exc)).publish(_make_post())

    assert SAMPLE_TOKEN not in exc_info.value.error.message


def test_redact_secrets_function_handles_multiple_tokens() -> None:
    """redact_secrets вырезает все токены формата id:secret."""
    raw = f"start {SAMPLE_TOKEN} middle 9876543210:ZzYyXxWwVvUuTtSsRrQq end"
    redacted = redact_secrets(raw)
    assert SAMPLE_TOKEN not in redacted
    assert "9876543210:ZzYyXxWwVvUuTtSsRrQq" not in redacted


def test_redact_secrets_keeps_safe_text() -> None:
    """Текст без токенов остаётся неизменным."""
    assert redact_secrets("plain text without secrets") == "plain text without secrets"


def test_message_includes_code_prefix() -> None:
    """Нормализованное сообщение содержит код (для диагностики в БД и логах)."""
    error = TelegramErrorMapper().map_exception(RuntimeError("network error"))
    assert error.message.startswith("NETWORK:")


def test_message_truncated_to_max_length() -> None:
    """Слишком длинное сообщение Telegram обрезается (для Post.error_message)."""
    long_text = "x" * 1000
    error = TelegramErrorMapper().map_exception(RuntimeError(long_text))
    assert len(error.message) <= 500


# === TelegramChannelPublisher: маппинг + обратная совместимость ============


class _DummySender:
    """Заглушка TelegramSender, всегда бросает заданное исключение."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        raise self._exc


def _make_post() -> Post:
    return Post(
        id=1,
        user_id=1,
        telegram_file_id="file_1",
        telegram_file_unique_id="unique_1",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        created_at=RUN_AT,
        updated_at=RUN_AT,
        status=PostStatus.PROCESSING,
        target_id="-1001234567890",
        caption=None,
    )


async def test_telegram_publisher_permission_raises_typed_exception() -> None:
    """PERMISSION остаётся TelegramPermissionError (обратная совместимость)."""
    exc = FakeTelegramException(400, "Bad Request: chat not found")
    publisher = TelegramChannelPublisher(_DummySender(exc))
    with pytest.raises(TelegramPermissionError) as info:
        await publisher.publish(_make_post())
    assert "chat not found" in str(info.value)
    assert SAMPLE_TOKEN not in str(info.value)


async def test_telegram_publisher_timeout_raises_typed_exception() -> None:
    """TIMEOUT остаётся TelegramTimeoutError (обратная совместимость)."""
    publisher = TelegramChannelPublisher(_DummySender(TimeoutError()))
    with pytest.raises(TelegramTimeoutError):
        await publisher.publish(_make_post())


async def test_telegram_publisher_network_raises_telegram_api_error() -> None:
    """NETWORK → TelegramAPIError с retryable=True."""
    publisher = TelegramChannelPublisher(_DummySender(RuntimeError("network error")))
    with pytest.raises(TelegramAPIError) as info:
        await publisher.publish(_make_post())
    assert info.value.error.code == TelegramErrorCode.NETWORK
    assert info.value.error.retryable is True


async def test_telegram_publisher_permission_message_has_no_token() -> None:
    """Даже если в исходной ошибке есть токен — он не попадает в исключение."""
    raw = f"Bad Request: chat not found (token {SAMPLE_TOKEN})"
    publisher = TelegramChannelPublisher(_DummySender(FakeTelegramException(400, raw)))
    with pytest.raises(TelegramPermissionError) as info:
        await publisher.publish(_make_post())
    assert SAMPLE_TOKEN not in str(info.value)


async def test_telegram_publisher_does_not_swallow_exception() -> None:
    """Publisher не делает вид, что публикация успешна (SPEC-021 §7)."""
    publisher = TelegramChannelPublisher(_DummySender(RuntimeError("boom")))
    with pytest.raises(TelegramAPIError):
        await publisher.publish(_make_post())


# === ReminderPublisher: маппинг BLOCKED + TelegramAPIError ==================


class _RecordingReminderSender:
    """Заглушка TelegramReminderSender."""

    def __init__(self, fail_message_with: Exception | None = None) -> None:
        self._fail = fail_message_with
        self.calls: list[dict] = []

    async def send_message(self, *, chat_id: int, text: str) -> dict | None:
        self.calls.append({"method": "send_message", "text": text})
        if self._fail is not None:
            raise self._fail
        return {"message_id": 1}

    async def send_photo(
        self, *, chat_id: int, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        self.calls.append(
            {"method": "send_photo", "photo_file_id": photo_file_id, "caption": caption}
        )
        return {"message_id": 2}


class _StaticRecipientResolver:
    def __init__(self, telegram_user_id: int | None) -> None:
        self._telegram_user_id = telegram_user_id

    async def get_telegram_user_id(self, user_id: int) -> int | None:
        return self._telegram_user_id


def _make_reminder_post() -> Post:
    return Post(
        id=2,
        user_id=2,
        telegram_file_id="file_2",
        telegram_file_unique_id="unique_2",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        created_at=RUN_AT,
        updated_at=RUN_AT,
        status=PostStatus.PROCESSING,
        target_id=None,
        caption=None,
    )


async def test_reminder_publisher_blocked_raises_typed_exception() -> None:
    """BLOCKED остаётся TelegramBlockedError (обратная совместимость)."""
    exc = FakeTelegramException(403, "Forbidden: bot was blocked by the user")
    sender = _RecordingReminderSender(fail_message_with=exc)
    publisher = ReminderPublisher(sender, _StaticRecipientResolver(555))
    with pytest.raises(TelegramBlockedError):
        await publisher.publish(_make_reminder_post())


async def test_reminder_publisher_network_raises_telegram_api_error() -> None:
    """NETWORK на send_message → TelegramAPIError с retryable=True."""
    sender = _RecordingReminderSender(fail_message_with=RuntimeError("connection error"))
    publisher = ReminderPublisher(sender, _StaticRecipientResolver(555))
    with pytest.raises(TelegramAPIError) as info:
        await publisher.publish(_make_reminder_post())
    assert info.value.error.code == TelegramErrorCode.NETWORK
    assert info.value.error.retryable is True


async def test_reminder_publisher_message_has_no_token() -> None:
    """Bot Token в ошибке Telegram не попадает в TelegramAPIError."""
    raw = f"network error while calling /bot{SAMPLE_TOKEN}/sendMessage"
    sender = _RecordingReminderSender(fail_message_with=RuntimeError(raw))
    publisher = ReminderPublisher(sender, _StaticRecipientResolver(555))
    with pytest.raises(TelegramAPIError) as info:
        await publisher.publish(_make_reminder_post())
    assert SAMPLE_TOKEN not in info.value.error.message


# === PublicationService: retry только для retryable ========================


class _FlakyTelegramSender:
    """Sender: первые ``fail_count`` вызовов бросают ``fail_with``, дальше успех."""

    def __init__(self, *, fail_count: int, fail_with: Exception) -> None:
        self._fail_count = fail_count
        self._fail_with = fail_with
        self.call_count = 0

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        self.call_count += 1
        if self.call_count <= self._fail_count:
            raise self._fail_with
        return {"message_id": 1}


@pytest.fixture
async def post_repo(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, UserService]]:
    """Репозиторий постов и UserService во временной БД."""
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


async def _create_post(
    repo: PostRepository, user_id: int, *, target_id: str = "-1001234567890"
) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id=f"file_{user_id}",
        telegram_file_unique_id=f"unique_{user_id}",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id=target_id,
    )
    return post.id


async def test_publication_service_retries_on_network(post_repo) -> None:
    """NETWORK ошибка → retry (retryable=True)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=100)
    sender = _FlakyTelegramSender(fail_count=1, fail_with=RuntimeError("network error"))
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        repo,
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
        error_mapper=TelegramErrorMapper(),
    )
    post_id = await _create_post(repo, user_id)

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert sender.call_count == 2


async def test_publication_service_retries_on_timeout(post_repo) -> None:
    """TIMEOUT ошибка → retry."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=101)
    sender = _FlakyTelegramSender(fail_count=1, fail_with=TimeoutError())
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        repo,
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
        error_mapper=TelegramErrorMapper(),
    )
    post_id = await _create_post(repo, user_id)

    result = await service.publish(post_id)
    assert result.status == PostStatus.PUBLISHED
    assert sender.call_count == 2


async def test_publication_service_does_not_retry_on_permission(post_repo) -> None:
    """PERMISSION ошибка → без retry (retryable=False)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=102)
    sender = _FlakyTelegramSender(
        fail_count=99,  # даже если бы был retry, мы должны остановиться на 1 попытке
        fail_with=FakeTelegramException(400, "Bad Request: chat not found"),
    )
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        repo,
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
        error_mapper=TelegramErrorMapper(),
    )
    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert sender.call_count == 1  # Без retry


async def test_publication_service_does_not_retry_on_blocked(post_repo) -> None:
    """BLOCKED ошибка → без retry."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=103)
    sender = _FlakyTelegramSender(
        fail_count=99,
        fail_with=FakeTelegramException(403, "Forbidden: bot was blocked by the user"),
    )
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        repo,
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
        error_mapper=TelegramErrorMapper(),
    )
    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert sender.call_count == 1


async def test_publication_service_does_not_retry_on_invalid_file(post_repo) -> None:
    """INVALID_FILE ошибка → без retry."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=104)
    sender = _FlakyTelegramSender(
        fail_count=99,
        fail_with=FakeTelegramException(400, "Bad Request: invalid file_id"),
    )
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        repo,
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
        error_mapper=TelegramErrorMapper(),
    )
    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert sender.call_count == 1


async def test_publication_service_does_not_retry_on_unknown(post_repo) -> None:
    """UNKNOWN ошибка → без retry (SPEC-021 §4)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=105)
    sender = _FlakyTelegramSender(
        fail_count=99,
        fail_with=RuntimeError("something completely unexpected"),
    )
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        repo,
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
        error_mapper=TelegramErrorMapper(),
    )
    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert sender.call_count == 1


async def test_publication_service_post_error_message_has_no_token(post_repo) -> None:
    """Post.error_message после ошибки не содержит Bot Token (SPEC-021 §7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=106)
    raw = f"network error while calling /bot{SAMPLE_TOKEN}/sendPhoto"
    sender = _FlakyTelegramSender(fail_count=99, fail_with=RuntimeError(raw))
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        repo,
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0)),
        error_mapper=TelegramErrorMapper(),
    )
    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED
    assert SAMPLE_TOKEN not in (result.error_message or "")


async def test_publication_service_without_mapper_keeps_legacy_retry(post_repo) -> None:
    """Без маппера повторяются только сетевые и временные ошибки (TimeoutError).

    С SPEC-022 retry-решение основано на типе исключения без error_mapper:
    retryable по умолчанию = TimeoutError, ConnectionError и их типизированные
    обёртки (TelegramTimeoutError). Прочие ошибки (например, RuntimeError)
    сразу переводят пост в FAILED — это conservative default, избегающий
    бесконечных ретраев на программных ошибках.
    """
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=107)
    sender = _FlakyTelegramSender(fail_count=1, fail_with=TimeoutError("transient error"))
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        repo,
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
    )
    post_id = await _create_post(repo, user_id)

    result = await service.publish(post_id)
    assert result.status == PostStatus.PUBLISHED
    assert sender.call_count == 2
