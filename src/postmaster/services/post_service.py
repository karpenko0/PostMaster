"""PostService: публикации пользователя.

В SPEC-003 появляется create_post: он собирает публикацию из готового черновика (BR-03).
Запись в существующую таблицу posts пока не выполняется: интеграция — SPEC-009.
Остальные методы (выборка, отмена) добавляются в SPEC-017 и SPEC-018. Все выборки фильтруются
по текущему пользователю (SPEC-024).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from postmaster.domain.post import PostDraft


class PostService:
    """Работа с публикациями пользователя."""

    async def create_post(
        self,
        *,
        owner_telegram_id: int,
        photo_file_id: str,
        scheduled_at: datetime,
        timezone: str,
        local_datetime: datetime | None = None,
    ) -> PostDraft:
        """Собирает DTO с UTC и IANA-поясом. Ничего не сохраняет (SPEC-003 D9)."""
        if scheduled_at.tzinfo is None or scheduled_at.utcoffset() != timedelta(0):
            raise ValueError("scheduled_at должен быть timezone-aware UTC")
        ZoneInfo(timezone)
        return PostDraft(
            owner_telegram_id=owner_telegram_id,
            photo_file_id=photo_file_id,
            scheduled_at=scheduled_at.astimezone(UTC),
            timezone=timezone,
            local_datetime=local_datetime,
        )
