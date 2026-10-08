"""Публикация, которую создаёт сценарий (SPEC-003, BR-03).

Таблица posts описана в SPEC-008. Пока запись не сохраняется, объект живёт в ответе сервиса.
Время публикации хранится в UTC.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Post:
    """Публикация: владелец, фотография (file_id Telegram) и время выхода в UTC."""

    owner_telegram_id: int
    photo_file_id: str
    scheduled_at: datetime
