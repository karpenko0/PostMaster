"""Персистентное хранилище состояний pyTelegramBotAPI (PM-06 / BUG-07).

pyTelegramBotAPI хранит состояния в ``StateStorageBase``. Штатное ``StateMemoryStorage``
теряет их при перезапуске процесса. Этот адаптер сохраняет состояние и данные (черновик)
в ту же таблицу ``dialog_states``, что и ``DialogStateRepository``, поэтому:

1. Перезапуск приложения не теряет актуальный черновик и шаг сценария.
2. Состояние и черновик пишутся одной транзакцией — рассинхронизации не бывает.
3. Несколько экземпляров приложения видят одно состояние (общая БД).
4. Черновик не содержит токенов и других секретов: только file_id, время, тип, канал, подпись.

Модуль лежит в слое repositories: только этому слою разрешены SQLAlchemy, модели БД и
(для реализации контракта ``StateStorageBase``) telebot. Слой bot остаётся без зависимостей
от БД, а приложение связывает оба слоя в ``app``.

Адаптер намеренно не импортирует services и domain: он работает строками и словарями,
как и требует контракт хранилища telebot.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telebot.asyncio_storage.base_storage import StateDataContext, StateStorageBase

from postmaster.database.models import DialogStateModel
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)

# TTL незавершённого сценария: сутки. По истечении черновик можно безопасно удалить.
DEFAULT_DIALOG_TTL = timedelta(hours=24)


class SqlAlchemyStateStorage(StateStorageBase):
    """``StateStorageBase`` поверх таблицы ``dialog_states`` (PM-06 / BUG-07)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl: timedelta = DEFAULT_DIALOG_TTL,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._ttl = ttl
        # Локальная защита от гонок внутри процесса. Между процессами согласованность
        # обеспечивает БД: каждая запись — отдельная короткая транзакция.
        self._lock = asyncio.Lock()

    # === состояние ========================================================

    async def set_state(
        self,
        chat_id: int,
        user_id: int,
        state: str,
        business_connection_id: str | None = None,
        message_thread_id: int | None = None,
        bot_id: int | None = None,
    ) -> bool:
        """Записывает состояние, не теряя уже сохранённый черновик."""
        value = getattr(state, "name", state)
        if value is None:
            return await self.delete_state(
                chat_id, user_id, business_connection_id, message_thread_id, bot_id
            )
        await self._upsert(int(user_id), int(chat_id), state=str(value), draft=None)
        return True

    async def get_state(
        self,
        chat_id: int,
        user_id: int,
        business_connection_id: str | None = None,
        message_thread_id: int | None = None,
        bot_id: int | None = None,
    ) -> str | None:
        async with self._session_factory() as session:
            model = await session.get(DialogStateModel, (int(user_id), int(chat_id)))
            if model is None or self._is_expired(model):
                return None
            return model.state

    async def delete_state(
        self,
        chat_id: int,
        user_id: int,
        business_connection_id: str | None = None,
        message_thread_id: int | None = None,
        bot_id: int | None = None,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(DialogStateModel).where(
                    DialogStateModel.telegram_user_id == int(user_id),
                    DialogStateModel.chat_id == int(chat_id),
                )
            )
        return bool(result.rowcount)

    # === данные черновика =================================================

    async def set_data(
        self,
        chat_id: int,
        user_id: int,
        key: str,
        value,
        business_connection_id: str | None = None,
        message_thread_id: int | None = None,
        bot_id: int | None = None,
    ) -> bool:
        """Устанавливает одно поле черновика. Строка должна существовать (контракт telebot)."""
        async with self._lock:
            draft = await self.get_data(int(chat_id), int(user_id))
            if draft is None:
                # Контракт StateMemoryStorage: данных без состояния не бывает.
                raise RuntimeError("SqlAlchemyStateStorage: ключ не существует")
            draft[str(key)] = value
            await self._upsert(int(user_id), int(chat_id), state=None, draft=draft)
        return True

    async def get_data(
        self,
        chat_id: int,
        user_id: int,
        business_connection_id: str | None = None,
        message_thread_id: int | None = None,
        bot_id: int | None = None,
    ) -> dict:
        async with self._session_factory() as session:
            model = await session.get(DialogStateModel, (int(user_id), int(chat_id)))
            if model is None or self._is_expired(model):
                return {}
            return self._decode(model.draft)

    async def reset_data(
        self,
        chat_id: int,
        user_id: int,
        business_connection_id: str | None = None,
        message_thread_id: int | None = None,
        bot_id: int | None = None,
    ) -> bool:
        async with self._lock:
            async with self._session_factory() as session, session.begin():
                model = await session.get(DialogStateModel, (int(user_id), int(chat_id)))
                if model is None:
                    return False
                model.draft = "{}"
                model.updated_at = utc_now().replace(tzinfo=None)
            return True

    async def save(
        self,
        chat_id: int,
        user_id: int,
        data: dict,
        business_connection_id: str | None = None,
        message_thread_id: int | None = None,
        bot_id: int | None = None,
    ) -> bool:
        """Заменяет черновик целиком, не трогая состояние.

        Так порт ``DialogStore.save`` пишет пару «состояние + черновик»: состояние
        выставляется первым ``set_state``, затем одним вызовом сюда приходит черновик.
        Обе записи — короткие транзакции по одной строке.
        """
        async with self._lock:
            async with self._session_factory() as session, session.begin():
                model = await session.get(DialogStateModel, (int(user_id), int(chat_id)))
                if model is None:
                    return False
                model.draft = self._encode(data)
                model.updated_at = utc_now().replace(tzinfo=None)
            return True

    async def save_with_state(
        self,
        chat_id: int,
        user_id: int,
        *,
        state: str,
        draft: dict,
    ) -> bool:
        """Атомарно пишет состояние и черновик одной транзакцией (порт DialogStore.save).

        Отличие от telebot-контракта ``save(data)``: здесь пара «шаг + данные»
        коммитится целиком, поэтому ни один читатель не увидит новый шаг со старым
        черновиком — источник гонок в E2E (потеря отправленного фото, подтверждение
        предыдущего черновика).
        """
        async with self._lock:
            await self._upsert(int(user_id), int(chat_id), state=str(state), draft=draft)
            return True

    def get_interactive_data(
        self,
        chat_id: int,
        user_id: int,
        business_connection_id: str | None = None,
        message_thread_id: int | None = None,
        bot_id: int | None = None,
    ) -> StateDataContext:
        return StateDataContext(
            self,
            chat_id=chat_id,
            user_id=user_id,
            business_connection_id=business_connection_id,
            message_thread_id=message_thread_id,
            bot_id=bot_id,
        )

    # === обслуживание =====================================================

    async def purge_expired(self, *, now: datetime | None = None) -> int:
        """Удаляет просроченные черновики (TTL). Возвращает число удалённых строк."""
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

    async def get_active_user_ids(self) -> list[tuple[int, int]]:
        """Активные (не истёкшие) пары (telegram_user_id, chat_id). Нужно диагностике."""
        statement = select(DialogStateModel.telegram_user_id, DialogStateModel.chat_id).order_by(
            DialogStateModel.telegram_user_id
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [(int(row[0]), int(row[1])) for row in rows]

    # === внутреннее =======================================================

    async def _upsert(
        self,
        user_id: int,
        chat_id: int,
        *,
        state: str | None,
        draft: dict | None,
    ) -> None:
        """Запись одной транзакцией: состояние и черновик не расходятся."""
        stamp = utc_now()
        storage_stamp = stamp.replace(tzinfo=None)
        expiry = None if self._ttl is None else (stamp + self._ttl).replace(tzinfo=None)
        async with self._session_factory() as session, session.begin():
            model = await session.get(DialogStateModel, (user_id, chat_id))
            if model is None:
                if state is None:
                    raise RuntimeError("SqlAlchemyStateStorage: черновик без состояния")
                session.add(
                    DialogStateModel(
                        telegram_user_id=user_id,
                        chat_id=chat_id,
                        state=state,
                        draft=self._encode(draft or {}),
                        updated_at=storage_stamp,
                        expires_at=expiry,
                    )
                )
                return
            if state is not None:
                model.state = state
            if draft is not None:
                model.draft = self._encode(draft)
            model.updated_at = storage_stamp
            model.expires_at = expiry

    @staticmethod
    def _is_expired(model: DialogStateModel) -> bool:
        if model.expires_at is None:
            return False
        return model.expires_at < utc_now().replace(tzinfo=None)

    @staticmethod
    def _encode(draft: dict) -> str:
        return json.dumps(
            {str(key): str(value) for key, value in draft.items()},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode(raw: str | None) -> dict:
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
