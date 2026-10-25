"""SPEC-023: Безопасность и управление секретами.

Обязательные проверки §8:
- valid token: загрузка Settings с валидным BOT_TOKEN из env/.env;
- missing token: ConfigError, startup останавливается;
- empty token: ConfigError без утечки значения;
- log redaction: токен не попадает в логи и error_message;
- env override: переменная окружения перекрывает значение из .env.

Проверки §7 (приёмка):
- нет hardcoded секретов в src/;
- секреты не попадают в logs / Dockerfile / docker-compose;
- .env игнорируется Git, .env.example — без реальных секретов;
- .dockerignore исключает .env из образа;
- main() завершается с кодом 2 при отсутствии BOT_TOKEN.
"""

import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

from postmaster.config import (
    _TOKEN_PATTERN,
    _URL_CREDENTIALS_PATTERN,
    ConfigError,
    load_settings,
)
from postmaster.utils.logging_config import MASK, configure_logging

TEST_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src" / "postmaster"


# === valid token ============================================================


def test_valid_token_is_loaded_from_environment(clean_env, monkeypatch) -> None:
    """Валидный токен из env → Settings создан, токен доступен через get_secret_value."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)

    settings = load_settings(env_file=None)

    assert settings.bot_token.get_secret_value() == TEST_TOKEN


def test_valid_token_via_dotenv_file(clean_env, tmp_path) -> None:
    """Валидный токен из .env файла → Settings создан."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"BOT_TOKEN={TEST_TOKEN}\n", encoding="utf-8")

    settings = load_settings(env_file=env_file)

    assert settings.bot_token.get_secret_value() == TEST_TOKEN


# === missing token ==========================================================


def test_missing_token_raises_config_error(clean_env) -> None:
    """BOT_TOKEN не задан → ConfigError, упоминается имя поля."""
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "BOT_TOKEN" in str(info.value)


def test_missing_token_error_does_not_echo_value(clean_env) -> None:
    """В тексте ошибки нет значений переменных (SPEC-023 §6)."""
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "BOT_TOKEN=" not in str(info.value)
    assert TEST_TOKEN not in str(info.value)


# === empty token ============================================================


def test_empty_token_raises_config_error(clean_env, monkeypatch) -> None:
    """BOT_TOKEN="" → ConfigError без утечки значения."""
    monkeypatch.setenv("BOT_TOKEN", "")

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "BOT_TOKEN" in str(info.value)
    assert "BOT_TOKEN=" not in str(info.value)


def test_whitespace_token_raises_config_error(clean_env, monkeypatch) -> None:
    """BOT_TOKEN из пробелов → ConfigError (формат не совпадает с regex)."""
    monkeypatch.setenv("BOT_TOKEN", "   ")

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "BOT_TOKEN" in str(info.value)


def test_malformed_token_raises_config_error(clean_env, monkeypatch) -> None:
    """BOT_TOKEN без ':' и без цифрового id → ConfigError."""
    monkeypatch.setenv("BOT_TOKEN", "not-a-real-token")

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "BOT_TOKEN" in str(info.value)
    assert "not-a-real-token" not in str(info.value)


# === env override ==========================================================


def test_environment_overrides_env_file(clean_env, tmp_path, monkeypatch) -> None:
    """Переменная окружения имеет приоритет над .env файлом (SPEC-023 §4)."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"BOT_TOKEN={TEST_TOKEN}\nLOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")

    settings = load_settings(env_file=env_file)

    assert settings.bot_token.get_secret_value() == TEST_TOKEN
    assert settings.log_level == "ERROR"


def test_env_file_is_optional(clean_env, monkeypatch) -> None:
    """Отсутствие .env-файла не приводит к ошибке."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)

    settings = load_settings(env_file="/nonexistent/.env")

    assert settings.bot_token.get_secret_value() == TEST_TOKEN


# === log redaction =========================================================


def test_token_not_in_settings_repr(clean_env, monkeypatch) -> None:
    """repr/str Settings не содержит токен (SPEC-023 §6)."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    settings = load_settings(env_file=None)

    assert TEST_TOKEN not in repr(settings)
    assert TEST_TOKEN not in str(settings)


def test_token_not_in_settings_json(clean_env, monkeypatch) -> None:
    """model_dump_json() маскирует SecretStr."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    settings = load_settings(env_file=None)

    assert TEST_TOKEN not in settings.model_dump_json()


def test_token_redacted_in_log_output(clean_env, monkeypatch, capsys) -> None:
    """Полный путь: configure_logging с secret → токен не в stdout/stderr."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    settings = load_settings(env_file=None)
    configure_logging("INFO", secrets=settings.secret_values)

    logging.getLogger("postmaster.test").info("HTTP /bot%s/getMe", TEST_TOKEN)

    captured = capsys.readouterr()
    assert TEST_TOKEN not in captured.out + captured.err
    assert MASK in captured.out


def test_database_url_password_redacted(clean_env, monkeypatch, capsys) -> None:
    """Пароль из DATABASE_URL редактируется в логах (SPEC-023 §4)."""
    db_url = "postgresql+asyncpg://alice:Sup3rSecret@db.local/postmaster"
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = load_settings(env_file=None)
    configure_logging("INFO", secrets=settings.secret_values)

    logging.getLogger("postmaster.test").error("failed: %s", db_url)

    captured = capsys.readouterr()
    assert "Sup3rSecret" not in captured.out + captured.err
    assert "alice:Sup3rSecret@db.local" not in captured.out + captured.err


def test_secret_values_includes_token(clean_env, monkeypatch) -> None:
    """Settings.secret_values всегда содержит токен."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    settings = load_settings(env_file=None)

    assert TEST_TOKEN in settings.secret_values


def test_secret_values_includes_database_url_with_credentials(clean_env, monkeypatch) -> None:
    """Settings.secret_values содержит DATABASE_URL, если в нём есть user:password@."""
    db_url = "postgresql+asyncpg://u:p@host/db"
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = load_settings(env_file=None)

    assert db_url in settings.secret_values


def test_secret_values_excludes_plain_database_url(clean_env, monkeypatch) -> None:
    """Settings.secret_values не включает DATABASE_URL без credentials."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///data/postmaster.db")
    settings = load_settings(env_file=None)

    assert "sqlite+aiosqlite" not in settings.secret_values


def test_secret_values_are_unique(clean_env, monkeypatch) -> None:
    """Settings.secret_values возвращает уникальные значения (без дубликатов)."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    settings = load_settings(env_file=None)

    values = list(settings.secret_values)
    assert len(values) == len(set(values))


def test_url_credentials_pattern_detects_user_password() -> None:
    """Regex ловит user:password@host в URL."""
    assert _URL_CREDENTIALS_PATTERN.search("postgresql://u:p@h/d")
    assert _URL_CREDENTIALS_PATTERN.search("mysql://alice:S@host/db")
    assert not _URL_CREDENTIALS_PATTERN.search("sqlite+aiosqlite:///data/x.db")
    assert not _URL_CREDENTIALS_PATTERN.search("postgresql://host/db")


def test_token_pattern_matches_real_token() -> None:
    """Regex токена ловит Bot API токен и игнорирует мусор."""
    assert _TOKEN_PATTERN.fullmatch(TEST_TOKEN)
    assert not _TOKEN_PATTERN.fullmatch("not-a-token")
    assert not _TOKEN_PATTERN.fullmatch("")


# === no hardcoded secrets in src/ ==========================================


def test_no_hardcoded_tokens_in_source() -> None:
    """В src/ нет строк, похожих на токен Bot API."""
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Пропускаем сам regex-паттерн и комментарии, где токен упоминается
            # в документации/примерах в явном виде.
            stripped = line.strip()
            if stripped.startswith("#") or "TOKEN_PATTERN" in line:
                continue
            if _TOKEN_PATTERN.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}")

    assert offenders == [], "Найдены потенциальные токены в src/: " + ", ".join(offenders)


def test_no_hardcoded_tokens_in_dockerfile() -> None:
    """Dockerfile не содержит токенов."""
    dockerfile = PROJECT_ROOT / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")
    assert not _TOKEN_PATTERN.search(content)


def test_no_hardcoded_tokens_in_docker_compose() -> None:
    """docker-compose.yml не содержит токенов в values."""
    compose = PROJECT_ROOT / "docker-compose.yml"
    content = compose.read_text(encoding="utf-8")
    assert not _TOKEN_PATTERN.search(content)


# === .env / .env.example / .dockerignore ===================================


def test_gitignore_excludes_env_but_keeps_example() -> None:
    """Git игнорирует .env, но .env.example остаётся в репозитории."""
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(r"^\.env$|^\.env\*", gitignore, re.MULTILINE), (
        ".gitignore должен содержать .env"
    )
    assert "!.env.example" in gitignore, ".gitignore должен иметь исключение для .env.example"


def test_env_example_has_no_real_token() -> None:
    """В .env.example нет Bot Token — только подсказки."""
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert not _TOKEN_PATTERN.search(example)
    # BOT_TOKEN присутствует с пустым значением.
    assert re.search(r"^BOT_TOKEN=\s*$", example, re.MULTILINE)


def test_dockerignore_excludes_env_files() -> None:
    """..env и .env.* не попадают в Docker-контекст."""
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in dockerignore
    assert ".env.*" in dockerignore


def test_docker_compose_uses_env_file() -> None:
    """docker-compose читает секреты из env_file, а не из values."""
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # env_file: .env
    assert re.search(r"^\s*env_file:\s*\.env\s*$", compose, re.MULTILINE), (
        "docker-compose.yml должен использовать env_file: .env"
    )
    # В values не должно быть BOT_TOKEN.
    assert not re.search(r"^\s*BOT_TOKEN\s*:", compose, re.MULTILINE), (
        "BOT_TOKEN не должен быть в environment docker-compose"
    )


# === startup останавливается при missing token =============================


def test_main_exits_with_code_2_when_token_missing(
    clean_env, monkeypatch, tmp_path, capsys
) -> None:
    """main() без BOT_TOKEN → SystemExit с кодом 2 (SPEC-023 §7 + SPEC-026 §7).

    chdir в tmp_path, чтобы дефолтный ``.env`` из корня проекта не подгружался.
    """
    db_file = tmp_path / "postmaster.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["postmaster"])

    from postmaster.app import main

    with pytest.raises(SystemExit) as info:
        main()

    assert info.value.code == 2
    captured = capsys.readouterr()
    assert "BOT_TOKEN" in captured.err
    assert TEST_TOKEN not in captured.err


# === subprocess: main не утечёт токен в лог даже если есть в env ============


def test_subprocess_does_not_leak_token_to_stdout(clean_env, monkeypatch, tmp_path) -> None:
    """Запуск main() подпроцессом без BOT_TOKEN → сообщение в stderr, токена нет."""
    monkeypatch.setattr(sys, "argv", ["postmaster"])
    db_file = tmp_path / "postmaster.db"
    env = {
        "PATH": "/usr/bin:/bin",
        "SystemRoot": "C:\\Windows",
        "TEMP": str(tmp_path),
        "HOME": str(tmp_path),
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_file}",
    }

    result = subprocess.run(
        [sys.executable, "-m", "postmaster"],
        cwd=tmp_path,  # без .env в cwd
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert "BOT_TOKEN" in combined
    # Токен из тестового набора не должен оказаться в выводе.
    assert TEST_TOKEN not in combined
    assert "123456789:AAFake" not in combined
