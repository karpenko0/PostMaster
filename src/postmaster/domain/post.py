"""Публикация SPEC-008 и временный черновик сценария SPEC-003 до SPEC-009."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class PostStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PublicationType(StrEnum):
    REMINDER = "REMINDER"
    TELEGRAM = "TELEGRAM"


class UserNotFoundError(ValueError):
    """Нельзя создать публикацию без владельца (ERR-008-01)."""


class PostNotFoundError(ValueError):
    """Запись для обновления статуса не существует."""


class InvalidPostTransitionError(ValueError):
    """Переход статуса не описан в SPEC-008 §42–43."""


ALLOWED_TRANSITIONS: dict[PostStatus, frozenset[PostStatus]] = {
    PostStatus.PENDING: frozenset({PostStatus.PROCESSING, PostStatus.CANCELLED}),
    PostStatus.PROCESSING: frozenset({PostStatus.PUBLISHED, PostStatus.FAILED, PostStatus.PENDING}),
    PostStatus.PUBLISHED: frozenset(),
    PostStatus.FAILED: frozenset(),
    PostStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Post:
    """Сохранённая публикация; системные даты — aware UTC, timezone — пояс отображения."""

    id: int
    user_id: int
    telegram_file_id: str
    telegram_file_unique_id: str
    scheduled_at_utc: datetime
    timezone: str
    publication_type: PublicationType
    created_at: datetime
    updated_at: datetime
    status: PostStatus = PostStatus.PENDING
    target_id: str | None = None
    error_message: str | None = None
    published_at: datetime | None = None
    caption: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, PostStatus) or not isinstance(
            self.publication_type, PublicationType
        ):
            raise ValueError("неизвестный статус или тип публикации")
        if not self.telegram_file_id or not self.telegram_file_id.strip():
            raise ValueError("telegram_file_id обязателен")
        if not self.telegram_file_unique_id or not self.telegram_file_unique_id.strip():
            raise ValueError("telegram_file_unique_id обязателен")
        if not self.timezone:
            raise ValueError("timezone обязателен")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, OSError, ValueError):
            raise ValueError("timezone должен быть поясом IANA") from None
        if self.publication_type == PublicationType.TELEGRAM and (
            not self.target_id or not self.target_id.strip()
        ):
            raise ValueError("для TELEGRAM требуется target_id")
        if self.status == PostStatus.PUBLISHED and self.published_at is None:
            raise ValueError("PUBLISHED требует published_at")
        if self.status != PostStatus.PUBLISHED and self.published_at is not None:
            raise ValueError("published_at заполняется только при успехе")
        for name, value in (
            ("scheduled_at_utc", self.scheduled_at_utc),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("published_at", self.published_at),
        ):
            if value is None:
                if name != "published_at":
                    raise ValueError(f"{name} обязателен")
            elif value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("системные даты публикации должны быть aware UTC")


@dataclass(frozen=True, slots=True)
class PostDraft:
    """Прежняя заглушка диалога: не сохраняется в posts до SPEC-009."""

    owner_telegram_id: int
    photo_file_id: str
    scheduled_at: datetime
    timezone: str
    local_datetime: datetime | None = None

    @property
    def scheduled_at_utc(self) -> datetime:
        return self.scheduled_at.astimezone(UTC)
