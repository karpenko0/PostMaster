# Проверка SPEC-018 — Команда cancel

## Объём и результат

Проверена реализация команды `/cancel <post_id>`: атомарная отмена, удаление задания
Scheduler, обработка гонок с claim/execute. Полный прогон на Python 3.14.6:
**506 passed**, 1 skipped. `ruff check .`, `ruff format --check .` — успешно.

Исходный текст SPEC-018: `docs/specs/SPEC-018-команда-cancel.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| PROCESSING не отменяется | `test_cancel_processing_post_rejected` | ✅ Условие `status = PENDING` в UPDATE |
| PUBLISHED не отменяется | `test_cancel_published_post_rejected` | ✅ |
| FAILED не отменяется | `test_cancel_failed_post_rejected` | ✅ |
| CANCELLED не отменяется повторно | `test_cancel_already_cancelled_rejected` | ✅ |
| Scheduler remove failure не возвращает Post в PENDING | `test_cancel_scheduler_remove_failure_keeps_post_cancelled` | ✅ Ошибка `remove_job` логируется, пост остаётся CANCELLED |
| Запись из БД не удаляется | `test_cancel_post_record_remains` | ✅ Только обновление `status`, не DELETE |
| Atomic UPDATE own PENDING→CANCELLED | `test_cancel_valid_id`, `test_cancel_foreign_post` | ✅ Один `UPDATE ... WHERE id=? AND user_id=? AND status='PENDING'` |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| valid id | `test_cancel_valid_id`, `test_cancel_e2e_via_fake_telegram`, `test_cancel_with_real_scheduler_removes_job` | ✅ |
| missing id | `test_cancel_missing_id`, `test_cancel_e2e_missing_id` | ✅ |
| invalid id | `test_cancel_e2e_invalid_id` | ✅ Handler парсит и валидирует |
| ownership | `test_cancel_foreign_post`, `test_cancel_e2e_foreign_post` | ✅ Условие `user_id` в WHERE |
| cancel race | `test_cancel_race_concurrent_cancels`, `test_cancel_race_with_claim` | ✅ Два cancel → один успех; cancel vs claim → cancel выигрывает |
| scheduler cleanup | `test_cancel_with_real_scheduler_removes_job` | ✅ Job `post:{id}` удаляется из APScheduler |

Дополнительные тесты:
- `test_cancel_post_record_remains` — запись остаётся в БД
- `test_cancel_processing_post_rejected`, `test_cancel_published_post_rejected`, `test_cancel_failed_post_rejected`, `test_cancel_already_cancelled_rejected`
- `test_cancel_scheduler_remove_failure_keeps_post_cancelled`
- `test_handler_register_only_private_chat` — handler только в личных чатах

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `CancelService` в `services/` | Бизнес-логика вне handler (BR-01): handler только разбирает сообщение и отправляет ответ |
| D2 | `PostRepository.cancel_post` переиспользован без изменений | Уже реализован с правильным условным UPDATE (RT-008-05) |
| D3 | Добавлен `SchedulerService.remove_job(post_id)` | §1 SPEC-018: «затем remove Scheduler job». Idempotent: повторное удаление = no-op |
| D4 | Ошибка `remove_job` НЕ пробрасывается и НЕ возвращает пост в PENDING | §7 SPEC-018: «scheduler remove failure не возвращает Post в PENDING». БД — источник истины |
| D5 | `PostNotFoundForUserError` для всех «не найден среди PENDING»: чужой, отсутствует, не PENDING | Не отличаем причину: пользователь не должен различать «чужой пост» vs «не PENDING» — это утечка |
| D6 | Handler парсит `text.split(maxsplit=1)`, проверяет что ID положительный | SPEC-018 §1: «/cancel <post_id>» |
| D7 | `chat_types=["private"]` | Решение D14 плана SPEC-003 |
| D8 | `CANCEL_USAGE` подсказывает использовать `/my_posts` | Улучшает UX — пользователь знает, как получить ID |
| D9 | Неудачный парсинг или чужой пост → разные сообщения | UX: `INVALID_ID_TEXT` vs `CANCEL_NOT_FOUND` — но не раскрываем существование чужих постов |

## Сценарии

### Happy path

```
/cancel 42
→ handler: parse "42" → post_id=42
→ user_service.get_by_telegram_id(sender.id) → user
→ CancelService.cancel(post_id=42, user_id=user.id)
   1. PostRepository.cancel_post(42, user.id)
      → UPDATE WHERE id=42 AND user_id=user.id AND status='PENDING'
      → rowcount=1, status=CANCELLED
   2. SchedulerService.remove_job(42)
      → scheduler.remove_job("post:42")
→ handler: bot.send_message("Публикация ID 42 отменена.")
```

### Чужой пост

```
/cancel 99 (user_id=Алиса)
→ PostRepository.cancel_post(99, user.id_Боба)
   → UPDATE WHERE id=99 AND user_id=Боб AND status='PENDING'
   → rowcount=0 (пост принадлежит Алисе, не Бобу)
→ CancelService: PostNotFoundForUserError
→ handler: bot.send_message("Публикация не найдена среди ваших ожидающих.")
```

### Гонка cancel vs claim

```
Время T0: post.status = PENDING
Время T1: CancelService.cancel(post_id, user_id) → UPDATE → status=CANCELLED
Время T2: PublicationService.publish(post_id) → claim_for_processing
            → rowcount=0 (статус не PENDING)
            → возвращает None → PostClaimLostError
Пост остаётся CANCELLED.
```

### Гонка двух cancel

```
T1: cancel(post_id) → rowcount=1, success
T2: cancel(post_id) → rowcount=0, PostNotFoundForUserError
remove_job вызван один раз.
```

### Ошибка удаления задания

```
T1: cancel → UPDATE → rowcount=1, status=CANCELLED
T2: Scheduler.remove_job(post_id) → RuntimeError
T3: CancelService логирует ошибку, не пробрасывает
T4: cancel возвращает успех
Пост остаётся CANCELLED. Задание Scheduler может остаться — при срабатывании
_on_post_due проверяет статус и пропускает не-PENDING посты.
```

## Ограничения scope

- Команда не показывает подтверждение перед отменой (`/cancel 42` сразу отменяет).
  Для MVP это приемлемо — пользователь сам выбрал ID через `/my_posts`.
- Не отменяет посты со статусом PROCESSING (они уже в работе). Это сознательно —
  после claim отмена невозможна, нужно ждать публикации или ошибки.
- `SchedulerService.remove_job` не выбрасывает исключение в вызывающий код —
  если Scheduler остановлен или job уже удалён, считается что cleanup не нужен.

## Команды

```bash
python -m pytest tests/test_spec018.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
