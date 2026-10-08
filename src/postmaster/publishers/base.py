"""Интерфейс Publisher (BR-03).

PublicationService публикует пост только через этот интерфейс. Конкретные реализации
(Telegram-канал, позже другие площадки) находятся в этом же пакете.
"""

from abc import ABC, abstractmethod
from typing import Any


class Publisher(ABC):
    """Публикует пост на внешней площадке.

    Аргумент post пока не типизирован: доменная модель Post появится в SPEC-008.
    """

    @abstractmethod
    async def publish(self, post: Any) -> None:
        """Публикует пост. При ошибке выбрасывает исключение, которое обрабатывает сервис."""
