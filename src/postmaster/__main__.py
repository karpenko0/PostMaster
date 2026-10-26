"""Запуск через ``python -m postmaster``.

Без аргументов запускается бот. Подкоманды:

- ``health`` — проверка жизнеспособности, код 0 (healthy) или 1 (unhealthy);
- ``backup`` — резервное копирование SQLite, код 0 (создан) или 1 (ошибка).
"""

import sys

from postmaster.app import backup_main, health_main, main

COMMAND_HEALTH = "health"
COMMAND_BACKUP = "backup"


def _dispatch(argv: list[str]) -> int | None:
    """Обрабатывает подкоманды. Возвращает код завершения или None для запуска бота."""
    if not argv:
        return None
    command = argv[0]
    if command == COMMAND_HEALTH:
        return health_main(argv[1:])
    if command == COMMAND_BACKUP:
        return backup_main(argv[1:])
    return None


if __name__ == "__main__":
    exit_code = _dispatch(sys.argv[1:])
    if exit_code is None:
        main()
    else:
        raise SystemExit(exit_code)
