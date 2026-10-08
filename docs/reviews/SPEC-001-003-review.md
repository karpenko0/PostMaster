# Проверка SPEC-001…SPEC-003 по критериям приёмки

Дата: 2026-10-08. Ветка: `arena/1b9daf2a-postmaster`. Проверялись код и тесты на `849363b` и исправления этой проверки.

## Итог

| Спецификация | Итог | Что не закрыто |
|---|---|---|
| SPEC-001 Foundation | Все AC, TC и BR выполнены и проверены тестами | AC-05 и TC-02 проверены на fake Telegram API. AC-06: файлы есть, сборка образа не проверялась (Docker не установлен) |
| SPEC-002 Пользователь и `/start` | Все AC, TC и BR выполнены, кроме одного отступления | AC-03: в группах бот не отвечает приветствием (D14 SPEC-003). Текст SPEC-002 о группах молчит, нужно подтверждение |
| SPEC-003 FSM сценария | AC-01, AC-02, AC-04, TC-01…TC-03, BR-01, BR-02, BR-04 выполнены | AC-03 и BR-03 выполнены частично: состояние `IDLE` есть, запись поста в БД нет (D9, таблица `posts` относится к SPEC-008) |

Найдены слабые тесты блокировок D11. Тест параллельных фото проходил даже без блокировки в `DialogService`. Блокировка при создании поста тестами вообще не проверялась. Оба исправлены в `tests/test_dialog_service.py`. Код приложения не менялся.

Полный набор: **180 passed** (три прогона подряд). `ruff check` и `ruff format --check` чистые. TC-03 пройден.

## Что проверено

1. Полный набор тестов и ruff (три прогона).
2. Сверка каждого AC, TC и BR с конкретными тестами (разделы ниже).
3. AC-01 и AC-02: `pyproject.toml` и установленные версии в venv на Python 3.12.15: pyTelegramBotAPI 4.37.0, SQLAlchemy 2.1.4, APScheduler 3.11.3, python-dotenv 1.2.4, pydantic 2.13.5. Дополнительно: aiosqlite 0.22.1, pydantic-settings 2.15.0.
4. AC-03: консольная точка входа `postmaster` есть. Без `BOT_TOKEN` она завершается кодом 2 с сообщением об ошибке конфигурации.
5. Схема `users`: приложение запущено на временной SQLite с фиктивным токеном. Таблица создана, после чего запуск завершился кодом 1, потому что Telegram недоступен. `PRAGMA table_info` и `PRAGMA index_list` совпадают с SPEC-002 §6. Токен в логе не найден.
6. AC-06: `Dockerfile` и `docker-compose.yml` прочитаны. Содержимое соответствует комментариям в файлах. Сборка не запускалась, Docker в среде отсутствует.
7. Мутационная проверка: 11 намеренных поломок, каждая откатывается `git checkout`. Результаты в разделе «Мутации».

## SPEC-001 — Foundation

| Критерий | Статус | Основание |
|---|---|---|
| AC-01 Python 3.12+ | ✅ | `requires-python = ">=3.12"`. `test_smoke::test_python_is_312_or_newer` |
| AC-02 Зависимости | ✅ | pyTelegramBotAPI, SQLAlchemy[asyncio], APScheduler, python-dotenv, pydantic (плюс aiosqlite и pydantic-settings). `test_smoke::test_mandatory_dependency_is_installed` (7 случаев) |
| AC-03 Единая точка входа | ✅ | `postmaster = "postmaster.app:main"`. `test_smoke::test_console_script_points_to_app_main`, `test_entrypoint` (запуск через `python -m postmaster`) |
| AC-04 Слои | ✅ | Каталоги bot, handlers, domain, services, publishers, repositories, database, utils. `test_architecture::test_layer_directories_and_root_modules_exist` |
| AC-05 Long Polling | ✅ на fake API | `bot/transport.py`: `bot.polling(non_stop=True)`. `test_polling`, `test_entrypoint`. Реальный `api.telegram.org` не проверен |
| AC-06 Dockerfile и docker-compose.yml | ✅ по тексту критерия | Критерий требует наличия файлов. Dockerfile: `python:3.12-slim`, пользователь uid 1000, `CMD python -m postmaster`. Compose: build, env_file, volume `./data`, init, stop_grace_period 30s. Сборка не проверялась. Автотеста на наличие файлов нет. Ранее в памяти статус был «частично»; по тексту AC-06 это выполнено |
| TC-01 Smoke импорта | ✅ | `test_smoke::test_package_module_imports` (34 модуля) |
| TC-02 Smoke запуска с тестовой конфигурацией | ✅ на fake API | `test_entrypoint::test_full_cycle_polls_and_exits_cleanly_on_sigterm`, `test_app::test_run_starts_and_stops_every_component` |
| TC-03 Нет циклов между слоями | ✅ | `test_architecture`: циклы между модулями и между слоями, матрица зависимостей. Мутация M10 обнаружена |
| BR-01 Логика не в handlers | ✅ | `test_architecture::test_business_rules_on_direct_dependencies`. Мутация M9 обнаружена |
| BR-02 Данные через Repository | ✅ | Матрица: services → repositories. SQLAlchemy импортируется только в database и repositories. Мутация M9 обнаружена |
| BR-03 Публикация через Publisher | ✅ контракт | `publishers/base.py`: `Publisher.publish(post)` абстрактен. `test_contracts::test_publisher_is_abstract`, `test_concrete_publisher_implements_publish`. Реальная публикация не входит в SPEC-001 |
| BR-04 Scheduler через сервис, без Telegram API | ✅ | `SchedulerService` находится в services. Сервисы не импортируют telebot. Мутация M8 обнаружена |
| BR-05 Long Polling в MVP | ✅ | `Transport` и реализация через polling. `test_contracts::test_polling_transport_satisfies_transport_protocol` |

## SPEC-002 — Пользователь и `/start`

| Критерий | Статус | Основание |
|---|---|---|
| AC-01 Создаётся один раз | ✅ | `test_users::test_new_user_is_created_once`, `test_users::test_parallel_registrations_create_one_row` |
| AC-02 Повторный `/start` без дубликата | ✅ | `test_users::test_repeated_start_returns_same_user_without_duplicate`, `test_start_command::test_repeated_start_greets_again_without_duplicate`. Мутация M6 обнаружена |
| AC-03 Бот отвечает приветствием | ⚠️ личные чаты ✅, группы — отступление | `test_start_command::test_start_greets_new_user_and_waits_for_photo`. В группах `/start` молчит по D14 SPEC-003: `test_dialog_flow::test_group_chat_messages_are_ignored` |
| AC-04 После ответа ожидается фото | ✅ | `test_start_command`: ожидание состояния `WAITING_PHOTO` |
| TC-01 Новый пользователь | ✅ | `test_start_command::test_start_greets_new_user_and_waits_for_photo` |
| TC-02 Повторный `/start` | ✅ | `test_start_command::test_repeated_start_greets_again_without_duplicate` |
| TC-03 Пользователь без username | ✅ | `test_start_command::test_user_without_username_is_registered`, `test_users::test_user_without_username_is_stored_with_empty_username` |
| TC-04 Уникальность `telegram_user_id` | ✅ | `test_users::test_database_rejects_second_row_with_same_telegram_user_id`. В схеме `UNIQUE (telegram_user_id)`. Мутация M6 обнаружена |
| BR-01 `telegram_user_id` уникален | ✅ | Проверяется тем же тестом, что TC-04 |
| BR-02 Повторный `/start` не создаёт пользователя | ✅ | `test_users::test_repeated_start_returns_same_user_without_duplicate` |
| BR-03 После `/start` — `WAITING_PHOTO` | ✅ | `test_dialog_service::test_start_sets_waiting_photo_and_empty_draft` |
| BR-04 Пояс по умолчанию | ✅ | `test_users::test_effective_timezone_falls_back_to_default_when_user_has_none`, `test_config::test_default_timezone_is_read_from_environment` |
| §6 Таблица `users` | ✅ | Реальный запуск: все 7 полей и их типы совпадают с SPEC-002 §6. Уникальность через autoindex |

## SPEC-003 — FSM сценария создания публикации

| Критерий | Статус | Основание |
|---|---|---|
| AC-01 Три состояния | ✅ | `test_dialog_fsm::test_dialog_has_exactly_three_states` |
| AC-02 Переходы только по сценарию | ✅ | `test_dialog_fsm::test_transition_table_matches_scenario_exactly`, `test_only_scenario_transitions_are_allowed` (9 случаев). Мутации M1–M3 обнаружены |
| AC-03 После успешного создания поста — `IDLE` | ⚠️ частично | Состояние `IDLE` ✅: `test_dialog_service::test_valid_date_creates_post_and_returns_to_idle`. Запись поста в БД ❌: `PostService.create_post` только собирает объект `Post` (D9), таблица `posts` относится к SPEC-008 |
| AC-04 Новое фото на шаге даты | ✅ | `test_dialog_service::test_new_photo_on_date_step_replaces_previous_and_asks_again`, `test_dialog_flow::test_repeated_photo_replaces_previous_and_asks_date_again`. Мутация M1 обнаружена |
| TC-01 Каждый переход | ✅ | `test_dialog_fsm`: таблица переходов и все пары «состояние — событие» |
| TC-02 Недопустимое сообщение в каждом состоянии | ✅ | `test_dialog_service::test_invalid_message_keeps_state_and_draft` (8 случаев), `test_dialog_flow` (подсказки). Мутация M2 обнаружена |
| TC-03 Повторная фотография в `WAITING_DATETIME` | ✅ | `test_dialog_flow::test_repeated_photo_replaces_previous_and_asks_date_again` |
| BR-01 `/start` переводит в `WAITING_PHOTO` | ✅ | `test_dialog_service::test_start_from_any_state_begins_new_scenario` (3 случая) |
| BR-02 Фото переводит в `WAITING_DATETIME` | ✅ | `test_dialog_service::test_photo_moves_to_waiting_datetime_and_keeps_file` |
| BR-03 Корректная дата и создание поста → `IDLE` | ⚠️ частично | То же, что AC-03. Мутация M3 обнаружена |
| BR-04 Замена фото на шаге даты | ✅ | То же, что AC-04 |
| §7 п. 4: сбой не нарушает данные | ✅ | `test_dialog_service::test_failed_post_creation_keeps_draft_for_retry`. Мутация M7 обнаружена |
| Блокировка на пользователя (D11) | ✅ после исправления | `test_dialog_service::test_parallel_photos_are_handled_one_at_a_time` (мутация M4), `test_dialog_service::test_parallel_dates_create_one_post` (мутация M11) |

## Исправления в этой проверке

1. **`test_parallel_photos_are_handled_one_at_a_time`.** Тест проходил без блокировки (мутация M4: 179 passed). Причина: хранилище в памяти не уступает управление event loop, поэтому два вызова выполнялись подряд и гонка не возникала. Добавлен `YieldingDialogStore`, который уступает управление перед чтением и записью состояния. Теперь M4 обнаруживается.
2. **Новый тест `test_parallel_dates_create_one_post`.** Блокировка при создании поста (мутация M11: 179 passed) тестами не проверялась. Тест проверяет, что две корректные даты, присланные подряд, создают один пост, а вторая получает `INVALID_MESSAGE`.

Код приложения не менялся.

## Мутации

Скрипт `/tmp/mutate_review.py` лежит вне репозитория. Каждая мутация применяется к одному файлу, запускается полный набор тестов, затем файл откатывается через `git checkout`.

| № | Что сломано | Результат | После исправления |
|---|---|---|---|
| M1 | Нет перехода `WAITING_DATETIME` + `PHOTO` | 5 failed | — |
| M2 | Лишний недопустимый переход `IDLE` + `PHOTO` | 4 failed | — |
| M3 | `VALID_DATETIME` ведёт в `WAITING_PHOTO` | 5 failed | — |
| M4 | Нет блокировки в `receive_photo` | **179 passed, не обнаружена** | 1 failed ✅ |
| M5 | `/start` без `chat_types=["private"]` | 1 failed | — |
| M6 | Нет UNIQUE у `telegram_user_id` | 4 failed | — |
| M7 | Черновик очищается до создания поста | 1 failed | — |
| M8 | `import telebot` в services | 2 failed | — |
| M9 | Handlers импортируют repositories | 2 failed | — |
| M10 | Цикл между модулями services | 1 failed | — |
| M11 | Нет блокировки в `receive_text` | **179 passed, не обнаружена** | 1 failed ✅ |

## Отступления и решения, которые нужно подтвердить

1. **SPEC-002 AC-03 и группы (D14 SPEC-003).** SPEC-002 о группах не говорит, а AC-03 сформулирован без исключений. Сейчас `/start` и шаги сценария работают только в личных чатах. Варианты: оставить D14, или отвечать приветствием и в группах без запуска сценария.
2. **SPEC-003 AC-03 и BR-03: запись поста.** Сейчас «Публикация создана» — только ответ бота. SPEC-003 §6 запрещает новые постоянные таблицы, а `posts` относится к SPEC-008. Варианты: принять до SPEC-008, или ввести `posts` уже сейчас (это нарушит §6).
3. **Открытые вопросы SPEC-003 (план, раздел 6):**
   - формат даты `ДД.ММ.ГГГГ ЧЧ:ММ` в поясе пользователя временный; какая SPEC описывает ввод даты и часовые пояса;
   - тексты подсказок и подтверждения: нужна ли правка формулировок;
   - альбомы: каждое фото заменяет предыдущее; нужна ли обработка альбома целиком;
   - состояние диалога хранится в памяти, поэтому при перезапуске бота сценарий сбрасывается. План не решает, устраивает ли это.
4. **Таблицы `users` и `posts`.** SPEC-001 §6 относит `users` к SPEC-007, а SPEC-002 описывает `users` сама. Текста SPEC-007 в репозитории нет. Если SPEC-007 меняет `users`, понадобится миграция. Для сверки нужен текст SPEC-007.

## Что не проверено

- Реальный `api.telegram.org`: сеть среды его не открывает. AC-05 и TC-02 проверены только на fake API.
- Сборка Docker-образа: Docker в среде не установлен.
- Тексты SPEC-004…SPEC-038 и SPEC-007 в репозитории отсутствуют. Сверка выполнена только по SPEC-001…SPEC-003.
- Windows и DST не проверялись. DST относится к SPEC-006.

## Открытые вопросы из предыдущих этапов (ответа нет, ничего не изменено)

- `POST_MODE` и политика `PROCESSING` нужны до функциональных SPEC.
- Убрать ли из фундамента часть требований SPEC-023…028: маскировку токена, PRAGMA SQLite, uid 1000 в Docker. Сейчас всё это реализовано.

## Изменённые файлы

- `tests/test_dialog_service.py`: исправлен тест параллельных фото, добавлены `YieldingDialogStore` и тест на параллельные даты.
- `docs/reviews/SPEC-001-003-review.md`: этот отчёт.
