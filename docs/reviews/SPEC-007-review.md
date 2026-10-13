# Приёмка SPEC-007 — модель данных пользователей

## Итог

Модель `users` на новых БД, регистрация/обновление `/start`, nullable Telegram-поля, поиск, timezone и защита от дублей реализованы. **Полная приёмка SPEC-007 отложена:** физической модели `posts` ещё нет (SPEC-008), создание/привязка поста и изоляция — SPEC-009/024; изменения существующей SQLite-колонки `first_name NOT NULL` требуют миграции SPEC-027.

## Сверка с критериями

| Требования | Проверка | Статус |
|---|---|---|
| US-007-01/02, AC-007-01/02, TC-007-01/02, IT-007-01/02, E2E §55/56 | `test_start_with_missing_name_then_profile_changes_preserve_user_id_and_timezone`, `test_start_greets_new_user_and_waits_for_photo`, `test_repeated_start_greets_again_without_duplicate`, `test_none_name_and_username_are_stored_and_found_by_telegram_id` | ✅ Для новой SQLite-таблицы |
| §8–12, §17/18, §39, BR-007-01/02/09/10, INV-007-01/05 | `test_users_schema_matches_spec_and_unique_index_exists`, `test_large_telegram_id_is_preserved`, `test_profile_updates_nulls_and_preserves_identity_timezone_and_created_at`, прежние тесты на BIGINT/UNIQUE/даты | ✅ На новой схеме: INTEGER PK, BIGINT UNIQUE NOT NULL, даты NOT NULL |
| AC-007-03, ERR-007-02, CT-007-01, §38 | `test_concurrent_nullable_profile_creation_is_one_row` (8 конкурентных вызовов), старый `test_parallel_registrations_create_one_row` (5 вызовов), тест DB UNIQUE | ✅ Уникальность обеспечивает БД; сервис повторно читает найденную запись |
| AC-007-04/05, BR-007-04/05, TC-007-03 | `test_none_name_and_username_are_stored_and_found_by_telegram_id`, E2E с `first_name=null`; без имени приветствие не содержит `None` | ✅ Для входного `null`/аргумента `None`. При полностью отсутствующем ключе first_name pyTelegramBotAPI отказывает ещё до handler (Telegram API требует поле) |
| AC-007-06/07/09, BR-007-08/09/10 (Telegram-поля), TC-007-04/05, IT-007-03, INV-007-03/06 | `test_profile_updates_nulls_and_preserves_identity_timezone_and_created_at`, `test_each_telegram_field_can_change_independently`, E2E изменение и исчезновение username/имени | ✅ users.id, telegram_user_id, created_at, timezone сохраняются; updated_at меняется только при изменении Telegram-полей |
| AC-007-08, BR-007-06, §16/28 | `test_none_name_and_username_are_stored_and_found_by_telegram_id`, прежние тесты SPEC-006 на fallback и приоритет профиля | ✅ timezone=NULL допустима, DEFAULT_TIMEZONE применяется при расчёте; /start не меняет timezone |
| AC-007-11/12, BR-007-03, TC-007-06, INV-007-02 | `test_same_username_does_not_merge_different_telegram_users`, `test_two_users_with_same_username_have_separate_profiles`, сервис/репозиторий `get_by_telegram_id` | ✅ Внешний ключ идентичности — Telegram ID, не username |
| ERR-007-01, §50 | `test_failed_registration_does_not_log_personal_data_or_create_fictive_id`, тест ошибки SPEC-002; логи успеха в `test_profile_updates_nulls_and_preserves_identity_timezone_and_created_at` | ✅ Без выдуманного users.id и SQL-параметров/лишних персональных данных в логах |
| US-007-04, AC-007-10, BR-007-11/12, IT-007-04, INV-007-04, связанный с постами фрагмент E2E §57 | Нет `posts`/FK (`TABLES` содержит только users); существующая заглушка PostService пока передаёт внешний `owner_telegram_id`, не внутренний users.id | ⏳ Реализация/проверка требуют SPEC-008/009/024 |
| §18: обновление `updated_at` при смене timezone | `/start` не меняет timezone (§28), интерфейс его настройки вне scope (SPEC-041) | ⏳ При реализации обновления timezone в SPEC-041 нужно также обновлять `updated_at` |
| Обновление уже существующей SQLite-БД, созданной до SPEC-007 | `test_existing_sqlite_schema_needs_spec027_migration` воспроизводит старое `NOT NULL`: `create_all` сохраняет данные, но не меняет колонку, NULL отвергается БД | ⏳ Требуется версионированная миграция SPEC-027; до неё `/start` с first_name=None на старой БД не проходит |

## Повторная проверка по каждому пункту §51–59

Легенда: ✅ проверено на новой БД; ⏳ зависит от ещё не реализованной спецификации.

| AC | Результат и конкретная проверка |
|---|---|
| AC-007-01 | ✅ Новый `/start` → одна строка `users` (`test_start_greets_new_user_and_waits_for_photo`, `test_start_with_missing_name_then_profile_changes_preserve_user_id_and_timezone`). |
| AC-007-02 | ✅ Повторный `/start` не добавляет строку (`test_repeated_start_greets_again_without_duplicate`). |
| AC-007-03 | ✅ БД отклоняет дубликат, сервис при гонке возвращает существующий row (`test_database_rejects_second_row_with_same_telegram_user_id`, `test_concurrent_nullable_profile_creation_is_one_row`). |
| AC-007-04 | ✅ `username=NULL` сохраняется и читается (`test_none_name_and_username_are_stored_and_found_by_telegram_id`). |
| AC-007-05 | ✅ `first_name=NULL` в новой схеме и в fake Telegram Update не блокирует `/start` (`test_start_with_missing_name_then_profile_changes_preserve_user_id_and_timezone`). На старой БД без миграции — ⏳. |
| AC-007-06 | ✅ Смена username сохраняется (`test_repeated_start_stores_changed_name`, `test_each_telegram_field_can_change_independently`). |
| AC-007-07 | ✅ `users.id` и `telegram_user_id` сохраняются при изменении username (`test_profile_updates_nulls_and_preserves_identity_timezone_and_created_at`). |
| AC-007-08 | ✅ NULL timezone → конфигурационный `DEFAULT_TIMEZONE` (`test_none_name_and_username_are_stored_and_found_by_telegram_id`, тесты SPEC-006). |
| AC-007-09 | ✅ `/start` не перезаписывает timezone (`test_profile_updates_nulls_and_preserves_identity_timezone_and_created_at`, E2E). |
| AC-007-10 | ⏳ `posts.user_id=users.id` — таблица `posts` и FK ещё отсутствуют, текущий DTO содержит внешний `owner_telegram_id` (SPEC-008/009). |
| AC-007-11 | ✅ `get_by_telegram_id` возвращает нужного User либо None (`test_none_name_and_username_are_stored_and_found_by_telegram_id`). |
| AC-007-12 | ✅ Разные Telegram ID с одинаковым username → разные `users.id` (`test_same_username_does_not_merge_different_telegram_users`, E2E). |

| Дополнительные пункты | Статус и свидетельство |
|---|---|
| TC-007-01…06 | ✅ Создание, существующий пользователь, username=None, изменение имени, сохранение timezone, поиск. `test_existing_user_never_calls_create_even_when_profile_changes` дополнительно доказывает, что повторный create **не вызывается** (COUNT=1 недостаточен). |
| IT-007-01…03 | ✅ Сквозные тесты с fake Telegram, UserService, UserRepository и SQLite: первичный `/start`, повтор, изменение username. |
| IT-007-04 | ⏳ Связь записи в posts с users.id — SPEC-008/009. |
| CT-007-01 | ✅ 8 конкурентных вызовов, одна строка; UNIQUE ограничение также проверено отдельно. |
| E2E §55–56 | ✅ Первый и повторный `/start`, ответ и состояние; E2E §57 ✅ изменение username и стабильный ID, ⏳ сохранение связи с постами до SPEC-008/009. |
| ERR-007-01 | ✅ При сбое создания не выдаётся фиктивный ID, ответ об ошибке и запись в логе без SQL-параметров (`test_failed_registration_does_not_log_personal_data_or_create_fictive_id`). |
| ERR-007-02 | ✅ Нарушение UNIQUE преобразуется в `DuplicateTelegramUserError`; сервис возвращает существующего пользователя. |
| ERR-007-03 | ✅ Обновление отсутствующего обязательного User не создаёт фиктивный профиль (`test_update_of_missing_user_does_not_create_ghost_profile`); `/start` вправе создать запись. |
| DoD §59 | ✅ Поля/типы/UNIQUE/nullable/CRUD/тесты пользователей; ⏳ физическая связь User 1:N Posts и полные E2E публикаций до SPEC-008/009; legacy-миграция до SPEC-027. |

## Ограничения и следующие шаги

1. SPEC-008 должна добавить `posts.user_id` с FK на `users.id`; SPEC-009 — преобразовать Telegram ID в внутренний ID при создании поста. Запросы публикаций должны фильтроваться по владельцу (SPEC-024). Сегодня модель временного Post — **не** строка БД.
2. Существующие SQLite-базы с `first_name NOT NULL` не обновляются автоматически: процедура переноса — в SPEC-027. Перед развёртыванием версии с nullable first_name на существующей БД миграция обязательна.
3. Реальная сеть Telegram, Docker и Windows в этой среде не проверялись. Сквозные проверки используют fake Telegram API.

## Прогон

Python **3.12.11**: `python -m pytest -q -p no:cacheprovider` — **297 passed**; `ruff check .` — чисто; `ruff format --check .` — **66 файлов**; `git diff --check` — чисто. Полная регрессия SPEC-001…006 входит в прогон.

Контроль чувствительности: четыре временные мутации с восстановлением кода (first_name снова NOT NULL, пропуск обновления имени, потеря обработки дубля, вывод `None` в приветствии) обнаружены тестами.
