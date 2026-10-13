"""Персистентность Post (SPEC-008). Telegram-обработчики не обращаются к репозиторию.

SQLite хранит datetime без tzinfo; на границе репозитория все системные даты — aware UTC.
Условные UPDATE не дают отменять чужие/уже выполняемые публикации или менять терминальный статус.
"""

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from postmaster.database.models import PostModel, UserModel
from postmaster.domain.post import (
    ALLOWED_TRANSITIONS,
    InvalidPostTransitionError,
    Post,
    PostNotFoundError,
    PostStatus,
    PublicationType,
    UserNotFoundError,
)
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)


def _to_storage(value: datetime | None, name: str) -> datetime:
    if value is None:
        raise ValueError(f"{name} обязателен")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} должен быть aware UTC")
    return value.astimezone(UTC).replace(tzinfo=None)


def _from_storage(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC)


def _to_domain(model: PostModel) -> Post:
    return Post(
        id=model.id,
        user_id=model.user_id,
        telegram_file_id=model.telegram_file_id,
        telegram_file_unique_id=model.telegram_file_unique_id,
        scheduled_at_utc=_from_storage(model.scheduled_at_utc),
        timezone=model.timezone,
        status=PostStatus(model.status),
        publication_type=PublicationType(model.publication_type),
        target_id=model.target_id,
        error_message=model.error_message,
        created_at=_from_storage(model.created_at),
        updated_at=_from_storage(model.updated_at),
        published_at=None if model.published_at is None else _from_storage(model.published_at),
        caption=model.caption,
    )


class PostRepository:
    """CRUD модели публикации; не вызывает Publisher и не создаёт задания Scheduler."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_post(
        self,
        *,
        user_id: int,
        telegram_file_id: str,
        telegram_file_unique_id: str,
        scheduled_at_utc: datetime,
        timezone: str,
        publication_type: PublicationType,
        target_id: str | None = None,
        caption: str | None = None,
        now: datetime | None = None,
    ) -> Post:
        """Создаёт одну PENDING-запись, связанную с users.id (RT-008-01, ERR-008-01…05)."""
        if not telegram_file_id or not telegram_file_id.strip():
            raise ValueError("telegram_file_id обязателен")
        if not telegram_file_unique_id or not telegram_file_unique_id.strip():
            raise ValueError("telegram_file_unique_id обязателен")
        when = _to_storage(scheduled_at_utc, "scheduled_at_utc")
        timestamp = _to_storage(now if now is not None else utc_now(), "now")
        ZoneInfo(timezone)
        kind = PublicationType(publication_type)
        if kind == PublicationType.TELEGRAM and (not target_id or not target_id.strip()):
            raise ValueError("для TELEGRAM требуется target_id")
        async with self._session_factory() as session, session.begin():
            if await session.get(UserModel, user_id) is None:
                raise UserNotFoundError("пользователь для публикации не найден")
            model = PostModel(
                user_id=user_id,
                telegram_file_id=telegram_file_id,
                telegram_file_unique_id=telegram_file_unique_id,
                scheduled_at_utc=when,
                timezone=timezone,
                status=PostStatus.PENDING.value,
                publication_type=kind.value,
                target_id=target_id,
                error_message=None,
                created_at=timestamp,
                updated_at=timestamp,
                published_at=None,
                caption=caption,
            )
            session.add(model)
            await session.flush()
        post = _to_domain(model)
        logger.info("Post created id=%s user_id=%s status=PENDING", post.id, post.user_id)
        return post

    async def get_by_id(self, post_id: int) -> Post | None:
        """Получает запись без авторизации; для пользовательской операции проверьте владельца."""
        async with self._session_factory() as session:
            model = await session.get(PostModel, post_id)
            return None if model is None else _to_domain(model)

    async def list_pending_by_user(self, user_id: int) -> list[Post]:
        """Пользователь видит только собственные PENDING-записи в порядке времени."""
        statement = (
            select(PostModel)
            .where(PostModel.user_id == user_id, PostModel.status == PostStatus.PENDING.value)
            .order_by(PostModel.scheduled_at_utc, PostModel.id)
        )
        async with self._session_factory() as session:
            return [_to_domain(model) for model in (await session.scalars(statement)).all()]

    async def list_pending(self) -> list[Post]:
        """Очередь для будущего recovery (SPEC-011); только PENDING, по UTC."""
        statement = (
            select(PostModel)
            .where(PostModel.status == PostStatus.PENDING.value)
            .order_by(PostModel.scheduled_at_utc, PostModel.id)
        )
        async with self._session_factory() as session:
            return [_to_domain(model) for model in (await session.scalars(statement)).all()]

    async def update_status(
        self,
        post_id: int,
        status: PostStatus,
        error_message: str | None = None,
        published_at: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> Post:
        """Разрешённый переход §42–44 с условным UPDATE по прежнему статусу."""
        target = PostStatus(status)
        if error_message is not None and target != PostStatus.FAILED:
            raise ValueError("error_message допустим только для FAILED")
        if published_at is not None and target != PostStatus.PUBLISHED:
            raise ValueError("published_at допустим только для PUBLISHED")
        timestamp = _to_storage(now if now is not None else utc_now(), "now")
        published = (
            (_to_storage(published_at, "published_at") if published_at is not None else timestamp)
            if target == PostStatus.PUBLISHED
            else None
        )
        async with self._session_factory() as session, session.begin():
            model = await session.get(PostModel, post_id)
            if model is None:
                raise PostNotFoundError("публикация не найдена")
            old = PostStatus(model.status)
            if target not in ALLOWED_TRANSITIONS[old]:
                raise InvalidPostTransitionError(f"недопустимый переход {old}->{target}")
            result = await session.execute(
                update(PostModel)
                .where(PostModel.id == post_id, PostModel.status == old.value)
                .values(
                    status=target.value,
                    error_message=error_message,
                    published_at=published,
                    updated_at=timestamp,
                )
            )
            if result.rowcount != 1:
                raise InvalidPostTransitionError("статус изменился параллельно")
            await session.refresh(model)
            post = _to_domain(model)
        if target == PostStatus.FAILED:
            logger.error("Post failed id=%s", post_id)
        elif target == PostStatus.PUBLISHED:
            logger.info("Post published id=%s", post_id)
        else:
            logger.info("Post status changed id=%s %s->%s", post_id, old, target)
        return post

    async def cancel_post(self, post_id: int, user_id: int, *, now: datetime | None = None) -> bool:
        """Отмена только собственного PENDING по одному условному UPDATE (RT-008-05)."""
        stamp = _to_storage(now if now is not None else utc_now(), "now")
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(PostModel)
                .where(
                    PostModel.id == post_id,
                    PostModel.user_id == user_id,
                    PostModel.status == PostStatus.PENDING.value,
                )
                .values(status=PostStatus.CANCELLED.value, updated_at=stamp)
            )
        if result.rowcount == 1:
            logger.info("Post status changed id=%s PENDING->CANCELLED", post_id)
            return True
        return False
