"""PublicationService: идемпотентное выполнение публикации с atomic claim (SPEC-013…016).

Любой источник запуска (Scheduler, Recovery, Missed Job) вызывает PublicationService.execute,
не переводя Post в PROCESSING самостоятельно (§19). Право на вызов Publisher получает только
обработчик, чей условный UPDATE перевёл запись из PENDING в PROCESSING (BR-013-01…10).
Выбор конкретной реализации Publisher выполняется через PublisherResolver (SPEC-016 §13–16).
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from postmaster.domain.post import (
    Post,
    PostStatus,
    PublicationErrorCode,
    PublicationType,
    TemporaryPublicationError,
)
from postmaster.publishers.base import Publisher, PublishResult
from postmaster.publishers.resolver import PublisherNotFoundError, PublisherResolver
from postmaster.publishers.telegram_errors import (
    TelegramError,
    TelegramErrorCode,
    TelegramErrorMapper,
    redact_secrets,
)
from postmaster.publishers.telegram_publisher import (
    TelegramPermissionError,
    TelegramTimeoutError,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.utils.clock import utc_now
from postmaster.utils.logging_config import (
    EVENT_PUBLICATION_ATTEMPT_FAILED,
    EVENT_PUBLICATION_FAILED,
    EVENT_PUBLICATION_OK,
    EVENT_PUBLICATION_START,
    EVENT_RETRY,
    log_event,
)

logger = logging.getLogger(__name__)

DEFAULT_RETRY_DELAYS = (30, 120, 600)
PUBLICATION_TIMEOUT_SECONDS = 120


class PublicationError(Exception):
    """Ошибка публикации legacy-контракта SPEC-010/021/022."""


class PostClaimLostError(PublicationError):
    """Пост больше не принадлежит исполнителю (отменён или забран другим процессом)."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Политика повторов SPEC-022: задержки между попытками в секундах."""

    delays: tuple[int, ...] = DEFAULT_RETRY_DELAYS

    def __post_init__(self) -> None:
        if any(delay < 0 for delay in self.delays):
            raise ValueError("задержка не может быть отрицательной")

    @property
    def max_attempts(self) -> int:
        return 1 + len(self.delays)

    def should_retry(self, attempt: int) -> bool:
        return 1 <= attempt <= len(self.delays)

    def delay_after_attempt(self, attempt: int) -> int:
        return self.delays[attempt - 1]


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """Результат legacy-метода PublicationService.publish()."""

    status: PostStatus
    published_at: datetime | None = None
    error_message: str | None = None


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
        retry_policy: RetryPolicy | None = None,
        error_mapper: TelegramErrorMapper | None = None,
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
        self._retry_policy = retry_policy or RetryPolicy(delays=())
        self._error_mapper = error_mapper or TelegramErrorMapper()

    @property
    def publisher_resolver(self) -> PublisherResolver | None:
        """Возвращает подключённый PublisherResolver (SPEC-016 §13, §16)."""
        return self._resolver

    async def execute(
        self,
        post_id: int,
        *,
        expected_schedule_version: int | None = None,
        expected_run_at: datetime | None = None,
        ignore_schedule: bool = False,
    ) -> bool:
        """Атомарно захватывает PENDING-пост, выбирает Publisher, публикует и меняет статус.

        PM06-R2-003: версия и время задания передаются в условный UPDATE, поэтому
        устаревшее задание APScheduler не получает право на публикацию.
        """
        if self._repository is None:
            raise RuntimeError("PublicationService требует PostRepository")

        claimed = await self._repository.claim_post_for_processing(
            post_id,
            now=self._now_fn(),
            expected_schedule_version=expected_schedule_version,
            expected_run_at=expected_run_at,
            ignore_schedule=ignore_schedule,
        )
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

        deadline = asyncio.timeout(PUBLICATION_TIMEOUT_SECONDS)
        try:
            publisher = self._resolve_publisher(post)
            async with deadline:
                result = await publisher.publish(post)
        except Exception as exc:
            if isinstance(exc, TimeoutError) and deadline.expired():
                exc = TemporaryPublicationError("Истекло время отправки публикации")
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

    async def publish(self, post_id: int) -> PublicationResult:
        """Совместимый контракт SPEC-010/020/021/022/025/032.

        Ручной вызов публикации (тесты и служебные команды) не является заданием
        Scheduler, поэтому проверка due-time для него не применяется. Повторы
        выполняются по ``RetryPolicy``; после каждой неудачи пост возвращается в
        PENDING, чтобы параллельная отмена могла остановить публикацию.
        """
        if self._repository is None:
            raise RuntimeError("PublicationService требует PostRepository")
        policy = self._retry_policy
        attempt = 0
        log_event(logger, EVENT_PUBLICATION_START, post_id=post_id)
        while True:
            attempt += 1
            claimed = await self._repository.claim_post_for_processing(
                post_id, now=self._now_fn(), ignore_schedule=True
            )
            if not claimed:
                raise PostClaimLostError("пост недоступен для публикации")
            post = await self._repository.get_by_id(post_id)
            if post is None:
                raise PublicationError("пост не найден после захвата")
            try:
                publisher = self._resolve_publisher(post)
                result = await publisher.publish(post)
                if isinstance(result, PublishResult) and not result.success:
                    raise PublishResultError(result)
            except Exception as exc:
                error = self._normalize_error(exc)
                message = error.message
                log_event(
                    logger,
                    EVENT_PUBLICATION_ATTEMPT_FAILED,
                    level=logging.WARNING,
                    post_id=post_id,
                    attempt=attempt,
                    error_code=error.code.value,
                    retryable=error.retryable,
                )
                if error.retryable and policy.should_retry(attempt):
                    await self._repository.release_post_for_retry(
                        post_id, error_message=message, now=self._now_fn()
                    )
                    delay = policy.delay_after_attempt(attempt)
                    log_event(
                        logger,
                        EVENT_RETRY,
                        level=logging.WARNING,
                        post_id=post_id,
                        attempt=attempt,
                        delay_seconds=delay,
                        error_code=error.code.value,
                    )
                    if delay:
                        await asyncio.sleep(delay)
                    # Отмена во время ожидания останавливает дальнейшие повторы.
                    fresh = await self._repository.get_by_id(post_id)
                    if fresh is not None and fresh.status != PostStatus.PENDING:
                        raise PostClaimLostError("публикация отменена во время повтора") from None
                    continue
                await self._repository.mark_failed(post_id, message, now=self._now_fn())
                log_event(
                    logger,
                    EVENT_PUBLICATION_FAILED,
                    level=logging.ERROR,
                    post_id=post_id,
                    attempt=attempt,
                    error_code=error.code.value,
                )
                raise PublicationError(message) from None

            published_at = self._now_fn()
            updated = await self._repository.mark_published(
                post_id, published_at=published_at, now=published_at
            )
            if not updated:
                raise PublicationError("не удалось зафиксировать статус PUBLISHED")
            log_event(
                logger,
                EVENT_PUBLICATION_OK,
                post_id=post_id,
                attempt=attempt,
                status=PostStatus.PUBLISHED.value,
            )
            return PublicationResult(
                status=PostStatus.PUBLISHED,
                published_at=published_at,
                error_message=None,
            )

    def _normalize_error(self, exc: BaseException) -> TelegramError:
        """Нормализует ошибку публикации (SPEC-021) для логов, retry и error_message.

        Приоритет — тип исключения (SPEC-022 §1): типизированные ошибки Publisher'ов
        содержат уже готовый код и признак retryable.
        """
        if isinstance(exc, PublishResultError) and exc.error_code:
            mapped = self._error_mapper.map_exception(exc)
            return TelegramError(
                code=mapped.code,
                message=mapped.message,
                retryable=bool(exc.retryable),
            )
        mapped = self._error_mapper.map_exception(exc)
        # SPEC-022 §1: тип исключения важнее текста. Publisher поднимает
        # TelegramTimeoutError/TelegramPermissionError даже тогда, когда текст
        # ошибки не содержит ключевых слов (например, «fail1» в тестах).
        if isinstance(exc, TelegramTimeoutError):
            return TelegramError(
                code=TelegramErrorCode.TIMEOUT,
                message=self._safe_message(exc, TelegramErrorCode.TIMEOUT),
                retryable=True,
            )
        if isinstance(exc, TelegramPermissionError):
            return TelegramError(
                code=TelegramErrorCode.PERMISSION,
                message=self._safe_message(exc, TelegramErrorCode.PERMISSION),
                retryable=False,
            )
        return mapped

    @staticmethod
    def _safe_message(exc: BaseException, code: TelegramErrorCode) -> str:
        """Текст ошибки с кодом и без секретов (SPEC-021 §7)."""
        raw = redact_secrets(str(exc).strip() or type(exc).__name__)
        return f"{code.value}: {raw}"[:500]

    def _is_legacy_temporary(self, exc: BaseException) -> bool:
        if self._is_temporary(exc):
            return True
        retryable_names = {
            "TelegramTimeoutError",
            "TelegramNetworkError",
            "TelegramUnavailableError",
        }
        if type(exc).__name__ in retryable_names or isinstance(
            exc, (TimeoutError, ConnectionError)
        ):
            return True
        normalized = getattr(exc, "error", None)
        return bool(getattr(normalized, "retryable", False))

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
