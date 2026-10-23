# Проверка SPEC-011 — восстановление задач после перезапуска

## Объём и результат

Проверена реализация `RecoveryService` и его интеграция в жизненный цикл приложения.
Полный прогон на Python 3.14.6: **386 passed**, 1 skipped. `ruff check .`,
`ruff format --check .` — успешно.

Исходный текст SPEC-011: `docs/specs/SPEC-011-восстановление-задач-после-перезапуска.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| На startup Recovery загружает PENDING Posts | `test_application_startup_runs_recovery`, `test_recovery_with_empty_database` | ✅ |
| Будущие посты восстанавливаются в Scheduler | `test_recovery_reschedules_future_post`, `test_recovery_handles_mixed_posts` | ✅ |
| Overdue посты передаются missed handler | `test_recovery_delegates_overdue_to_missed_handler` | ✅ Делегирование вынесено в SPEC-012 |
| CANCELLED/PUBLISHED/FAILED не восстанавливаются | `test_recovery_skips_non_pending_posts` | ✅ `list_pending()` возвращает только PENDING |
| Повторный recovery не создаёт дубли | `test_recovery_duplicate_recovery_no_duplicates` | ✅ Новых записей Post нет, задание с тем же id перезаписывается (`replace_existing=True`) |
| Ошибка одного Post не блокирует остальные | `test_recovery_continues_after_missed_handler_error` | ✅ try/except внутри цикла, ошибки попадают в `RecoveryReport.errors` |
| Recovery работает с реальным SchedulerService | `test_recovery_uses_real_scheduler` | ✅ Задание `post:{post_id}` регистрируется в APScheduler |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| future | `test_recovery_reschedules_future_post` | ✅ |
| overdue | `test_recovery_delegates_overdue_to_missed_handler` | ✅ |
| duplicate recovery | `test_recovery_duplicate_recovery_no_duplicates` | ✅ |
| one failure | `test_recovery_continues_after_missed_handler_error` | ✅ |
| restart | `test_application_startup_runs_recovery` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `RecoveryService` получает порт `MissedPostGateway`, а не `PublicationExecutor` | §1 SPEC-011: «overdue передаёт missed handler». Обработка пропущенных публикаций — отдельная SPEC-012 |
| D2 | `RecoveryReport` содержит `rescheduled` и `missed` вместо `executed` | Счётчик `executed` смешивал две ответственности SPEC-011 и SPEC-012 |
| D3 | Ошибка в missed handler не увеличивает `missed`, а попадает в `errors` | Нечитаемое состояние не должно выглядеть как успешно обработанная публикация |
| D4 | Recovery выполняется в `Application.startup()` после `scheduler.start()` | `schedule_post` требует запущенного Scheduler (иначе `RuntimeError`) |
| D5 | `_recovery_service` — nullable-поле `Application` | Тесты создают `Application` напрямую через конструктор и подставляют заглушки |

## Ограничения scope

- Recovery не создаёт новых записей Post и не меняет `scheduled_at_utc`.
- Восстановление выполняется только при старте. Периодического опроса БД нет (это отдельная SPEC).
- Проверка `scheduled_at_utc <= now` использует `utc_now()` — часовой пояс сервера не влияет (SPEC-006).
- APScheduler по-прежнему не хранит задания между перезапусками: каждое восстановление
  регистрирует их заново в памяти процесса.

## Команды

```bash
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
