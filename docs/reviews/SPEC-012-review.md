# Проверка SPEC-012 — обработка пропущенных публикаций

## Объём и результат

Проверена реализация `MissedPostHandler`: перечитывание постов из БД, фильтрация по статусу
и времени, передача в `PublicationService`. Полный прогон на Python 3.14.6:
**386 passed**, 1 skipped. `ruff check .`, `ruff format --check .` — успешно.

Исходный текст SPEC-012: `docs/specs/SPEC-012-обработка-пропущенных-публикаций.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Missed Post: status=PENDING и scheduled_at_utc<=now | `test_missed_handler_executes_past_post`, `test_missed_handler_skips_future_post` | ✅ |
| reload из БД и передача в PublicationService | `test_missed_handler_updates_status_on_success`, `test_missed_handler_returns_none_for_missing_post` | ✅ |
| scheduled_at_utc не меняется | `test_missed_handler_does_not_change_scheduled_at` | ✅ |
| не-PENDING не выполняются | `test_missed_handler_skips_cancelled_post`, `test_missed_handler_skips_published_post` | ✅ |
| новая запись Post не создаётся | `test_missed_handler_does_not_create_new_post` | ✅ |
| past/equal/future | `test_missed_handler_executes_past_post`, `test_missed_handler_skips_future_post`, `test_missed_handler_executes_post_at_equal_time` | ✅ |
| cancelled | `test_missed_handler_skips_cancelled_post`, `test_missed_handler_skips_published_post` | ✅ |
| multiple missed | `test_missed_handler_handle_many_with_mixed_posts` | ✅ |
| failure isolation | `test_missed_handler_failure_isolation` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `MissedPostHandler` — отдельный сервис, а не метод RecoveryService | §1 SPEC-012 «Missed Post» — самостоятельная сущность со своим контрактом; выделение в сервис упрощает тестирование |
| D2 | `handle()` принимает `post_id`, а не `Post` | БД — источник истины (§6 SPEC-012); handler всегда перечитывает свежий снимок |
| D3 | `find_missed()` использует `list_pending()` + фильтр по времени | `list_pending` уже возвращает только PENDING; фильтр по `scheduled_at_utc <= now` отсекает будущие |
| D4 | `handle_many()` собирает `MissedPostReport` со счётчиками `handled`, `skipped`, `errors` | §8 SPEC-012 «failure isolation» — отдельный отчёт для мониторинга |
| D5 | Поле `_missed_post_handler` в `Application` для прямого доступа из тестов | Нужно для unit-тестов и интеграции с RecoveryService |
| D6 | При успехе `PublicationService` меняет статус `PENDING → PROCESSING → PUBLISHED` | Это побочный эффект `publish()`, а не самого handler; handler не дублирует логику |

## Сценарии

### Основной

`missed post_id → reload → status check → PublicationService`

1. `RecoveryService` обнаруживает overdue пост и вызывает `missed.handle(post_id)`.
2. Handler перечитывает `Post` из БД через `PostRepository.get_by_id(post_id)`.
3. Проверяет статус: если не PENDING — возвращает `None`, поста не выполняется.
4. Проверяет `scheduled_at_utc <= now`: если время не наступило — возвращает `None`.
5. Передаёт `post_id` в `PublicationService.publish()`, который переводит
   пост `PENDING → PROCESSING → PUBLISHED` и публикует через `Publisher`.

### Обработка ошибок

- Пост не найден — возвращается `None`, лог `WARNING`, без ошибки.
- Статус не PENDING — `None`, `INFO`.
- Время в будущем — `None`, `INFO`.
- Ошибка в `PublicationService` — исключение пробрасывается вверх,
  `MissedPostReport.errors` пополняется записью `{post_id, error}`.
- Остальные посты в `handle_many` продолжают обрабатываться.

## Ограничения scope

- Handler не выбирает `Publisher` — это делает `PublicationService`.
- Handler не планирует новые задания в Scheduler (overdue не повторяется).
- Нет дедупликации на уровне handler: если recovery вызывается дважды,
  handler обработает один и тот же пост дважды. Это допустимо, т.к. переход
  `PENDING → PUBLISHED` защищён атомарным `update_status` с условием по старому
  статусу (RT-008-01, ERR-008-04).

## Команды

```bash
python -m pytest tests/test_spec012.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
