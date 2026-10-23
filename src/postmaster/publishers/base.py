"""Интерфейс Publisher (BR-03, SPEC-014, SPEC-015).

PublicationService публикует пост только через этот интерфейс. Конкретные реализации
(Telegram-канал, позже другие площадки) находятся в этом же пакете.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Результат успешной публикации (SPEC-014 §3).

    Возвращается Publisher.publish(). PublicationService использует его только для
    логирования — финальный статус (PUBLISHED) выставляет сам.
    """

    post_id: int
    external_id: str | None = None


class Publisher(ABC):
    """Публикует пост на внешней площадке.

    Аргумент post пока не типизирован: конкретные Publisher появятся в SPEC-014/015.
    """

    @abstractmethod
    async def publish(self, post: Any) -> PublishResult:
        """Публикует пост. При ошибке выбрасывает исключение, которое обрабатывает сервис.

        Возвращает PublishResult с идентификатором опубликованного сообщения (если площадка
        его возвращает) — сервис записывает его в лог, но не использует для смены статуса.
        """
