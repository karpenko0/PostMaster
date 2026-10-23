"""PublisherResolver: выбор Publisher по типу публикации (SPEC-016).

Сервисы (PublicationService, Scheduler, Recovery, MissedPostHandler) знают только
протокол PublisherResolver — конкретные реализации Publisher скрыты за ним.

DefaultPublisherResolver маршрутизирует REMINDER → ReminderPublisher,
TELEGRAM → TelegramChannelPublisher. Неизвестный тип или ненастроенный
publisher приводят к типизированным исключениям — пост НЕ публикуется.
"""

from typing import Protocol

from postmaster.domain.post import PublicationType
from postmaster.publishers.base import Publisher


class PublisherResolver(Protocol):
    """Порт выбора Publisher по типу публикации (SPEC-016 §1, §4)."""

    def resolve(self, publication_type: PublicationType) -> Publisher:
        """Возвращает Publisher для данного типа публикации.

        Raises:
            UnknownPublicationTypeError: publication_type не поддерживается.
            PublisherNotConfiguredError: publisher для типа не зарегистрирован.
        """
        ...


class UnknownPublicationTypeError(Exception):
    """Тип публикации не поддерживается ни одним Publisher (SPEC-016 §7)."""

    def __init__(self, publication_type: object) -> None:
        self.publication_type = publication_type
        super().__init__(f"неизвестный тип публикации: {publication_type}")


class PublisherNotConfiguredError(Exception):
    """Publisher для запрошенного типа не зарегистрирован в резолвере."""

    def __init__(self, publication_type: PublicationType) -> None:
        self.publication_type = publication_type
        super().__init__(f"publisher для типа {publication_type.value} не настроен")


class DefaultPublisherResolver:
    """Стандартный резолвер: маршрутизирует типы публикаций к Publisher'ам (SPEC-016).

    Регистрация по типу:
    - REMINDER → reminder_publisher
    - TELEGRAM → telegram_publisher

    Если Publisher не зарегистрирован, возбуждается PublisherNotConfiguredError —
    пост НЕ публикуется.
    """

    def __init__(
        self,
        telegram_publisher: Publisher | None = None,
        reminder_publisher: Publisher | None = None,
    ) -> None:
        self._telegram = telegram_publisher
        self._reminder = reminder_publisher

    def resolve(self, publication_type: PublicationType) -> Publisher:
        """Возвращает Publisher для типа публикации или возбуждает исключение."""
        if publication_type == PublicationType.TELEGRAM:
            if self._telegram is None:
                raise PublisherNotConfiguredError(publication_type)
            return self._telegram
        if publication_type == PublicationType.REMINDER:
            if self._reminder is None:
                raise PublisherNotConfiguredError(publication_type)
            return self._reminder
        raise UnknownPublicationTypeError(publication_type)
