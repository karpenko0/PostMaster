"""Создание клиента Telegram Bot API на pyTelegramBotAPI (асинхронный вариант)."""

import logging

import aiohttp
from aiohttp_socks import ProxyConnector
from telebot import asyncio_helper
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_storage import StateMemoryStorage

logger = logging.getLogger(__name__)


class _ProxySessionManager(asyncio_helper.SessionManager):
    """Общая для pyTelegramBotAPI сессия; коннектор пересоздаётся вместе с ней."""

    def __init__(self, proxy_url: str) -> None:
        super().__init__()
        self._proxy_url = proxy_url

    async def create_session(self) -> aiohttp.ClientSession:
        connector = ProxyConnector.from_url(
            self._proxy_url, limit=asyncio_helper.REQUEST_LIMIT, ssl=self.ssl_context
        )
        self.session = aiohttp.ClientSession(connector=connector)
        return self.session


# pyTelegramBotAPI 4.37 использует один session_manager и proxy для всех ботов
# в процессе, а не отдельную сессию на экземпляр AsyncTeleBot.
_proxy_url: str | None = None


def create_bot(token: str, proxy_url: str | None = None) -> AsyncTeleBot:
    """Создаёт бота; URL предварительно проверяется в Settings.

    Одновременно запущенные боты должны использовать одну конфигурацию proxy.
    Изменять её при живой сессии нельзя: иначе запросы уже запущенного бота
    внезапно пойдут через другой маршрут, а старая сессия останется открытой.
    """
    global _proxy_url
    if proxy_url != _proxy_url:
        session = asyncio_helper.session_manager.session
        if session is not None and not session.closed:
            raise RuntimeError("Нельзя изменить Telegram proxy при открытой HTTP-сессии")
        asyncio_helper.session_manager = (
            _ProxySessionManager(proxy_url) if proxy_url else asyncio_helper.SessionManager()
        )
        _proxy_url = proxy_url
    # Все запросы и загрузки файлов проходят через коннектор, без второго proxy.
    asyncio_helper.proxy = None
    logger.info("Telegram proxy %s", "enabled" if proxy_url else "disabled")
    return AsyncTeleBot(token, state_storage=StateMemoryStorage())


async def close_bot_session() -> None:
    """Закрывает общую HTTP-сессию pyTelegramBotAPI, если она была создана."""
    session = asyncio_helper.session_manager.session
    if session is None or session.closed:
        return
    await session.close()
