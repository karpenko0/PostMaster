"""PM06-R2-007: персистентное хранилище FSM на существующей SQLAlchemy-инфраструктуре.

State и draft сохраняются в одной строке ``dialog_states`` одной транзакцией — это
исключает «полузаписанный» черновик при аварийном завершении. Записи имеют TTL и
удаляются методом :meth:`DatabaseDialogStore.purge_expired`. Секреты (токены,
пароли, proxy credentials) в данные FSM не записываются.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from postmaster.database.models import DialogStateModel
from postmaster.utils.clock import utc_now

logger = logging.getLogger(__name__)

DEFAULT_DIALOG_TTL_SECONDS = 24 * 60 * 60

# Ключи, которые никогда не должны оказываться в персистентном черновике.
FORBIDDEN_DRAFT_KEY_PARTS = ("token", "secret", "password", "proxy", "credential")


def _storage(value: datetime) -> datetime:
    """SQLite не хранит tzinfo: системные даты кладутся в UTC без tzinfo."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("дата FSM должна быть aware UTC")
    return value.astimezone(UTC).replace(tzinfo=None)


class DatabaseDialogStore:
    """Порт DialogStore с сохранением в БД и TTL.

    Конкурентное обновление одного черновика несколькими процессами безопасно:
    запись идёт через ``merge`` по первичному ключу (user_id, chat_id), а редкий
    конфликт вставки повторяется как UPDATE. Последняя запись wins, но состояние и
    draft всегда согласованы и не содержат частичных данных.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl_seconds: int = DEFAULT_DIALOG_TTL_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds должен быть положительным")
        self._session_factory = session_factory
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock

    async def save(self, user_id: int, chat_id: int, state: str, draft: dict[str, str]) -> None:
        """Атомарно сохраняет state и draft (одна строка, одна транзакция)."""
        self._validate_draft(draft)
        now = _storage(self._clock())
        model = DialogStateModel(
            user_id=user_id,
            chat_id=chat_id,
            state=state,
            draft_json=json.dumps(draft, ensure_ascii=False, sort_keys=True),
            updated_at=now,
            expires_at=now + self._ttl,
            version=1,
        )
        try:
            await self._merge(model)
        except IntegrityError:
            # Два процесса одновременно создали строку: повторяем как UPDATE.
            await self._merge(model)

    async def _merge(self, model: DialogStateModel) -> None:
        async with self._session_factory() as session, session.begin():
            await session.merge(model)

    async def get_state(self, user_id: int, chat_id: int) -> str | None:
        row = await self._load(user_id, chat_id)
        return None if row is None else row.state

    async def get_draft(self, user_id: int, chat_id: int) -> dict[str, str]:
        row = await self._load(user_id, chat_id)
        if row is None:
            return {}
        try:
            data = json.loads(row.draft_json)
        except (ValueError, TypeError):
            logger.warning("Dialog draft is corrupted user_id=%s", user_id)
            return {}
        return {str(key): str(value) for key, value in data.items()}

    async def set_state(self, user_id: int, chat_id: int, state: str) -> None:
        """Совместимость с портом: сохраняет state, оставляя текущий draft."""
        await self.save(user_id, chat_id, state, await self.get_draft(user_id, chat_id))

    async def set_draft(self, user_id: int, chat_id: int, draft: dict[str, str]) -> None:
        """Совместимость с портом: сохраняет draft, оставляя текущий state."""
        state = await self.get_state(user_id, chat_id)
        await self.save(user_id, chat_id, state or "IDLE", draft)

    async def clear(self, user_id: int, chat_id: int) -> None:
        await self.save(user_id, chat_id, "IDLE", {})

    async def _load(self, user_id: int, chat_id: int) -> DialogStateModel | None:
        now = _storage(self._clock())
        statement = select(DialogStateModel).where(
            DialogStateModel.user_id == user_id,
            DialogStateModel.chat_id == chat_id,
            DialogStateModel.expires_at > now,
        )
        async with self._session_factory() as session:
            return (await session.scalars(statement)).first()

    async def load_all(self) -> list[tuple[int, int, str, dict[str, str]]]:
        """Все непросроченные состояния — для восстановления после рестарта."""
        now = _storage(self._clock())
        statement = select(DialogStateModel).where(DialogStateModel.expires_at > now)
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
        result = []
        for row in rows:
            try:
                draft = json.loads(row.draft_json)
            except (ValueError, TypeError):
                draft = {}
            result.append((row.user_id, row.chat_id, row.state, dict(draft)))
        return result

    def load_all_sync(self) -> list[tuple[int, int, str, dict[str, str]]]:
        """Синхронное чтение непросроченных FSM для восстановления при создании бота.

        Используется только для SQLite: :meth:`Application.startup` дополнительно
        выполняет асинхронное восстановление для любого SQLAlchemy-драйвера.
        """
        engine = self._session_factory.kw.get("bind")
        if engine is None or engine.url.get_backend_name() != "sqlite" or not engine.url.database:
            return []
        import json as _json
        import sqlite3

        moment = _storage(utc_now()).isoformat(sep=" ")
        try:
            with sqlite3.connect(engine.url.database) as connection:
                rows = connection.execute(
                    "SELECT user_id, chat_id, state, draft_json FROM dialog_states "
                    "WHERE expires_at > ?",
                    (moment,),
                ).fetchall()
        except sqlite3.Error:
            logger.warning("Невозможно прочитать dialog_states при восстановлении FSM")
            return []
        result: list[tuple[int, int, str, dict[str, str]]] = []
        for user_id, chat_id, state, draft_json in rows:
            try:
                draft = _json.loads(draft_json)
            except (ValueError, TypeError):
                draft = {}
            result.append((int(user_id), int(chat_id), str(state), dict(draft)))
        return result

    def purge_expired(self, *, clock: Callable[[], datetime] | None = None) -> int:
        """Синхронная очистка просроченных черновиков (используется в CLI/тестах).

        Возвращает количество удалённых строк. Реализация через sqlite3 исключает
        необходимость отдельного event loop для служебной операции.
        """
        moment = _storage((clock or self._clock)())
        # async_sessionmaker хранит engine в kw["bind"].
        engine = self._session_factory.kw.get("bind")
        if engine is None:
            return 0
        import sqlite3

        url = engine.url
        if url.get_backend_name() != "sqlite" or not url.database:
            logger.info("TTL cleanup поддержано только для SQLite")
            return 0
        with sqlite3.connect(url.database) as connection:
            cursor = connection.execute(
                "DELETE FROM dialog_states WHERE expires_at <= ?",
                (moment.isoformat(sep=" "),),
            )
            return cursor.rowcount

    async def purge_expired_async(self, *, clock: Callable[[], datetime] | None = None) -> int:
        """Асинхронная очистка для работающего приложения (любой SQLAlchemy-драйвер)."""
        moment = _storage((clock or self._clock)())
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(DialogStateModel).where(DialogStateModel.expires_at <= moment)
            )
            return int(result.rowcount or 0)

    @staticmethod
    def _validate_draft(draft: dict[str, str]) -> None:
        for key in draft:
            lowered = key.lower()
            if any(part in lowered for part in FORBIDDEN_DRAFT_KEY_PARTS):
                raise ValueError("секреты нельзя сохранять в данных FSM")
