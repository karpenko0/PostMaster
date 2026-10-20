"""SPEC-021: общий mapper ошибок Telegram, retry-классификация и redaction."""

from types import SimpleNamespace

import pytest

from postmaster.domain.post import PostStatus, PublicationType
from postmaster.publishers.base import PublishResult
from postmaster.publishers.reminder import ReminderPublisher
from postmaster.publishers.telegram import TelegramPublisher
from postmaster.publishers.telegram_errors import (
    TelegramErrorCode,
    TelegramErrorMapper,
    TelegramOperation,
)


class FakeTelegramClient:
    def __init__(
        self,
        *,
        message_error: BaseException | None = None,
        photo_error: BaseException | None = None,
    ) -> None:
        self.message_error = message_error
        self.photo_error = photo_error

    async def send_message(self, chat_id: str | int, text: str) -> None:
        if self.message_error is not None:
            raise self.message_error

    async def send_photo(
        self, chat_id: str | int, file_id: str, caption: str | None = None
    ) -> None:
        if self.photo_error is not None:
            raise self.photo_error


class FakeUserRepository:
    async def get_by_id(self, user_id: int) -> SimpleNamespace:
        return SimpleNamespace(telegram_user_id=998877)


def test_mapper_normalizes_retryable_permanent_and_operation_fallbacks() -> None:
    mapper = TelegramErrorMapper()
    cases = (
        (
            TimeoutError("request timed out"),
            TelegramOperation.PHOTO,
            TelegramErrorCode.TELEGRAM_API_TIMEOUT,
            True,
        ),
        (
            ConnectionError("connection reset"),
            TelegramOperation.MESSAGE,
            TelegramErrorCode.NETWORK_ERROR,
            True,
        ),
        (
            RuntimeError("Telegram service unavailable"),
            TelegramOperation.PHOTO,
            TelegramErrorCode.TELEGRAM_API_UNAVAILABLE,
            True,
        ),
        (
            RuntimeError("Forbidden: bot is not a member of the channel"),
            TelegramOperation.PHOTO,
            TelegramErrorCode.TELEGRAM_PERMISSION_DENIED,
            False,
        ),
        (
            RuntimeError("bot was blocked by the user"),
            TelegramOperation.MESSAGE,
            TelegramErrorCode.BOT_BLOCKED,
            False,
        ),
        (
            RuntimeError("Bad Request: wrong file identifier"),
            TelegramOperation.PHOTO,
            TelegramErrorCode.INVALID_FILE_ID,
            False,
        ),
        (
            RuntimeError("unrecognized response"),
            TelegramOperation.PHOTO,
            TelegramErrorCode.PHOTO_SEND_FAILED,
            False,
        ),
        (
            RuntimeError("unrecognized response"),
            TelegramOperation.MESSAGE,
            TelegramErrorCode.MESSAGE_SEND_FAILED,
            False,
        ),
    )

    for exception, operation, code, retryable in cases:
        mapped = mapper.map(exception, operation=operation)
        assert mapped.code == code
        assert mapped.retryable is retryable
        assert mapped.message


def test_mapper_redacts_configured_secrets_file_id_and_caption() -> None:
    token = "123456789:AA_SUPER_SECRET_TOKEN"
    file_id = "AgAC-secret-file-identifier"
    caption = "private caption text"
    mapper = TelegramErrorMapper(secrets=(token,))

    mapped = mapper.map(
        RuntimeError(f"API error {token}; {file_id}; {caption}"),
        operation=TelegramOperation.PHOTO,
        file_id=file_id,
        caption=caption,
    )

    assert token not in mapped.message
    assert file_id not in mapped.message
    assert caption not in mapped.message
    assert "<file_id>" in mapped.message
    assert "<caption_redacted>" in mapped.message


@pytest.mark.asyncio
async def test_both_publishers_return_failure_and_redacted_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "123456789:AA_SUPER_SECRET_TOKEN"
    file_id = "AgAC-secret-file-identifier"
    caption = "private caption text"
    exception = RuntimeError(f"Telegram failed: {token} {file_id} {caption}")
    mapper = TelegramErrorMapper(secrets=(token,))
    client = FakeTelegramClient(photo_error=exception)

    telegram_result = await TelegramPublisher(client, error_mapper=mapper).publish(
        SimpleNamespace(
            id=1,
            publication_type=PublicationType.TELEGRAM,
            status=PostStatus.PROCESSING,
            target_id="@channel",
            telegram_file_id=file_id,
            caption=caption,
        )
    )
    reminder_result = await ReminderPublisher(
        client,
        FakeUserRepository(),
        error_mapper=mapper,
    ).publish(
        SimpleNamespace(
            id=2,
            user_id=3,
            publication_type=PublicationType.REMINDER,
            telegram_file_id=file_id,
            caption=caption,
        )
    )

    for result in (telegram_result, reminder_result):
        assert isinstance(result, PublishResult)
        assert result.success is False
        assert result.retryable is False
        assert result.error_message is not None
        assert token not in result.error_message
        assert file_id not in result.error_message
        assert caption not in result.error_message

    assert "PUBLISHED" not in caplog.text
    assert token not in caplog.text
    assert file_id not in caplog.text
    assert caption not in caplog.text
