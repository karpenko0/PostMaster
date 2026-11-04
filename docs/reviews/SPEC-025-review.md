# Проверка SPEC-025 — Логирование

## Объём и результат

Реализовано структурированное логирование канонических событий приложения.
Добавлены:

- `log_event(logger, event, **fields)` — структурированная запись формата
  `event=NAME key=value key=value …`.
- Словарь канонических событий `EVENT_*` (startup, post_created, claim, publish_ok,
  scheduler_*, recovery_*, publication_*, retry, publish_*, command_*, error).
- `truncate_for_log` и `caption_for_log` — защита длинных и чувствительных полей.
- Миграция канонических `logger.*` вызовов на `log_event` в 10 модулях.
- `tests/test_spec025.py` — 27 тестов.

Полный прогон тестов: **689 passed**, 1 skipped. `ruff check .` и
`ruff format --check .` — успешно.

Исходный текст SPEC-025: `docs/specs/SPEC-025-логирование.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Не логируются токены | `test_token_redacted_in_event_values`, `test_token_redacted_by_pattern_in_event_values` | ✅ Редактирование в `RedactingFormatter` покрывает и значения полей событий |
| Не логируется binary фото | `test_file_id_not_logged_in_events`, `test_photo_service_does_not_log_caption` | ✅ Логируются только размеры (`width`/`height`), не `file_id` и не данные изображения |
| Не логируются credentials | `test_token_redacted_in_event_values` | ✅ `Settings.secret_values` (SPEC-023) + `RedactingFormatter` |
| Не логируется полный caption | `test_caption_for_log_truncates_long_text`, `test_caption_never_logged_in_full`, `test_photo_service_does_not_log_caption` | ✅ Caption в логах отсутствует; `caption_for_log` даёт обрезанный вид |
| Logging не меняет бизнес-state | `test_logging_does_not_change_state`, `test_log_event_helper_is_side_effect_free` | ✅ Статус поста и состав записей БД не меняются от логирования |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| events | `test_log_event_format`, `test_log_event_first_token_is_event`, `test_startup_event_emitted`, `test_post_created_event_contains_post_id`, `test_claim_event_contains_post_id`, `test_publish_ok_event_contains_post_id`, `test_post_failed_event_is_error_level`, `test_scheduler_start_and_stop_events`, `test_recovery_events`, `test_publication_events_on_success`, `test_retry_event_emitted_with_delay`, `test_publish_telegram_event_contains_post_id`, `test_command_events_defined` | ✅ 14 тестов, все 9 категорий SPEC §1 |
| error_code / post_id | `test_publication_error_event_has_error_code`, `test_post_created_event_contains_post_id`, `test_claim_event_contains_post_id`, `test_publish_ok_event_contains_post_id`, `test_retry_event_emitted_with_delay` | ✅ Ошибки содержат `error_code`, посты — `post_id` |
| token redaction | `test_token_redacted_in_event_values`, `test_token_redacted_by_pattern_in_event_values` | ✅ |
| caption protection | `test_caption_for_log_returns_dash_for_none`, `test_caption_for_log_truncates_long_text`, `test_caption_for_log_keeps_short_text`, `test_truncate_for_log_respects_limit`, `test_caption_never_logged_in_full`, `test_photo_service_does_not_log_caption` | ✅ |

## Событийный словарь (SPEC-025 §1)

| Категория SPEC §1 | Событие | Модуль |
|---|---|---|
| startup | `startup`, `startup_error` | `app.py` |
| Post create | `post_created` | `repositories/post_repository.py` |
| Scheduler | `scheduler_start`, `scheduler_stop`, `scheduler_job_added`, `scheduler_job_removed` | `services/scheduler_service.py` |
| Recovery | `recovery_start`, `recovery_done`, `recovery_error` | `services/recovery_service.py` |
| claim | `claim`, `claim_skipped` | `repositories/post_repository.py` |
| publish | `publish_ok`, `publish_telegram`, `publish_reminder`, `publication_start`, `publication_ok` | `post_repository.py`, `publishers/*`, `services/publication_service.py` |
| retry | `retry`, `publication_attempt_failed` | `services/publication_service.py` |
| commands | `command_start`, `command_cancel`, `command_my_posts`, `command_help`, `command_failed` | `handlers/*.py` |
| errors | `publish_error`, `publication_failed`, `post_failed`, `startup_error`, `recovery_error`, `command_failed` | по всем слоям |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | Формат `event=NAME key=value key=value` | Первый токен фиксирован — события ищутся grep'ом и парсятся без знания кода. Остальные поля свободно расширяются |
| D2 | `log_event` — единственная точка для структурированных событий | Единый формат вместо разнородных `logger.info("... %s ...", ...)` |
| D3 | Редактирование секретов осталось в `RedactingFormatter`, а не в `log_event` | Секреты маскируются на выводе, поэтому любое поле события безопасно — включая `file_id` и URL с токеном |
| D4 | `EVENT_*` — константы вместо строковых литералов | Единый словарь событий; опечатка в имени события становится ошибкой импорта, а не молчаливым расхождением |
| D5 | `truncate_for_log` / `caption_for_log` в `utils/logging_config.py` | Защита §7: длинные значения обрезаются с маркером `...`; `None` → `-` |
| D6 | В publisher'ах `event=publish_error` логируется **до** ветвления по типу ошибки | Раньше PERMISSION/TIMEOUT/BLOCKED логировались свободным текстом, а `publish_error` — только для остальных кодов. Теперь событие единообразно для всех ошибок и содержит `error_code` |
| D7 | `publication_attempt_failed` и `publication_failed` содержат `error_code` | SPEC §8: `error_code` обязателен в событиях ошибок. Для `TelegramAPIError` берётся `exc.error.code.value`, для прочих — имя типа |
| D8 | `retry` — отдельное событие с `attempt`, `next_attempt`, `delay_seconds` | SPEC §1 перечисляет retry отдельной категорией; раньше задержка не была видна в логах |
| D9 | `handlers/help.py` логирует `command_help` | Ранее `/help` вообще не оставлял следов в логах — команда не попадала в §1 «commands» |
| D10 | В `handlers/start.py` и `cancel.py` сохранён `logger.exception` рядом с `log_event` | Трассировка нужна для диагностики, а структурированное событие — для парсинга. `logger.exception` печатает traceback, но не содержит токенов |
| D11 | Не мигрированы не-канонические вызовы (`photo_service`, `user_service`, `dialog_service`, `post_service`) | SPEC §1 перечисляет 9 категорий; остальные вызовы — вспомогательные, их миграция не влияет на соответствие SPEC и увеличила бы diff без выгоды |

## Связь с другими SPEC

- **SPEC-023:** `RedactingFormatter` + `Settings.secret_values` редактируют секреты в любых полях событий.
- **SPEC-021:** `TelegramErrorMapper` даёт `error_code` (TIMEOUT/NETWORK/PERMISSION/…), который пишется в `publish_error`.
- **SPEC-022:** событие `retry` фиксирует `attempt` и `delay_seconds` — фактическая задержка повтора видна в логах.
- **SPEC-013:** `claim` и `claim_skipped` показывают, кто получил право на публикацию.
- **SPEC-017/018/019:** команды логируются как `command_*` — можно посчитать использование команд.

## Изменения в существующих файлах

1. `src/postmaster/utils/logging_config.py` — `log_event`, `truncate_for_log`,
   `caption_for_log`, словарь `EVENT_*`.
2. Миграция на `log_event`:
   - `src/postmaster/app.py` — `startup`, `startup_error`;
   - `src/postmaster/repositories/post_repository.py` — `post_created`, `claim`,
     `claim_skipped`, `publish_ok`, `post_failed`, `post_cancelled`, `status_changed`;
   - `src/postmaster/services/scheduler_service.py` — `scheduler_*`;
   - `src/postmaster/services/recovery_service.py` — `recovery_*`;
   - `src/postmaster/services/publication_service.py` — `publication_*`, `retry`;
   - `src/postmaster/publishers/telegram_publisher.py` — `publish_telegram`, `publish_error`;
   - `src/postmaster/publishers/reminder_publisher.py` — `publish_reminder`, `publish_error`;
   - `src/postmaster/handlers/start.py`, `cancel.py`, `my_posts.py`, `help.py` — `command_*`.

## Обновление существующих тестов

Три теста проверяли свободный текст логов и переведены на структурированный формат:

1. `tests/test_post_repository_spec008.py::test_create_get_by_id_and_utc_roundtrip` —
   `event=post_created` + `post_id=`/`user_id=`/`status=PENDING`.
2. `tests/test_spec007_flow.py::test_failed_registration_does_not_log_personal_data_or_create_fictive_id` —
   `event=command_failed` + `command=start` + `telegram_user_id=`.
3. `tests/test_start_command.py::test_failed_registration_keeps_data_and_state` —
   `event=command_failed` в предикате ожидания лога.

Проверка «персональные данные не логируются» в этих тестах сохранена: имя
пользователя по-прежнему не попадает в лог.

## Новые файлы

- `tests/test_spec025.py` — 27 тестов.
- `docs/reviews/SPEC-025-review.md` — этот отчёт.

## Ограничения scope

- Логи пишутся в stdout в виде текста, а не JSON. Для MVP достаточно; переход
  на JSON-формат потребует смены `LOG_FORMAT` и `RedactingFormatter`.
- Отладочные сообщения SQLAlchemy/aiosqlite на уровне DEBUG печатают SQL с
  параметрами (включая caption). Это поведение сторонней библиотеки, не
  логирование приложения; в production `LOG_LEVEL=INFO` его не видно.
- Счётчики и метрики по событиям не ведутся — только логи.

## Команды

```bash
python -m pytest tests/test_spec025.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
