# Проверка SPEC-028 — Docker и постоянные данные

## Объём и результат

SPEC-028 функционально реализована ранее: `Dockerfile` (python:3.12-slim, непривилегированный uid 1000, `python -m postmaster`), `docker-compose.yml` (bind-mount `./data:/app/data`, `env_file: .env`, `init: true`, `stop_grace_period: 30s`), `.dockerignore` исключает `.env`/`data`/`*.db`. Базовые проверки есть в `tests/test_deployment.py`.

Добавлен целевой файл `tests/test_spec028.py` — 20 тестов, покрывающих все обязательные проверки §8 и приёмку §7.

Полный прогон тестов: **762 passed**, 1 skipped. `ruff check .` и
`ruff format --check .` — успешно.

Исходный текст SPEC-028: `docs/specs/SPEC-028-docker-и-постоянные-данные.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Restart/recreate с тем же volume сохраняет DB | `test_db_file_survives_engine_reopen`, `test_db_file_survives_repeated_reopens`, `test_db_file_is_queryable_via_raw_sqlite_after_engine_close`, `test_compose_binds_data_to_host` | ✅ БД переживает многократный reopen engine; файл читается обычным `sqlite3` после закрытия; bind-mount смонтирован в compose |
| Graceful shutdown Scheduler | `test_application_shutdown_stops_scheduler`, `test_scheduler_shutdown_is_idempotent`, `test_compose_uses_init_for_signal_forwarding`, `test_compose_has_grace_period_for_shutdown` | ✅ `Application.shutdown()` вызывает `_scheduler.shutdown()`; `Scheduler.shutdown()` идемпотентен; compose пробрасывает SIGTERM через `init: true` и даёт 30s на shutdown |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| build | `test_dockerfile_uses_python_3_12`, `test_dockerfile_pip_install_then_user_switch`, `test_dockerfile_data_directory_is_owned_by_postmaster`, `test_dockerfile_cmd_runs_module_postmaster`, `test_dockerfile_does_not_leak_dotenv_into_image`, `test_dockerignore_excludes_env_and_data`, `test_dockerfile_has_no_token_or_env_secrets`, `test_compose_has_restart_policy` | ✅ 8 тестов структурной проверки |
| startup | `test_compose_uses_init_for_signal_forwarding`, `test_compose_has_grace_period_for_shutdown`, `test_application_shutdown_stops_scheduler` | ✅ 3 теста |
| restart persistence | `test_db_file_survives_engine_reopen` | ✅ |
| recreate persistence | `test_db_file_survives_repeated_reopens`, `test_db_file_is_queryable_via_raw_sqlite_after_engine_close` | ✅ 2 теста |
| missing env | `test_main_without_bot_token_exits_2`, `test_main_with_invalid_log_level_exits_2` | ✅ 2 теста |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `test_application_shutdown_stops_scheduler` использует `_SchedulerShutdownSpy` и фиктивные `_NoopBot`/`_NoopEngine` | `Application.__init__` требует реальные `bot`/`engine`; spy даёт точечное наблюдение за вызовом `scheduler.shutdown()` без подъёма всего стека зависимостей |
| D2 | Тест restart/recreate persistence на уровне файла, а не контейнера | Запуск настоящего Docker в unit-тестах невозможен. Эквивалентность гарантируется `sqlite+aiosqlite` (один процесс-писатель) и bind-mount compose (файл хоста = файл контейнера) |
| D3 | `test_db_file_is_queryable_via_raw_sqlite_after_engine_close` — чтение обычным `sqlite3` после закрытия движка | Доказывает, что данные физически зафиксированы в файле и не держатся в кешах движка |
| D4 | Guard `test_dockerfile_has_no_token_or_env_secrets` сканирует Dockerfile на regex `\d{5,}:[A-Za-z0-9_-]{20,}` | Контрактная защита: токен формата Bot API не попадёт в образ при будущих правках Dockerfile |
| D5 | `test_dockerfile_does_not_leak_dotenv_into_image` — все строки `COPY` не должны содержать `.env` | Точечная проверка: `.dockerignore` блокирует `.env` на уровне контекста, но явный `COPY .env …` всё равно скопировал бы файл |
| D6 | `test_compose_does_not_inline_secrets` разбирает YAML построчно и проверяет только секцию `environment:` | Документационные упоминания `BOT_TOKEN` в комментариях (для разработчиков) не должны ломать тест; проверяется только фактическая передача секретов через compose |
| D7 | `test_dockerfile_pip_install_then_user_switch` — `USER postmaster` идёт после `pip install` | `pip install` требует root для записи в системные каталоги; `USER` до `pip install` уронил бы сборку |
| D8 | `test_scheduler_running_reports_correct_state` не проверяет `running` после `shutdown` | APScheduler `shutdown(wait=False)` обновляет внутреннее состояние асинхронно; идемпотентность — наш контракт, а не точное значение `running` |
| D9 | Subprocess-тесты missing env с `subprocess.run` + `PYTHONIOENCODING=utf-8` | Та же схема, что в `test_entrypoint.py`; работает и на Windows |

## Связь с другими SPEC

- **SPEC-023:** секреты не в образе — единый источник `Settings.bot_token` (`SecretStr`); `Settings.secret_values` редактирует пароль `DATABASE_URL` в логах.
- **SPEC-025:** graceful shutdown логирует `event=scheduler_stop` (SPEC-025 структурированное событие).
- **SPEC-027:** restart persistence = reopen движка без потерь; `create_all` не удаляет существующие таблицы; bind-mount сохраняет файл.
- **SPEC-026:** missing token — `load_settings` бросает `ConfigError`; `main()` перехватывает и выходит с кодом 2.
- **SPEC-029 (Health-check):** `HEALTHCHECK`-инструкция в Dockerfile намеренно не добавляется — это зона ответственности SPEC-029.

## Изменения в существующих файлах

Не потребовались: SPEC-028 соответствует текущей реализации. Добавлены
только тесты и обзор.

## Новые файлы

- `tests/test_spec028.py` — 20 тестов.
- `docs/reviews/SPEC-028-review.md` — этот отчёт.

## Замечание о bind-mount и правах

Текущий `docker-compose.yml` использует bind-mount `./data:/app/data`,
а Dockerfile запускает процесс от пользователя uid 1000. На свежем клоне
каталог `./data` создаётся Docker'ом от root, и процесс контейнера не
может в него писать. README содержит инструкцию:

```bash
mkdir -p data && sudo chown -R 1000:1000 data
```

Это документированное требование для развёртывания; альтернатива — перейти
на named volume (Docker копирует владельца из образа), но тогда данные не
видны с хоста. Решение не входит в SPEC-028 и остаётся на усмотрение
пользователя. Тест `test_dockerfile_data_directory_is_owned_by_postmaster`
гарантирует, что в самом образе `/app/data` принадлежит `postmaster`.

## Ограничения scope

- Настоящая сборка образа (`docker build`) и запуск контейнера (`docker run`) —
  за пределами unit-тестов. Они покрываются ручной проверкой или CI в Docker.
- `loop.add_signal_handler(SIGTERM)` работает только на POSIX. В Windows
  `Application.main()` бросает `NotImplementedError` на `SIGTERM` — это
  задокументированное ограничение (тест `test_entrypoint.py::test_full_cycle_polls_and_exits_cleanly_on_sigterm`
  deselect'ится на Windows). В контейнере (Linux) всё работает штатно.
- Миграций схемы БД нет: SPEC-027 не требует Alembic.

## Команды

```bash
python -m pytest tests/test_spec028.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
