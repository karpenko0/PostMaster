"""Настройка proxy и сквозные запросы pyTelegramBotAPI через локальные туннели."""

import asyncio
import base64
import logging
import socket
import traceback
from pathlib import Path
from urllib.parse import quote

import pytest
from pydantic import ValidationError
from telebot import asyncio_helper

from fake_telegram import TEST_TOKEN, FakeTelegram
from postmaster.app import Application
from postmaster.bot.factory import close_bot_session, create_bot
from postmaster.bot.transport import PollingTransport
from postmaster.config import ConfigError, Settings, load_settings
from postmaster.publishers.reminder import _safe_error_message
from postmaster.utils.logging_config import RedactingFormatter


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_proxy_is_none(clean_env, value):
    assert Settings(bot_token=TEST_TOKEN, bot_proxy_url=value).bot_proxy_url is None


@pytest.mark.parametrize("value", [
    "socks5://127.0.0.1:1080", "socks5://u:p@127.0.0.1:1080",
    "http://user:pass@host:8080", "http://[::1]:8080", "http://host:8080/",
])
def test_valid_proxy(clean_env, value):
    assert Settings(bot_token=TEST_TOKEN, bot_proxy_url=value).bot_proxy_url == value


@pytest.mark.parametrize("value", [
    "garbage", "socks4://host:1080", "https://host:8080", "http://:8080",
    "http://host", "http://host:0", "http://host:65536", "http://host:abc",
    "http://user:supersecret@:8080", "http://user:supersecret@host:80/path",
    "http://user:supersecret@host:80?query", "http://user:supersecret@host:80#fragment",
    "http://user@host:80", "socks5://user:@host:80", "http://host name:80",
    "http://invalid!host:80", "socks5://" + "a" * 256 + ":secret@host:1080",
    "http://user:supersecret@host:80\n", "http://host:80/%ZZ",
])
def test_invalid_proxy_is_safe(clean_env, monkeypatch, value):
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("BOT_PROXY_URL", value)
    # Surrounding whitespace is trimmed, so put the control character inside the URL.
    if value.endswith("\n"):
        monkeypatch.setenv("BOT_PROXY_URL", value.replace("@host", "@ho\nst"))
    with pytest.raises(ConfigError) as exc:
        load_settings(None)
    assert "BOT_PROXY_URL" in str(exc.value)
    assert "supersecret" not in "".join(traceback.format_exception(exc.type, exc.value, exc.tb))
    with pytest.raises(ValidationError) as direct:
        Settings(bot_token=TEST_TOKEN, bot_proxy_url=value.replace("@host", "@ho\nst"))
    assert "supersecret" not in str(direct.value)


def test_secrets_hidden_and_redacted(clean_env):
    url = "socks5://privateuser:super%40secret@proxy-host:1080"
    settings = Settings(bot_token=TEST_TOKEN, bot_proxy_url=url)
    assert url not in repr(settings)
    assert url not in settings.model_dump_json()
    record = logging.LogRecord("test", logging.ERROR, "", 0, "%s", (url,), None)
    record.exc_info = None
    formatter = RedactingFormatter(secrets=settings.secret_values)
    assert "privateuser" not in formatter.format(record)
    assert "super%40secret" not in formatter.format(record)
    message = _safe_error_message(ValueError("super@secret privateuser"), None, None,
                                  settings.secret_values)
    assert "super@secret" not in message and "privateuser" not in message


class TunnelProxy:
    """Простейший HTTP CONNECT/SOCKS5 proxy с проверкой авторизации для теста."""

    def __init__(self, kind, auth):
        self.kind, self.auth = kind, auth
        self.connections = 0
        self.authentications = []
        self.server = None
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream = None
        try:
            if self.kind == "http":
                headers = (await reader.readuntil(b"\r\n\r\n")).decode()
                lines = headers.split("\r\n")
                method, address, _ = lines[0].split(" ")
                assert method == "CONNECT"
                self.authentications.append(next((x for x in lines if x.lower().startswith(
                    "proxy-authorization:")), ""))
                host, port = address.rsplit(":", 1)
                port = int(port)
            else:
                version, count = await reader.readexactly(2)
                methods = await reader.readexactly(count)
                assert version == 5
                method = 2 if self.auth else 0
                assert method in methods
                writer.write(bytes([5, method]))
                await writer.drain()
                if self.auth:
                    version, size = await reader.readexactly(2)
                    user = await reader.readexactly(size)
                    size = (await reader.readexactly(1))[0]
                    password = await reader.readexactly(size)
                    assert version == 1
                    self.authentications.append((user.decode(), password.decode()))
                    writer.write(b"\x01\x00")
                    await writer.drain()
                version, command, _, atyp = await reader.readexactly(4)
                assert version == 5 and command == 1
                if atyp == 1:
                    host = socket.inet_ntoa(await reader.readexactly(4))
                elif atyp == 3:
                    size = (await reader.readexactly(1))[0]
                    host = (await reader.readexactly(size)).decode()
                else:
                    raise AssertionError("unexpected address type")
                port = int.from_bytes(await reader.readexactly(2), "big")
            remote_reader, upstream = await asyncio.open_connection(host, port)
            self.connections += 1
            if self.kind == "http":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()

            async def copy(source, dest):
                while data := await source.read(65536):
                    dest.write(data)
                    await dest.drain()

            pipes = [asyncio.create_task(copy(reader, upstream)),
                     asyncio.create_task(copy(remote_reader, writer))]
            try:
                await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pipe in pipes:
                    pipe.cancel()
                await asyncio.gather(*pipes, return_exceptions=True)
        finally:
            if upstream:
                upstream.close()
                await upstream.wait_closed()
            writer.close()
            await writer.wait_closed()
            self.tasks.discard(task)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.mark.parametrize("kind", ["http", "socks5"])
@pytest.mark.parametrize("auth", [False, True])
async def test_bot_requests_through_proxy(kind, auth, fake_telegram: FakeTelegram, caplog):
    proxy = TunnelProxy(kind, auth)
    port = await proxy.start()
    credentials = f"{quote('proxy-user')}:{quote('proxy@secret', safe='')}@" if auth else ""
    url = f"{kind}://{credentials}127.0.0.1:{port}"
    bot = create_bot(TEST_TOKEN, url)
    received = asyncio.Event()

    @bot.message_handler(commands=["start"])
    async def on_start(message):
        await bot.send_message(message.chat.id, "hello")
        received.set()

    fake_telegram.add_text_message("/start")
    transport = PollingTransport(bot, timeout=1)
    task = None
    try:
        assert (await bot.get_me()).username == "postmaster_test_bot"
        task = asyncio.create_task(transport.run())
        await asyncio.wait_for(received.wait(), timeout=10)
        assert {"getMe", "getUpdates", "sendMessage"}.issubset(fake_telegram.methods)
        assert proxy.connections > 0
        if auth and kind == "socks5":
            assert ("proxy-user", "proxy@secret") in proxy.authentications
        if auth and kind == "http":
            token = base64.b64encode(b"proxy-user:proxy@secret").decode()
            assert any(token in header for header in proxy.authentications)
        assert "proxy@secret" not in caplog.text
        if credentials:
            assert credentials not in caplog.text
    finally:
        await transport.stop()
        if task:
            await asyncio.wait_for(task, 5)
        await close_bot_session()
        await proxy.stop()


async def test_recreation_and_direct_mode(fake_telegram: FakeTelegram):
    bot = create_bot(TEST_TOKEN, "socks5://127.0.0.1:1080")
    # No connection is made; switching back to direct mode is safe.
    bot = create_bot(TEST_TOKEN)
    assert (await bot.get_me()).username == "postmaster_test_bot"
    with pytest.raises(RuntimeError, match="HTTP-сессии"):
        create_bot(TEST_TOKEN, "http://localhost:8080")
    await close_bot_session()
    assert asyncio_helper.session_manager.session.closed
    bot = create_bot(TEST_TOKEN)
    assert (await bot.get_me()).username == "postmaster_test_bot"


async def test_application_wires_proxy(clean_env, tmp_path: Path):
    settings = Settings(bot_token=TEST_TOKEN, bot_proxy_url="socks5://127.0.0.1:1080",
                        database_url=f"sqlite+aiosqlite:///{tmp_path}/db.sqlite")
    app = Application.from_settings(settings)
    try:
        assert asyncio_helper.session_manager._proxy_url == settings.bot_proxy_url
    finally:
        await app.shutdown()


async def test_application_getme_polling_and_start_via_proxy(
    clean_env, tmp_path: Path, fake_telegram: FakeTelegram,
):
    proxy = TunnelProxy("socks5", True)
    port = await proxy.start()
    settings = Settings(
        bot_token=TEST_TOKEN,
        bot_proxy_url=f"socks5://proxy-user:proxy%40secret@127.0.0.1:{port}",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/db.sqlite",
        health_file=str(tmp_path / "health.json"),
    )
    app = Application.from_settings(settings)
    fake_telegram.add_text_message("/start")
    task = asyncio.create_task(app.run())
    try:
        async with asyncio.timeout(10):
            while not fake_telegram.sent_messages:
                await asyncio.sleep(0.01)
        assert {"getMe", "getUpdates", "sendMessage"}.issubset(fake_telegram.methods)
        assert ("proxy-user", "proxy@secret") in proxy.authentications
    finally:
        await app.stop()
        await asyncio.wait_for(task, 5)
        await proxy.stop()


def test_proxy_is_loaded_from_env_file(clean_env, tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"BOT_TOKEN={TEST_TOKEN}\nBOT_PROXY_URL=http://proxy:8080\n")
    assert load_settings(env).bot_proxy_url == "http://proxy:8080"


async def test_proxy_recreated_after_session_closed(fake_telegram: FakeTelegram):
    proxy = TunnelProxy("socks5", False)
    port = await proxy.start()
    bot = create_bot(TEST_TOKEN, f"socks5://127.0.0.1:{port}")
    try:
        await bot.get_me()
        await close_bot_session()
        await bot.get_me()
        assert proxy.connections == 2
    finally:
        await close_bot_session()
        await proxy.stop()


async def test_proxy_status_logs_do_not_contain_url(clean_env, caplog):
    caplog.set_level(logging.INFO, logger="postmaster.bot.factory")
    create_bot(TEST_TOKEN, "socks5://privateuser:supersecret@127.0.0.1:1080")
    create_bot(TEST_TOKEN)
    assert "Telegram proxy enabled" in caplog.text
    assert "Telegram proxy disabled" in caplog.text
    assert "privateuser" not in caplog.text and "supersecret" not in caplog.text
