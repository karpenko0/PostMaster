"""TelebotDialogStore на настоящем хранилище pyTelegramBotAPI (SPEC-003, решения D2 и D3).

Сеть не нужна: состояния и данные хранятся в памяти бота.
"""

import pytest

from fake_telegram import TEST_TOKEN
from postmaster.bot.dialog_store import TelebotDialogStore
from postmaster.bot.factory import create_bot


@pytest.fixture
def store() -> TelebotDialogStore:
    return TelebotDialogStore(create_bot(TEST_TOKEN))


async def test_state_is_absent_until_set(store: TelebotDialogStore) -> None:
    assert await store.get_state(1, 1) is None


async def test_state_is_kept_separately_for_each_chat(store: TelebotDialogStore) -> None:
    await store.set_state(1, 10, "WAITING_PHOTO")
    await store.set_state(1, 20, "WAITING_DATETIME")

    assert await store.get_state(1, 10) == "WAITING_PHOTO"
    assert await store.get_state(1, 20) == "WAITING_DATETIME"


async def test_draft_round_trip(store: TelebotDialogStore) -> None:
    await store.set_state(1, 1, "WAITING_DATETIME")

    await store.set_draft(1, 1, {"telegram_file_id": "photo-1"})

    assert await store.get_draft(1, 1) == {"telegram_file_id": "photo-1"}


async def test_draft_is_replaced_and_cleared(store: TelebotDialogStore) -> None:
    await store.set_state(1, 1, "WAITING_PHOTO")
    await store.set_draft(1, 1, {"telegram_file_id": "first"})

    await store.set_draft(1, 1, {"telegram_file_id": "second"})
    assert await store.get_draft(1, 1) == {"telegram_file_id": "second"}

    await store.set_draft(1, 1, {})
    assert await store.get_draft(1, 1) == {}


async def test_returned_draft_is_a_copy(store: TelebotDialogStore) -> None:
    # Хранилище отдаёт внутренний словарь. Изменение копии не должно менять хранимые данные.
    await store.set_state(1, 1, "WAITING_DATETIME")
    await store.set_draft(1, 1, {"telegram_file_id": "photo-1"})

    draft = await store.get_draft(1, 1)
    draft["telegram_file_id"] = "changed"

    assert await store.get_draft(1, 1) == {"telegram_file_id": "photo-1"}


async def test_draft_cannot_be_written_before_state(store: TelebotDialogStore) -> None:
    # Решение D3: данные без записи состояния хранилище отклоняет. Адаптер пишет состояние первым.
    with pytest.raises(RuntimeError):
        await store.set_draft(5, 5, {"telegram_file_id": "photo-1"})
