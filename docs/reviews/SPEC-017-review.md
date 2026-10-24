# Проверка SPEC-017 — Команда my_posts

## Объём и результат

Проверена реализация команды `/my_posts`: сервис формирования списка PENDING-публикаций
пользователя, Telegram-обработчик и регистрация команды в боте. Полный прогон на
Python 3.14.6: **485 passed**, 1 skipped. `ruff check .`, `ruff format --check .` — успешно.

Исходный текст SPEC-017: `docs/specs/SPEC-017-команда-my_posts.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Показывается post.id | `test_my_posts_includes_post_id`, `test_my_posts_e2e_via_fake_telegram` | ✅ Формат `ID {post_id}` |
| Показывается локальная дата/время | `test_my_posts_timezone`, `test_my_posts_timezone_different_user` | ✅ `format_local_datetime(post.scheduled_at_utc, post.timezone)` |
| Показывается тип | `test_my_posts_includes_publication_type_label` | ✅ «публикация в канал» / «напоминание» |
| Чужие Posts недоступны | `test_my_posts_isolation` | ✅ Фильтрация по `user_id` в SQL (`list_pending_by_user`) |
| Только PENDING Posts | `test_my_posts_filter_only_pending` | ✅ `WHERE status = 'PENDING'` |
| Сортировка scheduled_at_utc ASC | `test_my_posts_sorting_by_scheduled_at_utc` | ✅ `ORDER BY scheduled_at_utc, id` |
| Часовой пояс пользователя | `test_my_posts_timezone`, `test_my_posts_timezone_different_user` | ✅ Разные пояса дают разное локальное время |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| empty | `test_my_posts_empty`, `test_my_posts_unknown_user_returns_empty` | ✅ |
| sorting | `test_my_posts_sorting_by_scheduled_at_utc` | ✅ |
| timezone | `test_my_posts_timezone`, `test_my_posts_timezone_different_user` | ✅ |
| filtering | `test_my_posts_filter_only_pending` | ✅ |
| isolation | `test_my_posts_isolation` | ✅ |

Дополнительно: `test_my_posts_with_stub_post_listing` (работа с любым PostListing),
`test_handler_register_only_private_chat` (команда только в личных чатах),
`test_my_posts_e2e_via_fake_telegram` (сквозной тест через Long Polling).

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `PostRepository.list_pending_by_user(user_id)` уже реализован — переиспользован | Метод удовлетворяет всем требованиям SPEC-017: фильтр по user_id, статус PENDING, сортировка по `scheduled_at_utc` |
| D2 | Создан `MyPostsService` в слое services | Бизнес-логика вне handler (BR-01): handler только разбирает сообщение и отправляет ответ |
| D3 | Handler использует `chat_types=["private"]` | Решение D14 плана SPEC-003 — команды работают только в личных чатах |
| D4 | Время форматируется по `post.timezone`, а не по текущему пою пользователя | `Post.timezone` фиксируется при создании поста (SPEC-009) — это и есть пояс, который пользователь выбрал для этой публикации |
| D5 | Неизвестный `telegram_user_id` → `EMPTY_LIST`, а не ошибка | Команда не должна падать для незарегистрированного пользователя |
| D6 | Ошибка БД/Telegram → `MY_POSTS_FAILED` + логирование | §6 SPEC-017: контролируемая обработка ошибок, без ложного успеха |
| D7 | Порт `PostListing` вместо прямой зависимости от `PostRepository` | Упрощает подмену в тестах и не привязывает сервис к целому репозиторию |
| D8 | `file_id` в вывод не показывается | Секретные данные не должны попадать в сообщения пользователю |

## Сценарии

### Happy path

```
/my_posts
→ handler: sender.id из message.from_user
→ MyPostsService.render_list(telegram_user_id)
   1. user = user_service.get_by_telegram_id(telegram_user_id)
      → None → EMPTY_LIST
   2. posts = post_repository.list_pending_by_user(user.id)
      → пусто → EMPTY_LIST
   3. для каждого поста: format_local_datetime(post.scheduled_at_utc, post.timezone)
   4. строка "{index}. ID {post_id} — {local_time} ({тип})"
→ handler: bot.send_message(chat.id, text)
```

### Пример вывода

```
Ваши запланированные публикации:

1. ID 42 — 20.10.2026 15:00 (публикация в канал)
2. ID 43 — 21.10.2026 09:30 (напоминание)
```

### Изоляция

```
Пользователь A (telegram_user_id=106) → posts WHERE user_id=A.id
Пользователь B (telegram_user_id=107) → posts WHERE user_id=B.id
Чужие посты не попадают в вывод ни одного из них.
```

## Ограничения scope

- Команда показывает только PENDING-посты. Опубликованные и отменённые не видны
  (SPEC-017 §1 требует именно PENDING).
- Нет пагинации — вывод содержит все PENDING-посты пользователя. Для MVP достаточно.
- Нет кнопки отмены — это отдельная SPEC-018.
- `MyPostsService` не показывает `target_id` (канал) — только тип публикации.
- Пользователь без `/start` (не зарегистрирован) получает `EMPTY_LIST`.

## Команды

```bash
python -m pytest tests/test_spec017.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
