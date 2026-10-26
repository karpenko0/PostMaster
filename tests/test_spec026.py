"""SPEC-026: Конфигурация приложения.

Settings централизует TELEGRAM_BOT_TOKEN, DATABASE_URL, DEFAULT_TIMEZONE
и валидирует конфигурацию на старте.

§8 Обязательные проверки:
- valid settings: корректная конфигурация загружается;
- missing token: без BOT_TOKEN — ConfigError, startup останавливается;
- DB URL: DATABASE_URL читается, пустое значение отклоняется;
- timezone default: DEFAULT_TIMEZONE по умолчанию Europe/Moscow.

§7 Дополнительно:
- переменная окружения имеет приоритет над .env;
- сервисы получают значения через init/DI, а не читают окружение сами.
"""

from pathlib import Path

import pytest

from fake_telegram import TEST_TOKEN
from postmaster.config import (
    DEFAULT_DATABASE_URL,
    DEFAULT_TIMEZONE,
    ConfigError,
    Settings,
    load_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src" / "postmaster"


def _write_env(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# === valid settings ========================================================


def test_valid_settings_are_loaded(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Корректная конфигурация из окружения загружается без ошибок."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:////tmp/pm.db")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Europe/Kaliningrad")

    settings = load_settings(env_file=None)

    assert isinstance(settings, Settings)
    assert settings.bot_token.get_secret_value() == TEST_TOKEN
    assert settings.database_url == "sqlite+aiosqlite:////tmp/pm.db"
    assert settings.log_level == "WARNING"
    assert settings.default_timezone == "Europe/Kaliningrad"


def test_settings_defaults_are_applied(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Опциональные значения имеют дефолты (SPEC-026 §1)."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)

    settings = load_settings(env_file=None)

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.log_level == "INFO"
    assert settings.post_mode == "REMINDER"
    assert settings.telegram_target_id is None


def test_env_file_provides_settings(clean_env: None, tmp_path: Path) -> None:
    """.env-файл — источник настроек для локальной разработки."""
    env_file = _write_env(
        tmp_path / ".env",
        f"BOT_TOKEN={TEST_TOKEN}\nDATABASE_URL=sqlite+aiosqlite:////tmp/x.db\n",
    )

    settings = load_settings(env_file=env_file)

    assert settings.bot_token.get_secret_value() == TEST_TOKEN
    assert settings.database_url == "sqlite+aiosqlite:////tmp/x.db"


# === missing token =========================================================


def test_missing_token_raises_config_error(clean_env: None) -> None:
    """Без BOT_TOKEN — ConfigError с именем поля (SPEC-026 §8)."""
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    message = str(info.value)
    assert "BOT_TOKEN" in message
    assert "некорректная конфигурация" in message


def test_empty_token_raises_config_error(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой BOT_TOKEN — ConfigError."""
    monkeypatch.setenv("BOT_TOKEN", "")

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "BOT_TOKEN" in str(info.value)


def test_missing_token_does_not_echo_any_value(clean_env: None) -> None:
    """Текст ошибки не содержит значений переменных (SPEC-023 §6)."""
    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    message = str(info.value)
    assert "BOT_TOKEN=" not in message
    assert TEST_TOKEN not in message


def test_config_error_lists_all_invalid_fields(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Все невалидные поля перечисляются в одном сообщении."""
    monkeypatch.setenv("BOT_TOKEN", "bad-token")
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Mars/Olympus")

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    message = str(info.value)
    assert "BOT_TOKEN" in message
    assert "LOG_LEVEL" in message
    assert "DEFAULT_TIMEZONE" in message


# === DB URL ================================================================


def test_database_url_is_read_from_environment(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DATABASE_URL читается из окружения (SPEC-026 §8)."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:////var/lib/pm.db")

    assert load_settings(env_file=None).database_url == "sqlite+aiosqlite:////var/lib/pm.db"


def test_empty_database_url_is_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой DATABASE_URL отклоняется."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "")

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "DATABASE_URL" in str(info.value)


def test_whitespace_database_url_is_rejected(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DATABASE_URL из одних пробелов отклоняется."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "   ")

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "DATABASE_URL" in str(info.value)


def test_database_url_with_credentials_is_accepted(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DATABASE_URL с credentials принимается (пароль не попадает в ошибки)."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db.local/pm")

    settings = load_settings(env_file=None)

    assert settings.database_url == "postgresql+asyncpg://user:pass@db.local/pm"


# === timezone default ======================================================


def test_default_timezone_is_europe_moscow(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дефолтный часовой пояс — Europe/Moscow (SPEC-026 §7, §8)."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)

    assert load_settings(env_file=None).default_timezone == "Europe/Moscow"
    assert DEFAULT_TIMEZONE == "Europe/Moscow"


def test_default_timezone_is_read_from_environment(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEFAULT_TIMEZONE перекрывает дефолт."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Asia/Tokyo")

    assert load_settings(env_file=None).default_timezone == "Asia/Tokyo"


@pytest.mark.parametrize("value", ["Mars/Olympus", "Europe", "../etc/passwd", "", "UTC+3"])
def test_invalid_default_timezone_is_rejected(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Неизвестный часовой пояс IANA отклоняется."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DEFAULT_TIMEZONE", value)

    with pytest.raises(ConfigError) as info:
        load_settings(env_file=None)

    assert "DEFAULT_TIMEZONE" in str(info.value)


# === Приоритет окружения (SPEC-026 §7) ====================================


def test_environment_takes_precedence_over_env_file(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Переменная окружения перекрывает значение из .env (SPEC-026 §7)."""
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Asia/Tokyo")
    env_file = _write_env(
        tmp_path / ".env",
        f"BOT_TOKEN={TEST_TOKEN}\nLOG_LEVEL=DEBUG\nDEFAULT_TIMEZONE=Europe/Moscow\n",
    )

    settings = load_settings(env_file=env_file)

    assert settings.log_level == "ERROR"
    assert settings.default_timezone == "Asia/Tokyo"


def test_env_file_does_not_override_existing_environment(
    clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Значение из .env не затирает уже заданную переменную окружения."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    env_file = _write_env(tmp_path / ".env", "BOT_TOKEN=999:another-token-value-here\n")

    settings = load_settings(env_file=env_file)

    assert settings.bot_token.get_secret_value() == TEST_TOKEN


# === Валидация startup (SPEC-026 §1) =====================================


def test_application_from_settings_uses_validated_config(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Application.from_settings принимает валидный Settings (валидация на старте)."""
    from postmaster.app import Application

    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/pm.db")
    settings = load_settings(env_file=None)

    app = Application.from_settings(settings)

    assert app.bot is not None


def test_application_startup_fails_on_invalid_config(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Некорректная конфигурация не создаёт приложение (валидация до старта)."""
    from postmaster.app import Application

    monkeypatch.setenv("BOT_TOKEN", "bad-token")

    with pytest.raises(ConfigError):
        load_settings(env_file=None)

    # Приложение собирается только из уже валидных Settings.
    assert hasattr(Application, "from_settings")


def test_main_exits_with_code_2_on_invalid_config(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """При старте с некорректной конфигурацией процесс завершается кодом 2."""
    import sys

    monkeypatch.setenv("BOT_TOKEN", "bad-token")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/pm.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["postmaster"])

    from postmaster.app import main

    with pytest.raises(SystemExit) as info:
        main()

    assert info.value.code == 2
    assert "BOT_TOKEN" in capsys.readouterr().err


# === Централизация Settings (SPEC-026 §1, §4) =============================


def test_only_config_module_reads_environment() -> None:
    """Только config.py читает переменные окружения (SPEC-026 §1, §4).

    Контрактная защита: сервисы и слои получают значения через DI, а не
    обращаются к os.environ напрямую.
    """
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        if path.name == "config.py":
            continue
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "os.environ" in line or "os.getenv" in line:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}")

    assert offenders == [], "Переменные окружения читаются вне config.py: " + ", ".join(offenders)


class _StubUserRepository:
    """Репозиторий без записей: пользователь не найден."""

    async def find_by_telegram_id(self, telegram_user_id: int) -> None:
        return None


async def test_services_receive_settings_via_constructor(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сервисы получают настройки через init/DI (SPEC-026 §7)."""
    from postmaster.services.user_service import UserService

    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Asia/Tokyo")
    settings = load_settings(env_file=None)

    service = UserService(
        repository=_StubUserRepository(),
        default_timezone=settings.default_timezone,
    )

    assert await service.timezone_of(42) == "Asia/Tokyo"


async def test_user_service_uses_injected_default_timezone() -> None:
    """UserService хранит переданный пояс, а не читает его из окружения."""
    from postmaster.services.user_service import UserService

    service = UserService(
        repository=_StubUserRepository(),
        default_timezone="Europe/Kaliningrad",
    )

    assert await service.timezone_of(42) == "Europe/Kaliningrad"
