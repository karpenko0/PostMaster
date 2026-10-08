"""Пользователь PostMaster (SPEC-002).

Состояния диалога описаны в domain/dialog.py. Модуль не импортирует Telegram и базу данных.
"""

from dataclasses import dataclass
from datetime import datetime


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
