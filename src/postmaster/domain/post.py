"""Публикация, которую создаёт сценарий (SPEC-003, BR-03).

Таблица posts описана в SPEC-008. Пока запись не сохраняется, объект живёт в ответе сервиса.
scheduled_at хранится в UTC; локальное время и пояс передаются следующему этапу SPEC-009.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Post:
    """Публикация: фото, UTC-момент и локальное время/пояс для следующего этапа."""

    owner_telegram_id: int
    photo_file_id: str
    scheduled_at: datetime
    timezone: str | None = None
    local_datetime: datetime | None = None
