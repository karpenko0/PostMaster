# Проверка SPEC-029 — Health Check

## Объём и результат

SPEC-029 реализует проверку жизнеспособности процесса, БД и Scheduler
без публичного REST API и без новых зависимостей.

Добавлено:

- `src/postmaster/utils/health.py` — структуры `CheckResult`/`HealthReport`,
  атомарные `write_report`/`read_report`, ошибки `HealthReportStale`/
  `HealthReportCorrupted`.
- `src/postmaster/database/health.py` — `probe_database(engine)` через
  `SELECT 1` (read-only).
- `src/postmaster/services/health_service.py` — `HealthCheck` (асинхронная
  оркестрация) и `HealthReporter` (фоновый цикл записи отчёта).
- `src/postmaster/app.py` — `health_main()` CLI, диспетчер в
  `__main__.py` (`python -m postmaster health`).
- `Settings.health_file` + `Settings.health_max_age_seconds`.
- `Dockerfile` — инструкция `HEALTHCHECK`.
- `tests/test_spec029.py` — 33 теста.

Полный прогон тестов: **798 passed**, 1 skipped. `ruff check .` и
`ruff format --check .` — успешно.

Исходный текст SPEC-029: `docs/specs/SPEC-029-health-check.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Process alive, DB доступна, Scheduler запущен | `test_health_check_is_healthy_when_all_ok`, `test_health_check_is_unhealthy_when_database_down`, `test_health_check_is_unhealthy_when_scheduler_down` | ✅ Все три индикатора проверяются, объединены в `HealthReport.healthy` |
| Ответ без секретов и данных пользователей | `test_health_report_summary_does_not_contain_secrets`, `test_report_json_does_not_leak_token_or_credentials`, `test_health_check_report_contains_no_secrets`, `test_health_cli_does_not_echo_token`, `test_health_cli_uses_dotenv_for_token` | ✅ В JSON-отчёте и выводе CLI нет ни токена, ни пароля БД, ни user_id |
| DB или Scheduler failure → unhealthy | `test_health_check_is_unhealthy_when_database_down`, `test_health_check_is_unhealthy_when_scheduler_down`, `test_health_check_is_unhealthy_when_both_fail` | ✅ Любой отказ даёт `healthy=False` |
| Check read-only | `test_reporter_check_is_read_only` | ✅ `probe_database` выполняет только `SELECT 1`; 5 прогонов health-check не меняют данные |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| healthy | `test_health_check_is_healthy_when_all_ok`, `test_health_cli_reports_healthy`, `test_health_main_in_process_returns_0_when_healthy` | ✅ |
| DB down | `test_probe_database_returns_false_when_unavailable`, `test_health_check_is_unhealthy_when_database_down` | ✅ |
| scheduler down | `test_health_check_is_unhealthy_when_scheduler_down`, `test_health_cli_reports_unhealthy_when_scheduler_down`, `test_reporter_with_real_scheduler` | ✅ |
| no secrets | `test_health_report_summary_does_not_contain_secrets`, `test_report_json_does_not_leak_token_or_credentials`, `test_health_check_report_contains_no_secrets`, `test_health_cli_does_not_echo_token` | ✅ |

## Архитектурные решения (по слоям)

| Слой | Файл | Что |
|---|---|---|
| `utils` | `utils/health.py` | Чистые структуры данных (`CheckResult`, `HealthReport`), ошибки, `write_report`/`read_report`. Никаких зависимостей от БД или Telegram |
| `database` | `database/health.py` | `probe_database(engine) -> bool` через `SELECT 1`. Здесь разрешён `sqlalchemy` (архитектурное правило) |
| `services` | `services/health_service.py` | `HealthCheck` принимает `database_probe: Callable` и `scheduler_status` (port-объекты). Не импортирует `sqlalchemy` напрямую — сохраняет слой services «без прямого доступа к БД» |
| `app` | `app.py` — `health_main()` | CLI: читает настройки, читает отчёт, печатает summary/JSON, возвращает код |
| `__main__` | `__main__.py` | Диспетчер: `python -m postmaster` → бот, `python -m postmaster health` → CLI |

Эта структура **не требует изменения архитектурной матрицы**
(`test_architecture.py`): каждый модуль находится в слое, которому
разрешён его импорт. Root-модулей со «свободным» импортом sqlalchemy
не появляется.

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | Health check без HTTP-сервера | §5 спецификации: «Публичный REST API для MVP не требуется». Файловый отчёт вместо эндпойнта совместим с `HEALTHCHECK` Docker и любой внешней наблюдающей системой |
| D2 | HealthReporter как фоновая asyncio-задача в `Application` | Не нагружает основной код: тик каждые 30 секунд (`DEFAULT_INTERVAL_SECONDS`). Останавливается в `Application.shutdown` |
| D3 | Атомарная запись через `temp + os.replace` | Читатель (CLI/Docker) видит либо валидный отчёт, либо ничего — частичное состояние невозможно |
| D4 | `probe_database` — `True`/`False`, без текста ошибки | SPEC §1 «ответ без секретов/данных»: текст SQLAlchemy-исключения может содержать URL с credentials. Возвращаем только факт доступности |
| D5 | `SchedulerStatusProvider` — Protocol | Health check не зависит от `SchedulerService` конкретно — порт-объект легко подменить в тестах |
| D6 | `health_main` использует `load_settings()` (по умолчанию `.env`) | CLI должен работать так же, как приложение: читать `.env`, валидировать конфиг |
| D7 | CLI возвращает 2 при конфигурационной ошибке | Согласуется с кодами приложения: 0 = ok, 1 = runtime/health issue, 2 = config |
| D8 | `HEALTHCHECK --start-period=20s` | Даём приложению время на startup + Recovery + первый health write |
| D9 | `HealthReportError` иерархия | `Stale` и `Corrupted` — подклассы для разной обработки в CLI и в будущих HTTP/observability-интеграциях |

## Связь с другими SPEC

- **SPEC-023:** Отчёт не содержит токена и пароля БД — единая политика редактирования секретов.
- **SPEC-025:** Канал `log_event` не используется в health-потоке (там нет бизнес-событий); но `logger.exception` для сбоев записи отчёта попадает в общий лог-формат с redaction.
- **SPEC-026:** Настройки `HEALTH_FILE`/`HEALTH_MAX_AGE_SECONDS` приходят из `Settings`.
- **SPEC-028:** `HEALTHCHECK` в Dockerfile использует тот же CLI, что и оператор.
- **SPEC-027:** `probe_database` использует уже настроенный `AsyncEngine`; переживает restart/recreate контейнера.

## Изменения в существующих файлах

- `src/postmaster/config.py` — `health_file`/`health_max_age_seconds`.
- `src/postmaster/app.py` — `health_main()`, `_parse_health_args()`, `_check_health_file()`,
  `_HealthArgs`, интеграция `HealthReporter` в `startup`/`shutdown`,
  новые импорты (`probe_database`, `HealthCheck`, `HealthReporter`,
  утилит health).
- `src/postmaster/__main__.py` — диспетчер подкоманд.
- `Dockerfile` — инструкция `HEALTHCHECK`.
- `README.md` — переменные `HEALTH_FILE`/`HEALTH_MAX_AGE_SECONDS`,
  раздел «Health check (SPEC-029)», обновлённая таблица кодов завершения.

## Новые файлы

- `src/postmaster/utils/health.py`
- `src/postmaster/database/health.py`
- `src/postmaster/services/health_service.py`
- `tests/test_spec029.py` — 33 теста.
- `docs/reviews/SPEC-029-review.md` — этот отчёт.

## Ограничения scope

- HTTP-эндпойнта `/health` нет: SPEC §5 запрещает публичный REST API. Если
  понадобится — добавляется новый слой `http` с веб-сервером.
- В отдельном процессе CLI видит только свежесть отчёта; если файл не
  пишется (приложение не запущено), CLI сообщает «unhealthy». Это
  совпадает с реальностью: Docker пометит контейнер unhealthy по тем же
  причинам.
- CLI не импортирует `application` целиком: только настройки и
  `utils.health.read_report`. Это даёт быстрый старт (<1 секунды),
  что важно для Docker `HEALTHCHECK --timeout=10s`.
- Health-отчёт не содержит информации о конкретных постах или
  пользователях — только «БД доступна/недоступна» и «Scheduler запущен/
  остановлен». Этого достаточно для оркестратора; глубокая диагностика
  выполняется через логи.

## Команды

```bash
python -m pytest tests/test_spec029.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .

# В работающем приложении:
python -m postmaster health               # → healthy
python -m postmaster health --print       # JSON-отчёт

# Из Dockerfile:
HEALTHCHECK CMD ["python", "-m", "postmaster", "health"]
```
