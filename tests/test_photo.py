"""Выбор фотографии и метаданные (SPEC-004: TC-004-01…TC-004-04, ERR-004-02, ERR-004-03).

PhotoData и select_largest_photo живут в domain, handle_photo — контракт §22. Ошибки обработки
логируются (§28), бинарные данные изображения не логируются (§29).
"""

import logging
from dataclasses import dataclass

import pytest

from postmaster.domain.photo import PhotoData, PhotoProcessingError, select_largest_photo
from postmaster.services.photo_service import PhotoService

USER = 123456789


@dataclass
class FakePhotoSize:
    """Структурно повторяет telebot.types.PhotoSize: те же поля и типы."""

    file_id: str
    file_unique_id: str
    width: int
    height: int
    file_size: int | None = None


def size(
    file_id: str,
    width: int,
    height: int,
    *,
    unique: str | None = None,
    file_size: int | None = None,
) -> FakePhotoSize:
    return FakePhotoSize(
        file_id=file_id,
        file_unique_id=unique if unique is not None else f"{file_id}-uid",
        width=width,
        height=height,
        file_size=file_size,
    )


def test_one_photo_size_returns_its_file_id() -> None:
    # TC-004-01.
    photo = select_largest_photo([size("photo1", 1280, 720)])

    assert photo == PhotoData(
        file_id="photo1",
        file_unique_id="photo1-uid",
        file_size=None,
        width=1280,
        height=720,
    )


@pytest.mark.parametrize(
    "sizes",
    [
        # TC-004-02 и AC-004-03: 90×90, 320×320, 1280×1280. Порядок не важен.
        [size("small", 90, 90), size("medium", 320, 320), size("large", 1280, 1280)],
        [size("large", 1280, 1280), size("medium", 320, 320), size("small", 90, 90)],
        [size("medium", 320, 320), size("large", 1280, 1280), size("small", 90, 90)],
    ],
)
def test_largest_photo_size_is_selected(sizes: list[FakePhotoSize]) -> None:
    # BR-004-04: выбирается максимальный доступный размер.
    photo = select_largest_photo(sizes)

    assert photo.file_id == "large"
    assert (photo.width, photo.height) == (1280, 1280)


def test_all_metadata_is_captured() -> None:
    # TC-004-03: file_id, file_unique_id, file_size, width, height.
    photo = select_largest_photo(
        [
            size("small", 90, 90, unique="uid-s", file_size=2500),
            size("large", 1280, 1280, unique="uid-l", file_size=150000),
        ]
    )

    assert photo == PhotoData(
        file_id="large",
        file_unique_id="uid-l",
        file_size=150000,
        width=1280,
        height=1280,
    )


def test_file_size_may_be_absent() -> None:
    # §11: file_size не обязателен.
    photo = select_largest_photo([size("photo1", 1280, 720)])

    assert photo.file_size is None


def test_empty_list_is_processing_error() -> None:
    # TC-004-04, ERR-004-02: ошибка обработки, состояние не изменяется (проверяется в сервисе).
    with pytest.raises(PhotoProcessingError):
        select_largest_photo([])


def test_selected_size_without_file_id_is_processing_error() -> None:
    # ERR-004-03: у выбранного объекта нет file_id — обработка неуспешна.
    with pytest.raises(PhotoProcessingError):
        select_largest_photo([size("", 1280, 1280)])


def test_missing_file_id_on_largest_is_not_replaced_by_smaller() -> None:
    # ERR-004-03: выбран максимальный размер, его file_id не заменяется данными меньшего.
    with pytest.raises(PhotoProcessingError):
        select_largest_photo([size("small", 90, 90), size("", 1280, 1280)])


def test_handle_photo_returns_photo_data() -> None:
    # §22: handle_photo(telegram_user_id, photo_sizes) -> PhotoData.
    photo = PhotoService().handle_photo(
        USER,
        [size("small", 90, 90), size("large", 1280, 1280, unique="uid-l", file_size=150000)],
    )

    assert photo == PhotoData(
        file_id="large",
        file_unique_id="uid-l",
        file_size=150000,
        width=1280,
        height=1280,
    )


def test_handle_photo_logs_success_without_binary_data(caplog: pytest.LogCaptureFixture) -> None:
    # §29: INFO Photo received user_id=… и INFO Photo selected width=… height=….
    with caplog.at_level(logging.INFO):
        PhotoService().handle_photo(USER, [size("large", 1280, 1280)])

    messages = [record.getMessage() for record in caplog.records]
    assert f"Photo received user_id={USER}" in messages
    assert "Photo selected width=1280 height=1280" in messages
    # Бинарное содержимое и file_id в логи не попадают (§29, §30).
    assert not any("large" in message for message in messages)


def test_handle_photo_logs_processing_error(caplog: pytest.LogCaptureFixture) -> None:
    # ERR-004-02: ошибка регистрируется в логах.
    with caplog.at_level(logging.INFO):
        with pytest.raises(PhotoProcessingError):
            PhotoService().handle_photo(USER, [])

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert f"user_id={USER}" in errors[0].getMessage()
