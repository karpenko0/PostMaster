"""SPEC-015: TelegramPublisher, контракты, unit, integration и сквозные E2E-сценарии."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telebot.asyncio_helper import ApiTelegramException

from fake_telegram import TEST_TOKEN, FakeTelegram
from harness import db_path, future_date_text, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.bot.factory import create_bot
from postmaster.config import Settings
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.dialog import DialogState
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.domain.user import User
from postmaster.publishers.base import (
    BotTelegramClient,
    Publisher,
    PublishResult,
    TelegramClient,
)
from postmaster.publishers.telegram import TelegramErrorCode, TelegramPublisher
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import PublicationService
from postmaster.services.scheduler_service import SchedulerService

SPEC_015_PATH = Path(__file__).resolve().parent.parent / "docs" / "specs" / "SPEC-015.md"
CREATED_AT = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)
CLAIM_AT = datetime(2026, 10, 7, 15, 0, 1, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 10, 7, 15, 20, 0, tzinfo=UTC)
CHANNEL_ID = "-1001234567890"


class FakeChannelClient:
    """Тестовый TelegramClient (§31) с поддержкой задержки и инъекции ошибок."""

    def __init__(
        self,
        *,
        photo_error: BaseException | None = None,
        entered: asyncio.Event | None = None,
        proceed: asyncio.Event | None = None,
    ) -> None:
        self.messages: list[tuple[str | int, str]] = []
        self.photos: list[tuple[str | int, str, str | None]] = []
        self._photo_error = photo_error
        self._entered = entered
        self._proceed = proceed

    async def send_message(self, chat_id: str | int, text: str) -> None:
        self.messages.append((chat_id, text))

    async def send_photo(
        self,
        chat_id: str | int,
        file_id: str,
        caption: str | None = None,
    ) -> None:
        if self._entered is not None:
            self._entered.set()
        if self._proceed is not None:
            await asyncio.wait_for(self._proceed.wait(), timeout=2.0)
        if self._photo_error is not None:
            raise self._photo_error
        self.photos.append((chat_id, file_id, caption))


def _sample_telegram_post(
    *,
    post_id: int = 42,
    user_id: int = 15,
    file_id: str = "AgACAgIAAxkBAAIB_channel_photo",
    publication_type: PublicationType = PublicationType.TELEGRAM,
    target_id: str | None = CHANNEL_ID,
    caption: str | None = None,
    status: PostStatus = PostStatus.PROCESSING,
) -> Post:
    return Post(
        id=post_id,
        user_id=user_id,
        telegram_file_id=file_id,
        telegram_file_unique_id="AQAD_channel_unique",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        status=status,
        publication_type=publication_type,
        target_id=target_id,
        caption=caption,
        published_at=PUBLISHED_AT if status == PostStatus.PUBLISHED else None,
        created_at=CREATED_AT,
        updated_at=CLAIM_AT,
    )


@pytest.fixture
async def spec015_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], UserRepository, PostRepository, User]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/spec015.db")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user_repo = UserRepository(factory)
        post_repo = PostRepository(factory)
        user = await user_repo.add(
            telegram_user_id=800111222,
            username="channel_owner",
            first_name="Алексей",
            now=CREATED_AT,
        )
        yield factory, user_repo, post_repo, user
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Контракты и сверка кодов ошибок со спецификацией (§13, §14, §31, §34)
# ---------------------------------------------------------------------------


def test_telegram_publisher_contracts_and_spec015_error_codes(
    fake_telegram: FakeTelegram,
) -> None:
    """§13, §14, §31, §34: TelegramPublisher наследует Publisher, коды ERR-015-01…10 объявлены."""
    assert issubclass(TelegramPublisher, Publisher)
    client = FakeChannelClient()
    assert isinstance(client, TelegramClient)
    assert isinstance(BotTelegramClient(create_bot(fake_telegram.token)), TelegramClient)

    spec_text = SPEC_015_PATH.read_text(encoding="utf-8")
    spec015_codes = {
        "INVALID_PUBLICATION_TYPE",
        "INVALID_POST_STATUS",
        "TARGET_REQUIRED",
        "FILE_ID_MISSING",
        "TELEGRAM_PERMISSION_DENIED",
        "TELEGRAM_TARGET_UNAVAILABLE",
        "INVALID_FILE_ID",
        "TELEGRAM_API_TIMEOUT",
        "NETWORK_ERROR",
        "TELEGRAM_API_UNAVAILABLE",
    }
    assert spec015_codes <= {code.value for code in TelegramErrorCode}
    for code in spec015_codes:
        assert code in spec_text


# ---------------------------------------------------------------------------
# §51–54 Unit Tests (TC-015-01 … TC-015-07, permission, timeout, invalid file_id)
# ---------------------------------------------------------------------------


async def test_tc_015_01_successful_publish_sends_photo_to_target_without_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC-015-01, AC-015-01…04, AC-015-14, BR-015-01…05, §46–47."""
    monkeypatch.chdir(tmp_path)
    post = _sample_telegram_post(
        post_id=42,
        target_id=CHANNEL_ID,
        file_id="AgAC_channel_42",
    )
    client = FakeChannelClient()
    publisher = TelegramPublisher(client)

    with caplog.at_level("INFO", logger="postmaster.publishers.telegram"):
        result = await publisher.publish(post)

    assert result == PublishResult(success=True, error_code=None, error_message=None)
    assert client.photos == [(CHANNEL_ID, "AgAC_channel_42", None)]
    assert client.messages == []
    assert list(tmp_path.iterdir()) == []
    assert f"Telegram publication started post_id=42 target_id={CHANNEL_ID}" in caplog.text
    assert "Telegram publication completed post_id=42" in caplog.text
    assert "AgAC_channel_42" not in caplog.text


async def test_tc_015_02_caption_is_passed_to_send_photo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TC-015-02, AC-015-05, BR-015-06, §18: caption передаётся вместе с фотографией."""
    post = _sample_telegram_post(caption="Hello")
    client = FakeChannelClient()
    publisher = TelegramPublisher(client)

    with caplog.at_level("INFO", logger="postmaster.publishers.telegram"):
        result = await publisher.publish(post)

    assert result.success is True
    assert client.photos == [(CHANNEL_ID, post.telegram_file_id, "Hello")]
    assert "Hello" not in caplog.text


async def test_tc_015_03_no_caption_sends_photo_without_caption() -> None:
    """TC-015-03, AC-015-06, §18: при caption=None фотография публикуется без подписи."""
    post = _sample_telegram_post(caption=None)
    client = FakeChannelClient()
    publisher = TelegramPublisher(client)

    result = await publisher.publish(post)

    assert result.success is True
    assert client.photos == [(CHANNEL_ID, post.telegram_file_id, None)]


@pytest.mark.parametrize("bad_target", [None, "", "   ", True])
async def test_tc_015_04_missing_target_returns_target_required_without_calling_api(
    bad_target: object, caplog: pytest.LogCaptureFixture
) -> None:
    """TC-015-04, AC-015-07, BR-015-02, ERR-015-03, INV-015-02, §27."""
    broken_post = SimpleNamespace(
        id=42,
        user_id=15,
        telegram_file_id="AgAC_valid",
        publication_type=PublicationType.TELEGRAM,
        target_id=bad_target,
        caption=None,
        status=PostStatus.PROCESSING,
    )
    client = FakeChannelClient()
    publisher = TelegramPublisher(client)

    with caplog.at_level("ERROR", logger="postmaster.publishers.telegram"):
        result = await publisher.publish(broken_post)

    assert result.success is False
    assert result.error_code == TelegramErrorCode.TARGET_REQUIRED.value
    assert client.photos == []
    assert "Telegram publication failed post_id=42 error_code=TARGET_REQUIRED" in caplog.text


@pytest.mark.parametrize("bad_file_id", [None, "", "   "])
async def test_tc_015_05_missing_file_returns_file_id_missing_without_calling_api(
    bad_file_id: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    """TC-015-05, AC-015-08, BR-015-03, ERR-015-04, INV-015-03, §28."""
    broken_post = SimpleNamespace(
        id=42,
        user_id=15,
        telegram_file_id=bad_file_id,
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        caption=None,
        status=PostStatus.PROCESSING,
    )
    client = FakeChannelClient()
    publisher = TelegramPublisher(client)

    with caplog.at_level("ERROR", logger="postmaster.publishers.telegram"):
        result = await publisher.publish(broken_post)

    assert result.success is False
    assert result.error_code == TelegramErrorCode.FILE_ID_MISSING.value
    assert client.photos == []
    assert "Telegram publication failed post_id=42 error_code=FILE_ID_MISSING" in caplog.text


async def test_tc_015_06_wrong_type_does_not_call_telegram(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TC-015-06, AC-015-09, BR-015-01, ERR-015-01, INV-015-01, §19."""
    post = _sample_telegram_post(
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    client = FakeChannelClient()
    publisher = TelegramPublisher(client)

    with caplog.at_level("ERROR", logger="postmaster.publishers.telegram"):
        result = await publisher.publish(post)

    assert result.success is False
    assert result.error_code == TelegramErrorCode.INVALID_PUBLICATION_TYPE.value
    assert client.photos == []
    assert "error_code=INVALID_PUBLICATION_TYPE" in caplog.text


@pytest.mark.parametrize(
    "wrong_status",
    [
        PostStatus.PENDING,
        PostStatus.PUBLISHED,
        PostStatus.FAILED,
        PostStatus.CANCELLED,
    ],
)
async def test_tc_015_07_wrong_status_rejects_direct_publication(
    wrong_status: PostStatus, caplog: pytest.LogCaptureFixture
) -> None:
    """TC-015-07, AC-015-10, BR-015-07…08, ERR-015-02, INV-015-04, §20."""
    post = _sample_telegram_post(status=wrong_status)
    client = FakeChannelClient()
    publisher = TelegramPublisher(client)

    with caplog.at_level("ERROR", logger="postmaster.publishers.telegram"):
        result = await publisher.publish(post)

    assert result.success is False
    assert result.error_code == TelegramErrorCode.INVALID_POST_STATUS.value
    assert client.photos == []
    assert "error_code=INVALID_POST_STATUS" in caplog.text


async def test_unit_52_permission_denied_returns_telegram_permission_denied(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§52, AC-015-11, ERR-015-05, §29, §35: отсутствие прав бота в канале."""
    perm_exc = ApiTelegramException(
        "sendPhoto",
        None,
        {
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: bot is not a member of the channel chat",
        },
    )
    post = _sample_telegram_post()
    client = FakeChannelClient(photo_error=perm_exc)
    publisher = TelegramPublisher(client, secrets=("123456:SECRET_TOKEN",))

    with caplog.at_level("ERROR", logger="postmaster.publishers.telegram"):
        result = await publisher.publish(post)

    assert result.success is False
    assert result.error_code == TelegramErrorCode.TELEGRAM_PERMISSION_DENIED.value
    assert result.retryable is False
    assert "error_code=TELEGRAM_PERMISSION_DENIED" in caplog.text


async def test_unit_53_timeout_and_network_errors_are_marked_retryable() -> None:
    """§53, AC-015-13, ERR-015-08…10, §38: timeout, network и 503 помечаются retryable=True."""
    post = _sample_telegram_post()

    # 1) Timeout
    timeout_pub = TelegramPublisher(
        FakeChannelClient(photo_error=TimeoutError("Request timed out"))
    )
    res_timeout = await timeout_pub.publish(post)
    assert res_timeout.success is False
    assert res_timeout.error_code == TelegramErrorCode.TELEGRAM_API_TIMEOUT.value
    assert res_timeout.retryable is True

    # 2) Network error
    net_pub = TelegramPublisher(
        FakeChannelClient(photo_error=ConnectionError("Network is unreachable"))
    )
    res_net = await net_pub.publish(post)
    assert res_net.success is False
    assert res_net.error_code == TelegramErrorCode.NETWORK_ERROR.value
    assert res_net.retryable is True

    # 3) Telegram API 503 Unavailable + маскировка токена (§49)
    secret_token = "999888777:AAH_SUPER_SECRET"
    unavail_exc = ApiTelegramException(
        "sendPhoto",
        None,
        {
            "ok": False,
            "error_code": 503,
            "description": f"Service Unavailable for bot {secret_token}",
        },
    )
    unavail_pub = TelegramPublisher(
        FakeChannelClient(photo_error=unavail_exc),
        secrets=(secret_token,),
    )
    res_unavail = await unavail_pub.publish(post)
    assert res_unavail.success is False
    assert res_unavail.error_code == TelegramErrorCode.TELEGRAM_API_UNAVAILABLE.value
    assert res_unavail.retryable is True
    assert res_unavail.error_message is not None
    assert secret_token not in res_unavail.error_message


async def test_unit_54_invalid_file_id_and_target_unavailable_errors() -> None:
    """§54, AC-015-12, ERR-015-06, ERR-015-07, §30, §36, §37."""
    post = _sample_telegram_post()

    # 1) Invalid file_id
    file_exc = ApiTelegramException(
        "sendPhoto",
        None,
        {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: wrong file identifier/HTTP URL specified",
        },
    )
    file_pub = TelegramPublisher(FakeChannelClient(photo_error=file_exc))
    res_file = await file_pub.publish(post)
    assert res_file.success is False
    assert res_file.error_code == TelegramErrorCode.INVALID_FILE_ID.value
    assert res_file.retryable is False

    # 2) Target channel unavailable
    target_exc = ApiTelegramException(
        "sendPhoto",
        None,
        {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: chat not found",
        },
    )
    target_pub = TelegramPublisher(FakeChannelClient(photo_error=target_exc))
    res_target = await target_pub.publish(post)
    assert res_target.success is False
    assert res_target.error_code == TelegramErrorCode.TELEGRAM_TARGET_UNAVAILABLE.value
    assert res_target.retryable is False


# ---------------------------------------------------------------------------
# §55–58 Integration Tests (IT-015-01…04, PublicationService, ошибки, SPEC-013)
# ---------------------------------------------------------------------------


async def test_it_015_01_02_03_04_database_post_and_status_ownership(
    spec015_db,
) -> None:
    """§55 IT-015-01…04, AC-015-15, INV-015-08: проверка chat_id, file_id, caption и ошибок."""
    _, _, post_repo, user = spec015_db
    created = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_it_015",
        telegram_file_unique_id="AQAD_it_015",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        caption="Подпись для канала",
        now=CREATED_AT,
    )
    assert await post_repo.claim_post_for_processing(created.id, now=CLAIM_AT) is True
    processing_post = await post_repo.get_by_id(created.id)
    assert processing_post is not None and processing_post.status == PostStatus.PROCESSING

    # IT-015-01 & IT-015-02: успешная отправка фото с caption
    client = FakeChannelClient()
    publisher = TelegramPublisher(client)
    res_ok = await publisher.publish(processing_post)
    assert res_ok.success is True
    assert client.photos == [(CHANNEL_ID, "AgAC_it_015", "Подпись для канала")]

    # AC-015-15, INV-015-08: сам TelegramPublisher не меняет статус в БД на PUBLISHED
    still_processing = await post_repo.get_by_id(created.id)
    assert still_processing is not None and still_processing.status == PostStatus.PROCESSING

    # IT-015-03: ошибка отсутствия target_id
    res_no_target = await publisher.publish(
        SimpleNamespace(
            id=processing_post.id,
            telegram_file_id=processing_post.telegram_file_id,
            publication_type=PublicationType.TELEGRAM,
            target_id=None,
            caption=None,
            status=PostStatus.PROCESSING,
        )
    )
    assert res_no_target.success is False
    assert res_no_target.error_code == TelegramErrorCode.TARGET_REQUIRED.value

    # IT-015-04: ошибка Telegram API
    err_publisher = TelegramPublisher(FakeChannelClient(photo_error=RuntimeError("Chat not found")))
    res_api_err = await err_publisher.publish(processing_post)
    assert res_api_err.success is False
    assert res_api_err.error_code == TelegramErrorCode.TELEGRAM_TARGET_UNAVAILABLE.value


async def test_it_015_56_publication_service_claims_publishes_and_marks_published(
    spec015_db,
) -> None:
    """§56 Integration Test: PENDING → PROCESSING → TelegramPublisher → PUBLISHED."""
    _, _, post_repo, user = spec015_db
    post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_pub_015",
        telegram_file_unique_id="AQAD_pub_015",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        caption="Автопост в канал",
        now=CREATED_AT,
    )
    client = FakeChannelClient()
    service = PublicationService(
        post_repo,
        publishers={PublicationType.TELEGRAM: TelegramPublisher(client)},
        now_fn=lambda: PUBLISHED_AT,
    )

    assert await service.execute(post.id) is True
    assert client.photos == [(CHANNEL_ID, "AgAC_pub_015", "Автопост в канал")]

    published = await post_repo.get_by_id(post.id)
    assert published is not None
    assert published.status == PostStatus.PUBLISHED
    assert published.published_at == PUBLISHED_AT
    assert published.error_message is None


async def test_it_015_57_publication_service_handles_permission_and_timeout_errors(
    spec015_db,
) -> None:
    """§57 Integration Test с ошибкой: permission error → FAILED; timeout → PENDING retry."""
    _, _, post_repo, user = spec015_db

    # 1) Permission error → FAILED (INV-015-09)
    post_perm = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_perm_err",
        telegram_file_unique_id="AQAD_perm_err",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        now=CREATED_AT,
    )
    perm_service = PublicationService(
        post_repo,
        publishers={
            PublicationType.TELEGRAM: TelegramPublisher(
                FakeChannelClient(
                    photo_error=RuntimeError(
                        "Forbidden: not enough rights to send photos to the chat"
                    )
                )
            )
        },
        now_fn=lambda: PUBLISHED_AT,
    )
    assert await perm_service.execute(post_perm.id) is False
    failed = await post_repo.get_by_id(post_perm.id)
    assert failed is not None
    assert failed.status == PostStatus.FAILED
    assert failed.published_at is None
    assert failed.error_message is not None

    # 2) Timeout → возврат в PENDING для retry (§38)
    post_timeout = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_timeout_err",
        telegram_file_unique_id="AQAD_timeout_err",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        now=CREATED_AT,
    )
    timeout_service = PublicationService(
        post_repo,
        publishers={
            PublicationType.TELEGRAM: TelegramPublisher(
                FakeChannelClient(photo_error=TimeoutError("Telegram API timeout"))
            )
        },
        now_fn=lambda: PUBLISHED_AT,
    )
    assert await timeout_service.execute(post_timeout.id) is False
    requeued = await post_repo.get_by_id(post_timeout.id)
    assert requeued is not None
    assert requeued.status == PostStatus.PENDING
    assert requeued.error_message == "Telegram API timeout"


async def test_it_015_58_spec013_atomic_claim_prevents_duplicate_channel_post(
    spec015_db,
) -> None:
    """§58 Integration Test с SPEC-013: конкурентные запуски публикуют в канал один раз."""
    _, _, post_repo, user = spec015_db
    post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_once_only",
        telegram_file_unique_id="AQAD_once_only",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        now=CREATED_AT,
    )
    entered = asyncio.Event()
    proceed = asyncio.Event()
    client = FakeChannelClient(entered=entered, proceed=proceed)
    service = PublicationService(
        post_repo,
        publishers={PublicationType.TELEGRAM: TelegramPublisher(client)},
        now_fn=lambda: PUBLISHED_AT,
    )

    first_task = asyncio.create_task(service.execute(post.id))
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    second_result = await service.execute(post.id)
    proceed.set()
    first_result = await first_task

    assert (first_result, second_result) == (True, False)
    assert len(client.photos) == 1


# ---------------------------------------------------------------------------
# §59–63 E2E Scenarios (Application + FakeTelegram HTTP сервер)
# ---------------------------------------------------------------------------


async def test_e2e_59_automatic_channel_publication_from_dialog_to_published(
    fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    """§59 E2E-сценарий — автопубликация в канал через диалог, Scheduler и FakeTelegram."""
    settings = Settings(
        bot_token=TEST_TOKEN,
        database_url=f"sqlite+aiosqlite:///{db_path(tmp_path)}",
        log_level="INFO",
        post_mode="TELEGRAM",
        telegram_target_id=CHANNEL_ID,
    )
    app = Application.from_settings(settings)
    task = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(fake_telegram.get_updates_called.wait(), timeout=5.0)
        fake_telegram.add_text_message("/start", chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

        fake_telegram.add_photo_message("channel-e2e-photo", chat_id=42, user_id=42)
        await wait_for_state(app, 42, DialogState.WAITING_DATETIME)

        fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
        await wait_for_state(app, 42, DialogState.IDLE)

        with sqlite3.connect(db_path(tmp_path)) as conn:
            post_id, pub_type, target_id, status = conn.execute(
                "SELECT id, publication_type, target_id, status FROM posts"
            ).fetchone()
        assert (pub_type, target_id, status) == ("TELEGRAM", CHANNEL_ID, "PENDING")

        # Наступает запланированное время публикации.
        await app._scheduler._on_post_due(post_id)

        assert fake_telegram.sent_photos == [
            {
                "chat_id": CHANNEL_ID,
                "photo": "channel-e2e-photo",
                "caption": None,
            }
        ]
        with sqlite3.connect(db_path(tmp_path)) as conn:
            final_status, published_at, error_msg = conn.execute(
                "SELECT status, published_at, error_message FROM posts WHERE id=?",
                (post_id,),
            ).fetchone()
        assert final_status == "PUBLISHED"
        assert published_at is not None
        assert error_msg is None
    finally:
        await app.stop()
        await asyncio.wait_for(task, timeout=5.0)


async def test_e2e_60_publication_after_recovery_schedules_and_publishes_once(
    spec015_db, fake_telegram: FakeTelegram
) -> None:
    """§60 E2E-сценарий — публикация после Recovery: job публикует фото и переводит в PUBLISHED."""
    _, _, post_repo, user = spec015_db
    post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_recovery_photo",
        telegram_file_unique_id="AQAD_recovery_photo",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        caption="Пост после перезапуска",
        now=CREATED_AT,
    )
    bot = create_bot(fake_telegram.token)
    pub_service = PublicationService(
        post_repo,
        publishers={PublicationType.TELEGRAM: TelegramPublisher(bot)},
        now_fn=lambda: PUBLISHED_AT,
    )
    scheduler = SchedulerService(post_repo, pub_service)
    scheduler.start()
    try:
        # Имитируем SPEC-011 Recovery при старте: перечитываем PENDING-посты и регистрируем job.
        for pending in await post_repo.list_pending():
            scheduler.schedule_post(
                post_id=pending.id,
                run_at=pending.scheduled_at_utc,
            )

        # Наступает время выполнения задания.
        await scheduler._on_post_due(post.id)
    finally:
        scheduler.shutdown()

    assert fake_telegram.sent_photos == [
        {
            "chat_id": CHANNEL_ID,
            "photo": "AgAC_recovery_photo",
            "caption": "Пост после перезапуска",
        }
    ]
    updated = await post_repo.get_by_id(post.id)
    assert updated is not None
    assert updated.status == PostStatus.PUBLISHED
    assert updated.published_at == PUBLISHED_AT


async def test_e2e_61_missed_job_preserves_scheduled_at_and_sets_actual_published_at(
    spec015_db, fake_telegram: FakeTelegram
) -> None:
    """§61 E2E-сценарий — Missed Job: запланирован на 15:00, доставлен в 15:20."""
    _, _, post_repo, user = spec015_db
    missed_post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_missed_channel",
        telegram_file_unique_id="AQAD_missed_channel",
        scheduled_at_utc=SCHEDULED_AT,  # 15:00 UTC
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        caption="Запоздавший пост",
        now=CREATED_AT,
    )
    bot = create_bot(fake_telegram.token)
    pub_service = PublicationService(
        post_repo,
        publishers={PublicationType.TELEGRAM: TelegramPublisher(bot)},
        now_fn=lambda: PUBLISHED_AT,  # 15:20 UTC
    )

    assert await pub_service.execute(missed_post.id) is True

    assert fake_telegram.sent_photos == [
        {
            "chat_id": CHANNEL_ID,
            "photo": "AgAC_missed_channel",
            "caption": "Запоздавший пост",
        }
    ]
    published = await post_repo.get_by_id(missed_post.id)
    assert published is not None
    assert published.status == PostStatus.PUBLISHED
    assert published.scheduled_at_utc == SCHEDULED_AT
    assert published.published_at == PUBLISHED_AT


async def test_e2e_62_bot_lost_permissions_marks_failed_and_never_published(
    spec015_db, fake_telegram: FakeTelegram
) -> None:
    """§62 E2E-сценарий — бот потерял права администратора канала (INV-015-09)."""
    _, _, post_repo, user = spec015_db
    post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_lost_rights",
        telegram_file_unique_id="AQAD_lost_rights",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        now=CREATED_AT,
    )
    fake_telegram.fail_send_photo = (
        403,
        "Forbidden: bot was kicked from the channel chat",
    )
    bot = create_bot(fake_telegram.token)
    pub_service = PublicationService(
        post_repo,
        publishers={PublicationType.TELEGRAM: TelegramPublisher(bot)},
        now_fn=lambda: PUBLISHED_AT,
    )

    assert await pub_service.execute(post.id) is False
    failed = await post_repo.get_by_id(post.id)
    assert failed is not None
    assert failed.status == PostStatus.FAILED
    assert failed.published_at is None
    assert failed.error_message is not None
    assert "kicked from the channel" in failed.error_message


async def test_e2e_63_invalid_target_channel_marks_failed(
    spec015_db, fake_telegram: FakeTelegram
) -> None:
    """§63 E2E-сценарий — invalid target: недоступный канал переводит пост в FAILED."""
    _, _, post_repo, user = spec015_db
    post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_invalid_target",
        telegram_file_unique_id="AQAD_invalid_target",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1009999999999",
        now=CREATED_AT,
    )
    fake_telegram.fail_send_photo = (
        400,
        "Bad Request: chat not found",
    )
    bot = create_bot(fake_telegram.token)
    pub_service = PublicationService(
        post_repo,
        publishers={PublicationType.TELEGRAM: TelegramPublisher(bot)},
        now_fn=lambda: PUBLISHED_AT,
    )

    assert await pub_service.execute(post.id) is False
    failed = await post_repo.get_by_id(post.id)
    assert failed is not None
    assert failed.status == PostStatus.FAILED
    assert failed.published_at is None
    assert failed.error_message is not None
    assert "chat not found" in failed.error_message
