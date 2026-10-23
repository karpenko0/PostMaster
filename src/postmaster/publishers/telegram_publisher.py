"""Публикация постов в Telegram-канал (SPEC-010).

Использует pyTelegramBotAPI через интерфейс TelegramSender, чтобы не зависеть от
конкретного клиента и упростить подмену в тестах. Caption передаётся только если
он задан; для MVP без текста отправляется только фотография.
"""

import logging
from typing import Protocol

from postmaster.domain.post import Post
from postmaster.publishers.base import Publisher

logger = logging.getLogger(__name__)


class TelegramSender(Protocol):
    """Минимальный контракт отправки в Telegram, который использует Publisher."""

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> None: ...


class TelegramChannelPublisher(Publisher):
    """Публикует пост в заданный Telegram-канал через TelegramSender."""

    def __init__(self, sender: TelegramSender) -> None:
        self._sender = sender

    async def publish(self, post: Post) -> None:
        if not post.target_id or not post.target_id.strip():
            raise ValueError("для TELEGRAM требуется target_id")
        await self._sender.send_photo(
            chat_id=post.target_id,
            photo_file_id=post.telegram_file_id,
            caption=post.caption,
        )
        logger.info("Post published via Telegram post_id=%s target=%s", post.id, post.target_id)
