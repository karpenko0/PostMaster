"""PublisherResolver: выбор реализации Publisher по publication_type (SPEC-016).

Позволяет PublicationService, Scheduler, Recovery и MissedJobService не зависеть
от конкретного способа доставки (ReminderPublisher, TelegramPublisher, в будущем VKPublisher).
Экземпляры Publisher передаются через DI при создании приложения (§16) и не пересоздаются
на каждый вызов resolve().
"""

import logging
from collections.abc import Mapping
from enum import Enum, StrEnum
from typing import Any

from postmaster.domain.post import PublicationType
from postmaster.publishers.base import Publisher

logger = logging.getLogger(__name__)


class PublisherResolverErrorCode(StrEnum):
    """Коды ошибок выбора Publisher (SPEC-016 §12, §21)."""

    PUBLISHER_NOT_FOUND = "PUBLISHER_NOT_FOUND"
    INVALID_PUBLICATION_TYPE = "INVALID_PUBLICATION_TYPE"


class PublisherResolutionError(Exception):
    """Базовое исключение ошибки выбора Publisher (SPEC-016 §12, §21)."""

    def __init__(self, code: PublisherResolverErrorCode, message: str) -> None:
        self.code = code.value
        self.error_code = code.value
        self.retryable = False
        self.is_temporary = False
        super().__init__(f"{code.value}: {message}")


class PublisherNotFoundError(PublisherResolutionError, KeyError):
    """Для запрошенного publication_type не зарегистрирован Publisher (ERR-016-01)."""

    def __init__(self, publication_type: Any) -> None:
        super().__init__(
            PublisherResolverErrorCode.PUBLISHER_NOT_FOUND,
            f"Publisher для типа {publication_type!r} не зарегистрирован",
        )


class InvalidPublicationTypeError(PublisherResolutionError, ValueError):
    """Получен некорректный или неподдерживаемый тип публикации (ERR-016-02)."""

    def __init__(self, publication_type: Any) -> None:
        super().__init__(
            PublisherResolverErrorCode.INVALID_PUBLICATION_TYPE,
            f"Некорректный тип публикации: {publication_type!r}",
        )


def _normalize_publication_type(publication_type: Any) -> str:
    raw = publication_type.value if isinstance(publication_type, Enum) else publication_type
    if isinstance(raw, str) and raw.strip():
        return raw.strip().upper()
    raise InvalidPublicationTypeError(publication_type)


class PublisherResolver:
    """Сопоставляет publication_type конкретной реализации Publisher (SPEC-016 §10–12)."""

    def __init__(
        self,
        reminder_publisher: Publisher | Mapping[Any, Publisher] | None = None,
        telegram_publisher: Publisher | None = None,
        *,
        publishers: Mapping[Any, Publisher] | None = None,
    ) -> None:
        self._publishers: dict[str, Publisher] = {}
        if isinstance(reminder_publisher, Mapping):
            for key, value in reminder_publisher.items():
                self.register(key, value)
        elif reminder_publisher is not None:
            self.register(PublicationType.REMINDER, reminder_publisher)
        if telegram_publisher is not None:
            self.register(PublicationType.TELEGRAM, telegram_publisher)
        if publishers is not None:
            for key, value in publishers.items():
                self.register(key, value)

    def register(self, publication_type: Any, publisher: Publisher) -> None:
        """Регистрирует Publisher для указанного типа публикации (REMINDER, TELEGRAM, VK)."""
        key = _normalize_publication_type(publication_type)
        self._publishers[key] = publisher

    def resolve(
        self,
        publication_type: Any,
        *,
        post_id: int | None = None,
    ) -> Publisher:
        """Возвращает зарегистрированный Publisher по publication_type (SPEC-016 §10–12, §22)."""
        try:
            key = _normalize_publication_type(publication_type)
        except InvalidPublicationTypeError:
            logger.error(
                "Publisher not found post_id=%s publication_type=%s",
                post_id,
                publication_type,
            )
            raise

        publisher = self._publishers.get(key)
        if publisher is None:
            logger.error(
                "Publisher not found post_id=%s publication_type=%s",
                post_id,
                key,
            )
            raise PublisherNotFoundError(key)

        logger.info("Publisher resolved post_id=%s type=%s", post_id, key)
        return publisher
