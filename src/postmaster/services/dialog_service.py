"""DialogService: состояние диалога и шаги сценария создания публикации (SPEC-003).

Сервис решает, как обработать сообщение в текущем состоянии, и возвращает исход. Тексты ответов
формирует слой handlers (решение D12). Сервис не импортирует telebot: состояние и черновик
читаются и пишутся через порт DialogStore, который реализует слой bot (решение D2).
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from postmaster.domain.dialog import (
    DialogEvent,
    DialogState,
    InvalidTransitionError,
    next_state,
)
from postmaster.domain.schedule import parse_local_datetime
from postmaster.services.post_service import PostService
from postmaster.services.user_service import UserService
from postmaster.utils.clock import utc_now

# Ключ черновика: file_id фотографии, которая ждёт даты.
PHOTO_KEY = "photo_file_id"


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
    DATETIME_INVALID = "datetime_invalid"
    POST_CREATED = "post_created"
    INVALID_MESSAGE = "invalid_message"


@dataclass(frozen=True, slots=True)
class DialogOutcome:
    """Результат шага и состояние после него.

    scheduled_at и zone_name заполняются только для POST_CREATED. Время дано в поясе пользователя.
    """

    kind: DialogOutcomeKind
    state: DialogState
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
    ) -> None:
        self._store = store
        self._users = user_service
        self._posts = post_service
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

    async def receive_photo(self, user_id: int, chat_id: int, file_id: str) -> DialogOutcome:
        """Фото: WAITING_PHOTO → WAITING_DATETIME (BR-02). На шаге даты заменяет фото (BR-04)."""
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            target = next_state(current, DialogEvent.PHOTO)
            if target is None:
                return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)
            await self._store.set_state(user_id, chat_id, target)
            # В черновике хранится только фото, поэтому замена сводится к перезаписи.
            await self._store.set_draft(user_id, chat_id, {PHOTO_KEY: file_id})
        if current == DialogState.WAITING_DATETIME:
            return DialogOutcome(DialogOutcomeKind.PHOTO_REPLACED, target)
        return DialogOutcome(DialogOutcomeKind.PHOTO_ACCEPTED, target)

    async def receive_text(self, user_id: int, chat_id: int, text: str) -> DialogOutcome:
        """Текст: на шаге даты создаёт пост и возвращает в IDLE (BR-03).

        В других состояниях текст недопустим: сценарий ждёт фото или сообщение не относится к нему.
        """
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
            target = next_state(current, DialogEvent.VALID_DATETIME)
            if target is None:
                return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)
            zone_name = await self._users.timezone_of(user_id)
            zone = ZoneInfo(zone_name)
            scheduled_at = parse_local_datetime(text, zone, utc_now())
            if scheduled_at is None:
                return DialogOutcome(DialogOutcomeKind.DATETIME_INVALID, current)
            draft = await self._store.get_draft(user_id, chat_id)
            photo_file_id = draft.get(PHOTO_KEY)
            if photo_file_id is None:
                raise RuntimeError("на шаге ввода даты в черновике нет фотографии")
            # Черновик очищается только после успешного создания поста. Если оно упадёт,
            # пользователь сможет повторить дату без повторной отправки фото.
            await self._posts.create_post(
                owner_telegram_id=user_id,
                photo_file_id=photo_file_id,
                scheduled_at=scheduled_at,
            )
            await self._store.set_state(user_id, chat_id, target)
            await self._store.set_draft(user_id, chat_id, {})
        return DialogOutcome(
            DialogOutcomeKind.POST_CREATED,
            target,
            scheduled_at=scheduled_at.astimezone(zone),
            zone_name=zone_name,
        )

    async def receive_other(self, user_id: int, chat_id: int) -> DialogOutcome:
        """Сообщение без текста и фото (стикер, документ и т. п.) недопустимо во всех состояниях."""
        async with self._locks[user_id, chat_id]:
            current = await self._current_state(user_id, chat_id)
        return DialogOutcome(DialogOutcomeKind.INVALID_MESSAGE, current)

    async def _current_state(self, user_id: int, chat_id: int) -> DialogState:
        raw = await self._store.get_state(user_id, chat_id)
        return DialogState.IDLE if raw is None else DialogState(raw)
