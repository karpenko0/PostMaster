"""PublicationService: идемпотентное выполнение публикации с atomic claim (SPEC-013…016).

Любой источник запуска (Scheduler, Recovery, Missed Job) вызывает PublicationService.execute,
не переводя Post в PROCESSING самостоятельно (§19). Право на вызов Publisher получает только
обработчик, чей условный UPDATE перевёл запись из PENDING в PROCESSING (BR-013-01…10).
Выбор конкретной реализации Publisher выполняется через PublisherResolver (SPEC-016 §13–16).
"""

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from postmaster.domain.post import (
    Post,
    PublicationErrorCode,
    PublicationType,
    TemporaryPublicationError,
)
from postmaster.publishers.base import Publisher, PublishResult
from postmaster.publishers.resolver import PublisherNotFoundError, PublisherResolver
from postmaster.repositories.post_repository import PostRepository
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)


class PublishResultError(Exception):
    """Обёртка над неуспешным PublishResult для передачи в предикат retry-политики."""

    def __init__(self, result: PublishResult) -> None:
        self.result = result
        self.error_code = result.error_code
        self.error_message = result.error_message
        self.retryable = result.retryable
        self.is_temporary = result.retryable
        super().__init__(result.error_message or result.error_code or "Publication failed")


class PublicationService:
    """Запуск публикации поста и атомарное обновление его статуса (SPEC-013…016)."""

    def __init__(
        self,
        post_repository: PostRepository | None = None,
        publisher: Publisher
        | PublisherResolver
        | Mapping[PublicationType | str, Publisher]
        | None = None,
        *,
        publisher_resolver: PublisherResolver | None = None,
        publishers: Mapping[PublicationType | str, Publisher] | None = None,
        is_temporary_error: Callable[[BaseException], bool] | None = None,
        now_fn: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = post_repository
        self._default_publisher: Publisher | None = None
        self._resolver: PublisherResolver | None = publisher_resolver

        if isinstance(publisher, PublisherResolver):
            self._resolver = publisher
        elif isinstance(publisher, Mapping):
            if self._resolver is None:
                self._resolver = PublisherResolver(publishers=publisher)
            else:
                for key, value in publisher.items():
                    self._resolver.register(key, value)
        elif publisher is not None:
            self._default_publisher = publisher

        if publishers is not None:
            if self._resolver is None:
                self._resolver = PublisherResolver(publishers=publishers)
            else:
                for key, value in publishers.items():
                    self._resolver.register(key, value)

        self._is_temporary_error = is_temporary_error
        self._now_fn = now_fn

    @property
    def publisher_resolver(self) -> PublisherResolver | None:
        """Возвращает подключённый PublisherResolver (SPEC-016 §13, §16)."""
        return self._resolver

    async def execute(self, post_id: int) -> bool:
        """Атомарно захватывает PENDING-пост, выбирает Publisher, публикует и меняет статус."""
        if self._repository is None:
            raise RuntimeError("PublicationService требует PostRepository")

        claimed = await self._repository.claim_post_for_processing(post_id, now=self._now_fn())
        if not claimed:
            return False

        post = await self._repository.get_by_id(post_id)
        if post is None:
            logger.error(
                "Post not found after claim post_id=%s code=%s",
                post_id,
                PublicationErrorCode.POST_NOT_FOUND_AFTER_CLAIM.value,
            )
            return False

        try:
            publisher = self._resolve_publisher(post)
            result = await publisher.publish(post)
        except Exception as exc:
            await self._handle_publish_error(post_id, exc)
            return False

        if isinstance(result, PublishResult) and not result.success:
            await self._handle_publish_error(post_id, PublishResultError(result))
            return False

        published_at = self._now_fn()
        try:
            updated = await self._repository.mark_published(
                post_id, published_at=published_at, now=published_at
            )
        except Exception:
            logger.error(
                "Final status update failed post_id=%s code=%s",
                post_id,
                PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value,
            )
            return False
        if not updated:
            logger.error(
                "Final status update failed post_id=%s code=%s",
                post_id,
                PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value,
            )
            return False
        return True

    execute_post = execute

    def _resolve_publisher(self, post: Post | Any) -> Publisher:
        publication_type = getattr(post, "publication_type", None)
        post_id = getattr(post, "id", None)
        if self._resolver is not None:
            try:
                return self._resolver.resolve(publication_type, post_id=post_id)
            except PublisherNotFoundError:
                if self._default_publisher is not None:
                    return self._default_publisher
                raise
        if self._default_publisher is not None:
            return self._default_publisher
        raise PublisherNotFoundError(publication_type)

    def _is_temporary(self, exc: BaseException) -> bool:
        if self._is_temporary_error is not None:
            return bool(self._is_temporary_error(exc))
        if isinstance(exc, TemporaryPublicationError):
            return True
        return bool(getattr(exc, "is_temporary", False) or getattr(exc, "retryable", False))

    async def _handle_publish_error(self, post_id: int, exc: BaseException) -> None:
        assert self._repository is not None
        message = str(exc).strip() or type(exc).__name__
        now = self._now_fn()
        if self._is_temporary(exc):
            try:
                updated = await self._repository.release_post_for_retry(
                    post_id, error_message=message, now=now
                )
            except Exception:
                logger.error(
                    "Final status update failed post_id=%s code=%s",
                    post_id,
                    PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value,
                )
                return
            if not updated:
                logger.error(
                    "Final status update failed post_id=%s code=%s",
                    post_id,
                    PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value,
                )
            return

        try:
            updated = await self._repository.mark_failed(post_id, error_message=message, now=now)
        except Exception:
            logger.error(
                "Final status update failed post_id=%s code=%s",
                post_id,
                PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value,
            )
            return
        if not updated:
            logger.error(
                "Final status update failed post_id=%s code=%s",
                post_id,
                PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value,
            )
