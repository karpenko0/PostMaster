"""DialogService: состояние диалога и шаги сценария создания публикации (SPEC-003).

Сервис решает, как обработать сообщение в текущем состоянии, и возвращает исход. Тексты ответов
формирует слой handlers (решение D12). Сервис не импортирует telebot: состояние и черновик
читаются и пишутся через порт DialogStore, который реализует слой bot (решение D2).
"""

import asyncio
import logging
import uuid
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
from postmaster.services.reschedule_service import (
    PostNotPendingError,
    RescheduleService,
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
# Предел кэша блокировок по (user_id, chat_id). При превышении свободные записи
# удаляются, чтобы словарь не рос бесконечно (PM-06).
LOCK_CACHE_LIMIT = 512

# PM-06 / BUG-01, BUG-06: черновик помнит свой этап. Старый callback несёт draft_id
# предыдущего черновика и не может воздействовать на новый.
DRAFT_STAGE_PREVIEW = "preview"
DRAFT_STAGE_CONFIRMED = "confirmed"
DRAFT_STAGE_KEY = "draft_stage"
# PM-06 / BUG-07: состояние подтверждения, в котором черновик считается ожидающим.
ACTIVE_DRAFT_STATES = frozenset(
    {"WAITING_CONFIRMATION", "WAITING_DATETIME", "WAITING_PUBLICATION_TYPE", "WAITING_TARGET"}
)
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

    async def save(
        self, user_id: int, chat_id: int, *, state: str, draft: dict[str, str]
    ) -> None:
        """Атомарно записывает состояние и черновик одной операцией.

        PM-06 / BUG-07: раздельные ``set_state`` и ``set_draft`` дают окно, в котором
        хранилище содержит новое состояние со старым черновиком. Пользователь в этот
        момент видит следующий шаг, а обработчик читает данные предыдущего: под
        нагрузкой это давало потерю только что отправленного фото и подтверждение
        чужого (старого) черновика. Сервис всегда пишет пару целиком через ``save``.
        """


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
    # PM-06 / BUG-01: callback относится к другому (устаревшему) черновику.
    STALE_CALLBACK = "stale_callback"
    # PM-06 / BUG-01: повторное нажатие уже обработанной кнопки (идемпотентно).
    ALREADY_CONFIRMED = "already_confirmed"
    # PM-06 / BUG-05: перенос запрошен во время другого активного сценария.
    RESCHEDULE_CONFLICT = "reschedule_conflict"


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
    # PM-06: идентификатор черновика, к которому относится исход (для callback data).
    draft_id: str | None = None
    # PM-06 / BUG-04: удалось ли синхронизировать Scheduler после операции.
    scheduler_synced: bool = True
    # PM-06: причина отказа для диагностики и текста пользователю.
    detail: str | None = None
    # PM-06: снимок черновика для превью — handlers не обращаются к хранилищу напрямую.
    draft: dict[str, str] | None = None


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
    ) -> None:
        self._store = store
        self._users = user_service
        self._posts = post_service
        # Legacy defaults are intentionally not substituted for an explicit choice.
        self._allowlist = parse_allowlist(telegram_target_allowlist)
        # PM-04: сервис переноса публикаций.
        self._reschedule = reschedule_service
        # Сообщения одного пользователя в одном чате обрабатываются по очереди (решение D11).
        self._locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def start(self, user_id: int, chat_id: int) -> DialogOutcome:
        """/start: переход в WAITING_PHOTO из любого состояния и пустой черновик (BR-01).

        PM-06 / BUG-01: новый сценарий получает новый ``draft_id``, поэтому inline-кнопки
        предыдущего превью становятся устаревшими и не могут подтвердить новый черновик.
        """
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            if current == DialogState.WAITING_CONFIRMATION:
                # Неподтверждённый черновик не удаляем молча: помечаем сценарий
                # устаревшим, чтобы старые кнопки получили понятный ответ (BUG-06).
                logger.info("New /start discards unconfirmed draft user_id=%s", user_id)
            target = next_state(current, DialogEvent.START)
            if target is None:
                raise InvalidTransitionError(f"/start недопустим в состоянии {current}")
            draft = {DRAFT_ID_KEY: str(uuid.uuid4())}
            # Одна запись вместо двух: состояние и черновик не расходятся (PM-06 / BUG-07).
            await self._store.save(user_id, chat_id, state=target, draft=draft)
        return DialogOutcome(DialogOutcomeKind.STARTED, target, draft_id=draft[DRAFT_ID_KEY])

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
            previous: dict[str, str] = {}
            if current in (
                DialogState.WAITING_PUBLICATION_TYPE,
                DialogState.WAITING_TARGET,
                DialogState.WAITING_DATETIME,
            ):
                previous = await self._store.get_draft(user_id, chat_id)
                for key in (PUBLICATION_TYPE_KEY, TARGET_ID_KEY):
                    if key in previous:
                        draft[key] = previous[key]
            # PM-06 / BUG-01: новый черновик получает новый draft_id — старые кнопки
            # не могут подтвердить или отменить этот черновик.
            replacement_id = str(uuid.uuid4())
            draft[DRAFT_ID_KEY] = replacement_id
            await self._store.save(user_id, chat_id, state=target, draft=draft)
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
                await self._store.save(user_id, chat_id, state=target, draft=draft)
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
                await self._store.save(user_id, chat_id, state=target, draft=draft)
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
                # PM-06 / BUG-01: draft_id создаётся вместе с черновиком и НЕ меняется при
                # повторном вводе даты. Иначе открытое ранее превью «оживало» бы на новом
                # времени, а старое превью подтверждало бы изменённый черновик.
                draft_id = draft.get(DRAFT_ID_KEY) or str(uuid.uuid4())
                draft[DRAFT_ID_KEY] = draft_id
                draft.pop(CONFIRMED_POST_ID_KEY, None)
                draft[DRAFT_STAGE_KEY] = DRAFT_STAGE_PREVIEW
                await self._store.save(user_id, chat_id, state=target, draft=draft)
                return DialogOutcome(
                    DialogOutcomeKind.PREVIEW_SHOWN,
                    target,
                    parsed_datetime=parsed,
                    scheduled_at=parsed.local_datetime,
                    zone_name=parsed.timezone,
                    draft_id=draft_id,
                    draft=dict(draft),
                )
            return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)

    async def receive_other(self, user_id: int, chat_id: int) -> DialogOutcome:
        """Сообщение без текста и фото (стикер, документ и т. п.) недопустимо во всех состояниях."""
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
        return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)

    async def _current_state(self, user_id: int, chat_id: int) -> DialogState:
        self._prune_locks()
        raw = await self._store.get_state(user_id, chat_id)
        return DialogState.IDLE if raw is None else DialogState(raw)

    def _prune_locks(self) -> None:
        """Очищает кэш блокировок от неиспользуемых чатов (PM-06, дополнительно).

        ``_locks`` — ``defaultdict``, поэтому без очистки он растёт вместе с числом
        уникальных чатов и никогда не освобождает память. Удаляются только
        свободные блокировки: занятая остаётся у владеющей корутины, а новый
        участник получит ту же запись, пока она в словаре.
        """
        if len(self._locks) <= LOCK_CACHE_LIMIT:
            return
        for key in [key for key, lock in self._locks.items() if not lock.locked()]:
            del self._locks[key]
            if len(self._locks) <= LOCK_CACHE_LIMIT:
                break

    # === PM-05 / PM-06: подтверждение и отмена черновика ==================

    async def confirm_post(
        self,
        user_id: int,
        chat_id: int,
        *,
        draft_id: str | None = None,
    ) -> DialogOutcome:
        """Обрабатывает подтверждение публикации (PM-05 §5, PM-06 / BUG-01, BUG-02).

        Защита строится на трёх независимых механизмах:

        1. ``draft_id`` из callback data сверяется с текущим черновиком. Кнопка старого
           превью не может подтвердить новый черновик — возвращается ``STALE_CALLBACK``,
           активный черновик при этом не изменяется.
        2. Подтверждение разрешено только из ``WAITING_CONFIRMATION``.
        3. ``draft_id`` передаётся в PostService как постоянный ключ идемпотентности:
           повторная доставка callback, падение между commit и записью FSM и второй
           экземпляр приложения не создают второй Post — возвращается существующий.
        """
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            draft = await self._store.get_draft(user_id, chat_id)
            current_draft_id = draft.get(DRAFT_ID_KEY)

            # Повторное нажатие уже обработанной кнопки: пост создан (в БД или в черновике).
            already = draft.get(CONFIRMED_POST_ID_KEY)
            if already is not None and (
                draft_id is None or draft_id == current_draft_id or draft_id == already
            ):
                logger.info("Duplicate confirm ignored user_id=%s post_id=%s", user_id, already)
                local_raw = draft.get(PARSED_LOCAL_KEY)
                tz = draft.get(PARSED_TZ_KEY)
                local = datetime.fromisoformat(local_raw) if local_raw else None
                return DialogOutcome(
                    DialogOutcomeKind.POST_CREATED,
                    DialogState.IDLE,
                    scheduled_at=local,
                    zone_name=tz,
                    draft_id=current_draft_id,
                )

            # Uстаревший callback: кнопка относится к другому черновику.
            if draft_id is not None and draft_id != current_draft_id:
                logger.info(
                    "Stale confirm callback rejected user_id=%s callback_draft=%s current_draft=%s",
                    user_id,
                    draft_id,
                    current_draft_id,
                )
                return DialogOutcome(
                    DialogOutcomeKind.STALE_CALLBACK,
                    await self._current_state(user_id, chat_id),
                    draft_id=current_draft_id,
                )

            if current != DialogState.WAITING_CONFIRMATION:
                logger.warning(
                    "Confirm without WAITING_CONFIRMATION user_id=%s state=%s",
                    user_id,
                    current,
                )
                return DialogOutcome(
                    DialogOutcomeKind.INVALID_MESSAGE, current, draft_id=current_draft_id
                )

            if current_draft_id is None:
                logger.warning("Confirm without draft_id user_id=%s", user_id)
                return DialogOutcome(
                    DialogOutcomeKind.INVALID_MESSAGE, current, draft_id=current_draft_id
                )

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
                return DialogOutcome(
                    DialogOutcomeKind.INVALID_MESSAGE, current, draft_id=current_draft_id
                )

            # Восстанавливаем datetime из iso-строки черновика.
            parsed_utc = datetime.fromisoformat(parsed_utc_raw)
            if parsed_utc <= utc_now():
                # PM-06 §10: повторная проверка времени при подтверждении. Превью могло
                # провисеть в чате дольше, чем до момента публикации.
                logger.info("Confirm rejected: datetime already past user_id=%s", user_id)
                return DialogOutcome(
                    DialogOutcomeKind.DATETIME_INVALID,
                    current,
                    error_code=DateTimeErrorCode.PAST_DATETIME,
                    draft_id=current_draft_id,
                    detail="past_on_confirm",
                )
            parsed_local = datetime.fromisoformat(parsed_local_raw)
            user = await self._users.get_by_telegram_id(user_id)
            if user is None:
                logger.error("Confirm rejected: user not found user_id=%s", user_id)
                return DialogOutcome(
                    DialogOutcomeKind.POST_FAILED,
                    current,
                    error_code=PostCreationErrorCode.USER_NOT_FOUND,
                    draft_id=current_draft_id,
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
                    draft_id=current_draft_id,
                )
            except PostCreationError as exc:
                logger.error(
                    "Post creation failed on confirm user_id=%s code=%s", user_id, exc.code.value
                )
                return DialogOutcome(
                    DialogOutcomeKind.POST_FAILED,
                    current,
                    error_code=exc.code,
                    draft_id=current_draft_id,
                )
            except SchedulerRegistrationError as exc:
                # Запись сохранена, Scheduler не зарегистрировал задачу — Recovery восстановит.
                post = exc.post
                await self._finish_draft(user_id, chat_id, draft, post.id, current_draft_id)
                logger.warning(
                    "Post saved but Scheduler failed on confirm post_id=%s",
                    post.id,
                )
                return DialogOutcome(
                    DialogOutcomeKind.POST_SAVED_UNSCHEDULED,
                    DialogState.IDLE,
                    post=post,
                    scheduled_at=parsed_local,
                    zone_name=parsed_tz,
                    draft_id=current_draft_id,
                    scheduler_synced=False,
                )
            except Exception:
                logger.exception("Unexpected error on confirm user_id=%s", user_id)
                return DialogOutcome(
                    DialogOutcomeKind.POST_FAILED,
                    current,
                    error_code=PostCreationErrorCode.POST_CREATE_FAILED,
                    draft_id=current_draft_id,
                )

            await self._finish_draft(user_id, chat_id, draft, post.id, current_draft_id)
            logger.info(
                "Post confirmed post_id=%s user_id=%s draft_id=%s",
                post.id,
                user_id,
                current_draft_id,
            )
            return DialogOutcome(
                DialogOutcomeKind.POST_CREATED,
                DialogState.IDLE,
                post=post,
                scheduled_at=parsed_local,
                zone_name=parsed_tz,
                draft_id=current_draft_id,
            )

    async def _finish_draft(
        self,
        user_id: int,
        chat_id: int,
        draft: dict[str, str],
        post_id: int,
        draft_id: str | None,
    ) -> None:
        """Фиксирует завершение черновика: отметка о созданном посте и переход в IDLE."""
        draft[CONFIRMED_POST_ID_KEY] = str(post_id)
        draft[DRAFT_STAGE_KEY] = DRAFT_STAGE_CONFIRMED
        if draft_id is not None:
            draft[DRAFT_ID_KEY] = draft_id
        await self._store.save(user_id, chat_id, state=DialogState.IDLE, draft=draft)

    async def resolve_confirmed_post(
        self,
        user_id: int,
        chat_id: int,
        *,
        draft_id: str | None = None,
    ) -> Post | None:
        """Публикация, уже созданная этим черновиком, если она есть (PM-06 / BUG-02).

        Нужна для восстановления после падения между commit и записью FSM, а также
        для повторной доставки callback после перезапуска.
        """
        draft = await self._store.get_draft(user_id, chat_id)
        current_draft_id = draft.get(DRAFT_ID_KEY)
        target_draft_id = draft_id if draft_id is not None else current_draft_id
        if target_draft_id is None:
            return None
        return await self._posts.find_by_draft_id(target_draft_id)

    async def cancel_creation(
        self,
        user_id: int,
        chat_id: int,
        *,
        draft_id: str | None = None,
    ) -> DialogOutcome:
        """Обрабатывает отмену создания публикации (PM-05 §6, PM-06 / BUG-01, BUG-06).

        Отмена устаревшей кнопки не удаляет активный черновик: возвращается
        ``STALE_CALLBACK``, а текущий сценарий продолжается.
        """
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            draft = await self._store.get_draft(user_id, chat_id)
            current_draft_id = draft.get(DRAFT_ID_KEY)

            if draft_id is not None and draft_id != current_draft_id:
                logger.info(
                    "Stale cancel callback rejected user_id=%s callback_draft=%s current_draft=%s",
                    user_id,
                    draft_id,
                    current_draft_id,
                )
                return DialogOutcome(
                    DialogOutcomeKind.STALE_CALLBACK, current, draft_id=current_draft_id
                )

            if draft.get(CONFIRMED_POST_ID_KEY) is not None:
                # Кнопка «Отменить» рядом с уже созданной публикацией: пост не удаляем,
                # отменять нужно через /my_posts.
                logger.info("Cancel ignored: draft already confirmed user_id=%s", user_id)
                return DialogOutcome(
                    DialogOutcomeKind.ALREADY_CONFIRMED,
                    current,
                    draft_id=current_draft_id,
                )

            if current != DialogState.WAITING_CONFIRMATION:
                return DialogOutcome(
                    DialogOutcomeKind.INVALID_MESSAGE, current, draft_id=current_draft_id
                )
            await self._store.save(user_id, chat_id, state=DialogState.IDLE, draft={})
        logger.info("Post creation cancelled by user user_id=%s", user_id)
        return DialogOutcome(
            DialogOutcomeKind.CONFIRMATION_CANCELLED,
            DialogState.IDLE,
            draft_id=current_draft_id,
        )

    # === PM-04: перенос публикации =========================================

    async def start_reschedule(self, user_id: int, chat_id: int, post_id: int) -> DialogOutcome:
        """PM-04: переключает FSM в режим ожидания новой даты/времени для поста.

        PM-06 / BUG-05: перенос не должен молча уничтожать активный черновик создания.
        Допустимые состояния:
        - IDLE — обычный перенос из /my_posts;
        - WAITING_RESCHEDULE_DATETIME — повторное нажатие «Перенести» (идемпотентно):
          сценарий переноса перезапускается на выбранный пост;
        - WAITING_CONFIRMATION — перенос заменяет неподтверждённое превью (Post ещё не создан);
        - WAITING_PHOTO — черновика ещё нет (только /start), перенос допустим.
        Во время активного ввода (фото, тип, канал, дата) перенос отклоняется:
        пользователь получает явный ответ, а данные сценария сохраняются.

        ID поста сохраняется в черновике (RESCHEDULE_POST_ID_KEY). Владелец и статус
        проверяются в RescheduleService при вводе даты (защита от подмены callback data).
        """
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            if current in (
                DialogState.WAITING_PUBLICATION_TYPE,
                DialogState.WAITING_TARGET,
                DialogState.WAITING_DATETIME,
            ):
                logger.info(
                    "Reschedule rejected: scenario in progress user_id=%s state=%s post_id=%s",
                    user_id,
                    current,
                    post_id,
                )
                return DialogOutcome(
                    DialogOutcomeKind.RESCHEDULE_CONFLICT, current, detail=current.value
                )
            if self._reschedule is not None and hasattr(self._reschedule, "check_pending"):
                user = await self._users.get_by_telegram_id(user_id)
                if (
                    user is None
                    or await self._reschedule.check_pending(post_id=int(post_id), user_id=user.id)
                    is None
                ):
                    # PM-06 §10: не запускаем диалог для чужого/не PENDING поста и не
                    # трогаем активный черновик.
                    logger.info(
                        "Reschedule rejected: post not pending user_id=%s post_id=%s",
                        user_id,
                        post_id,
                    )
                    return DialogOutcome(
                        DialogOutcomeKind.RESCHEDULE_REJECTED, current, detail="not_pending"
                    )
            draft = {RESCHEDULE_POST_ID_KEY: str(int(post_id))}
            if current == DialogState.WAITING_RESCHEDULE_DATETIME:
                # Повторное нажатие: сохраняем уже введённый draft_id сценария.
                previous = await self._store.get_draft(user_id, chat_id)
                if previous.get(DRAFT_ID_KEY):
                    draft[DRAFT_ID_KEY] = previous[DRAFT_ID_KEY]
            await self._store.save(
                user_id, chat_id, state=DialogState.WAITING_RESCHEDULE_DATETIME, draft=draft
            )
        return DialogOutcome(
            DialogOutcomeKind.RESCHEDULE_PROMPT,
            DialogState.WAITING_RESCHEDULE_DATETIME,
            draft_id=draft.get(DRAFT_ID_KEY),
        )

    async def _handle_reschedule_datetime_input(
        self, user_id: int, chat_id: int, text: str
    ) -> DialogOutcome:
        """PM-04: валидирует дату и применяет перенос через RescheduleService.

        Вызывается из receive_text под уже удерживаемой блокировкой (asyncio.Lock
        не реентрантный).
        """
        draft = await self._store.get_draft(user_id, chat_id)
        raw_post_id = draft.get(RESCHEDULE_POST_ID_KEY)
        if not raw_post_id or not raw_post_id.isdigit():
            logger.warning(
                "Reschedule datetime input without post_id user_id=%s draft=%s",
                user_id,
                draft,
            )
            await self._store.save(user_id, chat_id, state=DialogState.IDLE, draft={})
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
            await self._store.save(user_id, chat_id, state=DialogState.IDLE, draft={})
            return DialogOutcome(DialogOutcomeKind.RESCHEDULE_REJECTED, DialogState.IDLE)
        try:
            result = await self._reschedule.reschedule_with_status(
                post_id=post_id,
                user_id=user.id,
                scheduled_at_utc=parsed.utc_datetime,
            )
            post = result.post
            scheduler_synced = result.scheduler_synced
        except PostNotPendingError:
            logger.info(
                "Reschedule rejected by service user_id=%s post_id=%s",
                user_id,
                post_id,
            )
            await self._store.save(user_id, chat_id, state=DialogState.IDLE, draft={})
            return DialogOutcome(DialogOutcomeKind.RESCHEDULE_REJECTED, DialogState.IDLE)

        target = DialogState.IDLE
        await self._store.save(user_id, chat_id, state=target, draft={})
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
            scheduler_synced=scheduler_synced,
        )
