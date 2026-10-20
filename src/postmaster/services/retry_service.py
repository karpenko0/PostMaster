"""Ограниченный retry временных ошибок публикации (SPEC-022)."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from postmaster.publishers.telegram_errors import TelegramErrorCode
from postmaster.repositories.post_repository import PostRepository
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)

RETRY_DELAYS: dict[int, int] = {1: 30, 2: 120, 3: 600}
_RETRYABLE_CODES = frozenset(
    {
        TelegramErrorCode.TELEGRAM_API_TIMEOUT.value,
        TelegramErrorCode.NETWORK_ERROR.value,
        TelegramErrorCode.TELEGRAM_API_UNAVAILABLE.value,
    }
)


class RetryScheduler(Protocol):
    def schedule_retry(self, *, post_id: int, run_at: datetime, attempt: int) -> None: ...

    def remove_retry(self, post_id: int) -> bool: ...


class RetryErrorCode(StrEnum):
    RETRY_NOT_ALLOWED = "RETRY_NOT_ALLOWED"
    RETRY_LIMIT_REACHED = "RETRY_LIMIT_REACHED"
    RETRY_SCHEDULING_FAILED = "RETRY_SCHEDULING_FAILED"


class RetryService:
    """Управляет retry-графиком, сохраняя atomic claim за PublicationService."""

    def __init__(
        self,
        post_repository: PostRepository,
        scheduler: RetryScheduler,
        *,
        now_fn: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = post_repository
        self._scheduler = scheduler
        self._now_fn = now_fn

    async def handle_retryable_error(
        self,
        post_id: int,
        error_code: str,
        error_message: str,
        attempt: int,
    ) -> bool:
        """Планирует номерную retry-попытку; False означает финальный исход без retry."""
        if error_code not in _RETRYABLE_CODES:
            logger.error(
                "Retry not allowed post_id=%s error_code=%s retry_code=%s",
                post_id,
                error_code,
                RetryErrorCode.RETRY_NOT_ALLOWED.value,
            )
            await self._mark_failed(post_id, error_message)
            return False

        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt not in RETRY_DELAYS:
            logger.error(
                "Retry limit reached post_id=%s retry_code=%s",
                post_id,
                RetryErrorCode.RETRY_LIMIT_REACHED.value,
            )
            await self._mark_failed(post_id, error_message)
            return False

        delay_seconds = RETRY_DELAYS[attempt]
        now = self._now_fn()
        retry_at = now + timedelta(seconds=delay_seconds)
        try:
            # Сначала регистрируем job, пока Post ещё PROCESSING. Если Scheduler недоступен,
            # запись можно безопасно завершить из PROCESSING в FAILED.
            self._scheduler.schedule_retry(
                post_id=post_id,
                run_at=retry_at,
                attempt=attempt,
            )
        except Exception as exc:
            logger.error(
                "Retry scheduling failed post_id=%s attempt=%s retry_code=%s error_type=%s",
                post_id,
                attempt,
                RetryErrorCode.RETRY_SCHEDULING_FAILED.value,
                type(exc).__name__,
            )
            await self._mark_failed(post_id, error_message)
            return False

        try:
            released = await self._repository.release_post_for_retry(
                post_id,
                error_message=error_message,
                now=now,
            )
        except Exception as exc:
            self._remove_retry_job(post_id)
            logger.error(
                "Retry state update failed post_id=%s attempt=%s error_type=%s",
                post_id,
                attempt,
                type(exc).__name__,
            )
            await self._mark_failed(post_id, error_message)
            return False

        if not released:
            self._remove_retry_job(post_id)
            logger.error(
                "Retry state update skipped post_id=%s attempt=%s",
                post_id,
                attempt,
            )
            return False

        logger.warning(
            "Retry scheduled post_id=%s attempt=%s delay=%ss",
            post_id,
            attempt,
            delay_seconds,
        )
        return True

    async def _mark_failed(self, post_id: int, error_message: str) -> None:
        try:
            updated = await self._repository.mark_failed(
                post_id,
                error_message=error_message or "Retry failed",
                now=self._now_fn(),
            )
        except Exception as exc:
            logger.error(
                "Retry finalization failed post_id=%s error_type=%s",
                post_id,
                type(exc).__name__,
            )
            return
        if not updated:
            logger.error("Retry finalization skipped post_id=%s", post_id)

    def _remove_retry_job(self, post_id: int) -> None:
        try:
            self._scheduler.remove_retry(post_id)
        except Exception as exc:
            logger.warning(
                "Retry job removal failed post_id=%s error_type=%s",
                post_id,
                type(exc).__name__,
            )
