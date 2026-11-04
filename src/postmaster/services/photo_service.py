"""PhotoService: извлечение метаданных фотографии из Telegram Update (SPEC-004, SPEC-020).

Контракт из §22: handle_photo(telegram_user_id, photo_sizes, caption=None) -> PhotoData.
Сервис выбирает максимальный размер и логирует обработку (§29), но не импортирует telebot
(BR-04 SPEC-001): фото приходит уже разобранным, структура описана протоколом
domain.photo.PhotoSizeLike. Запись публикации и FSM ведёт DialogService — здесь только
метаданные (§21, BR-004-08).
"""

import logging
from collections.abc import Sequence

from postmaster.domain.photo import (
    PhotoData,
    PhotoProcessingError,
    PhotoSizeLike,
    select_largest_photo,
)

logger = logging.getLogger(__name__)


class PhotoService:
    """Приём фотографии: выбор размера и метаданные для дальнейшего сценария (§2, §22)."""

    def handle_photo(
        self,
        telegram_user_id: int,
        photo_sizes: Sequence[PhotoSizeLike],
        *,
        caption: str | None = None,
    ) -> PhotoData:
        """Возвращает метаданные максимального размера (BR-004-04).

        caption — необязательная подпись из Telegram (SPEC-020). Пробелы по краям
        отбрасываются, пустая строка трактуется как «нет подписи» и сохраняется как None.
        Текст подписи не изменяется иначе: Publisher передаёт его в Telegram как есть.

        Ошибка обработки (ERR-004-02, ERR-004-03) логируется (§28) и пробрасывается вызывающему:
        FSM при этом не меняется. Бинарные данные изображения не логируются (§29).
        """
        try:
            photo = select_largest_photo(photo_sizes)
        except PhotoProcessingError as error:
            logger.error("Photo rejected user_id=%s: %s", telegram_user_id, error)
            raise
        normalized_caption = caption if caption else None
        if normalized_caption is not None:
            normalized_caption = normalized_caption.strip() or None
        logger.info("Photo received user_id=%s", telegram_user_id)
        logger.info("Photo selected width=%s height=%s", photo.width, photo.height)
        return PhotoData(
            file_id=photo.file_id,
            file_unique_id=photo.file_unique_id,
            file_size=photo.file_size,
            width=photo.width,
            height=photo.height,
            caption=normalized_caption,
        )
