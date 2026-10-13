"""Сквозной запуск приложения в отдельном процессе (TC-02, AC-03, AC-05; этап 7 плана).

Процесс запускается как ``python -m postmaster``. Telegram API подменяется локальным fake-сервером
через переменную RUNNER: он меняет адрес API до запуска модуля __main__ и не трогает код приложения.
"""

import asyncio
import os
import signal
import socket
import sys
from pathlib import Path

from fake_telegram import TEST_TOKEN, FakeTelegram

RUNNER = (
    "import runpy, sys\n"
    "import telebot.asyncio_helper as telebot_http\n"
    "telebot_http.API_URL = sys.argv[1]\n"
    "sys.argv = ['postmaster']\n"
    "runpy.run_module('postmaster', run_name='__main__', alter_sys=True)\n"
)
STARTED_MARKER = "Long Polling запущен"
TIMEOUT = 60


def _settings_env(tmp_path: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env.update(
        {
            "BOT_TOKEN": TEST_TOKEN,
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/data/postmaster.db",
            "LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


async def _start_app(api_url: str, env: dict[str, str], cwd: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        RUNNER,
        api_url,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def _read_until(process: asyncio.subprocess.Process, marker: str) -> str:
    """Читает вывод процесса, пока не встретится маркер или поток не закроется."""
    collected: list[str] = []
    assert process.stdout is not None

    async def reader() -> None:
        while True:
            line = await process.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace")
            collected.append(text)
            if marker in text:
                return

    await asyncio.wait_for(reader(), timeout=TIMEOUT)
    return "".join(collected)


async def _finish(process: asyncio.subprocess.Process) -> str:
    output, _ = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT)
    return output.decode("utf-8", errors="replace")


async def test_full_cycle_polls_and_exits_cleanly_on_sigterm(
    fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    fake_telegram.add_text_message("/start")
    process = await _start_app(fake_telegram.api_url, _settings_env(tmp_path), tmp_path)
    try:
        head = await _read_until(process, STARTED_MARKER)
        await asyncio.wait_for(fake_telegram.update_delivered.wait(), timeout=TIMEOUT)
        process.send_signal(signal.SIGTERM)
        tail = await _finish(process)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    output = head + tail
    assert process.returncode == 0, output
    assert "getMe" in fake_telegram.methods
    assert "getUpdates" in fake_telegram.methods
    assert "PostMaster остановлен" in output
    assert TEST_TOKEN not in output
    assert (tmp_path / "data" / "postmaster.db").exists()


async def test_rejected_token_exits_with_code_1_without_leaking_token(
    fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    fake_telegram.reject_token = True
    process = await _start_app(fake_telegram.api_url, _settings_env(tmp_path), tmp_path)

    output = await _finish(process)

    assert process.returncode == 1, output
    assert "Telegram отклонил BOT_TOKEN" in output
    assert TEST_TOKEN not in output


async def test_unreachable_telegram_exits_with_code_1_without_leaking_token(
    tmp_path: Path,
) -> None:
    # Свободный порт, на котором никто не слушает: соединение будет отклонено.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    api_url = f"http://127.0.0.1:{port}/bot{{0}}/{{1}}"
    process = await _start_app(api_url, _settings_env(tmp_path), tmp_path)

    output = await _finish(process)

    assert process.returncode == 1, output
    assert "Telegram API недоступен" in output
    assert TEST_TOKEN not in output


async def test_python_dash_m_without_token_exits_with_code_2(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    for name in ("BOT_TOKEN", "DATABASE_URL", "LOG_LEVEL"):
        env.pop(name, None)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "postmaster",
        cwd=tmp_path,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT)

    assert process.returncode == 2
    assert b"BOT_TOKEN" in stderr
    assert stdout == b""
