"""PM-06-R2-001/004/006/009: E2E с FakeTelegram и проверки proxy.

Ни один тест не обращается к реальному Telegram API.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from urllib.parse import quote

import pytest
from telebot import asyncio_helper

from fake_telegram import TEST_TOKEN, FakeTelegram
from harness import db_path, draft_data, future_date_text, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.bot.factory import close_bot_session, create_bot
from postmaster.config import Settings
from postmaster.domain.dialog import DialogState
from postmaster.services.dialog_service import DRAFT_ID_KEY

USER_TG = 42
CHAT = 42


def _buttons(markup: dict) -> list[str]:
    return [
        button.get("callback_data", "")
        for row in markup.get("inline_keyboard", [])
        for button in row
    ]


def _latest_confirm_callback(fake: FakeTelegram) -> str:
    markups = [item["reply_markup"] for item in fake.sent_inline_markups]
    assert markups, "превью с клавиатурой не отправлено"
    data = _buttons(markups[-1])
    return next(value for value in data if value.startswith("pm05c:"))


def _latest_cancel_callback(fake: FakeTelegram) -> str:
    markups = [item["reply_markup"] for item in fake.sent_inline_markups]
    data = _buttons(markups[-1])
    return next(value for value in data if value.startswith("pm05x:"))


async def _drive_to_preview(
    app: Application, fake: FakeTelegram, *, caption: str | None = None
) -> None:
    fake.add_text_message("/start", chat_id=CHAT, user_id=USER_TG)
    await wait_for_state(app, USER_TG, DialogState.WAITING_PHOTO)
    fake.add_photo_message("pm06-photo", chat_id=CHAT, user_id=USER_TG, caption=caption)
    await wait_for_state(app, USER_TG, DialogState.WAITING_PUBLICATION_TYPE)
    fake.add_text_message("🔔 Напомнить мне", chat_id=CHAT, user_id=USER_TG)
    await wait_for_state(app, USER_TG, DialogState.WAITING_DATETIME)
    before = len(fake.sent_inline_markups)
    fake.add_text_message(future_date_text(days=2), chat_id=CHAT, user_id=USER_TG)
    await wait_until(lambda: len(fake.sent_inline_markups) > before)
    await wait_for_state(app, USER_TG, DialogState.WAITING_CONFIRMATION)


# === PM06-R2-001 ===========================================================


async def test_001_e2e_old_button_does_not_confirm_new_draft(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    await _drive_to_preview(running_app, fake_telegram)
    old_callback = _latest_confirm_callback(fake_telegram)

    await _drive_to_preview(running_app, fake_telegram)
    fresh_callback = _latest_confirm_callback(fake_telegram)
    assert fresh_callback != old_callback

    before = len(fake_telegram.sent_messages)
    fake_telegram.add_callback_query(old_callback, chat_id=CHAT, user_id=USER_TG)
    await wait_until(lambda: len(fake_telegram.sent_messages) > before)
    assert "устарел" in fake_telegram.sent_messages[-1]["text"].lower()
    with __import__("sqlite3").connect(db_path(tmp_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0

    before = len(fake_telegram.sent_messages)
    fake_telegram.add_callback_query(fresh_callback, chat_id=CHAT, user_id=USER_TG)
    await wait_until(lambda: len(fake_telegram.sent_messages) > before)
    await wait_for_state(running_app, USER_TG, DialogState.IDLE)
    with __import__("sqlite3").connect(db_path(tmp_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1


# === PM06-R2-004 ===========================================================


async def test_004_e2e_preview_has_no_markdown_and_falls_back(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    caption = "*жирный* _курсив_ [link] `code` 😀🎯"
    fake_telegram.fail_send_photo = (400, "Bad Request: wrong file identifier")
    fake_telegram.add_text_message("/start", chat_id=CHAT, user_id=USER_TG)
    await wait_for_state(running_app, USER_TG, DialogState.WAITING_PHOTO)
    fake_telegram.add_photo_message("pm06-photo", chat_id=CHAT, user_id=USER_TG, caption=caption)
    await wait_for_state(running_app, USER_TG, DialogState.WAITING_PUBLICATION_TYPE)
    fake_telegram.add_text_message("🔔 Напомнить мне", chat_id=CHAT, user_id=USER_TG)
    await wait_for_state(running_app, USER_TG, DialogState.WAITING_DATETIME)
    before = len(fake_telegram.sent_inline_markups)
    fake_telegram.add_text_message(future_date_text(days=2), chat_id=CHAT, user_id=USER_TG)
    await wait_until(lambda: len(fake_telegram.sent_inline_markups) > before)

    preview_params = fake_telegram.sent_message_params[-1]
    assert "parse_mode" not in preview_params
    assert "*жирный*" in preview_params["text"]
    assert "**" not in preview_params["text"]
    assert fake_telegram.sent_inline_markups[-1]["reply_markup"]["inline_keyboard"]
    draft = await draft_data(running_app, USER_TG)
    assert draft[DRAFT_ID_KEY]
    assert caption in draft.get("caption", "")
    assert await running_app.bot.get_state(USER_TG, CHAT) == DialogState.WAITING_CONFIRMATION


# === PM06-R2-006 ===========================================================


async def test_006_e2e_stale_callback_keeps_keyboard_and_answers_separately(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    await _drive_to_preview(running_app, fake_telegram)
    before_edits = len(fake_telegram.edited_markups)
    before_answers = len(fake_telegram.answered_callbacks)
    fake_telegram.add_callback_query("pm05:confirm", chat_id=CHAT, user_id=USER_TG)
    await wait_until(lambda: len(fake_telegram.answered_callbacks) > before_answers)
    assert len(fake_telegram.edited_markups) == before_edits
    assert "устарел" in fake_telegram.answered_callbacks[-1]["text"].lower()

    before_answers = len(fake_telegram.answered_callbacks)
    fake_telegram.add_callback_query(
        _latest_confirm_callback(fake_telegram), chat_id=CHAT, user_id=USER_TG
    )
    await wait_until(lambda: len(fake_telegram.answered_callbacks) > before_answers)
    await wait_for_state(running_app, USER_TG, DialogState.IDLE)
    assert len(fake_telegram.edited_markups) == before_edits + 1


async def test_006_e2e_recoverable_error_keeps_buttons(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    from postmaster.services.post_service import (
        PostCreationError,
        PostCreationErrorCode,
    )

    await _drive_to_preview(running_app, fake_telegram)
    post_service = running_app._dialog_service._posts
    original = post_service.create_scheduled_post

    async def failing_once(**kwargs):
        post_service.create_scheduled_post = original
        raise PostCreationError(PostCreationErrorCode.POST_CREATE_FAILED)

    post_service.create_scheduled_post = failing_once  # type: ignore[method-assign]
    before = len(fake_telegram.sent_messages)
    fake_telegram.add_callback_query(
        _latest_confirm_callback(fake_telegram), chat_id=CHAT, user_id=USER_TG
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) > before)
    assert len(fake_telegram.edited_markups) == 0
    with __import__("sqlite3").connect(db_path(tmp_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0

    before = len(fake_telegram.sent_messages)
    fake_telegram.add_callback_query(
        _latest_confirm_callback(fake_telegram), chat_id=CHAT, user_id=USER_TG
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) > before)
    await wait_for_state(running_app, USER_TG, DialogState.IDLE)
    assert len(fake_telegram.edited_markups) == 1
    with __import__("sqlite3").connect(db_path(tmp_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1


async def test_006_e2e_answer_callback_failure_does_not_create_duplicates(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    await _drive_to_preview(running_app, fake_telegram)
    callback = _latest_confirm_callback(fake_telegram)
    fake_telegram.fail_answer_callback = (500, "Internal Server Error")
    before = len(fake_telegram.sent_messages)
    fake_telegram.add_callback_query(callback, chat_id=CHAT, user_id=USER_TG)
    await wait_until(lambda: len(fake_telegram.sent_messages) > before)
    await wait_for_state(running_app, USER_TG, DialogState.IDLE)
    fake_telegram.fail_answer_callback = None
    before = len(fake_telegram.sent_messages)
    fake_telegram.add_callback_query(callback, chat_id=CHAT, user_id=USER_TG)
    await wait_until(lambda: len(fake_telegram.sent_messages) > before)
    with __import__("sqlite3").connect(db_path(tmp_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1


# === PM06-R2-007 ===========================================================


async def test_007_e2e_fsm_survives_restart(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    await _drive_to_preview(running_app, fake_telegram)
    draft = await draft_data(running_app, USER_TG)
    draft_id = draft[DRAFT_ID_KEY]
    callback = _latest_confirm_callback(fake_telegram)

    restarted = Application.from_settings(
        Settings(
            bot_token=TEST_TOKEN,
            database_url=f"sqlite+aiosqlite:///{db_path(tmp_path)}",
            log_level="INFO",
        )
    )
    try:
        assert await restarted.bot.get_state(USER_TG, CHAT) == DialogState.WAITING_CONFIRMATION
        restored = await draft_data(restarted, USER_TG)
        assert restored[DRAFT_ID_KEY] == draft_id
        assert await restarted._dialog_service._store.get_state(USER_TG, CHAT) == (
            DialogState.WAITING_CONFIRMATION
        )
    finally:
        await restarted.stop()
        await restarted._engine.dispose()
    assert callback.startswith("pm05c:")


# === PM06-R2-009 ===========================================================


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_009_proxy_getfile_and_sendphoto(kind, fake_telegram: FakeTelegram) -> None:
    from test_bot_proxy import TunnelProxy

    proxy = TunnelProxy(kind, True)
    port = await proxy.start()
    auth = f"{quote('proxy-user')}:{quote('proxy@secret', safe='')}@"
    bot = create_bot(TEST_TOKEN, f"{kind}://{auth}127.0.0.1:{port}")
    try:
        assert (await bot.get_me()).username == "postmaster_test_bot"
        file = await bot.get_file("pm06-file-id")
        assert file.file_id == "pm06-file-id"
        await bot.send_photo(CHAT, "pm06-file-id", caption="тест")
        assert {"getMe", "getFile", "sendPhoto"}.issubset(fake_telegram.methods)
        assert proxy.connections > 0
        if kind == "socks5":
            assert ("proxy-user", "proxy@secret") in proxy.authentications
        else:
            token = base64.b64encode(b"proxy-user:proxy@secret").decode()
            assert any(token in header for header in proxy.authentications)
    finally:
        await close_bot_session()
        await proxy.stop()


async def test_009_proxy_configuration_error_hides_credentials(caplog) -> None:
    from postmaster.bot.factory import ProxyConfigurationError

    caplog.set_level(logging.DEBUG)
    with pytest.raises(ProxyConfigurationError) as info:
        create_bot(TEST_TOKEN, "http://user:super%40secret@127.0.0.1:1080%ZZ")
    message = str(info.value)
    assert "super%40secret" not in message
    assert "super@secret" not in message
    assert "super%40secret" not in caplog.text
    await close_bot_session()


async def test_009_multiple_bots_share_one_proxy_configuration(
    fake_telegram: FakeTelegram,
) -> None:
    from test_bot_proxy import TunnelProxy

    proxy = TunnelProxy("socks5", False)
    port = await proxy.start()
    url = f"socks5://127.0.0.1:{port}"
    first = create_bot(TEST_TOKEN, url)
    second = create_bot(TEST_TOKEN, url)
    try:
        assert asyncio_helper.session_manager._proxy_url == url
        assert (await first.get_me()).username == "postmaster_test_bot"
        assert (await second.get_me()).username == "postmaster_test_bot"
        with pytest.raises(RuntimeError, match="HTTP-сессии"):
            create_bot(TEST_TOKEN, "http://127.0.0.1:9999")
    finally:
        await close_bot_session()
        await proxy.stop()


async def test_009_direct_mode_without_proxy(fake_telegram: FakeTelegram) -> None:
    bot = create_bot(TEST_TOKEN, None)
    try:
        assert (await bot.get_me()).username == "postmaster_test_bot"
        assert asyncio_helper.session_manager._proxy_url is None
        assert asyncio_helper.proxy is None
    finally:
        await close_bot_session()


def test_009_docker_compose_passes_proxy_from_env() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    example = Path(".env.example").read_text(encoding="utf-8")
    assert "BOT_PROXY_URL" in compose
    assert "BOT_PROXY_URL" in example
