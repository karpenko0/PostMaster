"""Пользователь PostMaster и его состояния в сценарии (SPEC-002).

Модуль не импортирует Telegram и базу данных, поэтому правила проверяются без них.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class UserState(StrEnum):
    """Состояние пользователя в сценарии (BR-03).

    Хранится в памяти процесса, а не в таблице users (решение D1 плана SPEC-002).
    """

    WAITING_PHOTO = "WAITING_PHOTO"


class DuplicateTelegramUserError(Exception):
    """Пользователь с таким telegram_user_id уже существует (BR-01)."""


@dataclass(frozen=True, slots=True)
class User:
    """Пользователь бота. Даты приходят из репозитория с часовым поясом UTC."""

    id: int
    telegram_user_id: int
    username: str | None
    first_name: str
    timezone: str | None
    created_at: datetime
    updated_at: datetime

    def effective_timezone(self, default_timezone: str) -> str:
        """Пояс пользователя или DEFAULT_TIMEZONE, если пользователь его не задал (BR-04)."""
        return self.timezone or default_timezone
