"""Данные фотографии из Telegram Update (SPEC-004).

PhotoData — внутренний контракт (§23). Модуль не зависит от Telegram: структура PhotoSize
описана протоколом, которому структурно удовлетворяет объект telebot.types.PhotoSize (§9).
Выбор максимального размера обязателен (BR-004-04): выполняется по площади width × height
и не зависит от порядка элементов массива (AC-004-03).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class PhotoProcessingError(ValueError):
    """Фото не удалось обработать: массив пуст или у выбранного размера нет file_id (§28)."""


class PhotoSizeLike(Protocol):
    """Размер фотографии из Telegram Update (§9). Поля совпадают с telebot.types.PhotoSize."""

    file_id: str
    file_unique_id: str
    width: int
    height: int
    file_size: int | None


@dataclass(frozen=True, slots=True)
class PhotoData:
    """Метаданные выбранной фотографии (§23). Бинарные данные изображения не входят."""

    file_id: str
    file_unique_id: str
    file_size: int | None
    width: int
    height: int


def select_largest_photo(photo_sizes: Sequence[PhotoSizeLike]) -> PhotoData:
    """Выбирает максимальный размер (BR-004-04) и собирает метаданные (§11).

    Пустой список — ошибка ERR-004-02, отсутствие file_id у выбранного объекта — ERR-004-03.
    """
    if not photo_sizes:
        raise PhotoProcessingError("Telegram передал пустой список PhotoSize")
    largest = max(photo_sizes, key=lambda size: size.width * size.height)
    if not largest.file_id:
        raise PhotoProcessingError("у выбранного PhotoSize нет file_id")
    return PhotoData(
        file_id=largest.file_id,
        file_unique_id=largest.file_unique_id,
        file_size=largest.file_size,
        width=largest.width,
        height=largest.height,
    )
