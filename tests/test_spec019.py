"""SPEC-019: Команда /help.

Тесты проверяют:
- content: текст справки содержит команды и формат даты.
- IDLE: /help работает без /start.
- WAITING_PHOTO: /help не меняет состояние, пользователь остаётся на шаге фото.
- WAITING_DATETIME: /help не меняет состояние, пользователь остаётся на шаге даты.
- FSM preservation: черновик не очищается после /help.
"""

import logging
from typing import Any

from postmaster.domain.dialog import DialogState
from postmaster.handlers.help import HELP_TEXT, register_help_handler

logger = logging.getLogger(__name__)


# === Help text content (SPEC-019 §8 content) ===


def test_help_text_describes_commands() -> None:
    """Текст справки содержит все команды бота (BR-019-01)."""
    assert "/start" in HELP_TEXT
    assert "/my_posts" in HELP_TEXT
    assert "/cancel" in HELP_TEXT
    assert "/help" in HELP_TEXT


def test_help_text_describes_date_format() -> None:
    """Текст справки содержит формат даты и времени (SPEC-019 §1)."""
    assert "ДД.ММ.ГГГГ" in HELP_TEXT
    assert "ЧЧ:ММ" in HELP_TEXT


def test_help_text_describes_workflow() -> None:
    """Текст справки содержит краткий workflow (SPEC-019 §1)."""
    assert "/start" in HELP_TEXT
    # Должен быть упомянут процесс: фото + дата/время
    assert "фото" in HELP_TEXT.lower()
    assert "дат" in HELP_TEXT.lower()


def test_help_text_does_not_describe_non_existing_features() -> None:
    """Текст справки не упоминает несуществующие функции (BR-019-01)."""
    # Список недопустимых утверждений — если они появятся, тест упадёт.
    forbidden = ["/admin", "/debug", "VK", "Twitter", "Instagram", "/schedule"]
    for word in forbidden:
        assert word not in HELP_TEXT, f"help mentions non-existent feature: {word}"


# === Handler registration (SPEC-019 §1) ===


async def test_handler_register_only_private_chat() -> None:
    """Handler /help зарегистрирован только для приватных чатов."""

    class _StubBot:
        def __init__(self) -> None:
            self.handlers: list[dict[str, Any]] = []

        def message_handler(self, *, commands: list[str], chat_types: list[str]):
            def decorator(func):
                self.handlers.append({"commands": commands, "chat_types": chat_types})
                return func

            return decorator

    bot = _StubBot()  # type: ignore[arg-type]
    register_help_handler(bot)

    assert len(bot.handlers) == 1
    assert bot.handlers[0]["commands"] == ["help"]
    assert bot.handlers[0]["chat_types"] == ["private"]


# === FSM preservation (SPEC-019 §1, §7) ===


async def test_help_preserves_idle_state(running_app, fake_telegram) -> None:
    """/help в состоянии IDLE не меняет состояние."""
    from harness import wait_until

    # Регистрируем пользователя
    fake_telegram.add_text_message(
        "/start", chat_id=400, user_id=400, username=None, first_name="Тестер"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    # После start пользователь в WAITING_PHOTO, отправим фото для перехода в WAITING_DATETIME,
    # затем текст «дата» — должно перейти в IDLE (создание завершено).
    # Проще: проверим, что /help работает без /start, не регистрируясь.
    # Регистрация уже произошла, поэтому тестируем на чистом пользователе.

    # Используем chat_id=500 (новый пользователь) — он не зарегистрирован.
    # Handler /help не требует регистрации, поэтому должен работать.
    fake_telegram.add_text_message("/help", chat_id=500, user_id=500)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    help_message = fake_telegram.sent_messages[-1]
    assert help_message["chat_id"] == "500"
    assert "📖" in help_message["text"]

    # FSM не меняется — у этого пользователя состояние не существует (None)
    state = await running_app.bot.get_state(500, 500)
    assert state is None


async def test_help_preserves_waiting_photo_state(running_app, fake_telegram) -> None:
    """/help в состоянии WAITING_PHOTO не меняет состояние (SPEC-019 §7)."""
    from harness import wait_for_state, wait_until

    fake_telegram.add_text_message(
        "/start", chat_id=600, user_id=600, username=None, first_name="Тестер"
    )
    await wait_for_state(running_app, 600, DialogState.WAITING_PHOTO)

    # Отправляем /help
    fake_telegram.add_text_message("/help", chat_id=600, user_id=600)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    # FSM не изменился
    state = await running_app.bot.get_state(600, 600)
    assert state == DialogState.WAITING_PHOTO.value


async def test_help_preserves_waiting_datetime_state(running_app, fake_telegram) -> None:
    """/help в состоянии WAITING_DATETIME не меняет состояние (SPEC-019 §7)."""
    from harness import wait_for_state, wait_until

    fake_telegram.add_text_message(
        "/start", chat_id=700, user_id=700, username=None, first_name="Тестер"
    )
    await wait_for_state(running_app, 700, DialogState.WAITING_PHOTO)
    fake_telegram.add_photo_message(file_id="photo-help-1", chat_id=700, user_id=700)
    await wait_for_state(running_app, 700, DialogState.WAITING_DATETIME)

    # Отправляем /help
    fake_telegram.add_text_message("/help", chat_id=700, user_id=700)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    # FSM не изменился
    state = await running_app.bot.get_state(700, 700)
    assert state == DialogState.WAITING_DATETIME.value


async def test_help_preserves_draft_data(running_app, fake_telegram) -> None:
    """/help не очищает временные данные FSM (черновик) (SPEC-019 §7)."""
    from harness import draft_data, wait_for_state, wait_until

    fake_telegram.add_text_message(
        "/start", chat_id=800, user_id=800, username=None, first_name="Тестер"
    )
    await wait_for_state(running_app, 800, DialogState.WAITING_PHOTO)
    fake_telegram.add_photo_message(file_id="photo-help-draft", chat_id=800, user_id=800)
    await wait_for_state(running_app, 800, DialogState.WAITING_DATETIME)

    # Проверяем, что draft_data содержит фото
    draft_before = await draft_data(running_app, 800)
    assert draft_before.get("telegram_file_id") == "photo-help-draft"

    # Отправляем /help
    fake_telegram.add_text_message("/help", chat_id=800, user_id=800)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    # Draft не очищен
    draft_after = await draft_data(running_app, 800)
    assert draft_after.get("telegram_file_id") == "photo-help-draft"


async def test_help_response_contains_help_text(running_app, fake_telegram) -> None:
    """/help возвращает текст HELP_TEXT."""
    from harness import wait_until

    fake_telegram.add_text_message("/help", chat_id=900, user_id=900)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

    message = fake_telegram.sent_messages[-1]
    assert message["text"] == HELP_TEXT


async def test_help_works_after_other_commands(running_app, fake_telegram) -> None:
    """/help работает после других команд без перезапуска сценария."""
    from harness import wait_for_state, wait_until

    # Регистрируем, загружаем фото, получаем WAITING_DATETIME
    fake_telegram.add_text_message(
        "/start", chat_id=1000, user_id=1000, username=None, first_name="Тестер"
    )
    await wait_for_state(running_app, 1000, DialogState.WAITING_PHOTO)
    fake_telegram.add_photo_message(file_id="photo-1000", chat_id=1000, user_id=1000)
    await wait_for_state(running_app, 1000, DialogState.WAITING_DATETIME)

    # /my_posts в этом состоянии
    fake_telegram.add_text_message("/my_posts", chat_id=1000, user_id=1000)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    # Состояние не изменилось
    state = await running_app.bot.get_state(1000, 1000)
    assert state == DialogState.WAITING_DATETIME.value

    # /help
    fake_telegram.add_text_message("/help", chat_id=1000, user_id=1000)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 3)

    # Состояние всё ещё не изменилось
    state = await running_app.bot.get_state(1000, 1000)
    assert state == DialogState.WAITING_DATETIME.value

    help_message = fake_telegram.sent_messages[-1]
    assert help_message["text"] == HELP_TEXT
