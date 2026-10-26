# Проверка SPEC-030 — Резервное копирование SQLite

## Объём и результат

SPEC-030 реализует резервное копирование SQLite через Online Backup API
без новых зависимостей.

Добавлено:

- `src/postmaster/database/backup.py` — `create_backup()` на Online Backup API,
  `BackupReport`, `BackupError`. Проверка `PRAGMA integrity_check` и подсчёт
  строк `users`/`posts` сразу после создания копии.
- `src/postmaster/app.py` — `backup_main()` CLI.
- `src/postmaster/__main__.py` — диспетчер подкоманд `health`/`backup`.
- `Settings.backup_path` (`BACKUP_PATH`).
- `tests/test_spec030.py` — 26 тестов.

Полный прогон тестов: **825 passed**, 1 skipped. `ruff check .` и
`ruff format --check .` — успешно.

Исходный текст SPEC-030: `docs/specs/SPEC-030-резервное-копирование-sqlite.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Backup не меняет активную БД | `test_backup_does_not_change_source_database`, `test_backup_does_not_modify_source_mtime`, `test_backup_works_with_wal_database` | ✅ Источник читается только через Online Backup API; mtime не меняется; работает на WAL-БД |
| Backup не включает .env | `test_backup_does_not_include_env_file` | ✅ Backup — это SQLite-файл, .env в нём отсутствует; только таблицы `users` и `posts` |
| Ошибка backup логируется | `test_backup_failure_is_logged` | ✅ `logger.exception` вызывается при `sqlite3.Error` и `OSError` |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| backup create | `test_backup_is_created`, `test_backup_creates_missing_destination_directory`, `test_backup_overwrites_existing_destination`, `test_backup_leaves_no_temp_file`, `test_backup_of_empty_database_is_created` | ✅ 5 тестов |
| open/verify | `test_backup_passes_integrity_check`, `test_backup_is_openable_and_has_tables`, `test_backup_report_contains_row_counts` | ✅ 3 теста |
| failed destination | `test_backup_fails_when_source_missing`, `test_backup_fails_when_destination_directory_is_a_file`, `test_backup_fails_when_source_is_not_sqlite`, `test_backup_failure_is_logged`, `test_backup_failure_leaves_no_partial_file` | ✅ 5 тестов |
| data comparison | `test_backup_data_matches_source`, `test_backup_preserves_user_and_post_contents`, `test_backup_captures_changes_made_after_first_backup` | ✅ 3 теста |

Дополнительно: CLI-сценарии (5 тестов) + секрет-не-в-выводе (2 теста).

## Архитектурные решения

| Слой | Файл | Что |
|---|---|---|
| `database` | `database/backup.py` | Использует stdlib `sqlite3` (Online Backup API). sqlalchemy не импортируется — `database` уже имеет право на `sqlite3` по архитектурной матрице |
| `app` | `app.py` → `backup_main()` | CLI: читает настройки, разбирает `DATABASE_URL` через `make_url` (тот же путь, что в `database/engine.py`) |
| `__main__` | `__main__.py` | Диспетчер подкоманд |

Без новых зависимостей. Архитектурная матрица не изменяется.

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | Online Backup API (`Connection.backup`) вместо `shutil.copy` | Безопасен при работающей БД: пошаговая итерация, WAL корректно обрабатывается; целостность гарантируется самим SQLite |
| D2 | Запись прямо в `destination`, без временного файла | На Windows файл остаётся залоченным после закрытия sqlite3-соединения — `os.replace(tmp, destination)` падает. Потеря атомарности компенсируется `PRAGMA integrity_check` после записи; при ошибке проверки backup удаляется |
| D3 | Удаление существующего `destination` перед записью | sqlite3 не может открыть файл с неверным форматом (например, остатки предыдущего backup). Удаляем заранее |
| D4 | `connection_factory` параметр для тестов | Позволяет подменить фабрику соединений; в продакшене используется `sqlite3.connect` |
| D5 | `BackupError` — единственный тип исключения наружу | CLI и обработчики ловят только этот тип; внутренние `sqlite3.Error`/`OSError`/`FileExistsError`/`NotADirectoryError` (все — `OSError`) конвертируются в `BackupError` с `logger.exception` |
| D6 | CLI возвращает 1 (а не 2) на ошибки backup | Согласуется с SPEC-029 (1 = runtime/health issue) и health_main; 2 — только конфигурация |
| D7 | `make_url` для проверки backend'а DATABASE_URL | Поддерживается только SQLite: для PostgreSQL команда сообщает, что backup неприменим (а не ломается) |

## Связь с другими SPEC

- **SPEC-027:** backup читает ту же БД, что и приложение. WAL-режим не мешает `Connection.backup`.
- **SPEC-029:** CLI-стиль и коды возврата согласованы со `health_main`. Backup не запускается как часть health — это отдельная операция.
- **SPEC-023:** секреты не попадают в backup и в вывод CLI; `Settings.backup_path` проходит через `SecretStr`-независимую валидацию.
- **SPEC-028:** файл `data/backup.db` в Docker остаётся на хосте, если `./data` смонтирован bind-mount'ом. Можно добавить отдельный volume при необходимости — в MVP это не требуется.

## Изменения в существующих файлах

- `src/postmaster/config.py` — `backup_path` (default `data/backup.db`).
- `src/postmaster/app.py` — `backup_main()`, `_parse_backup_args()`, `_BackupArgs`,
  `_database_file_path()`. Импорты `make_url`, `ArgumentError`, `BackupError`,
  `create_backup`.
- `src/postmaster/__main__.py` — `COMMAND_BACKUP` и `backup_main` в диспетчере.
- `README.md` — переменная `BACKUP_PATH`, раздел «Резервное копирование SQLite»,
  обновлённая таблица кодов завершения.

## Новые файлы

- `src/postmaster/database/backup.py`
- `tests/test_spec030.py` — 26 тестов.
- `docs/reviews/SPEC-030-review.md` — этот отчёт.

## Ограничения scope

- Только SQLite. Для PostgreSQL использовался бы `pg_dump` — отдельный
  CLI или скрипт.
- Backup не включает расписание/ротацию. Оператор вызывает команду вручную
  или через cron/systemd timer. Периодический автоматический backup
  не требуется SPEC-030.
- Backup создаётся на той же машине, что и приложение. Копирование
  backup'а на удалённое хранилище (S3, NAS) — задача инфраструктуры,
  не SPEC-030.
- На Windows запись прямо в файл (D2) вместо temp + rename — компромисс
  ради совместимости с file locking. На POSIX оба варианта работают
  одинаково, но атомарность теряется.

## Команды

```bash
python -m pytest tests/test_spec030.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .

# В работающем приложении:
python -m postmaster backup                              # default BACKUP_PATH
python -m postmaster backup --destination /var/backups/x.db
python -m postmaster backup --source /var/lib/app/db.sqlite
```
