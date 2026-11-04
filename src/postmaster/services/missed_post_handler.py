"""MissedPostHandler: обработка пропущенных публикаций (SPEC-012).

Missed Post — это запись со статусом PENDING, у которой scheduled_at_utc <= now.
Такие записи появляются, когда приложение было выключено в момент наступления
времени публикации.

Обработчик:
1. Перечитывает Post из БД по post_id — БД остаётся источником истины.
2. Проверяет статус: выполняются только PENDING.
3. Проверяет, что момент действительно прошёл: scheduled_at_utc <= now.
4. Передаёт запись в PublicationService для публикации.

Сервис не импортирует telebot (BR-04) и не создаёт новых записей Post.
"""

import logging
from datetime import datetime
from typing import Protocol

from postmaster.domain.post import Post, PostStatus
from postmaster.repositories.post_repository import PostRepository
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)


class PublicationExecutor(Protocol):
    """Порт выполнения публикации. Реализация — PublicationService."""

    async def publish(self, post_id: int): ...


class MissedPostHandler:
    """Находит пропущенные публикации и передаёт их в PublicationService."""

    def __init__(
        self,
        repository: PostRepository,
        publication_executor: PublicationExecutor,
    ) -> None:
        self._repository = repository
        self._executor = publication_executor

    async def handle(self, post_id: int) -> Post | None:
        """Обрабатывает одну пропущенную публикацию.

        Возвращает обработанный Post или None, если запись не подходит для публикации
        (не найдена, статус не PENDING, время ещё не наступило).
        """
        post = await self._repository.get_by_id(post_id)
        if post is None:
            logger.warning("Missed post not found post_id=%s", post_id)
            return None
        if post.status != PostStatus.PENDING:
            logger.info(
                "Missed post skipped: status is not PENDING post_id=%s status=%s",
                post_id,
                post.status,
            )
            return None
        if post.scheduled_at_utc > utc_now():
            logger.info(
                "Missed post skipped: scheduled time is in the future post_id=%s",
                post_id,
            )
            return None
        logger.info(
            "Missed post detected post_id=%s scheduled_at_utc=%s",
            post_id,
            post.scheduled_at_utc,
        )
        await self._executor.publish(post_id)
        return post

    async def handle_many(self, post_ids: list[int]) -> "MissedPostReport":
        """Обрабатывает несколько пропущенных публикаций.

        Ошибка одной записи не блокирует обработку остальных.
        """
        report = MissedPostReport()
        for post_id in post_ids:
            try:
                result = await self.handle(post_id)
            except Exception as exc:
                logger.error(
                    "Missed post handling failed post_id=%s error=%s",
                    post_id,
                    type(exc).__name__,
                )
                report.errors.append({"post_id": post_id, "error": type(exc).__name__})
                continue
            if result is not None:
                report.handled += 1
            else:
                report.skipped += 1
        logger.info(
            "Missed posts processed handled=%s skipped=%s errors=%s",
            report.handled,
            report.skipped,
            len(report.errors),
        )
        return report

    async def find_missed(self, now: datetime | None = None) -> list[int]:
        """Возвращает id пропущенных публикаций: PENDING и scheduled_at_utc <= now."""
        moment = now if now is not None else utc_now()
        posts = await self._repository.list_pending()
        return [post.id for post in posts if post.scheduled_at_utc <= moment]


class MissedPostReport:
    """Результат обработки пропущенных публикаций."""

    def __init__(self) -> None:
        self.handled: int = 0
        self.skipped: int = 0
        self.errors: list[dict[str, object]] = []
