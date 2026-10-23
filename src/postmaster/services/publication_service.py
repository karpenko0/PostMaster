"""PublicationService: выполнение публикации через Publisher (SPEC-010, SPEC-013).

Сервис координирует процесс публикации:
1. Атомарно забирает пост в работу: PENDING -> PROCESSING (claim_for_processing).
   Только rowcount=1 даёт право вызвать Publisher.
2. Выбирает нужный Publisher по типу публикации.
3. Вызывает Publisher.publish(post).
4. При успехе переводит PROCESSING -> PUBLISHED с временной меткой.
5. При ошибке переводит PROCESSING -> FAILED с сообщением об ошибке.
6. При наличии retry_policy и оставшихся попыток переводит PROCESSING -> PENDING
   для повторной попытки.

Сервис работает только с абстракцией Publisher (BR-03) и не импортирует telebot.
"""

import asyncio
import logging
from typing import Protocol

from postmaster.domain.post import (
    Post,
    PostNotFoundError,
    PostStatus,
    PublicationType,
)
from postmaster.publishers.base import Publisher
from postmaster.publishers.reminder_publisher import ReminderPublisher
from postmaster.publishers.telegram_publisher import TelegramChannelPublisher
from postmaster.repositories.post_repository import PostRepository
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)


class PublicationError(Exception):
    """Ошибка публикации. Содержит post_id для диагностики."""

    def __init__(self, post_id: int, message: str) -> None:
        self.post_id = post_id
        super().__init__(message)


class PostNotPendingError(PublicationError):
    """Пост не в статусе PENDING — нельзя начать публикацию."""


class PostClaimLostError(PublicationError):
    """Другой исполнитель уже забрал пост (claim вернул None)."""


class PublisherResolver(Protocol):
    """Порт выбора Publisher по типу публикации."""

    def resolve(self, publication_type: PublicationType) -> Publisher: ...


class DefaultPublisherResolver:
    """Стандартный резолвер: выбирает Publisher по типу публикации."""

    def __init__(
        self,
        telegram_publisher: TelegramChannelPublisher | None = None,
        reminder_publisher: ReminderPublisher | None = None,
    ) -> None:
        self._telegram = telegram_publisher
        self._reminder = reminder_publisher

    def resolve(self, publication_type: PublicationType) -> Publisher:
        if publication_type == PublicationType.TELEGRAM:
            if self._telegram is None:
                raise PublicationError(0, "Telegram publisher не настроен")
            return self._telegram
        if publication_type == PublicationType.REMINDER:
            if self._reminder is None:
                raise PublicationError(0, "Reminder publisher не настроен")
            return self._reminder
        raise PublicationError(0, f"неизвестный тип публикации: {publication_type}")


class RetryPolicy:
    """Политика повторных попыток при ошибке публикации."""

    def __init__(self, max_attempts: int = 3, delay_seconds: float = 1.0) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts должен быть >= 1")
        self.max_attempts = max_attempts
        self.delay_seconds = delay_seconds

    def should_retry(self, attempt: int) -> bool:
        """Нужно ли повторять попытку. attempt — номер текущей попытки (1-based)."""
        return attempt < self.max_attempts


class PublicationService:
    """Запускает публикацию поста и обновляет его статус."""

    def __init__(
        self,
        repository: PostRepository,
        publisher_resolver: PublisherResolver,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._publishers = publisher_resolver
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=1)

    async def publish(self, post_id: int) -> Post:
        """Выполняет публикацию поста с атомарным claim, выполнением и финальным переходом.

        SPEC-013 §1: атомарный claim — единственный момент, когда исполнитель
        получает право на вызов Publisher. Конкурирующие вызовы получают право
        ровно один раз: один делает claim, остальные получают PostClaimLostError.
        """
        logger.info("Publication started post_id=%s", post_id)

        # Проверяем существование поста, чтобы отличить «нет записи» от «не PENDING».
        existing = await self._repository.get_by_id(post_id)
        if existing is None:
            raise PostNotFoundError(f"публикация {post_id} не найдена")

        # Атомарный claim (SPEC-013 §1, §4): только rowcount=1 даёт право на execution.
        # Одновременные вызовы: один получает claim, остальные — PostClaimLostError.
        claimed = await self._repository.claim_for_processing(post_id)
        if claimed is None:
            # Другой исполнитель уже забрал пост или он отменён.
            raise PostClaimLostError(post_id, "другой исполнитель уже забрал пост")

        # С этого момента исполнитель имеет право на вызов Publisher.
        try:
            publisher = self._publishers.resolve(claimed.publication_type)
        except PublicationError as exc:
            await self._mark_failed(post_id, str(exc))
            raise

        last_error: Exception | None = None
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                await publisher.publish(claimed)
            except Exception as exc:
                error_message = str(exc)[:500] if str(exc) else type(exc).__name__
                last_error = exc
                logger.warning(
                    "Publication attempt failed post_id=%s attempt=%s error=%s",
                    post_id,
                    attempt,
                    type(exc).__name__,
                )
                if self._retry_policy.should_retry(attempt):
                    # Возвращаем в PENDING для повторной попытки.
                    try:
                        await self._repository.update_status(post_id, PostStatus.PENDING)
                    except Exception:
                        logger.warning("Failed to reset post to PENDING post_id=%s", post_id)
                    await asyncio.sleep(self._retry_policy.delay_seconds)
                    # Повторный claim перед следующей попыткой.
                    reclaimed = await self._repository.claim_for_processing(post_id)
                    if reclaimed is None:
                        raise PostClaimLostError(post_id, "потерян claim во время retry") from None
                    claimed = reclaimed
                    continue
                # Попытки исчерпаны.
                await self._mark_failed(post_id, error_message)
                logger.error(
                    "Publication failed after %s attempts post_id=%s",
                    attempt,
                    post_id,
                )
                raise PublicationError(post_id, error_message) from exc

            # Успех.
            published = await self._repository.update_status(
                post_id, PostStatus.PUBLISHED, published_at=utc_now()
            )
            logger.info("Publication completed post_id=%s attempt=%s", post_id, attempt)
            return published

        # Сюда не должны дойти, но на всякий случай.
        if last_error is not None:
            raise PublicationError(post_id, str(last_error)) from last_error
        raise PublicationError(post_id, "неизвестная ошибка публикации")

    async def _mark_failed(self, post_id: int, error_message: str) -> None:
        try:
            await self._repository.update_status(
                post_id, PostStatus.FAILED, error_message=error_message
            )
        except Exception:
            logger.warning("Failed to mark post as FAILED post_id=%s", post_id)
