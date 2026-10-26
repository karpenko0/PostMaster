"""Запуск через ``python -m postmaster``.

Без аргументов запускается бот. Подкоманда ``health`` выполняет проверку
жизнеспособности и возвращает код 0 (healthy) или 1 (unhealthy) — см. SPEC-029.
"""

import sys

from postmaster.app import health_main, main

COMMAND_HEALTH = "health"


def _dispatch(argv: list[str]) -> int | None:
    """Обрабатывает подкоманды. Возвращает код завершения или None для запуска бота."""
    if not argv:
        return None
    command = argv[0]
    if command == COMMAND_HEALTH:
        return health_main(argv[1:])
    return None


if __name__ == "__main__":
    exit_code = _dispatch(sys.argv[1:])
    if exit_code is None:
        main()
    else:
        raise SystemExit(exit_code)
