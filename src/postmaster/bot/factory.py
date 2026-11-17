"""Создание клиента Telegram Bot API на pyTelegramBotAPI (асинхронный вариант).

Модель изоляции (PM06-R2-009): pyTelegramBotAPI 4.x использует **один**
``asyncio_helper.session_manager`` и один ``asyncio_helper.proxy`` на процесс.
Поэтому все боты процесса должны иметь одинаковую конфигурацию proxy. Смена
маршрута при живой HTTP-сессии запрещена: иначе запросы уже работающего бота
внезапно пойдут через другой proxy, а старая сессия останется открытой. После
закрытия сессии конфигурацию можно сменить — сессия создаётся заново.
"""

import logging
import re
from urllib.parse import urlsplit

import aiohttp
from aiohttp_socks import ProxyConnector
from telebot import asyncio_helper
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_storage import StateMemoryStorage

logger = logging.getLogger(__name__)


class ProxyConfigurationError(RuntimeError):
    """Некорректная конфигурация proxy. Текст не содержит URL и credentials."""


class _ProxySessionManager(asyncio_helper.SessionManager):
    """Общая для pyTelegramBotAPI сессия; коннектор пересоздаётся вместе с ней."""

    def __init__(self, proxy_url: str | None) -> None:
        super().__init__()
        self._proxy_url = proxy_url

    async def create_session(self) -> aiohttp.ClientSession:
        try:
            connector = ProxyConnector.from_url(
                self._proxy_url, limit=asyncio_helper.REQUEST_LIMIT, ssl=self.ssl_context
            )
        except Exception:
            # Исключение aiohttp_socks может содержать URL с credentials — не даём
            # ему уйти в логи/сообщение об ошибке.
            raise ProxyConfigurationError("Telegram proxy сконфигурирован некорректно") from None
        self.session = aiohttp.ClientSession(connector=connector)
        return self.session


class _DirectSessionManager(asyncio_helper.SessionManager):
    """Прямое подключение без proxy (атрибут создан для единообразия диагностики)."""

    _proxy_url: str | None = None


# Конфигурация proxy и session_manager глобальны для процесса.
_proxy_url: str | None = None


def _validate_proxy_url(proxy_url: str) -> None:
    """Проверяет URL без создания сетевых объектов и без вывода значения в ошибку."""
    try:
        parsed = urlsplit(proxy_url)
        if (
            parsed.scheme not in {"http", "socks5"}
            or parsed.hostname is None
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or "@" in parsed.path
            or re.search(r"%(?![0-9a-fA-F]{2})", proxy_url)
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ProxyConfigurationError("Telegram proxy сконфигурирован некорректно") from None


def create_bot(token: str, proxy_url: str | None = None) -> AsyncTeleBot:
    """Создаёт бота; URL предварительно проверяется в Settings.

    Одновременно запущенные боты должны использовать одну конфигурацию proxy.
    Изменять её при живой сессии нельзя (см. docstring модуля).
    """
    global _proxy_url
    if proxy_url != _proxy_url:
        session = asyncio_helper.session_manager.session
        if session is not None and not session.closed:
            raise RuntimeError("Нельзя изменить Telegram proxy при открытой HTTP-сессии")
        if proxy_url:
            _validate_proxy_url(proxy_url)
            asyncio_helper.session_manager = _ProxySessionManager(proxy_url)
        else:
            asyncio_helper.session_manager = _DirectSessionManager()
        _proxy_url = proxy_url
    elif proxy_url:
        _validate_proxy_url(proxy_url)
    # Все запросы и загрузки файлов идут через коннектор, без второго proxy.
    asyncio_helper.proxy = None
    logger.info("Telegram proxy %s", "enabled" if proxy_url else "disabled")
    return AsyncTeleBot(token, state_storage=StateMemoryStorage())


async def close_bot_session() -> None:
    """Закрывает общую HTTP-сессию pyTelegramBotAPI, если она была создана."""
    session = asyncio_helper.session_manager.session
    if session is None or session.closed:
        return
    await session.close()
