"""DialogService: шаги сценария, черновик и исходы сообщений (SPEC-003: AC-02…AC-04, TC-01…TC-03).

Хранилище в памяти повторяет правило StateMemoryStorage: данные нельзя записать без состояния
(решение D3). Поэтому ошибка порядка записи проявится в тестах, а не только в Telegram.
Фото приходит как PhotoData, в черновике лежат метаданные из §16 SPEC-004 (IT-004-02, IT-004-03).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from harness import future_date_text
from postmaster.domain.dialog import DialogState
from postmaster.domain.photo import PhotoData
from postmaster.domain.post import Post
from postmaster.domain.schedule import DATETIME_FORMAT
from postmaster.services.dialog_service import DialogOutcomeKind, DialogService
from postmaster.services.post_service import PostService

USER = 42
CHAT = 42
IDLE = DialogState.IDLE
WAITING_PHOTO = DialogState.WAITING_PHOTO
WAITING_DATETIME = DialogState.WAITING_DATETIME


def photo(
    file_id: str,
    *,
    unique: str | None = None,
    file_size: int | None = 150000,
    width: int = 1280,
    height: int = 960,
) -> PhotoData:
    """PhotoData для тестов. Значения по умолчанию совпадают с большой фотографией fake Telegram."""
    return PhotoData(
        file_id=file_id,
        file_unique_id=unique if unique is not None else f"{file_id}-uid",
        file_size=file_size,
        width=width,
        height=height,
    )


def draft_of(
    file_id: str,
    *,
    unique: str | None = None,
    file_size: str | None = "150000",
    width: str = "1280",
    height: str = "960",
) -> dict[str, str]:
    """Ожидаемый черновик: ключи §16 SPEC-004. Задан вручную, чтобы ловить переименования."""
    result = {
        "telegram_file_id": file_id,
        "telegram_file_unique_id": unique if unique is not None else f"{file_id}-uid",
        "width": width,
        "height": height,
    }
    if file_size is not None:
        result["file_size"] = file_size
    return result


class InMemoryDialogStore:
    """Порт DialogStore в памяти. Записи данных без состояния отклоняются, как в telebot."""

    def __init__(self) -> None:
        self.states: dict[tuple[int, int], str] = {}
        self.drafts: dict[tuple[int, int], dict[str, str]] = {}

    async def get_state(self, user_id: int, chat_id: int) -> str | None:
        return self.states.get((user_id, chat_id))

    async def set_state(self, user_id: int, chat_id: int, state: str) -> None:
        self.states[user_id, chat_id] = state

    async def get_draft(self, user_id: int, chat_id: int) -> dict[str, str]:
        return dict(self.drafts.get((user_id, chat_id), {}))

    async def set_draft(self, user_id: int, chat_id: int, draft: dict[str, str]) -> None:
        if (user_id, chat_id) not in self.states:
            raise RuntimeError("данные нельзя записать без состояния")
        self.drafts[user_id, chat_id] = dict(draft)


class YieldingDialogStore(InMemoryDialogStore):
    """Уступает управление event loop перед чтением и перед записью состояния.

    Без уступки оба сообщения из теста выполнялись бы подряд, и гонка не возникала бы даже без
    блокировки в DialogService. С уступкой без блокировки оба фото прочитали бы WAITING_PHOTO.
    """

    async def get_state(self, user_id: int, chat_id: int) -> str | None:
        await asyncio.sleep(0)
        return await super().get_state(user_id, chat_id)

    async def set_state(self, user_id: int, chat_id: int, state: str) -> None:
        await asyncio.sleep(0)
        await super().set_state(user_id, chat_id, state)


class FakeUsers:
    """Возвращает один и тот же часовой пояс для любого пользователя."""

    def __init__(self, zone: str = "UTC") -> None:
        self.zone = zone

    async def timezone_of(self, telegram_user_id: int) -> str:
        return self.zone


class RecordingPostService(PostService):
    """Запоминает созданные публикации. Может имитировать сбой записи."""

    def __init__(self) -> None:
        self.created: list[Post] = []
        self.fail_with: Exception | None = None

    async def create_post(self, *, owner_telegram_id, photo_file_id, scheduled_at) -> Post:
        if self.fail_with is not None:
            raise self.fail_with
        post = await super().create_post(
            owner_telegram_id=owner_telegram_id,
            photo_file_id=photo_file_id,
            scheduled_at=scheduled_at,
        )
        self.created.append(post)
        return post


@pytest.fixture
def store() -> InMemoryDialogStore:
    return InMemoryDialogStore()


@pytest.fixture
def posts() -> RecordingPostService:
    return RecordingPostService()


@pytest.fixture
def service(store: InMemoryDialogStore, posts: RecordingPostService) -> DialogService:
    return DialogService(store, user_service=FakeUsers(), post_service=posts)  # type: ignore[arg-type]


async def test_start_sets_waiting_photo_and_empty_draft(
    service: DialogService, store: InMemoryDialogStore
) -> None:
    # BR-01, TC-01: /start переводит в WAITING_PHOTO.
    outcome = await service.start(USER, CHAT)

    assert outcome.kind == DialogOutcomeKind.STARTED
    assert store.states[USER, CHAT] == WAITING_PHOTO
    assert store.drafts[USER, CHAT] == {}


@pytest.mark.parametrize("state", [IDLE, WAITING_PHOTO, WAITING_DATETIME])
async def test_start_from_any_state_begins_new_scenario(
    service: DialogService, store: InMemoryDialogStore, state: DialogState
) -> None:
    # BR-01: /start из любого шага начинает новый сценарий и очищает прежний черновик.
    store.states[USER, CHAT] = state
    store.drafts[USER, CHAT] = {"telegram_file_id": "old-photo"}

    outcome = await service.start(USER, CHAT)

    assert outcome.state == WAITING_PHOTO
    assert store.drafts[USER, CHAT] == {}


async def test_photo_moves_to_waiting_datetime_and_keeps_file(
    service: DialogService, store: InMemoryDialogStore
) -> None:
    # BR-02, TC-01.
    await service.start(USER, CHAT)

    outcome = await service.receive_photo(USER, CHAT, photo("photo-1"))

    assert outcome.kind == DialogOutcomeKind.PHOTO_ACCEPTED
    assert outcome.state == WAITING_DATETIME
    assert store.states[USER, CHAT] == WAITING_DATETIME
    assert store.drafts[USER, CHAT] == draft_of("photo-1")


async def test_photo_draft_carries_all_metadata(
    service: DialogService, store: InMemoryDialogStore
) -> None:
    # TC-004-03, IT-004-02: в FSM сохранены file_id, file_unique_id, file_size, width, height (§16).
    await service.start(USER, CHAT)

    await service.receive_photo(
        USER,
        CHAT,
        photo("photo-1", unique="uid-77", file_size=154923, width=1280, height=1280),
    )

    assert store.drafts[USER, CHAT] == draft_of(
        "photo-1", unique="uid-77", file_size="154923", width="1280", height="1280"
    )


async def test_photo_without_file_size_omits_key(
    service: DialogService, store: InMemoryDialogStore
) -> None:
    # §11: file_size не обязателен, в черновик ключ не пишется.
    await service.start(USER, CHAT)

    await service.receive_photo(USER, CHAT, photo("photo-1", file_size=None))

    assert store.drafts[USER, CHAT] == draft_of("photo-1", file_size=None)


async def test_new_photo_on_date_step_replaces_previous_and_asks_again(
    service: DialogService, store: InMemoryDialogStore, posts: RecordingPostService
) -> None:
    # BR-04, AC-04, TC-03: замена фото, состояние то же, дата запрашивается снова.
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, photo("photo-1"))

    outcome = await service.receive_photo(USER, CHAT, photo("photo-2"))

    assert outcome.kind == DialogOutcomeKind.PHOTO_REPLACED
    assert outcome.state == WAITING_DATETIME
    assert store.drafts[USER, CHAT] == draft_of("photo-2")

    await service.receive_text(USER, CHAT, future_date_text())
    assert [post.photo_file_id for post in posts.created] == ["photo-2"]


async def test_replaced_photo_rewrites_all_metadata(
    service: DialogService, store: InMemoryDialogStore
) -> None:
    # IT-004-03: данные предыдущего фото полностью заменяются новыми (§27).
    await service.start(USER, CHAT)
    await service.receive_photo(
        USER, CHAT, photo("photo-a", unique="uid-a", file_size=2500, width=90, height=90)
    )

    await service.receive_photo(
        USER, CHAT, photo("photo-b", unique="uid-b", file_size=150000, width=1280, height=960)
    )

    assert store.drafts[USER, CHAT] == draft_of("photo-b", unique="uid-b")
    assert "uid-a" not in store.drafts[USER, CHAT].values()


async def test_valid_date_creates_post_and_returns_to_idle(
    service: DialogService, store: InMemoryDialogStore, posts: RecordingPostService
) -> None:
    # BR-03, AC-03, TC-01.
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, photo("photo-1"))

    outcome = await service.receive_text(USER, CHAT, future_date_text(days=2))

    assert outcome.kind == DialogOutcomeKind.POST_CREATED
    assert outcome.state == IDLE
    assert store.states[USER, CHAT] == IDLE
    assert store.drafts[USER, CHAT] == {}
    (post,) = posts.created
    assert post.owner_telegram_id == USER
    assert post.photo_file_id == "photo-1"
    assert post.scheduled_at.utcoffset() == timedelta(0)
    assert post.scheduled_at > datetime.now(UTC)


async def test_outcome_shows_time_in_user_timezone_and_post_keeps_utc(
    store: InMemoryDialogStore, posts: RecordingPostService
) -> None:
    # BR-04 SPEC-002: пояс пользователя применяется к дате. Москва = UTC+3.
    moscow = ZoneInfo("Europe/Moscow")
    service = DialogService(
        store,
        user_service=FakeUsers("Europe/Moscow"),  # type: ignore[arg-type]
        post_service=posts,
    )
    local = (datetime.now(moscow) + timedelta(days=2)).replace(second=0, microsecond=0)
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, photo("photo-1"))

    outcome = await service.receive_text(USER, CHAT, local.strftime(DATETIME_FORMAT))

    assert outcome.kind == DialogOutcomeKind.POST_CREATED
    assert outcome.zone_name == "Europe/Moscow"
    assert outcome.scheduled_at is not None
    assert outcome.scheduled_at.utcoffset() == timedelta(hours=3)
    assert outcome.scheduled_at.replace(tzinfo=None) == local.replace(tzinfo=None)
    assert posts.created[0].scheduled_at == outcome.scheduled_at.astimezone(UTC)


async def test_invalid_date_keeps_waiting_datetime_and_draft(
    service: DialogService, store: InMemoryDialogStore, posts: RecordingPostService
) -> None:
    # D7, TC-02: неверная дата не меняет шаг и не трогает фото.
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, photo("photo-1"))

    outcome = await service.receive_text(USER, CHAT, "31.02.2026 10:00")

    assert outcome.kind == DialogOutcomeKind.DATETIME_INVALID
    assert store.states[USER, CHAT] == WAITING_DATETIME
    assert store.drafts[USER, CHAT] == draft_of("photo-1")
    assert posts.created == []


async def test_failed_post_creation_keeps_draft_for_retry(
    service: DialogService, store: InMemoryDialogStore, posts: RecordingPostService
) -> None:
    # §7 п. 4 SPEC-003: сбой не нарушает данные. Черновик остаётся, дату можно повторить.
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, photo("photo-1"))
    posts.fail_with = RuntimeError("сбой сохранения")

    with pytest.raises(RuntimeError, match="сбой сохранения"):
        await service.receive_text(USER, CHAT, future_date_text())

    assert store.states[USER, CHAT] == WAITING_DATETIME
    assert store.drafts[USER, CHAT] == draft_of("photo-1")

    posts.fail_with = None
    outcome = await service.receive_text(USER, CHAT, future_date_text())

    assert outcome.kind == DialogOutcomeKind.POST_CREATED
    assert [post.photo_file_id for post in posts.created] == ["photo-1"]


@pytest.mark.parametrize(
    ("reached", "message"),
    [
        ("IDLE", "text"),
        ("IDLE", "date"),
        ("IDLE", "photo"),
        ("IDLE", "other"),
        ("WAITING_PHOTO", "text"),
        ("WAITING_PHOTO", "date"),
        ("WAITING_PHOTO", "other"),
        ("WAITING_DATETIME", "other"),
    ],
)
async def test_invalid_message_keeps_state_and_draft(
    service: DialogService,
    store: InMemoryDialogStore,
    posts: RecordingPostService,
    reached: str,
    message: str,
) -> None:
    # TC-02, AC-02: недопустимое сообщение в каждом состоянии не меняет ни состояние, ни черновик.
    if reached in ("WAITING_PHOTO", "WAITING_DATETIME"):
        await service.start(USER, CHAT)
    if reached == "WAITING_DATETIME":
        await service.receive_photo(USER, CHAT, photo("photo-1"))
    state_before = store.states.get((USER, CHAT))
    draft_before = dict(store.drafts.get((USER, CHAT), {}))

    if message == "text":
        outcome = await service.receive_text(USER, CHAT, "привет")
    elif message == "date":
        outcome = await service.receive_text(USER, CHAT, future_date_text())
    elif message == "photo":
        outcome = await service.receive_photo(USER, CHAT, photo("photo-x"))
    else:
        outcome = await service.receive_other(USER, CHAT)

    assert outcome.kind == DialogOutcomeKind.INVALID_MESSAGE
    assert outcome.state == DialogState(reached)
    assert store.states.get((USER, CHAT)) == state_before
    assert store.drafts.get((USER, CHAT), {}) == draft_before
    assert posts.created == []


async def test_parallel_photos_are_handled_one_at_a_time(posts: RecordingPostService) -> None:
    # D11: без блокировки оба фото прочитали бы WAITING_PHOTO и оба получили бы «принято».
    # YieldingDialogStore уступает управление между чтением и записью, поэтому гонка возможна.
    store = YieldingDialogStore()
    service = DialogService(store, user_service=FakeUsers(), post_service=posts)  # type: ignore[arg-type]
    await service.start(USER, CHAT)

    first, second = await asyncio.gather(
        service.receive_photo(USER, CHAT, photo("photo-a")),
        service.receive_photo(USER, CHAT, photo("photo-b")),
    )

    assert {first.kind, second.kind} == {
        DialogOutcomeKind.PHOTO_ACCEPTED,
        DialogOutcomeKind.PHOTO_REPLACED,
    }
    assert store.states[USER, CHAT] == WAITING_DATETIME
    assert store.drafts[USER, CHAT]["telegram_file_id"] in {"photo-a", "photo-b"}


async def test_parallel_dates_create_one_post(posts: RecordingPostService) -> None:
    # D11: две корректные даты подряд не создают два поста. Вторая приходит уже в IDLE и получает
    # INVALID_MESSAGE. Без блокировки обе прочитали бы WAITING_DATETIME и создали бы два поста.
    store = YieldingDialogStore()
    service = DialogService(store, user_service=FakeUsers(), post_service=posts)  # type: ignore[arg-type]
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, photo("photo-1"))

    outcomes = await asyncio.gather(
        service.receive_text(USER, CHAT, future_date_text()),
        service.receive_text(USER, CHAT, future_date_text()),
    )

    assert sorted(outcome.kind for outcome in outcomes) == sorted(
        [DialogOutcomeKind.POST_CREATED, DialogOutcomeKind.INVALID_MESSAGE]
    )
    assert len(posts.created) == 1
    assert store.states[USER, CHAT] == IDLE


async def test_users_do_not_share_state(service: DialogService, store: InMemoryDialogStore) -> None:
    await service.start(1, 1)
    await service.start(2, 2)

    await service.receive_photo(1, 1, photo("photo-of-user-1"))

    assert store.states[1, 1] == WAITING_DATETIME
    assert store.states[2, 2] == WAITING_PHOTO
    assert store.drafts[2, 2] == {}
