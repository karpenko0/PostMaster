"""ORM-модели таблиц PostMaster: users (SPEC-007), posts (SPEC-008).

SQLite не сохраняет часовой пояс, поэтому даты лежат в UTC без tzinfo. Репозитории
переводят их в обе стороны (см. repositories/user_repository.py).
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from postmaster.database.base import Base


class UserModel(Base):
    """Таблица users: одна строка на Telegram-пользователя (BR-01)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PostModel(Base):
    """Публикация: явный FK и индексы для очереди и выборки пользователя."""

    __tablename__ = "posts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'PUBLISHED', 'FAILED', 'CANCELLED')",
            name="ck_posts_status",
        ),
        CheckConstraint(
            "publication_type IN ('REMINDER', 'TELEGRAM')", name="ck_posts_publication_type"
        ),
        CheckConstraint("length(trim(telegram_file_id)) > 0", name="ck_posts_file_id"),
        CheckConstraint("length(trim(telegram_file_unique_id)) > 0", name="ck_posts_unique_id"),
        CheckConstraint("length(trim(timezone)) > 0", name="ck_posts_timezone"),
        CheckConstraint(
            "publication_type != 'TELEGRAM' OR "
            "(target_id IS NOT NULL AND length(trim(target_id)) > 0)",
            name="ck_posts_telegram_target",
        ),
        CheckConstraint(
            "(status = 'PUBLISHED' AND published_at IS NOT NULL) OR "
            "(status != 'PUBLISHED' AND published_at IS NULL)",
            name="ck_posts_published_at",
        ),
        Index("ix_posts_user_status_scheduled", "user_id", "status", "scheduled_at_utc"),
        Index("ix_posts_status_scheduled", "status", "scheduled_at_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    telegram_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_file_unique_id: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_at_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING", server_default="PENDING"
    )
    publication_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
