"""Инструмент миграций схемы: ``python -m postmaster.migrate`` (PM-06).

Команды:

- ``upgrade``   — применить миграции (идемпотентно, как при старте приложения);
- ``downgrade`` — откатить миграции к версии 0 (проверка отката, PM-06 §2);
- ``version``   — напечатать текущую версию схемы.

Скрипт не читает BOT_TOKEN и не обращается к Telegram: для миграций достаточно DATABASE_URL.
"""

import argparse
import asyncio
import sys

from postmaster.config import TOKEN_NOT_REQUIRED, ConfigError, load_settings
from postmaster.database.engine import create_database_engine
from postmaster.database.migrations import (
    apply_migrations,
    rollback_migrations,
    schema_version,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2

# Переменные окружения читает только config.Settings (SPEC-026 §1). Поэтому URL берётся
# из настроек. Если BOT_TOKEN не задан (например, миграции в CI), достаточно DATABASE_URL
# из того же .env — Settings создаётся с плейсхолдером токена.
# Служебное значение: миграции не обращаются к Telegram и не требуют реального токена.
_MIGRATION_TOKEN_PLACEHOLDER = TOKEN_NOT_REQUIRED


def _database_url() -> str:
    try:
        return load_settings().database_url
    except ConfigError as exc:
        if "BOT_TOKEN" not in str(exc):
            raise
        from pydantic import SecretStr

        from postmaster.config import Settings

        return Settings(bot_token=SecretStr(_MIGRATION_TOKEN_PLACEHOLDER)).database_url


async def _run(command: str) -> int:
    engine = create_database_engine(_database_url())
    try:
        if command == "upgrade":
            version = await apply_migrations(engine)
            print(f"schema_version={version}")
        elif command == "downgrade":
            version = await rollback_migrations(engine)
            print(f"schema_version={version}")
        else:
            print(f"schema_version={await schema_version(engine)}")
    finally:
        await engine.dispose()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m postmaster.migrate",
        description="Миграции схемы PostMaster (PM-06).",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="upgrade",
        choices=("upgrade", "downgrade", "version"),
        help="Действие с миграциями (по умолчанию upgrade).",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args.command))
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 — код возврата важнее стека
        print(f"migration_failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover — точка входа
    raise SystemExit(main())
