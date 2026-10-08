"""Конфигурация из переменных окружения и .env (SPEC-026, SPEC-023; AC-02)."""

from pathlib import Path

import pytest

from fake_telegram import TEST_TOKEN
from postmaster.config import DEFAULT_DATABASE_URL, ConfigError, load_settings


def _write_env(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_settings_are_read_from_environment(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:////tmp/pm.db")
    monkeypatch.setenv("LOG_LEVEL", " debug ")

    settings = load_settings(env_file=None)

    assert settings.bot_token.get_secret_value() == TEST_TOKEN
    assert settings.database_url == "sqlite+aiosqlite:////tmp/pm.db"
    assert settings.log_level == "DEBUG"


def test_defaults_apply_when_optional_values_are_absent(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)

    settings = load_settings(env_file=None)

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.log_level == "INFO"
    assert settings.default_timezone == "UTC"


def test_env_file_values_are_loaded(clean_env: None, tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / ".env", f"BOT_TOKEN={TEST_TOKEN}\nLOG_LEVEL=WARNING\n")

    settings = load_settings(env_file=env_file)

    assert settings.bot_token.get_secret_value() == TEST_TOKEN
    assert settings.log_level == "WARNING"


def test_default_env_file_is_read_from_working_directory(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path / ".env", f"BOT_TOKEN={TEST_TOKEN}\n")
    monkeypatch.chdir(tmp_path)

    assert load_settings().bot_token.get_secret_value() == TEST_TOKEN


def test_environment_takes_precedence_over_env_file(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    env_file = _write_env(tmp_path / ".env", f"BOT_TOKEN={TEST_TOKEN}\nLOG_LEVEL=DEBUG\n")

    assert load_settings(env_file=env_file).log_level == "ERROR"


def test_token_is_hidden_in_repr_str_and_json(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    settings = load_settings(env_file=None)

    assert TEST_TOKEN not in repr(settings)
    assert TEST_TOKEN not in str(settings)
    assert TEST_TOKEN not in settings.model_dump_json()


def test_missing_token_raises_config_error_naming_the_field(clean_env: None) -> None:
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "BOT_TOKEN" in str(info.value)


def test_invalid_token_error_does_not_echo_the_value(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_TOKEN", "not-a-token-value-123")

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "BOT_TOKEN" in str(info.value)
    assert "not-a-token-value-123" not in str(info.value)


def test_invalid_log_level_is_reported_without_value(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("LOG_LEVEL", "LOUD")

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "LOG_LEVEL" in str(info.value)
    assert "LOUD" not in str(info.value)


def test_empty_database_url_is_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "")

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "DATABASE_URL" in str(info.value)


def test_default_timezone_is_read_from_environment(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BR-04 SPEC-002: пояс по умолчанию задаётся переменной DEFAULT_TIMEZONE.
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Europe/Moscow")

    assert load_settings(env_file=None).default_timezone == "Europe/Moscow"


@pytest.mark.parametrize("value", ["Mars/Olympus", "Europe", "../etc/passwd", ""])
def test_unknown_default_timezone_is_rejected(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DEFAULT_TIMEZONE", value)

    with pytest.raises(ConfigError, match="DEFAULT_TIMEZONE"):
        load_settings(env_file=None)
