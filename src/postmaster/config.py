"""Настройки приложения (SPEC-001, SPEC-002, SPEC-026).

Значения читаются из переменных окружения. Файл .env для локальной разработки
загружается через python-dotenv, но переменные окружения имеют приоритет.
Секреты не попадают ни в repr, ни в текст ошибок (SPEC-023).
"""

import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///data/postmaster.db"
DEFAULT_ENV_FILE = ".env"
DEFAULT_TIMEZONE = "UTC"

# Формат токена Telegram Bot API: "<числовой id бота>:<секретная часть>".
_TOKEN_PATTERN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ConfigError(RuntimeError):
    """Конфигурация некорректна. Текст ошибки не содержит значений переменных."""


class Settings(BaseSettings):
    """Параметры приложения. Имена переменных окружения совпадают с полями без учёта регистра."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    bot_token: SecretStr = Field(description="Токен бота от @BotFather (BOT_TOKEN).")
    database_url: str = Field(
        default=DEFAULT_DATABASE_URL,
        min_length=1,
        description="Строка подключения SQLAlchemy с асинхронным драйвером (DATABASE_URL).",
    )
    log_level: LogLevel = Field(default="INFO", description="Уровень логирования (LOG_LEVEL).")
    default_timezone: str = Field(
        default=DEFAULT_TIMEZONE,
        description="Часовой пояс по умолчанию, IANA, например Europe/Moscow (DEFAULT_TIMEZONE).",
    )

    @field_validator("bot_token")
    @classmethod
    def _check_token_format(cls, value: SecretStr) -> SecretStr:
        if not _TOKEN_PATTERN.fullmatch(value.get_secret_value()):
            raise ValueError("не задан или имеет неверный формат")
        return value

    @field_validator("default_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("неизвестный часовой пояс IANA") from None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


def load_settings(env_file: str | Path | None = DEFAULT_ENV_FILE) -> Settings:
    """Загружает настройки из .env (если файл существует) и переменных окружения.

    Значения из файла не переопределяют уже заданные переменные окружения.
    При ошибке выбрасывает ConfigError. Текст ошибки содержит только имена полей.
    """
    if env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file, override=False)
    try:
        return Settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']).upper() or 'CONFIG'}: "
            f"{error['msg'].removeprefix('Value error, ')}"
            for error in exc.errors(include_input=False)
        )
        raise ConfigError(f"некорректная конфигурация: {problems}") from None
