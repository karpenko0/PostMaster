"""Файлы развёртывания: AC-06 (SPEC-001) и решение D7 плана SPEC-001.

Сборку образа эти тесты не запускают: для неё нужен Docker. Проверяются наличие файлов и
директивы, от которых зависят запуск контейнера, права пользователя и защита данных.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_and_compose_exist() -> None:
    assert (ROOT / "Dockerfile").is_file()
    assert (ROOT / "docker-compose.yml").is_file()


def test_dockerfile_runs_unprivileged_user_with_uid_1000() -> None:
    text = _text("Dockerfile")
    assert "FROM python:3.12" in text
    assert "--uid 1000" in text
    assert "USER postmaster" in text


def test_dockerfile_starts_application_with_python_m() -> None:
    assert 'CMD ["python", "-m", "postmaster"]' in _text("Dockerfile")


def test_compose_keeps_data_on_host_and_passes_env_file() -> None:
    text = _text("docker-compose.yml")
    assert "env_file: .env" in text
    assert "./data:/app/data" in text
    assert "init: true" in text
    assert "stop_grace_period" in text


def test_dockerignore_keeps_secrets_and_data_out_of_image() -> None:
    lines = {line.strip() for line in _text(".dockerignore").splitlines()}
    assert {".git", ".env", "data", "*.db"} <= lines
