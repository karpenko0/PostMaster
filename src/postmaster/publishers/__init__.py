"""Публикация постов на внешних площадках (BR-03, SPEC-014–016, SPEC-021)."""

from postmaster.publishers.base import (
    BotTelegramClient,
    Publisher,
    PublishResult,
    TelegramClient,
)
from postmaster.publishers.reminder import (
    REMINDER_TEXT,
    ReminderErrorCode,
    ReminderPublisher,
)
from postmaster.publishers.resolver import (
    InvalidPublicationTypeError,
    PublisherNotFoundError,
    PublisherResolutionError,
    PublisherResolver,
    PublisherResolverErrorCode,
)
from postmaster.publishers.telegram import TelegramErrorCode, TelegramPublisher
from postmaster.publishers.telegram_errors import (
    TelegramError,
    TelegramErrorMapper,
    TelegramOperation,
)

__all__ = [
    "BotTelegramClient",
    "InvalidPublicationTypeError",
    "PublishResult",
    "Publisher",
    "PublisherNotFoundError",
    "PublisherResolutionError",
    "PublisherResolver",
    "PublisherResolverErrorCode",
    "REMINDER_TEXT",
    "ReminderErrorCode",
    "ReminderPublisher",
    "TelegramClient",
    "TelegramError",
    "TelegramErrorCode",
    "TelegramErrorMapper",
    "TelegramOperation",
    "TelegramPublisher",
]
