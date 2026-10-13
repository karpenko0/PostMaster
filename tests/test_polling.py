"""Long Polling через локальный fake Telegram API (AC-05, BR-05)."""

import asyncio
from typing import Any

from fake_telegram import TEST_TOKEN, FakeTelegram
from postmaster.bot.factory import create_bot
from postmaster.bot.transport import PollingTransport


async def test_update_reaches_registered_handler(fake_telegram: FakeTelegram) -> None:
    bot = create_bot(TEST_TOKEN)
    received: list[str] = []
    handled = asyncio.Event()

    @bot.message_handler(commands=["start"])
    async def on_start(message: Any) -> None:
        received.append(message.text)
        handled.set()

    fake_telegram.add_text_message("/start")
    transport = PollingTransport(bot, timeout=1)
    run_task = asyncio.create_task(transport.run())
    try:
        await asyncio.wait_for(handled.wait(), timeout=10)
    finally:
        await transport.stop()
    await asyncio.wait_for(run_task, timeout=5)

    assert received == ["/start"]
    assert "getMe" in fake_telegram.methods
    assert "getUpdates" in fake_telegram.methods


async def test_stop_ends_run_while_polling(fake_telegram: FakeTelegram) -> None:
    transport = PollingTransport(create_bot(TEST_TOKEN), timeout=1)
    run_task = asyncio.create_task(transport.run())
    await asyncio.wait_for(fake_telegram.get_updates_called.wait(), timeout=10)

    await transport.stop()

    await asyncio.wait_for(run_task, timeout=5)
    assert run_task.cancelled() is False


async def test_stop_can_be_called_twice(fake_telegram: FakeTelegram) -> None:
    transport = PollingTransport(create_bot(TEST_TOKEN), timeout=1)
    run_task = asyncio.create_task(transport.run())
    await asyncio.wait_for(fake_telegram.get_updates_called.wait(), timeout=10)

    await transport.stop()
    await transport.stop()

    await asyncio.wait_for(run_task, timeout=5)


async def test_stop_before_run_returns_without_contacting_telegram() -> None:
    # Сеть не нужна: run() должен вернуться сразу, не обращаясь к api.telegram.org.
    transport = PollingTransport(create_bot(TEST_TOKEN))

    await transport.stop()
    await asyncio.wait_for(transport.run(), timeout=1)
