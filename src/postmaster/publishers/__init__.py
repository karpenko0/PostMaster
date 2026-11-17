"""Публикация постов на внешних площадках (BR-03, SPEC-014, SPEC-015, SPEC-016)."""

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

# Имя контракта SPEC-010/020/021/022/025/032. PR #4 переименовал класс, но не
# оставил alias; восстанавливаем его без дублирования реализации.
DefaultPublisherResolver = PublisherResolver

__all__ = [
    "DefaultPublisherResolver",
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
    "TelegramErrorCode",
    "TelegramPublisher",
]
