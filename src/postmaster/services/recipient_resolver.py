"""Адаптеры между сервисами и портами publishers (SPEC-010)."""

from postmaster.services.user_service import UserService


class UserServiceReminderRecipientResolver:
    """Реализация ReminderRecipientResolver через UserService."""

    def __init__(self, user_service: UserService) -> None:
        self._users = user_service

    async def get_telegram_user_id(self, user_id: int) -> int | None:
        user = await self._users.get_by_id(user_id)
        return None if user is None else user.telegram_user_id
