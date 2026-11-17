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
    is_valid_draft_id,
    new_draft_id,
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
from postmaster.repositories.post_repository import PostRepository
from postmaster.services.post_service import (
    PostCreationError,
    PostCreationErrorCode,
    PostService,
    SchedulerRegistrationError,
)
from postmaster.services.reschedule_service import PostNotPendingError, RescheduleService
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
# ID поста, для которого запрошен перенос времени.
RESCHEDULE_POST_ID_KEY = "reschedule_post_id"
# PM-05: сохранённое распарсенное время публикации в черновике (iso-строки).
PARSED_UTC_KEY = "parsed_utc"
PARSED_LOCAL_KEY = "parsed_local"
PARSED_TZ_KEY = "parsed_tz"
# PM-05: уникальный идентификатор черновика — защита от двойного подтверждения.
DRAFT_ID_KEY = "draft_id"
# PM-05: ID созданного поста в черновике — защита от повторного создания.
CONFIRMED_POST_ID_KEY = "confirmed_post_id"
# PM06-R2-001: черновик, для которого уже создан Post (защита старых кнопок).
CONFIRMED_DRAFT_ID_KEY = "confirmed_draft_id"
# PM06-R2-005: уникальная операция переноса; старые кнопки её не перезаписывают.
RESCHEDULE_OPERATION_ID_KEY = "reschedule_operation_id"
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
    # PM-05: предварительный просмотр публикации.
    PREVIEW_SHOWN = "preview_shown"
    POST_CREATED = "post_created"
    POST_SAVED_UNSCHEDULED = "post_saved_unscheduled"
    POST_FAILED = "post_failed"
    INVALID_MESSAGE = "invalid_message"
    # PM-04: перенос публикации.
    RESCHEDULE_PROMPT = "reschedule_prompt"
    RESCHEDULE_DATETIME_INVALID = "reschedule_datetime_invalid"
    RESCHEDULE_OK = "reschedule_ok"
    RESCHEDULE_REJECTED = "reschedule_rejected"
    # PM-05: отмена на этапе подтверждения.
    CONFIRMATION_CANCELLED = "confirmation_cancelled"
    # PM06-R2-001: callback относится к другому/устаревшему черновику.
    STALE_CALLBACK = "stale_callback"
    # PM06-R2-004: дата публикации успела пройти между preview и confirm.
    DATETIME_EXPIRED = "datetime_expired"
    # PM06-R2-005: попытка запустить перенос поверх активного сценария.
    FSM_CONFLICT = "fsm_conflict"
    # PM06-R2-003: перенос сохранён, но задание Scheduler не зарегистрировано.
    RESCHEDULE_SAVED_UNSCHEDULED = "reschedule_saved_unscheduled"


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
        reschedule_service: RescheduleService | None = None,
        post_repository: PostRepository | None = None,
    ) -> None:
        self._store = store
        self._users = user_service
        self._posts = post_service
        # Legacy defaults are intentionally not substituted for an explicit choice.
        self._allowlist = parse_allowlist(telegram_target_allowlist)
        # PM-04: сервис переноса публикаций.
        self._reschedule = reschedule_service
        # PM06-R2-005: проверка владельца/статуса до старта переноса.
        self._post_repository = post_repository
        # Сообщения одного пользователя в одном чате обрабатываются по очереди (решение D11).
        self._locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _write(
        self,
        user_id: int,
        chat_id: int,
        state: DialogState,
        draft: dict[str, str],
    ) -> None:
        """PM06-R2-007: сохраняет state и draft согласованно.

        Персистентные хранилища реализуют атомарный ``save``; для legacy-адаптеров
        (telebot StateMemoryStorage) сохраняется прежний порядок «состояние, данные».
        """
        raw_state = state.value
        save = getattr(self._store, "save", None)
        if save is not None:
            await save(user_id, chat_id, raw_state, draft)
            return
        await self._store.set_state(user_id, chat_id, raw_state)
        await self._store.set_draft(user_id, chat_id, draft)

    async def start(self, user_id: int, chat_id: int) -> DialogOutcome:
        """/start: переход в WAITING_PHOTO из любого состояния и пустой черновик (BR-01)."""
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            target = next_state(current, DialogEvent.START)
            if target is None:
                raise InvalidTransitionError(f"/start недопустим в состоянии {current}")
            await self._write(user_id, chat_id, target, {})
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
            await self._write(user_id, chat_id, target, draft)
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
                await self._write(user_id, chat_id, target, draft)
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
                await self._write(user_id, chat_id, target, draft)
                return DialogOutcome(DialogOutcomeKind.TYPE_SELECTED, target)
            if current == DialogState.WAITING_RESCHEDULE_DATETIME:
                # PM-04: перенос публикации на новое время.
                return await self._handle_reschedule_datetime_input(user_id, chat_id, text)
            if current == DialogState.WAITING_DATETIME:
                # PM-05: валидная дата → предварительный просмотр, Post ещё не создаётся.
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
                # Параметры сохраняются в черновике — в таблицу posts они попадут
                # только после явного подтверждения (PM05-BR-02).
                draft[PARSED_UTC_KEY] = parsed.utc_datetime.isoformat()
                draft[PARSED_LOCAL_KEY] = parsed.local_datetime.isoformat()
                draft[PARSED_TZ_KEY] = parsed.timezone
                # Уникальный идентификатор черновика: защита от двойного подтверждения.
                draft[DRAFT_ID_KEY] = new_draft_id()
                await self._write(user_id, chat_id, target, draft)
                return DialogOutcome(
                    DialogOutcomeKind.PREVIEW_SHOWN,
                    target,
                    parsed_datetime=parsed,
                    scheduled_at=parsed.local_datetime,
                    zone_name=parsed.timezone,
                )
            return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)

    async def receive_other(self, user_id: int, chat_id: int) -> DialogOutcome:
        """Сообщение без текста и фото (стикер, документ и т. п.) недопустимо во всех состояниях."""
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
        return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)

    async def _current_state(self, user_id: int, chat_id: int) -> DialogState:
        raw = await self._store.get_state(user_id, chat_id)
        return DialogState.IDLE if raw is None else DialogState(raw)

    async def confirm_post(
        self, user_id: int, chat_id: int, draft_id: str | None = None
    ) -> DialogOutcome:
        """PM06-R2-001/002/004: подтверждение, привязанное к конкретному черновику.

        ``draft_id`` из callback обязан совпадать с текущим черновиком. Повторные и
        параллельные callback не создают дубль: постоянный ключ идемпотентности
        уникален на уровне БД. Повторная проверка даты исключает публикацию в прошлом.
        """
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            draft = await self._store.get_draft(user_id, chat_id)
            active_draft_id = draft.get(DRAFT_ID_KEY)

            # Явная привязка callback: None означает внутренний вызов сервиса
            # (контракт PM-05), любой переданный id проверяется строго.
            if draft_id is not None and not is_valid_draft_id(draft_id):
                logger.info("Invalid confirmation callback user_id=%s", user_id)
                return DialogOutcome(DialogOutcomeKind.STALE_CALLBACK, current)

            existing_post_id = draft.get(CONFIRMED_POST_ID_KEY)
            if existing_post_id is not None:
                # Повторный или параллельный callback уже подтверждённого
                # черновика возвращает тот же успех без новой записи. Кнопка
                # другого черновика не должна выдавать успех за текущий.
                confirmed_draft_id = draft.get(CONFIRMED_DRAFT_ID_KEY)
                if draft_id is not None and (
                    draft_id != active_draft_id or draft_id != confirmed_draft_id
                ):
                    return DialogOutcome(DialogOutcomeKind.STALE_CALLBACK, current)
                logger.info(
                    "Duplicate confirm ignored user_id=%s post_id=%s", user_id, existing_post_id
                )
                local_raw = draft.get(PARSED_LOCAL_KEY)
                tz = draft.get(PARSED_TZ_KEY)
                local = datetime.fromisoformat(local_raw) if local_raw else None
                return DialogOutcome(
                    DialogOutcomeKind.POST_CREATED,
                    DialogState.IDLE,
                    scheduled_at=local,
                    zone_name=tz,
                )

            if draft_id is not None and (
                draft_id != active_draft_id or current != DialogState.WAITING_CONFIRMATION
            ):
                logger.info("Stale confirmation callback user_id=%s", user_id)
                return DialogOutcome(DialogOutcomeKind.STALE_CALLBACK, current)

            if current != DialogState.WAITING_CONFIRMATION:
                logger.warning(
                    "Confirm without WAITING_CONFIRMATION user_id=%s state=%s", user_id, current
                )
                return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)
            if active_draft_id is None or not is_valid_draft_id(active_draft_id):
                logger.warning("Confirm without valid draft_id user_id=%s", user_id)
                return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)

            parsed_utc_raw = draft.get(PARSED_UTC_KEY)
            parsed_local_raw = draft.get(PARSED_LOCAL_KEY)
            parsed_tz = draft.get(PARSED_TZ_KEY)
            telegram_file_id = draft.get(PHOTO_FILE_ID_KEY)
            telegram_file_unique_id = draft.get(PHOTO_UNIQUE_ID_KEY)
            if not all(
                [
                    parsed_utc_raw,
                    parsed_local_raw,
                    parsed_tz,
                    telegram_file_id,
                    telegram_file_unique_id,
                ]
            ):
                logger.warning("Confirm with incomplete draft user_id=%s", user_id)
                return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)

            parsed_utc = datetime.fromisoformat(parsed_utc_raw)
            parsed_local = datetime.fromisoformat(parsed_local_raw)
            # PM06-R2-004: время могло пройти пока пользователь читал превью.
            if parsed_utc <= utc_now():
                logger.info("Confirm rejected: datetime expired user_id=%s", user_id)
                return DialogOutcome(DialogOutcomeKind.DATETIME_EXPIRED, current)

            user = await self._users.get_by_telegram_id(user_id)
            if user is None:
                logger.error("Confirm rejected: user not found user_id=%s", user_id)
                return DialogOutcome(
                    DialogOutcomeKind.POST_FAILED,
                    current,
                    error_code=PostCreationErrorCode.USER_NOT_FOUND,
                )

            publication_type = draft.get(PUBLICATION_TYPE_KEY, PublicationType.REMINDER.value)
            target_id = draft.get(TARGET_ID_KEY) or None
            caption = draft.get(CAPTION_KEY) or None

            try:
                post = await self._posts.create_scheduled_post(
                    user_id=user.id,
                    telegram_file_id=telegram_file_id,
                    telegram_file_unique_id=telegram_file_unique_id,
                    scheduled_at_utc=parsed_utc,
                    timezone=parsed_tz,
                    publication_type=publication_type,
                    target_id=target_id,
                    caption=caption,
                    source_draft_id=active_draft_id,
                )
            except PostCreationError as exc:
                logger.error(
                    "Post creation failed on confirm user_id=%s code=%s", user_id, exc.code.value
                )
                return DialogOutcome(DialogOutcomeKind.POST_FAILED, current, error_code=exc.code)
            except SchedulerRegistrationError as exc:
                post = exc.post
                draft[CONFIRMED_POST_ID_KEY] = str(post.id)
                draft[CONFIRMED_DRAFT_ID_KEY] = active_draft_id
                await self._write(user_id, chat_id, DialogState.IDLE, draft)
                logger.warning("Post saved but Scheduler failed on confirm post_id=%s", post.id)
                return DialogOutcome(
                    DialogOutcomeKind.POST_SAVED_UNSCHEDULED,
                    DialogState.IDLE,
                    post=post,
                    scheduled_at=parsed_local,
                    zone_name=parsed_tz,
                )
            except Exception:
                logger.exception("Unexpected error on confirm user_id=%s", user_id)
                return DialogOutcome(
                    DialogOutcomeKind.POST_FAILED,
                    current,
                    error_code=PostCreationErrorCode.POST_CREATE_FAILED,
                )

            draft[CONFIRMED_POST_ID_KEY] = str(post.id)
            draft[CONFIRMED_DRAFT_ID_KEY] = active_draft_id
            await self._write(user_id, chat_id, DialogState.IDLE, draft)
            logger.info("Post confirmed post_id=%s user_id=%s", post.id, user_id)
            return DialogOutcome(
                DialogOutcomeKind.POST_CREATED,
                DialogState.IDLE,
                post=post,
                scheduled_at=parsed_local,
                zone_name=parsed_tz,
            )

    async def cancel_creation(
        self, user_id: int, chat_id: int, draft_id: str | None = None
    ) -> DialogOutcome:
        """PM06-R2-001: отмена только того черновика, для которого создана кнопка."""
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            draft = await self._store.get_draft(user_id, chat_id)
            active_draft_id = draft.get(DRAFT_ID_KEY)
            if draft_id is not None and (
                not is_valid_draft_id(draft_id)
                or draft_id != active_draft_id
                or current != DialogState.WAITING_CONFIRMATION
            ):
                return DialogOutcome(DialogOutcomeKind.STALE_CALLBACK, current)
            if current != DialogState.WAITING_CONFIRMATION:
                return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)
            await self._write(user_id, chat_id, DialogState.IDLE, {})
        logger.info("Post creation cancelled by user user_id=%s", user_id)
        return DialogOutcome(DialogOutcomeKind.CONFIRMATION_CANCELLED, DialogState.IDLE)

    async def start_reschedule(self, user_id: int, chat_id: int, post_id: int) -> DialogOutcome:
        """PM06-R2-005: запускает перенос, не перезаписывая активный сценарий.

        Проверяет состояние FSM, владельца и статус Post до изменения черновика.
        Старые кнопки переноса при активной операции возвращают явный конфликт.
        """
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            # Конфликт только тогда, когда есть что терять: начатый черновик или
            # незавершённая операция. Пустое состояние (например, сразу после
            # /start) перенос не разрушает.
            draft_before = await self._store.get_draft(user_id, chat_id)
            if current != DialogState.IDLE and draft_before:
                logger.info(
                    "Reschedule conflicts with active FSM user_id=%s state=%s", user_id, current
                )
                return DialogOutcome(DialogOutcomeKind.FSM_CONFLICT, current)

            if self._post_repository is not None:
                user = await self._users.get_by_telegram_id(user_id)
                post = (
                    None
                    if user is None
                    else await self._post_repository.get_user_post(post_id, user.id)
                )
                if post is None or post.status.value != "PENDING":
                    logger.info("Reschedule rejected: post is not own PENDING user_id=%s", user_id)
                    return DialogOutcome(DialogOutcomeKind.RESCHEDULE_REJECTED, current)

            draft = {
                RESCHEDULE_POST_ID_KEY: str(int(post_id)),
                RESCHEDULE_OPERATION_ID_KEY: new_draft_id(),
            }
            target = DialogState.WAITING_RESCHEDULE_DATETIME
            await self._write(user_id, chat_id, target, draft)
        return DialogOutcome(DialogOutcomeKind.RESCHEDULE_PROMPT, target)

    async def _handle_reschedule_datetime_input(
        self, user_id: int, chat_id: int, text: str
    ) -> DialogOutcome:
        """PM-04: валидирует дату и применяет перенос через RescheduleService.

        Вызывается из receive_text под уже удерживаемой блокировкой (asyncio.Lock
        не реентрантный).
        """
        draft = await self._store.get_draft(user_id, chat_id)
        raw_post_id = draft.get(RESCHEDULE_POST_ID_KEY)
        if RESCHEDULE_OPERATION_ID_KEY not in draft:
            logger.warning("Reschedule input without operation id user_id=%s", user_id)
            await self._write(user_id, chat_id, DialogState.IDLE, {})
            return DialogOutcome(DialogOutcomeKind.RESCHEDULE_REJECTED, DialogState.IDLE)
        if not raw_post_id or not raw_post_id.isdigit():
            logger.warning(
                "Reschedule datetime input without post_id user_id=%s draft=%s",
                user_id,
                draft,
            )
            await self._write(user_id, chat_id, DialogState.IDLE, {})
            return DialogOutcome(DialogOutcomeKind.RESCHEDULE_REJECTED, DialogState.IDLE)
        post_id = int(raw_post_id)
        zone_name = await self._users.timezone_of(user_id)
        try:
            parsed = parse_publication_datetime(text, zone_name, utc_now())
        except DateTimeValidationError as exc:
            logger.info(
                "Reschedule datetime rejected user_id=%s post_id=%s code=%s",
                user_id,
                post_id,
                exc.code.value,
            )
            return DialogOutcome(
                DialogOutcomeKind.RESCHEDULE_DATETIME_INVALID,
                DialogState.WAITING_RESCHEDULE_DATETIME,
                error_code=exc.code,
            )

        user = await self._users.get_by_telegram_id(user_id)
        if user is None or self._reschedule is None:
            await self._write(user_id, chat_id, DialogState.IDLE, {})
            return DialogOutcome(DialogOutcomeKind.RESCHEDULE_REJECTED, DialogState.IDLE)
        try:
            post = await self._reschedule.reschedule(
                post_id=post_id,
                user_id=user.id,
                scheduled_at_utc=parsed.utc_datetime,
            )
        except PostNotPendingError:
            logger.info(
                "Reschedule rejected by service user_id=%s post_id=%s",
                user_id,
                post_id,
            )
            await self._write(user_id, chat_id, DialogState.IDLE, {})
            return DialogOutcome(DialogOutcomeKind.RESCHEDULE_REJECTED, DialogState.IDLE)

        target = DialogState.IDLE
        await self._write(user_id, chat_id, target, {})
        logger.info(
            "Reschedule applied user_id=%s post_id=%s scheduled_at_utc=%s",
            user_id,
            post.id,
            post.scheduled_at_utc,
        )
        return DialogOutcome(
            DialogOutcomeKind.RESCHEDULE_OK,
            target,
            post=post,
            parsed_datetime=parsed,
            scheduled_at=parsed.local_datetime,
            zone_name=parsed.timezone,
        )
