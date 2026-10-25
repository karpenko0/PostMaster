"""SPEC-015: Telegram Publisher.

Тесты проверяют:
- success: photo+caption отправляется в target_id.
- caption: caption включается в сообщение.
- target missing: target_id пустой — TargetMissingError.
- file missing: telegram_file_id пустой — FileIdMissingError.
- permission: 400/403 ошибка — TelegramPermissionError.
- timeout: TimeoutError — TelegramTimeoutError.
- non-PENDING status: InvalidPostStatusError.
- Returns PublishResult с external_id.
- Не управляет Scheduler/retry/status.
"""

import asyncio
from datetime import UTC, datetime

import pytest

from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.publishers.base import PublishResult
from postmaster.publishers.telegram_errors import TelegramErrorCode
from postmaster.publishers.telegram_publisher import (
    FileIdMissingError,
    InvalidPostStatusError,
    TargetMissingError,
    TelegramAPIError,
    TelegramChannelPublisher,
    TelegramPermissionError,
    TelegramTimeoutError,
    _is_permission_error,
    _is_timeout,
)

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


class FakeTelegramException(Exception):
    """Эмуляция ApiTelegramException с error_code и description."""

    def __init__(self, error_code: int, description: str = "") -> None:
        self.error_code = error_code
        super().__init__(description)


class RecordingTelegramSender:
    """Заглушка TelegramSender с записью вызовов."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_with: Exception | None = None
        self.delay: float = 0.0
        self.next_message_id: int = 1

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        self.calls.append({"chat_id": chat_id, "photo_file_id": photo_file_id, "caption": caption})
        if self.fail_with is not None:
            raise self.fail_with
        return {"message_id": self.next_message_id}


def _make_post(
    user_id: int = 1,
    *,
    file_id: str = "AgAC_telegram_file",
    target_id: str | None = "-1001234567890",
    caption: str | None = "Test caption",
    status: PostStatus = PostStatus.PROCESSING,
) -> Post:
    return Post(
        id=1,
        user_id=user_id,
        telegram_file_id=file_id,
        telegram_file_unique_id="AQAD_telegram_unique",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        created_at=RUN_AT,
        updated_at=RUN_AT,
        status=status,
        target_id=target_id,
        caption=caption,
    )


async def test_telegram_publisher_success() -> None:
    """Успешная отправка: photo+caption в target_id."""
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    result = await publisher.publish(post)

    assert isinstance(result, PublishResult)
    assert result.post_id == post.id
    assert result.external_id == "1"
    assert len(sender.calls) == 1
    assert sender.calls[0]["chat_id"] == "-1001234567890"
    assert sender.calls[0]["photo_file_id"] == "AgAC_telegram_file"
    assert sender.calls[0]["caption"] == "Test caption"


async def test_telegram_publisher_without_caption() -> None:
    """Без caption — фото отправляется без подписи."""
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_post(caption=None)

    await publisher.publish(post)

    assert sender.calls[0]["caption"] is None


async def test_telegram_publisher_target_missing() -> None:
    """target_id пустой — TargetMissingError."""
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_post(target_id="-1001234567890")
    object.__setattr__(post, "target_id", "")

    with pytest.raises(TargetMissingError, match="target_id"):
        await publisher.publish(post)

    assert sender.calls == []


async def test_telegram_publisher_target_whitespace_missing() -> None:
    """target_id из пробелов — TargetMissingError."""
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()
    object.__setattr__(post, "target_id", "   ")

    with pytest.raises(TargetMissingError, match="target_id"):
        await publisher.publish(post)

    assert sender.calls == []


async def test_telegram_publisher_file_id_missing() -> None:
    """telegram_file_id пустой — FileIdMissingError."""
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_post(file_id="placeholder")
    object.__setattr__(post, "telegram_file_id", "")

    with pytest.raises(FileIdMissingError, match="telegram_file_id"):
        await publisher.publish(post)

    assert sender.calls == []


async def test_telegram_publisher_file_id_whitespace_missing() -> None:
    """telegram_file_id из пробелов — FileIdMissingError."""
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_post(file_id="placeholder")
    object.__setattr__(post, "telegram_file_id", "   ")

    with pytest.raises(FileIdMissingError, match="telegram_file_id"):
        await publisher.publish(post)


async def test_telegram_publisher_requires_processing_status() -> None:
    """Пост не в PROCESSING — InvalidPostStatusError."""
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_post(status=PostStatus.PENDING)

    with pytest.raises(InvalidPostStatusError, match="PROCESSING"):
        await publisher.publish(post)

    assert sender.calls == []


async def test_telegram_publisher_published_status_rejected() -> None:
    """Пост в PUBLISHED — InvalidPostStatusError."""
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()
    object.__setattr__(post, "status", PostStatus.PUBLISHED)

    with pytest.raises(InvalidPostStatusError):
        await publisher.publish(post)


async def test_telegram_publisher_permission_chat_not_found() -> None:
    """Telegram вернул 400 'chat not found' — TelegramPermissionError."""
    sender = RecordingTelegramSender()
    sender.fail_with = FakeTelegramException(400, "Bad Request: chat not found")
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    with pytest.raises(TelegramPermissionError):
        await publisher.publish(post)


async def test_telegram_publisher_permission_bot_kicked() -> None:
    """Telegram вернул 403 'bot was kicked' — TelegramPermissionError."""
    sender = RecordingTelegramSender()
    sender.fail_with = FakeTelegramException(403, "Forbidden: bot was kicked from the chat")
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    with pytest.raises(TelegramPermissionError, match="bot was kicked"):
        await publisher.publish(post)


async def test_telegram_publisher_permission_not_admin() -> None:
    """Telegram вернул 400 'need administrator rights' — TelegramPermissionError."""
    sender = RecordingTelegramSender()
    sender.fail_with = FakeTelegramException(
        400, "Bad Request: need administrator rights in the target chat"
    )
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    with pytest.raises(TelegramPermissionError):
        await publisher.publish(post)


async def test_telegram_publisher_permission_chat_deactivated() -> None:
    """Telegram вернул 403 'chat is deactivated' — TelegramPermissionError."""
    sender = RecordingTelegramSender()
    sender.fail_with = FakeTelegramException(403, "Forbidden: chat is deactivated")
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    with pytest.raises(TelegramPermissionError):
        await publisher.publish(post)


async def test_telegram_publisher_timeout_via_timeout_error() -> None:
    """asyncio.TimeoutError — TelegramTimeoutError."""
    sender = RecordingTelegramSender()
    sender.fail_with = TimeoutError()
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    with pytest.raises(TelegramTimeoutError):
        await publisher.publish(post)


async def test_telegram_publisher_timeout_via_text() -> None:
    """Ошибка с текстом 'timeout' — TelegramTimeoutError."""
    sender = RecordingTelegramSender()
    sender.fail_with = RuntimeError("Request timed out")
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    with pytest.raises(TelegramTimeoutError):
        await publisher.publish(post)


async def test_telegram_publisher_other_error_propagates() -> None:
    """Не-403/не-timeout ошибка Telegram нормализуется в TelegramAPIError (SPEC-021)."""
    sender = RecordingTelegramSender()
    sender.fail_with = RuntimeError("network error")
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    with pytest.raises(TelegramAPIError) as exc_info:
        await publisher.publish(post)

    error = exc_info.value.error
    assert error.code == TelegramErrorCode.NETWORK
    assert error.retryable is True
    assert "network error" in error.message


async def test_telegram_publisher_does_not_manage_retry() -> None:
    """Publisher не делает retry — ошибка пробрасывается сразу."""
    sender = RecordingTelegramSender()
    sender.fail_with = RuntimeError("transient error")
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    with pytest.raises(TelegramAPIError):
        await publisher.publish(post)

    assert len(sender.calls) == 1  # Одна попытка, без retry


async def test_telegram_publisher_does_not_manage_status() -> None:
    """Publisher не меняет статус Post — это ответственность PublicationService."""
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    await publisher.publish(post)

    # Статус остаётся PROCESSING — сервис решит, когда перевести в PUBLISHED.
    assert post.status == PostStatus.PROCESSING


async def test_telegram_publisher_returns_publish_result_with_external_id() -> None:
    """Publisher возвращает PublishResult с external_id из ответа Telegram."""
    sender = RecordingTelegramSender()
    sender.next_message_id = 12345
    publisher = TelegramChannelPublisher(sender)
    post = _make_post()

    result = await publisher.publish(post)

    assert result.external_id == "12345"


def test_is_permission_error_detects_400_with_chat_not_found() -> None:
    """_is_permission_error детектирует 400 + 'chat not found'."""
    exc = FakeTelegramException(400, "Bad Request: chat not found")
    assert _is_permission_error(exc) is True


def test_is_permission_error_detects_403_with_bot_kicked() -> None:
    """_is_permission_error детектирует 403 + 'bot was kicked'."""
    exc = FakeTelegramException(403, "Forbidden: bot was kicked")
    assert _is_permission_error(exc) is True


def test_is_permission_error_returns_false_for_400_other() -> None:
    """_is_permission_error возвращает False для 400 без permission-текста."""
    exc = FakeTelegramException(400, "Bad Request: invalid photo file_id")
    assert _is_permission_error(exc) is False


def test_is_permission_error_returns_false_for_network_error() -> None:
    """_is_permission_error возвращает False для обычной ошибки сети."""
    assert _is_permission_error(RuntimeError("network error")) is False


def test_is_timeout_detects_timeout_error_instance() -> None:
    """_is_timeout детектирует экземпляр TimeoutError."""
    assert _is_timeout(TimeoutError()) is True


def test_is_timeout_detects_timeout_text() -> None:
    """_is_timeout детектирует текст 'timeout'."""
    assert _is_timeout(RuntimeError("Request timed out")) is True
    assert _is_timeout(RuntimeError("connection timeout")) is True


def test_is_timeout_returns_false_for_other_errors() -> None:
    """_is_timeout возвращает False для других ошибок."""
    assert _is_timeout(RuntimeError("network error")) is False
    assert _is_timeout(ValueError("bad value")) is False
