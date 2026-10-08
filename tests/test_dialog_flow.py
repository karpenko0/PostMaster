"""Сценарий создания публикации через Long Polling и fake Telegram API (SPEC-003).

Проверяются переходы и ответы на каждом шаге, недопустимые сообщения и работа только в личных
чатах. Содержимое созданной публикации проверяется в test_dialog_service.py.
"""

import asyncio

from fake_telegram import FakeTelegram
from harness import current_state, future_date_text, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.domain.dialog import DialogState
from postmaster.handlers.dialog import HINT_BY_STATE

IDLE = DialogState.IDLE
WAITING_PHOTO = DialogState.WAITING_PHOTO
WAITING_DATETIME = DialogState.WAITING_DATETIME


async def _start(app: Application, fake: FakeTelegram, user_id: int = 42) -> None:
    fake.add_text_message("/start", chat_id=user_id, user_id=user_id, first_name="Иван")
    await wait_until(lambda: len(fake.sent_messages) >= 1)
    await wait_for_state(app, user_id, WAITING_PHOTO)


async def _reply_count(fake: FakeTelegram, expected: int) -> None:
    await wait_until(lambda: len(fake.sent_messages) == expected)


async def test_full_scenario_creates_post_and_returns_to_idle(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # US-01, AC-01, AC-03, TC-01: /start → фото → дата → IDLE.
    await _start(running_app, fake_telegram)

    fake_telegram.add_photo_message("photo-1", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 2)
    assert fake_telegram.sent_messages[1]["text"].startswith("Фото принято. ")
    await wait_for_state(running_app, 42, WAITING_DATETIME)

    fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 3)
    created = fake_telegram.sent_messages[2]["text"]
    assert created.startswith("Публикация создана на ")
    assert "(UTC)" in created
    await wait_for_state(running_app, 42, IDLE)


async def test_photo_before_start_gets_start_hint(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # TC-02: фото вне сценария. Состояние не появляется.
    fake_telegram.add_photo_message("photo-1", chat_id=42, user_id=42)

    await _reply_count(fake_telegram, 1)

    assert fake_telegram.sent_messages[0]["text"] == HINT_BY_STATE[IDLE]
    assert await current_state(running_app, 42) is None


async def test_text_while_waiting_photo_gets_photo_hint(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # TC-02: на шаге фото бот ждёт изображение.
    await _start(running_app, fake_telegram)

    fake_telegram.add_text_message("привет", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 2)

    assert fake_telegram.sent_messages[1]["text"] == HINT_BY_STATE[WAITING_PHOTO]
    await wait_for_state(running_app, 42, WAITING_PHOTO)


async def test_valid_date_before_photo_does_not_create_post(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # AC-02: дата допустима только после фото. Раньше она считается недопустимым сообщением.
    await _start(running_app, fake_telegram)

    fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 2)

    assert fake_telegram.sent_messages[1]["text"] == HINT_BY_STATE[WAITING_PHOTO]
    await wait_for_state(running_app, 42, WAITING_PHOTO)


async def test_invalid_date_keeps_waiting_datetime(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # D7, TC-02: несуществующая дата не сбивает шаг.
    await _start(running_app, fake_telegram)
    fake_telegram.add_photo_message("photo-1", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 2)

    fake_telegram.add_text_message("31.02.2026 10:00", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 3)

    assert fake_telegram.sent_messages[2]["text"].startswith("Не удалось распознать дату")
    await wait_for_state(running_app, 42, WAITING_DATETIME)


async def test_non_photo_media_on_date_step_gets_date_hint(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # TC-02: документ на шаге даты недопустим.
    await _start(running_app, fake_telegram)
    fake_telegram.add_photo_message("photo-1", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 2)

    fake_telegram.add_media_message("document", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 3)

    assert fake_telegram.sent_messages[2]["text"] == HINT_BY_STATE[WAITING_DATETIME]
    await wait_for_state(running_app, 42, WAITING_DATETIME)


async def test_repeated_photo_replaces_previous_and_asks_date_again(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # BR-04, AC-04, TC-03: новое фото заменяет старое, дата запрашивается снова.
    await _start(running_app, fake_telegram)
    fake_telegram.add_photo_message("photo-1", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 2)

    fake_telegram.add_photo_message("photo-2", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 3)

    assert fake_telegram.sent_messages[2]["text"].startswith("Фото заменено. ")
    await wait_for_state(running_app, 42, WAITING_DATETIME)

    fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 4)
    await wait_for_state(running_app, 42, IDLE)


async def test_start_during_scenario_discards_photo_and_restarts(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # BR-01: /start в середине сценария начинает его заново. Прежнее фото не сохраняется.
    await _start(running_app, fake_telegram)
    fake_telegram.add_photo_message("photo-1", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 2)

    fake_telegram.add_text_message("/start", chat_id=42, user_id=42, first_name="Иван")
    await _reply_count(fake_telegram, 3)
    await wait_for_state(running_app, 42, WAITING_PHOTO)

    fake_telegram.add_photo_message("photo-2", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 4)

    # После нового /start фото принимается как первое, а не заменяет прежнее.
    assert fake_telegram.sent_messages[3]["text"].startswith("Фото принято. ")


async def test_group_chat_messages_are_ignored(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # D14: в группе сценарий не запускается, бот не отвечает на каждое сообщение.
    fake_telegram.add_text_message(
        "/start", chat_id=-1001, user_id=77, first_name="Группа", chat_type="group"
    )
    fake_telegram.add_text_message(
        "привет", chat_id=-1001, user_id=77, first_name="Группа", chat_type="group"
    )
    fake_telegram.add_photo_message(
        "group-photo", chat_id=-1001, user_id=77, first_name="Группа", chat_type="group"
    )

    # Личный /start идёт в той же порции обновлений. Когда он обработан, групповые тоже.
    await _start(running_app, fake_telegram, user_id=42)
    await asyncio.sleep(0.2)

    assert {message["chat_id"] for message in fake_telegram.sent_messages} == {"42"}
    assert await current_state(running_app, 77, -1001) is None
