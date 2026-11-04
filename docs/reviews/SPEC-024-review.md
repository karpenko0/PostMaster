# Проверка SPEC-024 — Изоляция пользовательских данных

## Объём и результат

SPEC-024 в основном была реализована ранее: `PostRepository.cancel_post(post_id, user_id)`
фильтрует атомарным UPDATE, `list_pending_by_user(user_id)` фильтрует в SQL,
`CancelService` не различает причины отказа. Добавлены:

- `PostRepository.get_user_post(post_id, user_id)` — owner-checked SELECT, явно
  требуемый §8.
- `CancelService.cancel` больше не включает `post_id` в текст ошибки
  (`PostNotFoundForUserError`): иначе пользователь мог бы отличить «чужой»
  от «нет такого» по самому ID.
- `tests/test_spec024.py` — 16 тестов, покрывающих §7/§8 SPEC-024.

Полный прогон тестов: **662 passed**, 1 skipped. `ruff check .` и
`ruff format --check .` — успешно.

Исходный текст SPEC-024: `docs/specs/SPEC-024-изоляция-пользовательских-данных.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| User A не видит Posts User B | `test_list_pending_by_user_returns_only_own_posts`, `test_my_posts_service_does_not_leak_across_users`, `test_my_posts_service_empty_for_unknown_telegram_id` | ✅ Фильтрация в SQL по `user_id`; `MyPostsService.render_list` отдаёт только посты владельца telegram_user_id |
| User A не отменяет Posts User B | `test_cancel_foreign_post_raises_safe_error`, `test_cancel_post_in_terminal_status_remains` | ✅ `cancel_post(post_id, user_id)` — атомарный UPDATE с `WHERE id=? AND user_id=? AND status=PENDING` |
| Ошибка не раскрывает существование чужого Post | `test_cancel_nonexistent_post_raises_same_error`, `test_cancel_safe_error_message_does_not_reveal_ownership`, `test_get_user_post_does_not_distinguish_missing_vs_foreign` | ✅ Один и тот же текст ошибки и для «чужого», и для «нет такого»; `get_user_post` возвращает `None` в обоих случаях |
| Owner check в repository/query | `test_get_user_post_returns_own_post`, `test_get_user_post_returns_none_for_foreign_user`, `test_get_user_post_returns_none_for_missing_post` | ✅ `WHERE id=? AND user_id=?` в `get_user_post` |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| two-user list | `test_list_pending_by_user_returns_only_own_posts`, `test_my_posts_service_does_not_leak_across_users`, `test_my_posts_service_empty_for_unknown_telegram_id`, `test_list_pending_by_user_empty_for_unknown_user` | ✅ |
| cancel isolation | `test_cancel_foreign_post_raises_safe_error`, `test_cancel_post_in_terminal_status_remains`, `test_cancel_own_post_succeeds` | ✅ |
| safe errors | `test_cancel_nonexistent_post_raises_same_error`, `test_cancel_safe_error_message_does_not_reveal_ownership`, `test_get_user_post_does_not_distinguish_missing_vs_foreign` | ✅ |
| get_user_post | `test_get_user_post_returns_own_post`, `test_get_user_post_returns_none_for_foreign_user`, `test_get_user_post_returns_none_for_missing_post`, `test_get_user_post_for_own_post_in_terminal_status` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `PostRepository.get_user_post(post_id, user_id)` — новый публичный метод | SPEC-024 §8 явно требует «get_user_post». Даёт явный API для пользовательских операций (вместо `get_by_id` + ручная проверка) |
| D2 | `get_user_post` фильтрует в SQL, возвращает `None` если пост чужой или не существует | SPEC-024 §7: ошибка не должна раскрывать существование чужого поста; `None` единообразно для обоих случаев |
| D3 | `cancel_post` остался без изменений: атомарный UPDATE с `user_id` и `status=PENDING` уже изолирует | SPEC-013 + SPEC-018 обеспечивают owner-check на уровне SQL |
| D4 | `CancelService.cancel` больше не включает `post_id` в `PostNotFoundForUserError` | SPEC-024 §7: post_id в сообщении позволял отличить «чужой» от «нет такого». `logger.info` сохраняет post_id для диагностики, но это в логах, не в ошибке пользователя |
| D5 | `handlers/cancel.py` использует константу `CANCEL_NOT_FOUND` без ID | Текст для пользователя уже не содержал ID; сервисный текст теперь тоже |
| D6 | `list_pending_by_user` уже фильтрует в SQL — подтверждено тестом изоляции | Изоляция работает и для `MyPostsService`, и для repository напрямую |
| D7 | Тест `test_no_service_exposes_posts_without_user_filter` проверяет контракт: `list_pending` и `get_by_id` без user_id допустимы только для внутреннего использования | Контрактная защита от регрессии: если кто-то добавит публичный метод без user_id, тест напомнит |
| D8 | Тест `test_create_post_for_unknown_user_fails` проверяет FK на `users.id` | SPEC-024 §1: нельзя создать пост от имени несуществующего пользователя — `UserNotFoundError` |

## Связь с другими SPEC

- **SPEC-008 / SPEC-013:** `cancel_post` (атомарный UPDATE) и `claim_for_processing` (атомарный UPDATE) уже реализуют owner-check в SQL.
- **SPEC-017:** `MyPostsService.render_list` использует `list_pending_by_user(user.id)` — изоляция от идементификации по telegram_user_id до внутреннего user_id.
- **SPEC-018:** `CancelService` + `handlers/cancel.py` — owner-check в SQL + безопасный текст ошибки.
- **SPEC-023:** Безопасный текст ошибки дополняет политику «не утекаем секреты»: post_id в логах допустим (для диагностики), post_id в сообщении пользователю — нет.

## Изменения в существующих файлах

1. `src/postmaster/repositories/post_repository.py`:
   - добавлен `get_user_post(post_id, user_id)` с фильтрацией в SQL.
2. `src/postmaster/services/cancel_service.py`:
   - `PostNotFoundForUserError` больше не содержит `post_id` в тексте.
3. `docs/reviews/SPEC-024-review.md` (новый) — этот отчёт.

## Новые файлы

- `tests/test_spec024.py` — 16 тестов: two-user list (4), cancel isolation (4), safe errors (3), get_user_post (4), create_post isolation (1), контрактная проверка (1).

## Ограничения scope

- `PostRepository.get_by_id` остался без изменений и доступен для внутренних
  сервисов (`PublicationService`, `MissedPostHandler`, `SchedulerService`).
  Пользовательские сценарии должны использовать `get_user_post` — это
  зафиксировано в D7 как контрактная проверка.
- Если в будущем добавится новая операция «получить пост пользователя»,
  нужно либо принимать `user_id` аргументом, либо использовать `get_user_post`.

## Команды

```bash
python -m pytest tests/test_spec024.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
