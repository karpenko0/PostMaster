"""Хранилище состояния диалога на состояниях pyTelegramBotAPI (SPEC-003, решения D2 и D3).

Порт DialogStore объявлен в services. Этот модуль его реализует, но не импортирует services
и domain: данные передаются строками, а правила остаются в сервисе.
"""

from telebot.async_telebot import AsyncTeleBot


class TelebotDialogStore:
    """Состояние и черновик в памяти процесса. Сбрасываются при перезапуске (решение D2)."""

    def __init__(self, bot: AsyncTeleBot) -> None:
        self._bot = bot

    async def get_state(self, user_id: int, chat_id: int) -> str | None:
        return await self._bot.get_state(user_id, chat_id)

    async def set_state(self, user_id: int, chat_id: int, state: str) -> None:
        # Запись состояния создаёт нужную строку хранилища. Без неё set_data выбрасывает
        # RuntimeError, поэтому состояние записывается раньше данных (решение D3).
        await self._bot.set_state(user_id, state, chat_id)

    async def get_draft(self, user_id: int, chat_id: int) -> dict[str, str]:
        data = await self._bot.current_states.get_data(chat_id, user_id, bot_id=self._bot.bot_id)
        # Хранилище отдаёт свой внутренний словарь. Копия защищает его от изменений снаружи.
        return {key: str(value) for key, value in data.items()}

    async def set_draft(self, user_id: int, chat_id: int, draft: dict[str, str]) -> None:
        await self._bot.reset_data(user_id, chat_id)
        for key, value in draft.items():
            await self._bot.add_data(user_id, chat_id, **{key: value})
