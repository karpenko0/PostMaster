"""DialogService: состояние диалога и шаги сценария создания публикации (SPEC-003).

Сервис решает, как обработать сообщение в текущем состоянии, и возвращает исход. Тексты ответов
формирует слой handlers (решение D12). Сервис не импортирует telebot: состояние и черновик
читаются и пишутся через порт DialogStore, который реализует слой bot (решение D2).
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from postmaster.domain.dialog import (
    DialogEvent,
    DialogState,
    InvalidTransitionError,
    next_state,
)
from postmaster.domain.photo import PhotoData
from postmaster.domain.post import Post, PublicationType
from postmaster.domain.schedule import (
    DateTimeErrorCode,
    DateTimeValidationError,
    ParsedPublicationDateTime,
    parse_publication_datetime,
)
from postmaster.domain.telegram_target import normalize_target, parse_allowlist
from postmaster.services.post_service import (
    PostCreationError,
    PostCreationErrorCode,
    PostService,
    SchedulerRegistrationError,
)
from postmaster.services.user_service import UserService
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)

# Ключи черновика: данные фотографии из §16 SPEC-004. Минимум — telegram_file_id.
PHOTO_FILE_ID_KEY = "telegram_file_id"
PHOTO_UNIQUE_ID_KEY = "telegram_file_unique_id"
PHOTO_FILE_SIZE_KEY = "file_size"
PHOTO_WIDTH_KEY = "width"
PHOTO_HEIGHT_KEY = "height"
# Подпись к фото (SPEC-020): необязательная, хранится в черновике и передаётся в Post.
CAPTION_KEY = "caption"
PUBLICATION_TYPE_KEY = "publication_type"
TARGET_ID_KEY = "target_id"
REMINDER_BUTTON = "🔔 Напомнить мне"
TELEGRAM_BUTTON = "📢 Опубликовать в Telegram"


def _photo_draft(photo: PhotoData) -> dict[str, str]:
    """Данные фото для черновика. Значения строками: хранилище состояний хранит строки.

    caption сохраняется только если задан (SPEC-020 §1): отсутствие caption не должно
    попадать в БД как пустая строка.
    """
    draft = {
        PHOTO_FILE_ID_KEY: photo.file_id,
        PHOTO_UNIQUE_ID_KEY: photo.file_unique_id,
        PHOTO_WIDTH_KEY: str(photo.width),
        PHOTO_HEIGHT_KEY: str(photo.height),
    }
    if photo.file_size is not None:
        draft[PHOTO_FILE_SIZE_KEY] = str(photo.file_size)
    if photo.caption is not None:
        draft[CAPTION_KEY] = photo.caption
    return draft


class DialogStore(Protocol):
    """Порт хранилища состояния и черновика. Реализация находится в слое bot (решение D2)."""

    async def get_state(self, user_id: int, chat_id: int) -> str | None: ...

    async def set_state(self, user_id: int, chat_id: int, state: str) -> None: ...

    async def get_draft(self, user_id: int, chat_id: int) -> dict[str, str]: ...

    async def set_draft(self, user_id: int, chat_id: int, draft: dict[str, str]) -> None: ...


class DialogOutcomeKind(StrEnum):
    """Что произошло с сообщением. По этому значению handlers выбирает текст ответа."""

    STARTED = "started"
    PHOTO_ACCEPTED = "photo_accepted"
    PHOTO_REPLACED = "photo_replaced"
    TYPE_SELECTED = "type_selected"
    TARGET_REQUESTED = "target_requested"
    TARGET_INVALID = "target_invalid"
    TARGET_FORBIDDEN = "target_forbidden"
    DATETIME_INVALID = "datetime_invalid"
    POST_CREATED = "post_created"
    POST_SAVED_UNSCHEDULED = "post_saved_unscheduled"
    POST_FAILED = "post_failed"
    INVALID_MESSAGE = "invalid_message"


@dataclass(frozen=True, slots=True)
class DialogOutcome:
    """Результат шага и состояние после него.

    error_code заполнен только для DATETIME_INVALID; parsed_datetime передаёт UTC и локальное
    время в следующий этап. scheduled_at и zone_name — совместимые поля ответа SPEC-003.
    """

    kind: DialogOutcomeKind
    state: DialogState
    error_code: DateTimeErrorCode | PostCreationErrorCode | None = None
    post: Post | None = None
    parsed_datetime: ParsedPublicationDateTime | None = None
    scheduled_at: datetime | None = None
    zone_name: str | None = None


class DialogService:
    """Сценарий создания публикации: переходы, черновик и очередь сообщений на пользователя."""

    def __init__(
        self,
        store: DialogStore,
        *,
        user_service: UserService,
        post_service: PostService,
        publication_type: PublicationType = PublicationType.REMINDER,
        target_id: str | None = None,
        telegram_target_allowlist: str = "",
    ) -> None:
        self._store = store
        self._users = user_service
        self._posts = post_service
        # Legacy defaults are intentionally not substituted for an explicit choice.
        self._allowlist = parse_allowlist(telegram_target_allowlist)
        # Сообщения одного пользователя в одном чате обрабатываются по очереди (решение D11).
        self._locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def start(self, user_id: int, chat_id: int) -> DialogOutcome:
        """/start: переход в WAITING_PHOTO из любого состояния и пустой черновик (BR-01)."""
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            target = next_state(current, DialogEvent.START)
            if target is None:
                raise InvalidTransitionError(f"/start недопустим в состоянии {current}")
            # Состояние записывается раньше данных: без записи состояния данные не сохранятся.
            await self._store.set_state(user_id, chat_id, target)
            await self._store.set_draft(user_id, chat_id, {})
        return DialogOutcome(DialogOutcomeKind.STARTED, target)

    async def receive_photo(self, user_id: int, chat_id: int, photo: PhotoData) -> DialogOutcome:
        """Фото: WAITING_PHOTO → WAITING_DATETIME (BR-02). На шаге даты заменяет фото (BR-04).

        В черновик попадают все метаданные из §16 SPEC-004, замена сводится к перезаписи.
        """
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            target = next_state(current, DialogEvent.PHOTO)
            if target is None:
                return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)
            if (
                current == DialogState.IDLE
                and await self._users.get_by_telegram_id(user_id) is None
            ):
                return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)
            await self._store.set_state(user_id, chat_id, target)
            draft = _photo_draft(photo)
            if current in (
                DialogState.WAITING_PUBLICATION_TYPE,
                DialogState.WAITING_TARGET,
                DialogState.WAITING_DATETIME,
            ):
                previous = await self._store.get_draft(user_id, chat_id)
                for key in (PUBLICATION_TYPE_KEY, TARGET_ID_KEY):
                    if key in previous:
                        draft[key] = previous[key]
            await self._store.set_draft(user_id, chat_id, draft)
        if current in (
            DialogState.WAITING_PUBLICATION_TYPE,
            DialogState.WAITING_TARGET,
            DialogState.WAITING_DATETIME,
        ):
            return DialogOutcome(DialogOutcomeKind.PHOTO_REPLACED, target)
        return DialogOutcome(DialogOutcomeKind.PHOTO_ACCEPTED, target)

    async def receive_text(self, user_id: int, chat_id: int, text: str) -> DialogOutcome:
        """Текст: на шаге даты валидирует время и передаёт данные в PostService (SPEC-005).

        В других состояниях текст недопустим: сценарий ждёт фото или сообщение не относится к нему.
        """
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            if current == DialogState.WAITING_PUBLICATION_TYPE:
                event = {
                    REMINDER_BUTTON: DialogEvent.REMINDER,
                    TELEGRAM_BUTTON: DialogEvent.TELEGRAM,
                }.get(text.strip())
                if event is None:
                    return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)
                draft = await self._store.get_draft(user_id, chat_id)
                draft[PUBLICATION_TYPE_KEY] = event.value
                draft.pop(TARGET_ID_KEY, None)
                target = next_state(current, event)
                assert target is not None
                await self._store.set_draft(user_id, chat_id, draft)
                await self._store.set_state(user_id, chat_id, target)
                kind = (
                    DialogOutcomeKind.TYPE_SELECTED
                    if event == DialogEvent.REMINDER
                    else DialogOutcomeKind.TARGET_REQUESTED
                )
                return DialogOutcome(kind, target)
            if current == DialogState.WAITING_TARGET:
                try:
                    channel = normalize_target(text)
                except ValueError:
                    return DialogOutcome(DialogOutcomeKind.TARGET_INVALID, current)
                if self._allowlist and channel not in self._allowlist:
                    return DialogOutcome(DialogOutcomeKind.TARGET_FORBIDDEN, current)
                draft = await self._store.get_draft(user_id, chat_id)
                draft[TARGET_ID_KEY] = channel
                target = next_state(current, DialogEvent.VALID_TARGET)
                assert target is not None
                await self._store.set_draft(user_id, chat_id, draft)
                await self._store.set_state(user_id, chat_id, target)
                return DialogOutcome(DialogOutcomeKind.TYPE_SELECTED, target)
            target = next_state(current, DialogEvent.VALID_DATETIME)
            if target is None:
                return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)
            zone_name = await self._users.timezone_of(user_id)
            try:
                parsed = parse_publication_datetime(text, zone_name, utc_now())
            except DateTimeValidationError as exc:
                if exc.code == DateTimeErrorCode.PAST_DATETIME:
                    logger.info("Past publication datetime rejected user_id=%s", user_id)
                else:
                    logger.info("Invalid publication datetime format user_id=%s", user_id)
                return DialogOutcome(
                    DialogOutcomeKind.DATETIME_INVALID, current, error_code=exc.code
                )
            logger.info("Publication datetime accepted user_id=%s", user_id)
            draft = await self._store.get_draft(user_id, chat_id)
            try:
                user = await self._users.get_by_telegram_id(user_id)
                post = await self._posts.create_scheduled_post(
                    user_id=None if user is None else user.id,
                    telegram_file_id=draft.get(PHOTO_FILE_ID_KEY),
                    telegram_file_unique_id=draft.get(PHOTO_UNIQUE_ID_KEY),
                    scheduled_at_utc=parsed.utc_datetime,
                    timezone=parsed.timezone,
                    publication_type=draft.get(PUBLICATION_TYPE_KEY, ""),
                    target_id=draft.get(TARGET_ID_KEY),
                    caption=draft.get(CAPTION_KEY),
                )
            except PostCreationError as exc:
                # Нет записи: оставляем дату и черновик для повторной попытки.
                return DialogOutcome(DialogOutcomeKind.POST_FAILED, current, error_code=exc.code)
            except SchedulerRegistrationError as exc:
                # Запись уже есть: НЕ повторяем создание, иначе пользователь создаст дубликат.
                post = exc.post
                kind = DialogOutcomeKind.POST_SAVED_UNSCHEDULED
            except Exception:
                logger.error("Post creation failed user_id=%s", user_id)
                return DialogOutcome(
                    DialogOutcomeKind.POST_FAILED,
                    current,
                    error_code=PostCreationErrorCode.POST_CREATE_FAILED,
                )
            else:
                kind = DialogOutcomeKind.POST_CREATED
            await self._store.set_state(user_id, chat_id, target)
            await self._store.set_draft(user_id, chat_id, {})
        return DialogOutcome(
            kind,
            target,
            post=post,
            parsed_datetime=parsed,
            scheduled_at=parsed.local_datetime,
            zone_name=parsed.timezone,
        )

    async def receive_other(self, user_id: int, chat_id: int) -> DialogOutcome:
        """Сообщение без текста и фото (стикер, документ и т. п.) недопустимо во всех состояниях."""
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
        return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)

    async def _current_state(self, user_id: int, chat_id: int) -> DialogState:
        raw = await self._store.get_state(user_id, chat_id)
        return DialogState.IDLE if raw is None else DialogState(raw)
