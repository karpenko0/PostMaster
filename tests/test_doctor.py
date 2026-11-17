"""Подкоманда doctor: объясняет, почему бот молчит, и снимает webhook (SPEC-029).

Жалоба «бот запущен, но не отвечает» не оставляет следов в логах: приложение
пишет startup и продолжает работать. doctor проверяет причины по порядку и
возвращает код 0/1, чтобы это можно было использовать в скриптах и CI.
"""

from pathlib import Path

import pytest

from fake_telegram import TEST_TOKEN, FakeTelegram
from postmaster.app import DOCTOR_OK, DOCTOR_PROBLEMS, _doctor_probe
from postmaster.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token=TEST_TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'data' / 'app.db'}",
        log_level="WARNING",
        health_file=str(tmp_path / "data" / "health.json"),
    )


async def test_doctor_reports_no_problems_for_working_setup(
    fake_telegram: FakeTelegram, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    code = await _doctor_probe(_settings(tmp_path))

    output = capsys.readouterr().out
    assert code == DOCTOR_OK
    assert "Токен верный" in output
    assert "Webhook не задан" in output
    assert "Проблем не найдено" in output
    # Токен не должен попадать в отчёт ни в каком виде.
    assert TEST_TOKEN not in output
    assert TEST_TOKEN.split(":")[1] not in output


async def test_doctor_identifies_webhook_as_the_reason(
    fake_telegram: FakeTelegram, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """Webhook — самая частая причина молчания: getUpdates всегда получает 409."""
    fake_telegram.webhook_url = "https://stale-bot.example.com/hook"

    code = await _doctor_probe(_settings(tmp_path))

    output = capsys.readouterr().out
    assert code == DOCTOR_PROBLEMS
    assert "задан webhook" in output
    assert "stale-bot.example.com" in output
    assert "doctor --delete-webhook" in output
    # 409 при заданном webhook — следствие, а не отдельная проблема.
    assert "Найдено проблем: 1" in output


async def test_doctor_can_delete_webhook(
    fake_telegram: FakeTelegram, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    fake_telegram.webhook_url = "https://stale-bot.example.com/hook"

    code = await _doctor_probe(_settings(tmp_path), delete_webhook=True)

    output = capsys.readouterr().out
    assert "Webhook удалён" in output
    assert fake_telegram.webhook_url == ""
    assert code == DOCTOR_OK


async def test_doctor_reports_busy_token(
    fake_telegram: FakeTelegram, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """409 без webhook означает второй процесс на том же токене."""
    fake_telegram.conflict_on_get_updates = True

    code = await _doctor_probe(_settings(tmp_path))

    output = capsys.readouterr().out
    assert code == DOCTOR_PROBLEMS
    assert "409 Conflict" in output
    assert "другим процессом" in output


async def test_doctor_reports_rejected_token(
    fake_telegram: FakeTelegram, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    fake_telegram.reject_token = True

    code = await _doctor_probe(_settings(tmp_path))

    output = capsys.readouterr().out
    assert code == DOCTOR_PROBLEMS
    assert "BOT_TOKEN отклонён" in output
    assert TEST_TOKEN not in output


async def test_doctor_reports_unreachable_database(
    fake_telegram: FakeTelegram, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    blocker = tmp_path / "data"
    blocker.mkdir()
    # Файл вместо каталога: SQLite не сможет открыть базу по этому пути.
    (blocker / "app.db").mkdir()

    code = await _doctor_probe(_settings(tmp_path))

    output = capsys.readouterr().out
    assert code == DOCTOR_PROBLEMS
    assert "База данных" in output
