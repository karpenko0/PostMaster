# Проверка SPEC-009 — создание отложенной публикации

## Решение для режима публикации

Предыдущий диалог не собирал тип и `target_id`. После пропущенного уточнения принят минимальный вариант без изменения шагов: `POST_MODE=REMINDER` по умолчанию; при `POST_MODE=TELEGRAM` обязателен `TELEGRAM_TARGET_ID`. Это **конфигурация всего бота**, не выбор отдельного пользователя. Оба режима проходят поток `/start → фото → дата`; для TELEGRAM выполнен сквозной тест с каналом.

## Критерии приёмки

| Критерии | Проверка | Статус |
|---|---|---|
| AC-009-01/02/06/13/14, RT-009-01…03 | `test_service_commits_before_scheduler_and_passes_only_id_time` для REMINDER и TELEGRAM; `test_e2e_reminder_creates_post_job_and_resets_fsm`, `test_e2e_telegram_mode_persists_target_and_confirms_locally` | ✅ В SQLite сохраняется `Post` с `PENDING`, внутренним `users.id`, file IDs, UTC, поясом, типом/target, временными метками; `published_at` и `error_message` пусты |
| AC-009-03/04/05, TC-009-03…07 | `test_invalid_data_never_creates_post_or_scheduler_job` (несуществующий/отсутствующий пользователь, файл или unique ID, дата, тип, target) | ✅ Отказ без записи и job, код ошибки различается |
| AC-009-07/08, IT-009-01, INV-009-01/03 | `test_service_commits_before_scheduler_and_passes_only_id_time` открывает отдельное SQLite-соединение в `schedule_post` и видит уже закоммиченную запись; `test_real_scheduler_creates_date_job_with_id_and_utc` | ✅ Scheduler получает только `post_id` и aware UTC-время; ID задания `post:<id>` |
| AC-009-09, IT-009-02, INV-009-04 | `test_scheduler_failure_keeps_committed_pending_post`, `test_e2e_scheduler_failure_keeps_post_and_avoids_duplicate_on_new_date` | ✅ Запись остаётся PENDING, предупреждение без ложного успеха, FSM очищен во избежание повторного создания |
| AC-009-10, INV-009-05, §27–29 | `test_confirmation_messages` (точные русские шаблоны), E2E обоих режимов и тесты SPEC-006 | ✅ Локальные дата и время по timezone публикации; ответ только после commit; при ошибке БД/планировщика нет успешного подтверждения |
| AC-009-11/12, IT-009-03/04, INV-009-06 | `test_e2e_reminder_creates_post_job_and_resets_fsm`, `test_e2e_db_failure_keeps_photo_and_never_confirms` | ✅ При успешной записи/частичном успехе IDLE и пустой черновик; при ошибке БД WAITING_DATETIME и фото сохранено; новая фотография начинает новый сценарий |
| §21 | `test_due_job_reloads_post_by_id_instead_of_storing_object` | ✅ Job читает по ID из БД при запуске, не хранит объект Post |
| Регрессия SPEC-001…008 | `pytest -q -p no:cacheprovider` | ✅ 350 тестов пройдены |

## Обработка ошибок

`PostCreationError.code`: `USER_NOT_FOUND`, `PHOTO_DATA_MISSING`, `DATETIME_MISSING`, `INVALID_PUBLICATION_TYPE`, `TARGET_REQUIRED`, `POST_CREATE_FAILED`. После commit ошибка Scheduler представлена отдельным `SchedulerRegistrationError` с сохранённым `Post` и кодом `SCHEDULER_REGISTRATION_FAILED`: повтор даты не должен создавать дубликат. Логи не выводят file IDs и текст ошибок SQL.

## Ограничения scope

- Scheduler регистрирует одноразовую date-job и при её срабатывании перечитывает Post. **Доставка ещё не реализована**: job пишет предупреждение, не переводит статус и не отправляет фото/напоминание. Это предмет SPEC-010/014/015. Формулировки подтверждений из §27/28 означают планирование, а не факт отправки.
- После перезапуска БД сохраняет PENDING, но задания APScheduler из памяти не восстанавливаются до SPEC-011. Missed jobs, retry и реальные Telegram Publisher не проверены.
- Если пользователь не получает Telegram-ответ из-за сбоя `send_message` после commit, запись и задание уже существуют; гарантированная доставка подтверждения и транзакционная координация с Telegram API не входят в SPEC-009.
- Старые SQLite-схемы `users` по-прежнему требуют миграции SPEC-027.

## Прогон

Python 3.12.11: `python -m pytest -q -p no:cacheprovider` — **350 passed**; `ruff check .`, `ruff format --check .`, `git diff --check` — успешно. Тесты Telegram используют локальный fake API, а не внешнюю сеть.
