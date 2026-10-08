"""PostService: публикации пользователя.

В SPEC-003 появляется create_post: он собирает публикацию из готового черновика (BR-03).
Запись в таблицу posts пока не выполняется: таблица описана в SPEC-001 §6 как часть SPEC-008.
Остальные методы (выборка, отмена) добавляются в SPEC-017 и SPEC-018. Все выборки фильтруются
по текущему пользователю (SPEC-024).
"""

from datetime import datetime

from postmaster.domain.post import Post


class PostService:
    """Работа с публикациями пользователя."""

    async def create_post(
        self,
        *,
        owner_telegram_id: int,
        photo_file_id: str,
        scheduled_at: datetime,
    ) -> Post:
        """Собирает публикацию из черновика. Ничего не сохраняет (решение D9 плана SPEC-003)."""
        return Post(
            owner_telegram_id=owner_telegram_id,
            photo_file_id=photo_file_id,
            scheduled_at=scheduled_at,
        )
