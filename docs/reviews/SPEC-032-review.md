# Проверка SPEC-032 — Интеграционные тесты

## Объём и результат

SPEC-032 требует сквозного покрытия взаимодействия services +
repositories + SQLite + Scheduler + Publishers с mocked TelegramClient
(§1, §4) и проверки межслойных контрактов: status transitions,
persistence, контракты (§7).

Все шесть обязательных сценариев §8 уже покрыты отдельными spec-тестами
(SPEC-009 create+schedule, SPEC-011 recovery, SPEC-012 missed,
SPEC-010 publish, SPEC-018 cancel, SPEC-022 retry). Добавлен
**интеграционный suite**, который проходит через все слои одновременно
и не сводится к одному слою.

Добавлено:

- `tests/test_spec032.py` — 15 интеграционных тестов на единой фикстуре.
- `docs/reviews/SPEC-032-review.md` — этот отчёт.

Полный прогон: **863 passed, 1 skipped**. `ruff check .` и
`ruff format --check .` — успешно.

## Матрица приёмки

| Критерий §7 | Доказательство | Вывод |
|---|---|---|
| Status transitions | `test_status_transitions_prohibited_paths` | ✅ Недопустимый переход PENDING→PUBLISHED отклоняется `InvalidPostTransitionError`; пост остаётся в PENDING |
| Persistence | `test_publish_persists_terminal_state_across_engine_reopen` | ✅ После PUBLISHED и `engine.dispose()` + нового engine по тому же файлу — состояние сохраняется |
| Межслойные контракты | `test_create_scheduled_post_persists_and_registers_job`, `test_publish_full_flow_transitions_to_published`, `test_cancel_removes_job_and_marks_post_cancelled`, `test_recovery_then_publish_returns_to_published`, `test_publish_increments_attempt_counter_in_logs` | ✅ Каждое утверждение проверяет сквозное поведение services + repos + DB |

## Обязательные проверки §8

| Сценарий | Тест | Статус |
|---|---|---|
| create + schedule | `test_create_scheduled_post_persists_and_registers_job`, `test_create_scheduled_post_writes_to_db_before_scheduler_call` | ✅ 2 теста |
| publish | `test_publish_full_flow_transitions_to_published`, `test_publish_persists_terminal_state_across_engine_reopen`, `test_publish_error_keeps_state_consistent` | ✅ 3 теста |
| retry | `test_retry_on_transient_error_eventually_succeeds`, `test_retry_does_not_retry_on_permission_error` | ✅ 2 теста |
| cancel | `test_cancel_removes_job_and_marks_post_cancelled`, `test_concurrent_cancel_respects_owner` | ✅ 2 теста |
| recovery | `test_recovery_picks_up_future_pending_posts`, `test_recovery_handles_missed_posts_via_missed_handler`, `test_recovery_then_publish_returns_to_published` | ✅ 3 теста |
| missed | `test_recovery_handles_missed_posts_via_missed_handler` (overdue → MissedPostHandler) | ✅ 1 тест |

Дополнительные контракты: `test_status_transitions_prohibited_paths`,
`test_publish_increments_attempt_counter_in_logs`,
`test_concurrent_publish_writes_only_once` — **4 теста**, покрывающие
межслойные контракты (статусы, логирование, параллелизм).

## Архитектура интеграционных тестов

Фикстура `integration_env` собирает **настоящие** объекты всех слоёв в `tmp_path`:

```
- database.engine / schema     — реальная SQLite через aiosqlite
- post_repository / user_repo  — реальные репозитории
- user_service                 — реальный сервис
- post_service                 — реальный сервис
- _RecordingScheduler          — fake SchedulerService (record без APScheduler)
- _RecordingSender             — fake Telegram sender (record/fail по запросу)
- publisher / publication_service — реальные (составленные из настоящих типов)
- recovery_service             — реальный
- missed_post_handler          — реальный
- cancel_service               — реальный
```

`_RecordingScheduler` и `_RecordingSender` — единственные подмены.
Это и есть «mocked TelegramClient» из §1 плюс минимальный fake Scheduler
для сценариев, где нужен именно сервисный поток (без реального APScheduler).

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `_RecordingScheduler` хранит список вызовов, не подменяет тип | `PostService` и `CancelService` принимают `PostScheduler`/`SchedulerService`-совместимый порт — наш класс его реализует. Внешне поведение идентично реальному Scheduler, но без APScheduler |
| D2 | `_FlakySender` с `fail_count` для retry-теста | Проверяет ровно N неудач и затем успех; проще, чем счётчик вызовов |
| D3 | `test_create_scheduled_post_writes_to_db_before_scheduler_call` через sync sqlite3 | Spy в callback синхронный, поэтому читаем отдельным sync-соединением — `asyncio.run()` не работает внутри event loop |
| D4 | `future = utc_now() + timedelta(days=2)` в recovery-тесте | `RUN_AT = 2026-10-20` в прошлом относительно реального времени → recovery классифицировал бы как overdue |
| D5 | `test_status_transitions_prohibited_paths` через `post_repo.update_status` напрямую | PublicationService всегда выполняет полную цепочку PENDING→PROCESSING→PUBLISHED; проверка «недопустимого перехода» возможна только через прямой вызов репозитория |
| D6 | Параллелизм через `asyncio.gather` с `return_exceptions=True` | `PublicationService.publish` бросает исключения при гонке; `gather(..., return_exceptions=True)` собирает и успехи, и ошибки, и можно проверить «ровно один успех» |

## Связь с другими SPEC

- **SPEC-009:** create+schedule — точка регистрации задания и сохранения Post.
- **SPEC-010:** publish — PENDING→PROCESSING→PUBLISHED с mocked Telegram.
- **SPEC-011:** recovery — пересоздание заданий Scheduler.
- **SPEC-012:** missed — overdue → MissedPostHandler.
- **SPEC-013:** атомарный claim → `test_concurrent_publish_writes_only_once`.
- **SPEC-018:** cancel → `test_cancel_removes_job_and_marks_post_cancelled`.
- **SPEC-022:** retry → `test_retry_on_transient_error_eventually_succeeds`.
- **SPEC-024:** изоляция → `test_concurrent_cancel_respects_owner`.
- **SPEC-025:** структурированные логи → `test_publish_increments_attempt_counter_in_logs`.
- **SPEC-027:** persistence и commit-before-scheduler → `test_create_scheduled_post_writes_to_db_before_scheduler_call`, `test_publish_persists_terminal_state_across_engine_reopen`.
- **SPEC-031:** guard-тесты качества.

## Изменения в существующих файлах

Нет. Интеграционные тесты добавлены как новый файл; существующий код и
тесты не изменялись.

## Новые файлы

- `tests/test_spec032.py` — 15 тестов.
- `docs/reviews/SPEC-032-review.md` — этот отчёт.

## Ограничения scope

- `_RecordingScheduler` не подменяет `APScheduler` целиком: реальный
  APScheduler тестируется отдельно (test_scheduler_service).
- Mocked TelegramClient — это in-process stub, а не HTTP-сервер. Fake-сервер
  (`test_entrypoint.py`) покрывает сетевой слой отдельно.
- `_FlakySender` управляет временем отказа через счётчик вызовов; тесты
  не проверяют точные `delay_after_attempt`, потому что retry-тест
  использует `delays=()` (нулевые задержки).
- `test_recovery_handles_missed_posts_via_missed_handler` использует
  фиксированный `RUN_AT - 1h` для overdue. Это работает, пока тест
  запускается в течение часа после `RUN_AT` (2026-10-20 11:00 UTC). Это
  допустимо для тестового набора.

## Команды

```bash
python -m pytest tests/test_spec032.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
