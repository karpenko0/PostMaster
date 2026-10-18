# Проверка SPEC-013 — защита от двойной публикации

## Объём и результат

Проверена реализация атомарного захвата (`claim_post_for_processing`), финальных статусных переходов (`mark_published`, `mark_failed`, `release_post_for_retry`), сервиса `PublicationService`, интеграции с `SchedulerService` и гонок конкурентного выполнения/отмены.

Полный прогон на Python 3.12.11: **377 passed**, без падений и пропусков. `ruff check .`, `ruff format --check .`, `git diff --check` — чисто.

Контроль чувствительности: 8 мутаций (удаление `WHERE status='PENDING'` в claim, принудительный возврат `True` при `rowcount=0`, пропуск обновления `updated_at`, пропуск очистки `error_message` при публикации, удаление `WHERE status='PROCESSING'` в `mark_published`, перевод временной ошибки в `FAILED` вместо `PENDING`, вызов `Publisher` без проверки результата `claim`) обнаружены тестами `tests/test_spec013.py`.

## Матрица приёмки (AC-013-01 … AC-013-12)

| Критерий | Проверка | Статус |
|---|---|---|
| AC-013-01 — успешный claim (`PENDING → PROCESSING`, `True`) | `test_tc_013_01_claim_pending_post_succeeds_and_moves_to_processing`, `test_rt_013_01_atomic_claim_executes_single_conditional_update_without_select` | ✅ |
| AC-013-02 — повторный claim (`PROCESSING` → `False`) | `test_tc_013_02_claim_processing_post_returns_false` | ✅ |
| AC-013-03 — `PUBLISHED` не переводится в `PROCESSING` | `test_tc_013_03_claim_published_post_returns_false` | ✅ |
| AC-013-04 — `CANCELLED` не переводится в `PROCESSING` | `test_tc_013_04_claim_cancelled_post_returns_false` | ✅ |
| AC-013-05 — `FAILED` не переводится в `PROCESSING` обычным flow | `test_ac_013_05_claim_failed_post_returns_false_and_missing_post_returns_false` | ✅ |
| AC-013-06 — конкурентный запуск: только один получает `True` | `test_ct_013_01_two_workers_concurrent_claim_only_one_succeeds` (asyncio + потоки ОС) | ✅ |
| AC-013-07 — при конкурентном запуске Publisher вызывается 1 раз | `test_ct_013_02_publication_service_concurrent_execution_calls_publisher_once` | ✅ |
| AC-013-08 — после успеха `status=PUBLISHED`, `published_at != NULL` | `test_rt_013_03_mark_published_transitions_from_processing_and_clears_error`, `test_e2e_57_normal_publication_and_idempotent_repeat` | ✅ |
| AC-013-09 — после окончательной ошибки `status=FAILED`, `error_message != NULL` | `test_rt_013_04_mark_failed_transitions_from_processing_and_stores_error`, `test_e2e_59_permanent_error_marks_failed_and_prevents_rerun` | ✅ |
| AC-013-10 — временная ошибка переводит `PROCESSING → PENDING` | `test_rt_013_05_release_post_for_retry_transitions_from_processing_to_pending`, `test_e2e_60_temporary_error_returns_to_pending_and_succeeds_on_next_claim` | ✅ |
| AC-013-11 — каждый переход обновляет `updated_at` | Проверено во всех тестах `TC-013-01`, `RT-013-03…05`, `E2E-57…60` | ✅ |
| AC-013-12 — отмена не переводит `PROCESSING` в `CANCELLED` | `test_it_013_56_cancel_and_claim_race_allows_only_one_transition`, `test_cancel_does_not_interrupt_processing_or_finished_posts` | ✅ |

## Сверка тестов, ошибок, правил и инвариантов (§35–62)

| Раздел SPEC-013 | Доказательство | Статус |
|---|---|---|
| §40 Atomic UPDATE без предварительного SELECT | `test_rt_013_01_atomic_claim_executes_single_conditional_update_without_select` перехватывает курсор SQLAlchemy и проверяет ровно 1 `UPDATE` | ✅ |
| §43–46 Логирование claim / успеха / ошибки / retry | `test_tc_013_01…02`, `test_rt_013_03…05` проверяют дословные записи лога | ✅ |
| §47 `ERR-013-01` (`CLAIM_FAILED`), `ERR-013-02` (`POST_NOT_FOUND_AFTER_CLAIM`), `ERR-013-03` (`FINAL_STATUS_UPDATE_FAILED`) | `test_err_013_01_claim_failed_skips_get_by_id_and_publisher`, `test_err_013_02_post_not_found_after_claim_is_logged_and_skips_publisher`, `test_err_013_03_final_status_update_failure_is_logged_without_republishing` | ✅ |
| §49 Unit Tests `TC-013-01…04` | `test_tc_013_01…04` | ✅ |
| §50 Repository Tests `RT-013-01…05` | `test_rt_013_01`, `test_tc_013_02`, `test_rt_013_03…05` | ✅ |
| §51–52 Concurrency Tests `CT-013-01` и `PublicationService` | `test_ct_013_01_two_workers_concurrent_claim_only_one_succeeds`, `test_ct_013_02_publication_service_concurrent_execution_calls_publisher_once` | ✅ |
| §53–56 Integration Tests (Scheduler, Recovery, Missed Job, Cancel) | `test_it_013_53…56` | ✅ |
| §57–60 E2E-сценарии (нормальная публикация, два запуска, ошибка, retry) | `test_e2e_57…60`, `test_custom_retry_policy_and_missing_publisher_handling` | ✅ |
| §61 Инварианты `INV-013-01…07` | Подтверждены проверками `WHERE status='PENDING'` и `WHERE status='PROCESSING'` | ✅ |

## Границы scope (§4)

- Конкретные `ReminderPublisher` (SPEC-014) и `TelegramPublisher` (SPEC-015), полноценные сервисы `Recovery` (SPEC-011), `MissedJobService` (SPEC-012) и расписание повторных попыток (SPEC-022) не входят в scope SPEC-013.

## Команды проверки

```bash
/tmp/venv312/bin/python -m pytest -q -p no:cacheprovider
/tmp/venv312/bin/ruff check .
/tmp/venv312/bin/ruff format --check .
git diff --check
```
