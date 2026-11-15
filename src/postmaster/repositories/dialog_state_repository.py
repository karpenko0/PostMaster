"""Персистентное хранилище состояния диалога и черновиков (PM-06 / BUG-07).

До PM-06 состояние FSM и черновик жили в памяти процесса (``StateMemoryStorage``
pyTelegramBotAPI) и терялись при перезапуске. Здесь реализован порт ``DialogStore``
(см. ``services.dialog_service``) поверх существующей инфраструктуры SQLAlchemy.
Модуль лежит в слое repositories: только этому слою разрешены SQLAlchemy и модели БД,
а сервис диалога знает лишь протокол и не зависит от способа хранения.

Свойства решения:

1. Состояние и черновик лежат в одной строке ``dialog_states`` и пишутся одной транзакцией:
   рассинхронизации «состояние без черновика» не бывает.
2. Черновик сериализуется в JSON. Секреты в черновик не попадают по построению: там только
   file_id, время, тип публикации, канал и подпись.
3. ``expires_at`` даёт TTL незавершённым сценариям; просроченные строки удаляются фоновой
   очисткой ``purge_expired``.
4. Модуль не импортирует services и domain (правило слоёв): работает строками и словарями.
"""

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from postmaster.database.models import DialogStateModel
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)

# TTL незавершённого черновика по умолчанию: сутки. За это время пользователь либо
# завершает сценарий, либо черновик можно безопасно удалить (PM-06 / BUG-07).
DEFAULT_TTL = timedelta(hours=24)

# Состояние, в котором сценарий ещё не завершён и черновик имеет смысл хранить.
DRAFT_STATES = frozenset(
    {
        "WAITING_PHOTO",
        "WAITING_PUBLICATION_TYPE",
        "WAITING_TARGET",
        "WAITING_DATETIME",
        "WAITING_CONFIRMATION",
        "WAITING_RESCHEDULE_DATETIME",
    }
)


class DialogStateRepository:
    """Реализация порта DialogStore на таблице ``dialog_states`` (PM-06 / BUG-07)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl

    async def get_state(self, user_id: int, chat_id: int) -> str | None:
        """Состояние FSM. Просроченная строка трактуется как отсутствующая."""
        async with self._session_factory() as session:
            model = await session.get(DialogStateModel, (user_id, chat_id))
            if model is None:
                return None
            if self._is_expired(model):
                logger.info("Dialog state expired telegram_user_id=%s chat_id=%s", user_id, chat_id)
                return None
            return model.state

    async def set_state(self, user_id: int, chat_id: int, state: str) -> None:
        """Записывает состояние, сохраняя черновик одной транзакцией."""
        await self._upsert(user_id, chat_id, state=state, draft=None)

    async def get_draft(self, user_id: int, chat_id: int) -> dict[str, str]:
        """Черновик. Неизвестные/испорченные данные не роняют сценарий."""
        async with self._session_factory() as session:
            model = await session.get(DialogStateModel, (user_id, chat_id))
            if model is None or self._is_expired(model):
                return {}
            return self._decode(model.draft)

    async def set_draft(self, user_id: int, chat_id: int, draft: dict[str, str]) -> None:
        """Записывает черновик, сохраняя состояние одной транзакцией."""
        await self._upsert(user_id, chat_id, state=None, draft=draft)

    async def clear(self, user_id: int, chat_id: int) -> None:
        """Полностью удаляет строку: сценарий завершён, хранить нечего."""
        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(DialogStateModel).where(
                    DialogStateModel.telegram_user_id == user_id,
                    DialogStateModel.chat_id == chat_id,
                )
            )
        logger.info("Dialog state cleared telegram_user_id=%s chat_id=%s", user_id, chat_id)

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """Удаляет просроченные черновики. Возвращает количество удалённых строк."""
        stamp = (now if now is not None else utc_now()).replace(tzinfo=None)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(DialogStateModel).where(
                    DialogStateModel.expires_at.is_not(None),
                    DialogStateModel.expires_at < stamp,
                )
            )
        removed = int(result.rowcount or 0)
        if removed:
            logger.info("Expired dialog states purged count=%s", removed)
        return removed

    async def _upsert(
        self,
        user_id: int,
        chat_id: int,
        *,
        state: str | None,
        draft: dict[str, str] | None,
    ) -> None:
        """Запись одной транзакцией: состояние и черновик не расходятся."""
        stamp = utc_now()
        storage_stamp = stamp.replace(tzinfo=None)
        async with self._session_factory() as session, session.begin():
            model = await session.get(DialogStateModel, (user_id, chat_id))
            if model is None:
                # Первая запись пары. Черновик без состояния невозможен: состояние
                # обязательно появится следующим вызовом, поэтому подставляем требуемое.
                if state is None:
                    raise RuntimeError("черновик нельзя записать без состояния")
                model = DialogStateModel(
                    telegram_user_id=user_id,
                    chat_id=chat_id,
                    state=state,
                    draft=self._encode(draft or {}),
                    updated_at=storage_stamp,
                    expires_at=self._expiry(stamp),
                )
                session.add(model)
                return
            if state is not None:
                model.state = state
            if draft is not None:
                model.draft = self._encode(draft)
            model.updated_at = storage_stamp
            model.expires_at = self._expiry(stamp)

    def _expiry(self, now: datetime) -> datetime | None:
        if self._ttl is None:
            return None
        return (now + self._ttl).replace(tzinfo=None)

    @staticmethod
    def _is_expired(model: DialogStateModel) -> bool:
        if model.expires_at is None:
            return False
        return model.expires_at < utc_now().replace(tzinfo=None)

    @staticmethod
    def _encode(draft: dict[str, str]) -> str:
        return json.dumps(
            {str(key): str(value) for key, value in draft.items()},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode(raw: str | None) -> dict[str, str]:
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.error("Dialog draft is corrupted, resetting")
            return {}
        if not isinstance(data, dict):
            logger.error("Dialog draft has unexpected structure, resetting")
            return {}
        return {str(key): str(value) for key, value in data.items()}


# Совместимое имя: приложение использует хранилище как FSM-store.
SqlAlchemyDialogStore = DialogStateRepository

__all__ = [
    "DEFAULT_TTL",
    "DRAFT_STATES",
    "DialogStateRepository",
    "SqlAlchemyDialogStore",
]
