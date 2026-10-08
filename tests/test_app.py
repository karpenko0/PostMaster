"""TC-02: жизненный цикл приложения с тестовой конфигурацией, без внешней сети (US-02, AC-03)."""

import asyncio
import types
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from telebot.asyncio_helper import ApiTelegramException

from fake_telegram import TEST_TOKEN
from postmaster.app import Application, StartupError, main
from postmaster.config import Settings
from postmaster.database.engine import create_database_engine
from postmaster.services.scheduler_service import SchedulerService


class FakeBot:
    """Подменяет AsyncTeleBot: get_me либо возвращает бота, либо выбрасывает заданную ошибку."""

    def __init__(self, error: BaseException | None = None) -> None:
        self._error = error
        self.get_me_calls = 0

    async def get_me(self) -> types.SimpleNamespace:
        self.get_me_calls += 1
        if self._error is not None:
            raise self._error
        return types.SimpleNamespace(username="postmaster_test_bot")


class FakeTransport:
    """Подменяет Long Polling: run() ждёт stop(), как настоящий опрос."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self._stop_requested = asyncio.Event()
        self.stop_calls = 0

    async def run(self) -> None:
        self.started.set()
        await self._stop_requested.wait()

    async def stop(self) -> None:
        self.stop_calls += 1
        self._stop_requested.set()


def _build(
    tmp_path: Path, bot: FakeBot | None = None, transport: FakeTransport | None = None
) -> tuple[Application, SchedulerService, AsyncEngine, FakeTransport]:
    scheduler = SchedulerService()
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/data/test.db")
    transport = transport or FakeTransport()
    app = Application(
        bot=bot or FakeBot(),
        engine=engine,
        scheduler=scheduler,
        transport=transport,
    )
    return app, scheduler, engine, transport


def _count_dispose_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Считает вызовы AsyncEngine.dispose. Подмена на уровне класса: экземпляр её не допускает."""
    calls: list[int] = []
    original = AsyncEngine.dispose

    async def counting_dispose(self: AsyncEngine, *args: Any, **kwargs: Any) -> None:
        calls.append(1)
        await original(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", counting_dispose)
    return calls


async def test_run_starts_and_stops_every_component(tmp_path: Path) -> None:
    bot = FakeBot()
    app, scheduler, _engine, transport = _build(tmp_path, bot=bot)

    run_task = asyncio.create_task(app.run())
    await asyncio.wait_for(transport.started.wait(), timeout=5)
    assert scheduler.running is True
    assert (tmp_path / "data" / "test.db").exists()

    await app.stop()
    await asyncio.wait_for(run_task, timeout=5)

    assert scheduler.running is False
    assert transport.stop_calls == 1
    assert bot.get_me_calls == 1


async def test_shutdown_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, scheduler, _engine, _transport = _build(tmp_path)
    dispose_calls = _count_dispose_calls(monkeypatch)

    await app.startup()
    await app.shutdown()
    await app.shutdown()

    assert scheduler.running is False
    assert dispose_calls == [1]


async def test_database_failure_stops_startup_and_releases_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory_as_database = tmp_path / "not-a-file"
    directory_as_database.mkdir()
    scheduler = SchedulerService()
    engine = create_database_engine(f"sqlite+aiosqlite:///{directory_as_database}")
    dispose_calls = _count_dispose_calls(monkeypatch)
    transport = FakeTransport()
    app = Application(bot=FakeBot(), engine=engine, scheduler=scheduler, transport=transport)

    with pytest.raises(StartupError, match="DATABASE_URL"):
        await app.run()

    assert scheduler.running is False
    assert transport.started.is_set() is False
    assert dispose_calls == [1]


async def test_rejected_token_stops_before_polling(tmp_path: Path) -> None:
    error = ApiTelegramException(
        "getMe", None, {"ok": False, "error_code": 401, "description": "Unauthorized"}
    )
    app, scheduler, _engine, transport = _build(tmp_path, bot=FakeBot(error=error))

    with pytest.raises(StartupError, match="BOT_TOKEN"):
        await app.run()

    assert transport.started.is_set() is False
    assert scheduler.running is False


async def test_network_error_text_with_token_is_not_propagated(tmp_path: Path) -> None:
    # Сетевые исключения содержат URL с токеном. Текст этого исключения не должен попасть наружу.
    error = RuntimeError(f"Cannot connect to https://api.telegram.org/bot{TEST_TOKEN}/getMe")
    app, _scheduler, _engine, transport = _build(tmp_path, bot=FakeBot(error=error))

    with pytest.raises(StartupError) as info:
        await app.run()

    assert TEST_TOKEN not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True
    assert transport.started.is_set() is False


async def test_stop_before_run_makes_run_return_at_once(tmp_path: Path) -> None:
    app, scheduler, _engine, transport = _build(tmp_path)

    await app.stop()
    await asyncio.wait_for(app.run(), timeout=5)

    assert transport.stop_calls == 1
    assert scheduler.running is False


async def test_from_settings_builds_application_without_network(tmp_path: Path) -> None:
    settings = Settings(
        bot_token=TEST_TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/nested/db.sqlite",
        log_level="INFO",
    )

    app = Application.from_settings(settings)

    assert (tmp_path / "nested").is_dir()
    await app.shutdown()


def test_main_exits_with_code_2_when_token_is_missing(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as info:
        main()

    assert info.value.code == 2
    assert "BOT_TOKEN" in capsys.readouterr().err
