"""Создание публикаций: проверка данных, commit, затем регистрация задания (SPEC-009)."""

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import NoReturn, Protocol
from zoneinfo import ZoneInfo

from postmaster.domain.post import Post, PostDraft, PublicationType, UserNotFoundError
from postmaster.repositories.post_repository import PostRepository

logger = logging.getLogger(__name__)


class PostCreationErrorCode(StrEnum):
    USER_NOT_FOUND = "USER_NOT_FOUND"
    PHOTO_DATA_MISSING = "PHOTO_DATA_MISSING"
    DATETIME_MISSING = "DATETIME_MISSING"
    INVALID_PUBLICATION_TYPE = "INVALID_PUBLICATION_TYPE"
    TARGET_REQUIRED = "TARGET_REQUIRED"
    POST_CREATE_FAILED = "POST_CREATE_FAILED"
    SCHEDULER_REGISTRATION_FAILED = "SCHEDULER_REGISTRATION_FAILED"


class PostCreationError(Exception):
    """Отказ до сохранения: в БД нет новой публикации."""

    def __init__(self, code: PostCreationErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class SchedulerRegistrationError(Exception):
    """Запись уже сохранена. Нельзя повторять создание: можно получить дубликат."""

    def __init__(self, post: Post) -> None:
        self.post = post
        self.code = PostCreationErrorCode.SCHEDULER_REGISTRATION_FAILED
        super().__init__(self.code.value)


class PostScheduler(Protocol):
    def schedule_post(self, *, post_id: int, run_at: datetime) -> None: ...


def _reject(code: PostCreationErrorCode, user_id: int | None) -> NoReturn:
    logger.error("Post creation failed user_id=%s code=%s", user_id, code.value)
    raise PostCreationError(code)


class PostService:
    """Сохраняет PENDING до передачи идентификатора Scheduler (BR-009-06/07)."""

    def __init__(
        self, repository: PostRepository | None = None, scheduler: PostScheduler | None = None
    ) -> None:
        self._repository = repository
        self._scheduler = scheduler

    async def create_post(
        self,
        *,
        owner_telegram_id: int,
        photo_file_id: str,
        scheduled_at: datetime,
        timezone: str,
        local_datetime: datetime | None = None,
    ) -> PostDraft:
        """Совместимый DTO-контракт SPEC-003/006; рабочий диалог его больше не вызывает."""
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

    async def create_scheduled_post(
        self,
        *,
        user_id: int | None,
        telegram_file_id: str | None,
        telegram_file_unique_id: str | None,
        scheduled_at_utc: datetime | None,
        timezone: str | None,
        publication_type: PublicationType | str,
        target_id: str | None = None,
        caption: str | None = None,
    ) -> Post:
        """Создаёт запись и задание; возвращает только сохранённый Post."""
        logger.info("Post creation started user_id=%s", user_id)
        if self._repository is None or self._scheduler is None:
            raise RuntimeError("PostService требует репозиторий и Scheduler")
        if user_id is None:
            _reject(PostCreationErrorCode.USER_NOT_FOUND, user_id)
        if (
            not telegram_file_id
            or not telegram_file_id.strip()
            or not telegram_file_unique_id
            or not telegram_file_unique_id.strip()
        ):
            _reject(PostCreationErrorCode.PHOTO_DATA_MISSING, user_id)
        if scheduled_at_utc is None:
            _reject(PostCreationErrorCode.DATETIME_MISSING, user_id)
        if not timezone:
            _reject(PostCreationErrorCode.POST_CREATE_FAILED, user_id)
        try:
            kind = PublicationType(publication_type)
        except ValueError:
            _reject(PostCreationErrorCode.INVALID_PUBLICATION_TYPE, user_id)
        if kind == PublicationType.TELEGRAM and (not target_id or not target_id.strip()):
            _reject(PostCreationErrorCode.TARGET_REQUIRED, user_id)
        try:
            post = await self._repository.create_post(
                user_id=user_id,
                telegram_file_id=telegram_file_id,
                telegram_file_unique_id=telegram_file_unique_id,
                scheduled_at_utc=scheduled_at_utc,
                timezone=timezone,
                publication_type=kind,
                target_id=target_id,
                caption=caption,
            )
        except UserNotFoundError:
            logger.error("Post creation failed user_id=%s: user not found", user_id)
            raise PostCreationError(PostCreationErrorCode.USER_NOT_FOUND) from None
        except Exception:
            # Не пишем текст ошибки БД: он может содержать file_id/другие личные данные.
            logger.error("Post creation failed user_id=%s", user_id)
            raise PostCreationError(PostCreationErrorCode.POST_CREATE_FAILED) from None
        try:
            self._scheduler.schedule_post(post_id=post.id, run_at=post.scheduled_at_utc)
        except Exception:
            logger.error("Scheduler registration failed post_id=%s", post.id)
            raise SchedulerRegistrationError(post) from None
        logger.info("Post scheduled id=%s scheduled_at_utc=%s", post.id, post.scheduled_at_utc)
        return post
