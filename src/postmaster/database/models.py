"""ORM-модели таблиц PostMaster. Таблица users описана в SPEC-002, §6.

SQLite не сохраняет часовой пояс, поэтому даты лежат в UTC без tzinfo. Репозитории
переводят их в обе стороны (см. repositories/user_repository.py).
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from postmaster.database.base import Base


class UserModel(Base):
    """Таблица users: одна строка на Telegram-пользователя (BR-01)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    first_name: Mapped[str] = mapped_column(String, nullable=False)
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
