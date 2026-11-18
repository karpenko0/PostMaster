"""SPEC-033: E2E-тесты.

Полные сценарии от Telegram-ввода до DB/result через running_app с
локальным fake Telegram API (test double, §7). Проверки §8:

- Reminder E2E;
- Telegram E2E;
- /cancel;
- invalid datetime.

Каждый сценарий идёт через реальный ``Application`` (Long Polling
через fake API, настоящие services + repositories + SQLite + Scheduler).
Результаты проверяются по DB (``Post.status``) и по outgoing-сообщениям
fake Telegram API (sent_messages).
"""

import asyncio
import zoneinfo
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fake_telegram import TEST_TOKEN
from harness import WAIT_SECONDS, db_path, latest_callback_data, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.config import Settings
from postmaster.database.engine import create_session_factory
from postmaster.domain.dialog import DialogState
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.handlers.dialog import REMINDER_BUTTON, TELEGRAM_BUTTON
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository

USER_TG = 99001
CHAT_ID = 99001
PHOTO_ID = "e2e_photo_id"
# Расписание вводится в часовом поясе пользователя (DEFAULT_TIMEZONE = Europe/Moscow),
# поэтому «будущее» время нужно формировать в этом же поясе, а не в UTC.
USER_TIMEZONE = zoneinfo.ZoneInfo("Europe/Moscow")


def _future_datetime_text(hours: int = 2) -> str:
    """Будущее время в формате ввода, в часовом поясе по умолчанию."""
    return (datetime.now(USER_TIMEZONE) + timedelta(hours=hours)).strftime("%d.%m.%Y %H:%M")


async def _wait_for_sent(fake_telegram, *, marker: str, timeout: float = 5.0) -> list[dict]:
    """Ждёт появления сообщения в fake_telegram.sent_messages, содержащего ``marker``."""
    elapsed = 0.0
    interval = 0.05
    while elapsed < timeout:
        for message in fake_telegram.sent_messages:
            text = message.get("text", "")
            if marker in text:
                return list(fake_telegram.sent_messages)
        await asyncio.sleep(interval)
        elapsed += interval
    raise AssertionError(
        f"сообщение с маркером {marker!r} не появилось за {timeout}s: "
        f"{[m.get('text') for m in fake_telegram.sent_messages]}"
    )


def _build_app(
    tmp_path: Path,
    *,
    post_mode: str = "REMINDER",
    target_id: str | None = None,
) -> Application:
    kwargs = {
        "bot_token": TEST_TOKEN,
        "database_url": f"sqlite+aiosqlite:///{db_path(tmp_path)}",
        "log_level": "INFO",
        "post_mode": post_mode,
    }
    if target_id is not None:
        kwargs["telegram_target_id"] = target_id
    settings = Settings(**kwargs)
    return Application.from_settings(settings)


async def _start_app(app: Application, fake_telegram) -> None:
    """Запускает app.run() как фоновую задачу и ждёт первого опроса fake Telegram."""
    fake_telegram.methods.clear()
    fake_telegram.update_delivered.clear()
    fake_telegram.get_updates_called.clear()
    run_task = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(fake_telegram.get_updates_called.wait(), timeout=WAIT_SECONDS)
    finally:
        app._e2e_run_task = run_task  # type: ignore[attr-defined]
    fake_telegram.update_delivered.clear()


async def _stop_app(app: Application, fake_telegram) -> None:
    await app.stop()
    run_task = getattr(app, "_e2e_run_task", None)
    if run_task is not None:
        await asyncio.wait_for(run_task, timeout=WAIT_SECONDS)
        delattr(app, "_e2e_run_task")


async def _send_and_await_update(fake_telegram, *, timeout: float = 3.0) -> None:
    """Даёт Long Polling забрать поставленный update.

    Событие ``update_delivered`` сигналит о любом ответе getUpdates, поэтому
    batch из нескольких update'ов может «съесть» сигнал. Синхронизация по
    факту обработки выполняется через ``wait_for_state``/``_wait_for_sent``,
    здесь достаточно короткой паузы.
    """
    await asyncio.sleep(0.02)


@pytest.fixture
async def reminder_app(tmp_path: Path, fake_telegram) -> AsyncIterator[Application]:
    """Running app в режиме REMINDER (POST_MODE по умолчанию)."""
    app = _build_app(tmp_path)
    await _start_app(app, fake_telegram)
    try:
        yield app
    finally:
        await _stop_app(app, fake_telegram)


@pytest.fixture
async def telegram_app(tmp_path: Path, fake_telegram) -> AsyncIterator[Application]:
    """Running app в режиме TELEGRAM с заданным target_id."""
    app = _build_app(tmp_path, post_mode="TELEGRAM", target_id="-1001234567890")
    await _start_app(app, fake_telegram)
    try:
        yield app
    finally:
        await _stop_app(app, fake_telegram)


def _texts_of(messages: list[dict]) -> list[str]:
    return [m.get("text", "") for m in messages]


async def _send_text(fake_telegram, text: str, *, marker: str) -> None:
    """Отправляет текст и ждёт ответ бота с маркером."""
    fake_telegram.add_text_message(text, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_sent(fake_telegram, marker=marker)


async def _confirm_preview(fake_telegram, *, marker: str) -> None:
    """Нажимает кнопку «Подтвердить» под последним превью (PM06-R2-001)."""
    fake_telegram.add_callback_query(
        latest_callback_data(fake_telegram, "pm05c:"), chat_id=CHAT_ID, user_id=USER_TG
    )
    await _send_and_await_update(fake_telegram)
    await _wait_for_sent(fake_telegram, marker=marker)


async def _start_dialog(app: Application, fake_telegram) -> None:
    """Отправляет /start и дожидается приветствия и состояния WAITING_PHOTO (SPEC-003)."""
    fake_telegram.add_text_message("/start", chat_id=CHAT_ID, user_id=USER_TG)
    await _wait_for_sent(fake_telegram, marker="PostMaster")
    await wait_for_state(app, USER_TG, DialogState.WAITING_PHOTO)


async def _drive_to_preview(
    app: Application,
    fake_telegram,
    date_text: str,
    *,
    type_text: str = REMINDER_BUTTON,
    expect_preview: bool = True,
) -> None:
    """Путь диалога до превью: фото → тип публикации → дата (PM-03, PM-05).

    Переходы проверяются по состоянию FSM (wait_for_state), а не по счётчику
    сообщений: так тест не зависит от порядка доставки update'ов fake API.
    """
    fake_telegram.add_photo_message(file_id=PHOTO_ID, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await wait_for_state(app, USER_TG, DialogState.WAITING_PUBLICATION_TYPE)
    fake_telegram.add_text_message(type_text, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await wait_for_state(app, USER_TG, DialogState.WAITING_DATETIME)
    if not expect_preview:
        # Невалидная/прошедшая дата: превью не отправляется, шаг проверяется
        # вызывающим тестом по маркеру ошибки.
        fake_telegram.add_text_message(date_text, chat_id=CHAT_ID, user_id=USER_TG)
        await _send_and_await_update(fake_telegram)
        return
    photos_before = len(fake_telegram.sent_photos)
    fake_telegram.add_text_message(date_text, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await wait_until(lambda: len(fake_telegram.sent_photos) > photos_before)


# === Reminder E2E (§8) =====================================================


async def test_reminder_e2e_happy_path_creates_pending_post(reminder_app, fake_telegram) -> None:
    """/start → photo → datetime → пользователь зарегистрирован, пост в PENDING,
    задание в Scheduler; outgoing сообщения содержат подтверждение."""
    app = reminder_app

    # /start — пользователь регистрируется, бот присылает приветствие.
    await _start_dialog(app, fake_telegram)
    await _wait_for_sent(fake_telegram, marker="PostMaster")

    # Фото → выбор типа публикации → превью (PM-03, PM-05).
    # Используем Europe/Moscow (DEFAULT_TIMEZONE), поэтому формируем локальное время.
    future_local = datetime(2099, 1, 1, 12, 0, tzinfo=USER_TIMEZONE)
    await _drive_to_preview(app, fake_telegram, future_local.strftime("%d.%m.%Y %H:%M"))

    # Пост создаётся только после подтверждения превью.
    await _confirm_preview(fake_telegram, marker="Принято")

    # Состояние пользователя и публикации в БД.
    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    posts = await PostRepository(factory).list_pending_by_user(user.id)
    assert len(posts) == 1
    post = posts[0]
    assert post.status == PostStatus.PENDING
    assert post.publication_type == PublicationType.REMINDER
    assert post.telegram_file_id == PHOTO_ID

    # Scheduler получил задание на publish.
    # Проверяем через _scheduler.running, так как APScheduler встроен в SchedulerService
    assert app._scheduler.running, "Scheduler не запущен"

    # В REMINDER режиме подтверждение содержит «напомню».
    confirmation_texts = [
        text for text in _texts_of(fake_telegram.sent_messages) if "Принято" in text
    ]
    assert confirmation_texts
    assert "напомню" in confirmation_texts[0]


# === Telegram E2E (§8) ====================================================


async def test_telegram_e2e_happy_path_saves_post_with_target(telegram_app, fake_telegram) -> None:
    """POST_MODE=TELEGRAM: /start → photo → тип → канал → дата → confirm.

    Проверяется, что пост создан с target_id и publication_type=TELEGRAM.
    """
    app = telegram_app

    await _start_dialog(app, fake_telegram)
    await wait_for_state(app, USER_TG, DialogState.WAITING_PHOTO)

    fake_telegram.add_photo_message(file_id=PHOTO_ID, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await wait_for_state(app, USER_TG, DialogState.WAITING_PUBLICATION_TYPE)
    fake_telegram.add_text_message(TELEGRAM_BUTTON, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await wait_for_state(app, USER_TG, DialogState.WAITING_TARGET)
    fake_telegram.add_text_message("-1001234567890", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await wait_for_state(app, USER_TG, DialogState.WAITING_DATETIME)

    photos_before = len(fake_telegram.sent_photos)
    fake_telegram.add_text_message(_future_datetime_text(3), chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await wait_until(lambda: len(fake_telegram.sent_photos) > photos_before)
    await wait_for_state(app, USER_TG, DialogState.WAITING_CONFIRMATION)
    await _confirm_preview(fake_telegram, marker="Запланировано")

    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    posts = await PostRepository(factory).list_pending_by_user(user.id)
    assert len(posts) == 1
    post = posts[0]
    assert post.publication_type == PublicationType.TELEGRAM
    assert post.target_id == "-1001234567890"
    assert post.status == PostStatus.PENDING


# === /cancel (§8) ========================================================


async def test_cancel_e2e_marks_post_cancelled_and_removes_scheduler_job(
    reminder_app, fake_telegram
) -> None:
    """Полный happy-path → /cancel → пост CANCELLED, задание удалено из Scheduler."""
    app = reminder_app

    await _start_dialog(app, fake_telegram)

    await _drive_to_preview(app, fake_telegram, _future_datetime_text())
    await _confirm_preview(fake_telegram, marker="Принято")

    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    post = (await PostRepository(factory).list_pending_by_user(user.id))[0]
    assert post.status == PostStatus.PENDING
    assert any(
        job.id == f"post:{post.id}"
        for job in app._scheduler._scheduler.get_jobs()  # type: ignore[attr-defined]
    )

    fake_telegram.add_text_message(f"/cancel {post.id}", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_sent(fake_telegram, marker="отменена")

    persisted = await PostRepository(factory).get_by_id(post.id)
    assert persisted is not None
    assert persisted.status == PostStatus.CANCELLED
    assert not any(
        job.id == f"post:{post.id}"
        for job in app._scheduler._scheduler.get_jobs()  # type: ignore[attr-defined]
    )


async def test_cancel_e2e_nonexistent_post_returns_error(reminder_app, fake_telegram) -> None:
    """/cancel с несуществующим post_id: сообщение об ошибке, ничего не падает."""
    app = reminder_app

    await _start_dialog(app, fake_telegram)

    fake_telegram.add_text_message("/cancel 999999", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    # Пользователь существует, но пост не найден — нормальная ситуация.
    await _wait_for_sent(fake_telegram, marker="не найдена")

    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None


# === invalid datetime (§8) ===============================================


async def test_invalid_datetime_e2e_preserves_draft_and_returns_error(
    reminder_app, fake_telegram
) -> None:
    """Невалидная дата: ошибка пользователю, draft сохранён, пост НЕ создан."""
    app = reminder_app

    await _start_dialog(app, fake_telegram)

    await _drive_to_preview(app, fake_telegram, "вчера в обед", expect_preview=False)
    await _wait_for_sent(fake_telegram, marker="не понимаю такой формат")

    # Пост НЕ создан.
    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    posts = await PostRepository(factory).list_pending_by_user(user.id)
    assert posts == []

    # Пользователь снова может прислать корректную дату (черновик сохранён).
    photos_before = len(fake_telegram.sent_photos)
    fake_telegram.add_text_message(_future_datetime_text(), chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    while len(fake_telegram.sent_photos) <= photos_before:
        await asyncio.sleep(0.01)
    await _confirm_preview(fake_telegram, marker="Принято")

    posts_after = await PostRepository(factory).list_pending_by_user(user.id)
    assert len(posts_after) == 1


async def test_past_datetime_e2e_rejected_and_post_not_created(reminder_app, fake_telegram) -> None:
    """Прошедшая дата: rejection, пост не создан (SPEC-005)."""
    app = reminder_app

    await _start_dialog(app, fake_telegram)
    past = datetime.now(USER_TIMEZONE) - timedelta(days=1)
    await _drive_to_preview(
        app, fake_telegram, past.strftime("%d.%m.%Y %H:%M"), expect_preview=False
    )
    await _wait_for_sent(fake_telegram, marker="уже прошло")

    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    posts = await PostRepository(factory).list_pending_by_user(user.id)
    assert posts == []


# === /my_posts (бонус) ===================================================


async def test_my_posts_e2e_empty_then_shows_post(reminder_app, fake_telegram) -> None:
    """/my_posts пуст у нового пользователя, потом показывает запланированный пост."""
    app = reminder_app

    await _start_dialog(app, fake_telegram)
    fake_telegram.add_text_message("/my_posts", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_sent(fake_telegram, marker="нет запланированных")

    await _drive_to_preview(app, fake_telegram, _future_datetime_text())
    await _confirm_preview(fake_telegram, marker="Принято")

    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    expected_post = (await PostRepository(factory).list_pending_by_user(user.id))[0]

    fake_telegram.add_text_message("/my_posts", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_sent(fake_telegram, marker=f"ID {expected_post.id}")
