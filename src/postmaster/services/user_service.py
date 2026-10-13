"""UserService: регистрация пользователя по /start (SPEC-002).

Контракт из §5 SPEC-002: get_or_create(telegram_user_id, username, first_name) -> User.
Сервис работает с доменными объектами и репозиторием. Он не импортирует telebot и SQLAlchemy
(BR-01, BR-04). Проверка прав доступа добавится в SPEC-024.
"""

import logging

from postmaster.domain.timezone import resolve_user_timezone
from postmaster.domain.user import DuplicateTelegramUserError, User
from postmaster.repositories.user_repository import UserRepository
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)


class UserService:
    """Работа с учётными записями пользователей бота."""

    def __init__(self, repository: UserRepository, *, default_timezone: str) -> None:
        self._repository = repository
        self._default_timezone = default_timezone

    async def get_or_create(
        self,
        telegram_user_id: int,
        username: str | None,
        first_name: str | None,
    ) -> User:
        """Возвращает существующего пользователя или создаёт нового (AC-01, AC-02, BR-02).

        Если username или first_name изменились, они обновляются вместе с updated_at (решение D3
        плана SPEC-002, пересмотрено при закрытии открытых вопросов). Пояс и дата создания не
        меняются. Если данные совпадают, запись не пишется.
        """
        existing = await self._repository.find_by_telegram_id(telegram_user_id)
        if existing is None:
            try:
                created = await self._repository.add(
                    telegram_user_id=telegram_user_id,
                    username=username,
                    first_name=first_name,
                    now=utc_now(),
                )
                logger.info("User created telegram_user_id=%s", telegram_user_id)
                return created
            except DuplicateTelegramUserError:
                # Параллельный /start успел создать строку (решение D7). Берём её, дубликата нет.
                existing = await self._repository.find_by_telegram_id(telegram_user_id)
                if existing is None:
                    raise
        logger.info("Existing user resolved telegram_user_id=%s", telegram_user_id)
        if existing.username == username and existing.first_name == first_name:
            return existing
        updated = await self._repository.update_profile(
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            now=utc_now(),
        )
        logger.info("User profile updated user_id=%s", updated.id)
        return updated

    async def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        """Находит профиль по стабильному Telegram ID, не по изменяемому username."""
        return await self._repository.get_by_telegram_id(telegram_user_id)

    def effective_timezone(self, user: User) -> str:
        """Часовой пояс пользователя или DEFAULT_TIMEZONE, если пояс не задан (BR-04)."""
        return user.effective_timezone(self._default_timezone)

    async def timezone_of(self, telegram_user_id: int) -> str:
        """Пояс пользователя по telegram_user_id (BR-04). Без записи действует DEFAULT_TIMEZONE."""
        user = await self._repository.find_by_telegram_id(telegram_user_id)
        if user is None:
            return resolve_user_timezone(None, self._default_timezone)
        return self.effective_timezone(user)
