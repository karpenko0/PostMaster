"""Сценарий создания публикации через Long Polling и fake Telegram API (SPEC-003, SPEC-004).

Проверяются переходы и ответы на каждом шаге, недопустимые сообщения и работа только в личных
чатах. Шаг фото дополнительно проверяет метаданные в FSM (IT-004-01…IT-004-03), отсутствие
файлов изображений на диске (AC-004-05) и отсутствие записей в БД (BR-004-08).
"""

import asyncio
from pathlib import Path

from fake_telegram import FakeTelegram
from harness import (
    current_state,
    db_path,
    db_snapshot,
    draft_data,
    future_date_text,
    users_rows,
    wait_for_state,
    wait_until,
)
from postmaster.app import Application
from postmaster.domain.dialog import DialogState
from postmaster.handlers.dialog import HINT_BY_STATE, PHOTO_ACCEPTED_TEXT

IDLE = DialogState.IDLE
WAITING_PHOTO = DialogState.WAITING_PHOTO
WAITING_DATETIME = DialogState.WAITING_DATETIME

# Ответ §26 SPEC-004: бот просит фотографию вместо другого сообщения (AC-004-08, ERR-004-01).
PHOTO_REQUEST_TEXT = "Отправь фотографию, которую нужно опубликовать."


async def _start(app: Application, fake: FakeTelegram, user_id: int = 42) -> None:
    fake.add_text_message("/start", chat_id=user_id, user_id=user_id, first_name="Иван")
    await wait_until(lambda: len(fake.sent_messages) >= 1)
    await wait_for_state(app, user_id, WAITING_PHOTO)


async def _reply_count(fake: FakeTelegram, expected: int) -> None:
    await wait_until(lambda: len(fake.sent_messages) == expected)


async def _send_photo(app: Application, fake: FakeTelegram, file_id: str) -> None:
    fake.add_photo_message(file_id, chat_id=42, user_id=42)
    await wait_until(lambda: len(fake.sent_messages) >= 2)
    await wait_for_state(app, 42, WAITING_DATETIME)


async def test_full_scenario_creates_post_and_returns_to_idle(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # US-01, AC-01, AC-03, TC-01: /start → фото → дата → IDLE. E2E-004-01 на шаге фото.
    await _start(running_app, fake_telegram)

    fake_telegram.add_photo_message("photo-1", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 2)
    # §25 SPEC-004: точный текст с подсказкой формата (AC-004-07).
    assert fake_telegram.sent_messages[1]["text"] == PHOTO_ACCEPTED_TEXT
    assert "Теперь укажи дату и время публикации." in fake_telegram.sent_messages[1]["text"]
    assert "ДД.ММ.ГГГГ ЧЧ:ММ" in fake_telegram.sent_messages[1]["text"]
    await wait_for_state(running_app, 42, WAITING_DATETIME)

    fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 3)
    created = fake_telegram.sent_messages[2]["text"]
    assert created.startswith("Публикация создана на ")
    assert "(UTC)" in created
    await wait_for_state(running_app, 42, IDLE)


async def test_photo_step_saves_metadata_to_fsm(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # IT-004-01, IT-004-02: Telegram update → handler → PhotoData → FSM, telegram_file_id верный.
    await _start(running_app, fake_telegram)

    await _send_photo(running_app, fake_telegram, "photo-1")

    # Максимальный размер из fake Telegram: 1280×960, file_size 150000 (AC-004-03).
    assert await draft_data(running_app, 42) == {
        "telegram_file_id": "photo-1",
        "telegram_file_unique_id": "photo-1-l",
        "file_size": "150000",
        "width": "1280",
        "height": "960",
    }


async def test_photo_leaves_no_image_files_on_disk(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    # AC-004-05, BR-004-03: изображение не скачивается и не создаёт файлов на диске (§14).
    await _start(running_app, fake_telegram)

    await _send_photo(running_app, fake_telegram, "photo-1")

    images = [
        path
        for path in tmp_path.rglob("*")
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    ]
    assert images == []
    assert not [path for path in tmp_path.rglob("*") if path.name in {"downloads", "uploads"}]


async def test_photo_does_not_touch_database(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    # BR-004-08: фотография сама по себе не создаёт публикацию в БД. Запись придёт с SPEC-008.
    await _start(running_app, fake_telegram)
    before = db_snapshot(db_path(tmp_path))

    await _send_photo(running_app, fake_telegram, "photo-1")

    assert db_snapshot(db_path(tmp_path)) == before


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
    # AC-004-08, §26: на шаге фото бот ждёт изображение, состояние не меняется.
    await _start(running_app, fake_telegram)

    fake_telegram.add_text_message("привет", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 2)

    assert fake_telegram.sent_messages[1]["text"] == PHOTO_REQUEST_TEXT
    await wait_for_state(running_app, 42, WAITING_PHOTO)


async def test_empty_photo_array_asks_photo_and_keeps_state(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # TC-004-04, ERR-004-02: пустой photo[] не меняет FSM и не сохраняет данных.
    await _start(running_app, fake_telegram)

    fake_telegram.add_message({"photo": []}, chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 2)

    assert fake_telegram.sent_messages[1]["text"] == PHOTO_REQUEST_TEXT
    await wait_for_state(running_app, 42, WAITING_PHOTO)
    assert await draft_data(running_app, 42) == {}


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
    await _send_photo(running_app, fake_telegram, "photo-1")

    fake_telegram.add_text_message("31.02.2026 10:00", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 3)

    assert fake_telegram.sent_messages[2]["text"].startswith("Не удалось распознать дату")
    await wait_for_state(running_app, 42, WAITING_DATETIME)


async def test_non_photo_media_on_date_step_gets_date_hint(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # TC-02: документ на шаге даты недопустим.
    await _start(running_app, fake_telegram)
    await _send_photo(running_app, fake_telegram, "photo-1")

    fake_telegram.add_media_message("document", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 3)

    assert fake_telegram.sent_messages[2]["text"] == HINT_BY_STATE[WAITING_DATETIME]
    await wait_for_state(running_app, 42, WAITING_DATETIME)


async def test_repeated_photo_replaces_previous_and_asks_date_again(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    # BR-004-09, AC-004-09, IT-004-03: новое фото заменяет старое, дата запрашивается снова.
    await _start(running_app, fake_telegram)
    await _send_photo(running_app, fake_telegram, "photo-1")
    assert (await draft_data(running_app, 42))["telegram_file_id"] == "photo-1"

    fake_telegram.add_photo_message("photo-2", chat_id=42, user_id=42)
    await _reply_count(fake_telegram, 3)

    assert fake_telegram.sent_messages[2]["text"].startswith("Фото заменено. ")
    await wait_for_state(running_app, 42, WAITING_DATETIME)
    # §27: данные предыдущего фото заменены новыми.
    assert (await draft_data(running_app, 42))["telegram_file_id"] == "photo-2"
    assert (await draft_data(running_app, 42))["telegram_file_unique_id"] == "photo-2-l"

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
    assert fake_telegram.sent_messages[3]["text"] == PHOTO_ACCEPTED_TEXT
    assert (await draft_data(running_app, 42))["telegram_file_id"] == "photo-2"


async def test_group_chat_messages_are_ignored(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
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
    # В группе /start не обрабатывается, поэтому пользователь не регистрируется (D14).
    assert users_rows(db_path(tmp_path), 77) == []
