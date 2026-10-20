"""TelegramPublisher: автоматическая публикация фотографии в Telegram-канал (SPEC-015).

Работает только с publication_type=TELEGRAM и status=PROCESSING, отправляет фотографию
в целевой канал post.target_id по сохранённому telegram_file_id без скачивания на диск,
передаёт caption при наличии и возвращает PublishResult в PublicationService.
"""

import inspect
import logging
from collections.abc import Iterable
from typing import Any

from telebot.async_telebot import AsyncTeleBot

from postmaster.domain.post import PostStatus, PublicationType
from postmaster.publishers.base import BotTelegramClient, Publisher, PublishResult
from postmaster.publishers.telegram_errors import (
    TelegramErrorCode,
    TelegramErrorMapper,
    TelegramOperation,
)

logger = logging.getLogger(__name__)


# Совместимость с прежним публичным импортом из postmaster.publishers.telegram.


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class TelegramPublisher(Publisher):
    """Публикует фотографию в целевой Telegram-канал по telegram_file_id (SPEC-015)."""

    def __init__(
        self,
        telegram_client: Any,
        *,
        error_mapper: TelegramErrorMapper | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        bot_token = getattr(telegram_client, "token", None)
        self._client = (
            BotTelegramClient(telegram_client)
            if isinstance(telegram_client, AsyncTeleBot)
            else telegram_client
        )
        mapper_secrets = tuple(secret for secret in secrets if secret)
        if isinstance(bot_token, str) and bot_token:
            mapper_secrets = (*mapper_secrets, bot_token)
        self._error_mapper = error_mapper or TelegramErrorMapper(secrets=mapper_secrets)

    async def publish(self, post: Any) -> PublishResult:
        """Выполняет публикацию фото в Telegram-канал, возвращая PublishResult (SPEC-015 §13–15)."""
        post_id = getattr(post, "id", None)
        target_id = getattr(post, "target_id", None)
        logger.info(
            "Telegram publication started post_id=%s target_id=%s",
            post_id,
            target_id,
        )

        if getattr(post, "publication_type", None) != PublicationType.TELEGRAM:
            return self._fail(
                post_id,
                TelegramErrorCode.INVALID_PUBLICATION_TYPE,
                "TelegramPublisher поддерживает только publication_type=TELEGRAM",
            )

        if getattr(post, "status", None) != PostStatus.PROCESSING:
            return self._fail(
                post_id,
                TelegramErrorCode.INVALID_POST_STATUS,
                "TelegramPublisher ожидает post.status=PROCESSING",
            )

        if target_id is None or isinstance(target_id, bool):
            return self._fail(
                post_id,
                TelegramErrorCode.TARGET_REQUIRED,
                "Отсутствует target_id целевого канала",
            )
        if isinstance(target_id, str):
            if not target_id.strip():
                return self._fail(
                    post_id,
                    TelegramErrorCode.TARGET_REQUIRED,
                    "Отсутствует target_id целевого канала",
                )
        elif not isinstance(target_id, int):
            return self._fail(
                post_id,
                TelegramErrorCode.TARGET_REQUIRED,
                "Отсутствует target_id целевого канала",
            )

        file_id = getattr(post, "telegram_file_id", None)
        if not isinstance(file_id, str) or not file_id.strip():
            return self._fail(
                post_id,
                TelegramErrorCode.FILE_ID_MISSING,
                "Отсутствует telegram_file_id",
            )

        caption = getattr(post, "caption", None)
        try:
            if caption is not None:
                await _maybe_await(self._client.send_photo(target_id, file_id, caption=caption))
            else:
                await _maybe_await(self._client.send_photo(target_id, file_id))
        except Exception as exc:
            error = self._error_mapper.map(
                exc,
                operation=TelegramOperation.PHOTO,
                file_id=file_id,
                caption=caption,
            )
            return self._fail(
                post_id,
                error.code,
                error.message,
                retryable=error.retryable,
            )

        logger.info("Telegram publication completed post_id=%s", post_id)
        return PublishResult(success=True, error_code=None, error_message=None)

    def _fail(
        self,
        post_id: Any,
        code: TelegramErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> PublishResult:
        logger.error(
            "Telegram publication failed post_id=%s error_code=%s",
            post_id,
            code.value,
        )
        return PublishResult(
            success=False,
            error_code=code.value,
            error_message=message,
            retryable=retryable,
        )
