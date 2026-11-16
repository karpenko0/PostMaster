"""Настройки приложения (SPEC-001, SPEC-002, SPEC-023, SPEC-026).

Значения читаются из переменных окружения. Файл .env для локальной разработки
загружается через python-dotenv, но переменные окружения имеют приоритет.
Секреты не попадают ни в repr, ни в текст ошибок (SPEC-023 §6).
"""

import re
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pydantic import AliasChoices, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from postmaster.domain.telegram_target import parse_allowlist

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///data/postmaster.db"
DEFAULT_ENV_FILE = ".env"
DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_HEALTH_FILE = "data/health.json"
DEFAULT_HEALTH_MAX_AGE_SECONDS = 90
DEFAULT_BACKUP_PATH = "data/backup.db"

# Формат токена Telegram Bot API: "<числовой id бота>:<секретная часть>".
_TOKEN_PATTERN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")

# Поиск credentials в DATABASE_URL: "user:password@host". Пароль — секрет, который
# нельзя выводить в логи или error_message (SPEC-023 §6, SPEC-027 §7).
_URL_CREDENTIALS_PATTERN = re.compile(r"://[^/\s:@]+:[^/\s@]+@")

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ConfigError(RuntimeError):
    """Конфигурация некорректна. Текст ошибки не содержит значений переменных."""


class Settings(BaseSettings):
    """Параметры приложения. Имена переменных окружения совпадают с полями без учёта регистра."""

    model_config = SettingsConfigDict(
        extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    bot_token: SecretStr = Field(description="Токен бота от @BotFather (BOT_TOKEN).")
    bot_proxy_url: str | None = Field(
        default=None, repr=False, exclude=True,
        description="Proxy для Telegram API (BOT_PROXY_URL).",
    )
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
    default_post_mode: Literal["REMINDER", "TELEGRAM"] = Field(
        default="REMINDER",
        validation_alias=AliasChoices("DEFAULT_POST_MODE", "POST_MODE"),
        description="Режим по умолчанию для совместимости с POST_MODE.",
    )
    telegram_target_allowlist: str = ""
    telegram_target_id: str | None = Field(
        default=None,
        description="Канал для режима TELEGRAM (TELEGRAM_TARGET_ID).",
    )
    health_file: str = Field(
        default=DEFAULT_HEALTH_FILE,
        min_length=1,
        description="Путь к файлу с отчётом health check (HEALTH_FILE).",
    )
    health_max_age_seconds: int = Field(
        default=DEFAULT_HEALTH_MAX_AGE_SECONDS,
        gt=0,
        description="Максимальный возраст отчёта health check в секундах (HEALTH_MAX_AGE_SECONDS).",
    )
    backup_path: str = Field(
        default=DEFAULT_BACKUP_PATH,
        min_length=1,
        description="Путь к файлу резервной копии SQLite (BACKUP_PATH).",
    )

    @property
    def secret_values(self) -> tuple[str, ...]:
        """Все секретные значения Settings, которые нельзя выводить в логи (SPEC-023 §4).

        Включает Bot Token и пароль из DATABASE_URL, если он есть.
        Гарантирует, что приложение редактирует все секреты единообразно —
        централизованный источник истины для redaction.
        """
        values: list[str] = [self.bot_token.get_secret_value()]
        # Если DATABASE_URL содержит user:password@... — выводим его целиком,
        # чтобы redaction скрыла и логин, и пароль.
        if _URL_CREDENTIALS_PATTERN.search(self.database_url):
            values.append(self.database_url)
        if self.bot_proxy_url:
            parsed = urlsplit(self.bot_proxy_url)
            if parsed.username is not None:
                # Исключения могут содержать как URL целиком, так и отдельные
                # (в том числе декодированные) части данных авторизации.
                values.extend([self.bot_proxy_url, parsed.username, unquote(parsed.username)])
                if parsed.password is not None:
                    values.extend([parsed.password, unquote(parsed.password)])
        # Убираем дубликаты и пустые значения.
        seen: set[str] = set()
        unique: list[str] = []
        for value in values:
            if value and value not in seen:
                seen.add(value)
                unique.append(value)
        return tuple(unique)

    @field_validator("telegram_target_allowlist")
    @classmethod
    def _check_allowlist(cls, value: str) -> str:
        try:
            parse_allowlist(value)
        except ValueError:
            raise ValueError("некорректный идентификатор канала") from None
        return value

    @field_validator("bot_token")
    @classmethod
    def _check_token_format(cls, value: SecretStr) -> SecretStr:
        if not _TOKEN_PATTERN.fullmatch(value.get_secret_value()):
            raise ValueError("не задан или имеет неверный формат")
        return value

    @field_validator("bot_proxy_url", mode="before")
    @classmethod
    def _check_proxy_url(cls, value: object) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        error = "ожидается URL http:// или socks5:// с hostname и портом (1–65535)"
        if not isinstance(value, str):
            raise ValueError(error)
        value = value.strip()
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
            userinfo = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else None
            if (
                parsed.scheme not in {"http", "socks5"}
                or not host or port is None or not 1 <= port <= 65535
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)
                or "\\" in value
                or re.search(r"%(?![0-9a-fA-F]{2})", value)
                or (userinfo is not None and (
                    "@" in userinfo or not parsed.username or not parsed.password
                    or ":" not in userinfo
                ))
                or any(ch in host for ch in "%/#?@")
            ):
                raise ValueError
            # Домен должен содержать только допустимые символы; IPv6 уже
            # проверен urlsplit при разборе адреса в квадратных скобках.
            ascii_host = host.encode("idna").decode("ascii")
            if ":" not in ascii_host and (
                len(ascii_host) > 253 or not all(
                    re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?", label)
                    for label in ascii_host.rstrip(".").split(".")
                )
            ):
                raise ValueError
            if parsed.scheme == "socks5" and parsed.username is not None:
                if any(len(unquote(part).encode("utf-8")) > 255
                       for part in (parsed.username, parsed.password or "")):
                    raise ValueError
        except (ValueError, UnicodeError):
            raise ValueError(error) from None
        return value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("пустое значение")
        # Синтаксис SQLAlchemy URL проверяется в слое database при создании
        # движка — здесь только непустота, чтобы не нарушать архитектурное
        # правило «config не импортирует sqlalchemy» (test_architecture).
        return stripped

    @field_validator("default_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):
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
