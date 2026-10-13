# Проверка SPEC-008 — модель данных публикаций

> Исторический отчёт до SPEC-009: замечания о неподключённом Telegram-диалоге
> относятся к прежнему состоянию. Интеграция описана в [проверке SPEC-009](SPEC-009-review.md).

## Объём и результат

Проверена реализация модели `Post`, таблицы `posts`, репозитория, запуска схемы, регрессия ранее реализованных сценариев. Полный прогон на Python 3.12.11: **332 passed**, без падений и пропусков. `ruff check .`, `ruff format --check .`, `git diff --check` — успешно.

> В репозитории **нет исходного `docs/specs/SPEC-008.md`**. Таблица ниже сверяет доступные требования к SPEC-008 и её заявленный scope; она не выдаёт себя за дословную сверку всех пронумерованных AC оригинального текста, которого здесь нет.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| `posts` создаётся при старте; `user_id` с FK на `users.id`, требуемые и необязательные поля | `test_schema_has_all_fields_constraints_fk_and_indexes`, `test_user_fk_is_enforced_by_repository_and_database`, `test_spec008_posts_survive_application_restart` | ✅ Новая SQLite-БД и реальный startup |
| Файловые идентификаторы обязательны; типы и статусы ограничены | `test_file_ids_cannot_be_blank`, `test_invalid_input_does_not_create_post`, `test_database_rejects_unknown_status_type_and_published_without_timestamp`, `test_database_defaults_and_check_constraints_even_without_repository` | ✅ Репозиторий/домен и CHECK БД |
| UTC-момент, часовой пояс, системные даты и индексы очереди | `test_system_timestamps_must_be_aware_utc`, `test_create_get_by_id_and_utc_roundtrip`, `test_schema_has_all_fields_constraints_fk_and_indexes`, `test_pending_post_survives_new_engine_and_session` | ✅ На границе репозитория aware UTC; SQLite хранит даты без tzinfo, пояс — отдельным полем |
| `REMINDER` и `TELEGRAM`; для второго обязателен target; caption необязателен | `test_exact_enum_values`, `test_telegram_post_requires_target_and_optional_caption`, `test_telegram_target_and_iana_timezone`, `test_database_defaults_and_check_constraints_even_without_repository` | ✅ |
| Создание: `PENDING`, нет ошибки и `published_at`, существует владелец | `test_new_post_defaults_and_optional_fields`, `test_create_get_by_id_and_utc_roundtrip`, `test_user_fk_is_enforced_by_repository_and_database`, `test_database_defaults_and_check_constraints_even_without_repository` | ✅ В том числе серверный default БД |
| `get_by_id`, списки только ожидающих, фильтрация владельца и порядок времени | `test_create_get_by_id_and_utc_roundtrip`, `test_pending_lists_filter_owner_status_and_order` | ✅ `get_by_id` — внутренний, **без** проверки владельца; проверка нужна вызывающему пользовательскому сервису |
| Разрешённые переходы, ошибка у `FAILED`, время публикации только у `PUBLISHED`, сохранение временных меток | `test_status_transitions_published_failed_retry_and_timestamps`, `test_invalid_transitions_do_not_modify_post`, `test_database_rejects_unknown_status_type_and_published_without_timestamp` | ✅ |
| Отмена только своей `PENDING`, повторная/конкурентная отмена и отмена после обработки не проходят | `test_cancel_requires_owner_and_pending_status`, `test_concurrent_cancellation_only_succeeds_once`, `test_cancel_does_not_interrupt_processing_or_finished_posts` | ✅ В SQLite при проверенных условиях |
| Перезапуск не теряет запись и очередь | `test_pending_post_survives_new_engine_and_session`, `test_spec008_posts_survive_application_restart` | ✅ Чтение и восстановление данных, не выполнение задач Scheduler |
| Прежний диалог не сломан | Полная регрессия; `test_spec005_flow.py`, `test_spec006_flow.py`, `test_dialog_flow.py` | ✅ Сценарий по-прежнему не создаёт запись в `posts` |

## Ограничения, не засчитываемые как выполненные

- Подключение Telegram-диалога к `PostRepository` — SPEC-009; текущий ответ «Публикация создана» возвращает временный `PostDraft` и **не** означает сохранение. Автоматическое recovery-планирование, Scheduler, Publisher и команды `/my_posts`/`/cancel` в боте тоже не подключены.
- Изоляция в `list_pending_by_user` и `cancel_post` подтверждена; `get_by_id` специально не авторизует доступ. Будущий пользовательский сервис обязан проверять владельца (SPEC-024).
- Тесты не подтверждают работу с Telegram API, PostgreSQL, Docker и миграцию ранее существовавших колонок. `create_all` добавляет отсутствующую `posts`, но изменение старой схемы `users` требует SPEC-027.
- Дословную сверку каждого AC/TC/IT из исходного текста SPEC-008 нельзя закончить, пока сам текст не сохранён в репозитории или повторно не предоставлен для сравнения.

## Команды

```bash
/tmp/venv312/bin/python -m pytest -q -p no:cacheprovider
/tmp/venv312/bin/ruff check .
/tmp/venv312/bin/ruff format --check .
git diff --check
```
