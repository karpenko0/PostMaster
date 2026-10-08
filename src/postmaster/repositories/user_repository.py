"""Хранение пользователей в таблице users (SPEC-002, BR-01, BR-02).

Все запросы к users находятся здесь. Наружу выходят только доменные объекты User,
поэтому сервисы не работают с ORM и не импортируют SQLAlchemy.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from postmaster.database.models import UserModel
from postmaster.domain.user import DuplicateTelegramUserError, User


class UserRepository:
    """Доступ к таблице users. Каждый вызов выполняется в собственной сессии."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_by_telegram_id(self, telegram_user_id: int) -> User | None:
        """Возвращает пользователя или None, если его нет."""
        async with self._session_factory() as session:
            model = await session.scalar(
                select(UserModel).where(UserModel.telegram_user_id == telegram_user_id)
            )
        return None if model is None else _to_domain(model)

    async def add(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        first_name: str,
        now: datetime,
    ) -> User:
        """Добавляет пользователя. Пояс остаётся пустым, и действует DEFAULT_TIMEZONE (BR-04).

        Raises:
            DuplicateTelegramUserError: строка с таким telegram_user_id уже есть (BR-01).
        """
        model = UserModel(
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            timezone=None,
            created_at=_to_storage(now),
            updated_at=_to_storage(now),
        )
        try:
            async with self._session_factory() as session, session.begin():
                session.add(model)
                await session.flush()
        except IntegrityError as exc:
            # Нарушение могло быть и по другому полю. Считаем дубликатом, только если строка есть.
            if await self.find_by_telegram_id(telegram_user_id) is None:
                raise
            raise DuplicateTelegramUserError("пользователь уже зарегистрирован") from exc
        return _to_domain(model)


def _to_domain(model: UserModel) -> User:
    return User(
        id=model.id,
        telegram_user_id=model.telegram_user_id,
        username=model.username,
        first_name=model.first_name,
        timezone=model.timezone,
        created_at=_from_storage(model.created_at),
        updated_at=_from_storage(model.updated_at),
    )


def _to_storage(value: datetime) -> datetime:
    """Переводит дату с часовым поясом в UTC без tzinfo, как она хранится в SQLite."""
    return value.astimezone(UTC).replace(tzinfo=None)


def _from_storage(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC)
