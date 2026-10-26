"""PublicationService: выполнение публикации через Publisher (SPEC-010, SPEC-013, SPEC-016).

Сервис координирует процесс публикации:
1. Атомарно забирает пост в работу: PENDING -> PROCESSING (claim_for_processing).
   Только rowcount=1 даёт право вызвать Publisher.
2. Резолвит Publisher по типу публикации через PublisherResolver (SPEC-016).
3. Вызывает Publisher.publish(post).
4. При успехе переводит PROCESSING -> PUBLISHED с временной меткой.
5. При ошибке переводит PROCESSING -> FAILED с сообщением об ошибке.
6. При наличии retry_policy и оставшихся попыток переводит PROCESSING -> PENDING
   для повторной попытки.

Сервис работает только с абстракцией Publisher (BR-03) и не импортирует telebot.
"""

import asyncio
import logging

from postmaster.domain.post import (
    Post,
    PostNotFoundError,
    PostStatus,
)
from postmaster.publishers.resolver import (
    PublisherNotConfiguredError,
    PublisherResolver,
    UnknownPublicationTypeError,
)
from postmaster.publishers.telegram_errors import TelegramErrorMapper
from postmaster.publishers.telegram_publisher import (
    TelegramAPIError,
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


# Типы, которые считаются retryable по умолчанию (без error_mapper, SPEC-022 §1):
# сетевые и временные ошибки Telegram API, а также их типизированные обёртки.
_DEFAULT_RETRYABLE: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    TelegramTimeoutError,
)


class PublicationError(Exception):
    """Ошибка публикации. Содержит post_id для диагностики."""

    def __init__(self, post_id: int, message: str) -> None:
        self.post_id = post_id
        super().__init__(message)


class PostNotPendingError(PublicationError):
    """Пост не в статусе PENDING — нельзя начать публикацию."""


class PostClaimLostError(PublicationError):
    """Другой исполнитель уже забрал пост (claim вернул None)."""


class RetryPolicy:
    """Политика повторных попыток при ошибке публикации (SPEC-022 §1, §4).

    По умолчанию выполняется три повтора с задержками +30s, +2m, +10m. После
    четвёртой неудачной попытки пост переходит в FAILED.

    delays[i] — задержка в секундах перед повтором после неудачной попытки i+1.
    Количество попыток суммарно равно 1 + len(delays).
    """

    #: Задержки перед повторами: +30s, +2m, +10m (SPEC-022 §4).
    DEFAULT_DELAYS: tuple[int, ...] = (30, 120, 600)

    def __init__(self, delays: tuple[int, ...] = DEFAULT_DELAYS) -> None:
        if any(delay < 0 for delay in delays):
            raise ValueError("задержка должна быть >= 0")
        self.delays = delays

    @property
    def max_attempts(self) -> int:
        """Суммарное число попыток: первая плюс повторы. Пустые delays → одна попытка."""
        return 1 + len(self.delays)

    def should_retry(self, attempt: int) -> bool:
        """Нужно ли повторять попытку. attempt — номер текущей попытки (1-based)."""
        return 1 <= attempt <= len(self.delays)

    def delay_after_attempt(self, attempt: int) -> int:
        """Задержка в секундах после неудачной попытки с номером attempt (1-based).

        attempt=1 → +30s, attempt=2 → +2m, attempt=3 → +10m.
        """
        if not self.should_retry(attempt):
            raise ValueError(f"нет задержки для попытки {attempt}")
        return self.delays[attempt - 1]


class PublicationService:
    """Запускает публикацию поста и обновляет его статус."""

    def __init__(
        self,
        repository: PostRepository,
        publisher_resolver: PublisherResolver,
        retry_policy: RetryPolicy | None = None,
        error_mapper: TelegramErrorMapper | None = None,
    ) -> None:
        self._repository = repository
        self._publishers = publisher_resolver
        # SPEC-022: если retry_policy не передан, повторы отключены — одна попытка.
        self._retry_policy = retry_policy or RetryPolicy(delays=())
        # Если передан маппер, он управляет retry-решением через флаг retryable
        # (SPEC-021 §3). Без маппера сервис использует простую эвристику:
        # TimeoutError/ConnectionError — повторяемые, всё остальное — нет.
        self._error_mapper = error_mapper

    def _is_retryable(self, exc: Exception) -> bool:
        """True, если ошибку нужно повторить (SPEC-022 §1).

        При наличии error_mapper используется его классификация (TIMEOUT/NETWORK/
        UNAVAILABLE — retryable). Без маппера повторяемы только TimeoutError и
        ConnectionError (см. _DEFAULT_RETRYABLE). Чтобы типизированные исключения
        Publisher (TelegramTimeoutError и т. п.) корректно опознавались, решение
        принимается по оригинальному исключению из ``__cause__``.
        """
        if self._error_mapper is not None:
            return self._error_mapper.map_exception(exc).retryable
        cause = exc.__cause__
        candidates = (exc, cause) if isinstance(cause, BaseException) else (exc,)
        return any(isinstance(c, _DEFAULT_RETRYABLE) for c in candidates)

    def _build_error_message(self, exc: Exception) -> str:
        """Готовит безопасный текст ошибки для лога и Post.error_message.

        TelegramAPIError уже несёт нормализованное сообщение (SPEC-021 §7).
        Для прочих исключений текст обрезается до 500 символов и заменяется на
        имя типа, если строка пустая.
        """
        if isinstance(exc, TelegramAPIError):
            return exc.error.message
        raw = str(exc).strip()
        if not raw:
            return type(exc).__name__
        return raw[:500]

    async def publish(self, post_id: int) -> Post:
        """Выполняет публикацию поста с атомарным claim, выполнением и финальным переходом.

        SPEC-013 §1: атомарный claim — единственный момент, когда исполнитель
        получает право на вызов Publisher. Конкурирующие вызовы получают право
        ровно один раз: один делает claim, остальные получают PostClaimLostError.
        """
        log_event(logger, EVENT_PUBLICATION_START, post_id=post_id)

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
        except (PublisherNotConfiguredError, UnknownPublicationTypeError) as exc:
            # SPEC-016: неизвестный тип или ненастроенный publisher — пост не публикуется,
            # переходим в FAILED с информативным сообщением.
            await self._mark_failed(post_id, str(exc))
            raise PublicationError(post_id, str(exc)) from exc

        last_error: Exception | None = None
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                await publisher.publish(claimed)
            except Exception as exc:
                error_message = self._build_error_message(exc)
                retryable = self._is_retryable(exc)
                error_code = self._error_code(exc)
                last_error = exc
                log_event(
                    logger,
                    EVENT_PUBLICATION_ATTEMPT_FAILED,
                    level=logging.WARNING,
                    post_id=post_id,
                    attempt=attempt,
                    error=type(exc).__name__,
                    error_code=error_code,
                    retryable=retryable,
                )
                if retryable and self._retry_policy.should_retry(attempt):
                    # Возвращаем в PENDING для повторной попытки.
                    try:
                        await self._repository.update_status(post_id, PostStatus.PENDING)
                    except Exception:
                        log_event(
                            logger,
                            "publication_reset_failed",
                            level=logging.WARNING,
                            post_id=post_id,
                            error="reset_to_pending_failed",
                        )
                    log_event(
                        logger,
                        EVENT_RETRY,
                        post_id=post_id,
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        delay_seconds=self._retry_policy.delay_after_attempt(attempt),
                    )
                    # SPEC-022 §4: ждём delays[attempt-1] секунд перед повтором
                    # (+30s → +2m → +10m).
                    await asyncio.sleep(self._retry_policy.delay_after_attempt(attempt))
                    # Повторный claim перед следующей попыткой.
                    reclaimed = await self._repository.claim_for_processing(post_id)
                    if reclaimed is None:
                        raise PostClaimLostError(post_id, "потерян claim во время retry") from None
                    claimed = reclaimed
                    continue
                # Попытки исчерпаны или ошибка не повторяемая.
                await self._mark_failed(post_id, error_message)
                log_event(
                    logger,
                    EVENT_PUBLICATION_FAILED,
                    level=logging.ERROR,
                    post_id=post_id,
                    attempts=attempt,
                    error=type(exc).__name__,
                    error_code=error_code,
                )
                raise PublicationError(post_id, error_message) from exc

            # Успех.
            published = await self._repository.update_status(
                post_id, PostStatus.PUBLISHED, published_at=utc_now()
            )
            log_event(
                logger,
                EVENT_PUBLICATION_OK,
                post_id=post_id,
                attempt=attempt,
            )
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
            log_event(
                logger,
                "mark_failed_error",
                level=logging.WARNING,
                post_id=post_id,
            )

    def _error_code(self, exc: Exception) -> str:
        """Извлекает error_code из исключения для структурированного лога."""
        if isinstance(exc, TelegramAPIError):
            return exc.error.code.value
        return type(exc).__name__
