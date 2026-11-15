"""PublicationService: идемпотентное выполнение публикации с atomic claim (SPEC-013…016, SPEC-022).

Любой источник запуска (Scheduler, Recovery, Missed Job) вызывает PublicationService.execute,
не переводя Post в PROCESSING самостоятельно (§19). Право на вызов Publisher получает только
обработчик, чей условный UPDATE перевёл запись из PENDING в PROCESSING (BR-013-01…10).
Выбор конкретной реализации Publisher выполняется через PublisherResolver (SPEC-016 §13–16).

Retry-механизм (SPEC-022): ``execute`` делает не более ``RetryPolicy.max_attempts`` попыток.
Повтор выполняется только для retryable-ошибок (timeout/network/unavailable), каждая попытка
заново проходит atomic claim, поэтому параллельные исполнители не дублируют публикацию.
По исчерпании попыток пост переходит в FAILED и поднимается ``PublicationError``.

Совместимость: ``publish()`` — прежний контракт «sync-публикация с ожиданием ретраев»,
``execute()`` — контракт Scheduler/Recovery (возвращает bool и не бросает ошибок, переводя
неуспех в статус поста). Внутренняя реализация у них общая.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
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
from postmaster.publishers.telegram_errors import DEFAULT_ERROR_MAPPER, TelegramErrorMapper
from postmaster.repositories.post_repository import PostRepository
from postmaster.utils.clock import utc_now
from postmaster.utils.logging_config import (
    EVENT_PUBLICATION_ATTEMPT_FAILED,
    EVENT_PUBLICATION_FAILED,
    EVENT_PUBLICATION_OK,
    EVENT_PUBLICATION_START,
    EVENT_PUBLISH_ERROR,
    EVENT_RETRY,
    log_event,
)

logger = logging.getLogger(__name__)

# Задержки по умолчанию между попытками: +30s, +2m, +10m (SPEC-022 §4).
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (30, 120, 600)


class PublicationError(RuntimeError):
    """Публикация не выполнена: пост остался неопубликованным (SPEC-021 §1, SPEC-022 §4).

    Несёт нормализованный ``error_code`` (например, ``PERMISSION``/``TIMEOUT``) и признак
    ``retryable``, чтобы вызывающая сторона могла отличить окончательный отказ от
    временного сбоя, по которому имеет смысл повторить публикацию позже.
    """

    def __init__(
        self,
        message: str,
        *,
        post_id: int | None = None,
        error_code: str | None = None,
        retryable: bool = False,
        attempts: int = 0,
    ) -> None:
        self.post_id = post_id
        self.error_code = error_code
        self.retryable = retryable
        self.attempts = attempts
        super().__init__(message)


class _ClaimFailed(Exception):
    """Внутренний сигнал: захват поста не состоялся (пробрасывается как PublicationError)."""

    def __init__(self, error: PublicationError) -> None:
        self.error = error
        super().__init__(str(error))


class PostClaimLostError(PublicationError):
    """Пост потерян для исполнителя: статус перестал быть PROCESSING (отмена/другой воркер).

    Возникает, когда между попытками пост отменили или забрал другой исполнитель. Повторять
    публикацию в этом случае нельзя: решение уже принято в БД (SPEC-013 §19, SPEC-022 §8).
    """

    def __init__(self, post_id: int | None = None, *, attempts: int = 0) -> None:
        super().__init__(
            "Публикация прервана: пост больше не принадлежит этому исполнителю",
            post_id=post_id,
            error_code=PublicationErrorCode.CLAIM_FAILED.value,
            retryable=False,
            attempts=attempts,
        )


class PublishResultError(Exception):
    """Обёртка над неуспешным PublishResult для передачи в предикат retry-политики."""

    def __init__(self, result: PublishResult) -> None:
        self.result = result
        self.error_code = result.error_code
        self.error_message = result.error_message
        self.retryable = result.retryable
        self.is_temporary = result.retryable
        super().__init__(result.error_message or result.error_code or "Publication failed")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Расписание повторов публикации (SPEC-022 §4).

    ``delays`` — паузы в секундах перед каждой следующей попыткой: для значений по
    умолчанию это +30s, +2m, +10m. ``max_attempts`` включает первую попытку, поэтому
    три задержки дают четыре попытки. Пустой кортеж отключает повторы.
    """

    delays: Sequence[float] = DEFAULT_RETRY_DELAYS
    sleep: Callable[[float], Any] | None = None

    def __post_init__(self) -> None:
        normalized: list[float] = []
        for delay in self.delays:
            value = float(delay)
            if value < 0:
                raise ValueError(f"задержка повтора не может быть отрицательной: {delay}")
            normalized.append(value)
        object.__setattr__(self, "delays", tuple(normalized))

    @property
    def max_attempts(self) -> int:
        """Всего попыток: первая + по одной на каждую задержку."""
        return 1 + len(self.delays)

    def should_retry(self, attempt: int) -> bool:
        """True, если после попытки ``attempt`` (нумерация с 1) нужен ещё повтор."""
        return attempt < self.max_attempts

    def delay_after_attempt(self, attempt: int) -> float:
        """Пауза перед повтором после неудачной попытки ``attempt`` (нумерация с 1)."""
        if not self.should_retry(attempt):
            raise ValueError(f"повтор после попытки {attempt} не предусмотрен политикой")
        return self.delays[attempt - 1]

    async def wait_after_attempt(self, attempt: int) -> float:
        """Ждёт задержку перед повтором и возвращает её (0 для нулевых задержек)."""
        delay = self.delay_after_attempt(attempt)
        if delay:
            sleeper = self.sleep or asyncio.sleep
            result = sleeper(delay)
            if asyncio.iscoroutine(result):
                await result
        return delay


# Политика без повторов: одна попытка, как до SPEC-022.
NO_RETRY_POLICY = RetryPolicy(delays=())

# Максимум одновременно удерживаемых in-process блокировок по post_id.
MAX_ACTIVE_CLAIMS = 1024


class PublicationService:
    """Запуск публикации поста и атомарное обновление его статуса (SPEC-013…016, SPEC-022)."""

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
        retry_policy: RetryPolicy | None = None,
        error_mapper: Any | None = None,
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
        # Без явной политики поведение прежнее: одна попытка и перевод в FAILED.
        # Политика с повторами включается осознанно (например, в app.py), чтобы уже
        # сохранённые публикации не меняли сценарий выполнения неявно.
        self._retry_policy = retry_policy if retry_policy is not None else NO_RETRY_POLICY
        # Явная политика означает, что исчерпание повторов — окончательный отказ (SPEC-022 §4).
        # Без политики сохраняется поведение SPEC-013 §28–30: временная ошибка возвращает
        # пост в PENDING, и он будет выполнен следующим claim (Recovery / missed job).
        self._policy_explicit = retry_policy is not None
        # Классификатор ошибок: явный маппер, если он передан, иначе общий DEFAULT_ERROR_MAPPER.
        # Благодаря этому решение о повторе принимается по нормализованному коду
        # (TIMEOUT/NETWORK/UNAVAILABLE — retryable) и одинаково во всех точках вызова,
        # в том числе для типизированных обёрток Publisher'ов (SPEC-021 §1, SPEC-022 §4).
        self._error_mapper = error_mapper
        self._classifier: TelegramErrorMapper = error_mapper or DEFAULT_ERROR_MAPPER
        self._now_fn = now_fn
        # In-process блокировки по post_id: в рамках одного процесса второй исполнитель
        # того же поста сразу получает отказ и не мешает первому. Между процессами
        # защиту даёт atomic claim в БД, поэтому блокировка — дополнение, не замена.
        self._active_claims: dict[tuple[int, int], asyncio.Lock] = {}

    @property
    def publisher_resolver(self) -> PublisherResolver | None:
        """Возвращает подключённый PublisherResolver (SPEC-016 §13, §16)."""
        return self._resolver

    @property
    def retry_policy(self) -> RetryPolicy:
        """Действующая политика повторов (SPEC-022 §4)."""
        return self._retry_policy

    async def execute(self, post_id: int) -> bool:
        """Выполняет публикацию по контракту Scheduler/Recovery: True только при успехе.

        Ошибка не пробрасывается наружу (Scheduler не должен падать из-за поста), но и не
        маскируется: пост получает FAILED, а причина пишется в лог и ``Post.error_message``.
        """
        try:
            await self.publish(post_id)
        except PublicationError as exc:
            log_event(
                logger,
                EVENT_PUBLICATION_FAILED,
                level=logging.ERROR,
                post_id=post_id,
                error_code=exc.error_code,
                retryable=exc.retryable,
                attempts=exc.attempts,
            )
            return False
        return True

    execute_post = execute

    async def publish(self, post_id: int) -> Post:
        """Публикует пост с повторами по ``RetryPolicy`` и возвращает актуальную запись.

        Raises:
            PostClaimLostError: пост отменён/перехвачен другим исполнителем.
            PublicationError: публикация не удалась после всех попыток.
        """
        if self._repository is None:
            raise RuntimeError("PublicationService требует PostRepository")

        claim_key = (id(self), post_id)
        lock = self._active_claims.get(claim_key)
        if lock is not None and lock.locked():
            raise PostClaimLostError(post_id)
        lock = asyncio.Lock()
        self._active_claims[claim_key] = lock
        await lock.acquire()
        try:
            return await self._publish_with_retries(post_id)
        except _ClaimFailed as failure:
            raise failure.error from failure
        finally:
            lock.release()
            self._active_claims.pop(claim_key, None)
            if len(self._active_claims) > MAX_ACTIVE_CLAIMS:
                for stale_key in [k for k, v in self._active_claims.items() if not v.locked()]:
                    self._active_claims.pop(stale_key, None)

    async def _publish_with_retries(self, post_id: int) -> Post:
        """Попытки публикации по ``RetryPolicy`` (SPEC-022 §4)."""
        policy = self._retry_policy
        attempt = 0
        while True:
            attempt += 1
            assert self._repository is not None
            if not await self._repository.claim_post_for_processing(post_id, now=self._now_fn()):
                # Claim не наш: пост отменён, уже опубликован или забран другим
                # исполнителем. Читать запись и вызывать Publisher в этом случае нельзя.
                raise _ClaimFailed(PostClaimLostError(post_id, attempts=attempt))
            post = await self._repository.get_by_id(post_id)
            if post is None:
                raise _ClaimFailed(await self._claim_error(post_id, attempts=attempt))

            if attempt == 1:
                log_event(logger, EVENT_PUBLICATION_START, post_id=post_id)

            try:
                await self._publish_once(post)
            except Exception as exc:
                if attempt == 1 and self._error_mapper is not None:
                    self._log_mapped_error(post, exc)
                retryable = self._is_temporary(exc)
                log_event(
                    logger,
                    EVENT_PUBLICATION_ATTEMPT_FAILED,
                    level=logging.WARNING,
                    post_id=post_id,
                    attempt=attempt,
                    retryable=retryable,
                    error_code=self._error_code(exc),
                )
                if retryable and policy.should_retry(attempt):
                    delay = policy.delay_after_attempt(attempt)
                    log_event(
                        logger,
                        EVENT_RETRY,
                        post_id=post_id,
                        attempt=attempt,
                        delay_seconds=int(delay) if float(delay).is_integer() else delay,
                    )
                    released = await self._release_for_retry(post_id, exc)
                    if not released:
                        # Пост уже не PROCESSING (например, отменён): повторять нечего.
                        raise PostClaimLostError(post_id, attempts=attempt) from exc
                    await policy.wait_after_attempt(attempt)
                    continue

                if retryable and not self._policy_explicit:
                    # Временный сбой без настроенной политики: пост остаётся в очереди.
                    # Окончательный FAILED здесь потерял бы публикацию пользователя.
                    await self._release_for_retry(post_id, exc)
                else:
                    await self._mark_failed(post_id, exc)
                raise self._build_error(
                    post_id, exc, attempts=attempt, retryable=retryable
                ) from exc

            published_at = self._now_fn()
            try:
                updated = await self._repository.mark_published(
                    post_id, published_at=published_at, now=published_at
                )
            except Exception:
                # Ошибка записи не должна выглядеть как успешная публикация, но и не
                # должна вылетать из execute() в Scheduler (SPEC-013 ERR-013-03).
                logger.error(
                    "Final status update failed post_id=%s code=%s",
                    post_id,
                    PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value,
                )
                updated = False
            if not updated:
                logger.error(
                    "Final status update failed post_id=%s code=%s",
                    post_id,
                    PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value,
                )
                raise PublicationError(
                    "Не удалось зафиксировать результат публикации",
                    post_id=post_id,
                    error_code=PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value,
                    attempts=attempt,
                )
            log_event(logger, EVENT_PUBLICATION_OK, post_id=post_id, attempts=attempt)
            fresh = await self._repository.get_by_id(post_id)
            return fresh if fresh is not None else post

    async def _claim_error(
        self, post_id: int, *, attempts: int
    ) -> PublicationError | PostClaimLostError:
        """Различает «пост исчез после claim» и «claim не наш» по фактическому статусу.

        Если запись всё ещё PROCESSING, значит claim удался, но чтение не вернуло Post —
        это отдельный диагностируемый случай (ERR-013-02), а не конфликт исполнителей.
        """
        assert self._repository is not None
        try:
            current = await self._repository.get_by_id(post_id)
        except Exception:
            logger.exception("Не удалось перечитать Post после claim post_id=%s", post_id)
            return PostClaimLostError(post_id, attempts=attempts)
        if current is None:
            logger.error(
                "Post not found after claim post_id=%s code=%s",
                post_id,
                PublicationErrorCode.POST_NOT_FOUND_AFTER_CLAIM.value,
            )
            return PublicationError(
                "Пост не найден после захвата",
                post_id=post_id,
                error_code=PublicationErrorCode.POST_NOT_FOUND_AFTER_CLAIM.value,
                attempts=attempts,
            )
        if current.status == PostStatus.PROCESSING:
            # Запись осталась в PROCESSING: публикация не завершена. Возвращаем в очередь,
            # чтобы пост не завис навсегда (PM-07: раньше PROCESSING оставался сиротой).
            await self._release_for_retry(post_id, RuntimeError("claim read failed"))
        return PostClaimLostError(post_id, attempts=attempts)

    async def _publish_once(self, post: Post | Any) -> None:
        """Одна попытка публикации: выбор Publisher и вызов. Неуспех — исключение."""
        publisher = self._resolve_publisher(post)
        result = await publisher.publish(post)
        if isinstance(result, PublishResult) and not result.success:
            raise PublishResultError(result)

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
        """Является ли ошибка временной (SPEC-021 §4, SPEC-022 §4).

        Явный предикат и маппер ошибок имеют приоритет: если они классифицировали
        ошибку как non-retryable, повтор не выполняется даже для сетевых исключений
        (SPEC-022: повтор только для retryable-кодов). Без маппера действует
        консервативное правило «тип исключения»: TimeoutError/ConnectionError и их
        типизированные обёртки повторяются, программные ошибки — нет.
        """
        if self._is_temporary_error is not None:
            return bool(self._is_temporary_error(exc))
        if isinstance(exc, TemporaryPublicationError):
            return True
        if bool(getattr(exc, "retryable", False) or getattr(exc, "is_temporary", False)):
            return True
        mapped = self._classify(exc)
        return bool(mapped.retryable) if mapped is not None else False

    def _classify(self, exc: BaseException):
        """Нормализует ошибку через ``TelegramErrorMapper`` (SPEC-021 §1)."""
        try:
            return self._classifier.map_exception(exc)
        except Exception:
            logger.exception("TelegramErrorMapper не смог классифицировать ошибку")
            return None

    def _error_code(self, exc: BaseException) -> str | None:
        mapped = self._classify(exc)
        if mapped is not None:
            return getattr(mapped, "code", None) and str(getattr(mapped.code, "value", mapped.code))
        return getattr(exc, "error_code", None)

    def _log_mapped_error(self, post: Post | Any, exc: BaseException) -> None:
        """Пишет нормализованный код ошибки в лог без секретов (SPEC-021 §7, SPEC-025 §8)."""
        code = self._error_code(exc)
        if code is None:
            return
        log_event(
            logger,
            EVENT_PUBLISH_ERROR,
            level=logging.WARNING,
            post_id=getattr(post, "id", None),
            error_code=code,
        )

    def _build_error(
        self, post_id: int, exc: BaseException, *, attempts: int, retryable: bool
    ) -> PublicationError:
        """Собирает ``PublicationError`` с безопасным текстом (без Bot Token)."""
        message = str(exc).strip() or type(exc).__name__
        mapped = self._classify(exc)
        if mapped is not None:
            message = getattr(mapped, "message", message) or message
        return PublicationError(
            message,
            post_id=post_id,
            error_code=self._error_code(exc),
            retryable=retryable,
            attempts=attempts,
        )

    async def _release_for_retry(self, post_id: int, exc: BaseException) -> bool:
        """PROCESSING → PENDING перед повтором (SPEC-013 §28–30)."""
        assert self._repository is not None
        message = str(exc).strip() or type(exc).__name__
        try:
            updated = await self._repository.release_post_for_retry(
                post_id, error_message=message, now=self._now_fn()
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
        return bool(updated)

    async def _mark_failed(self, post_id: int, exc: BaseException) -> None:
        """PROCESSING → FAILED после исчерпания попыток (SPEC-013 §25–27)."""
        assert self._repository is not None
        # В БД сохраняется исходный текст ошибки: он нужен для разбора инцидентов и
        # не содержит секретов (Publisher и логгер уже редактируют токен, SPEC-021 §7).
        # Нормализованный ``code: message`` попадает в ``PublicationError``.
        message = str(exc).strip() or type(exc).__name__
        try:
            updated = await self._repository.mark_failed(
                post_id, error_message=message, now=self._now_fn()
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
