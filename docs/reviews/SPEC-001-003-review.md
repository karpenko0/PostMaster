# Проверка SPEC-001…SPEC-003 по критериям приёмки

Дата: 2026-10-09 (обновление). Первая проверка: 2026-10-08. Ветка: `arena/1b9daf2a-postmaster`. Проверялись код, тесты и документы на `0d9dcbb` и правки обновления (раздел «Что изменено после первой проверки»).

## Итог

| Спецификация | Итог | Что не закрыто |
|---|---|---|
| SPEC-001 Foundation | Все AC, TC и BR выполнены и проверены тестами | AC-05 и TC-02 проверены на fake Telegram API. AC-06: файлы есть, их содержимое проверяет автотест, шаги `Dockerfile` проверены вне Docker. Сборка образа не выполнялась: Docker не установлен |
| SPEC-002 Пользователь и `/start` | Все AC, TC и BR выполнены для личных чатов | AC-03: в группах `/start` не регистрирует и не отвечает. Группы не поддерживаются в MVP (решение D14 SPEC-003, по умолчанию). Нужно подтверждение |
| SPEC-003 FSM сценария | AC-01, AC-02, AC-04, TC-01…TC-03, BR-01, BR-02, BR-04 выполнены. AC-03 и BR-03 выполнены в объёме SPEC-003 | Запись поста в БД откладывается до SPEC-008 (таблица `posts`, решение D9, по умолчанию). Формат даты (D8) временный: спецификации ввода даты в репозитории нет |

Полный набор: **187 passed** (три прогона подряд) на CPython 3.12.11. `ruff check` и `ruff format --check` чистые. TC-03 пройден. Намеренных поломок: 18, все обнаружены тестами.

## Что изменено после первой проверки

1. **D3 пересмотрено (SPEC-002).** Повторный `/start` обновляет `username` и `first_name`, если они изменились, и пишет `updated_at`. Пояс и `created_at` не меняются. Если данные совпали, запись не пишется. Код: `UserService.get_or_create` и новый метод `UserRepository.update_profile`. Если строки нет, метод выбрасывает `RuntimeError`: строки в MVP не удаляются, значит это ошибка данных.
2. **D5 пересмотрено (SPEC-002).** При ошибке регистрации пользователь получает ответ «Не удалось обработать /start. Попробуйте ещё раз немного позже.» Состояние и данные не меняются (`START_FAILED` в `handlers/start.py`).
3. **Группы (SPEC-002 AC-03, SPEC-003 D14).** `test_group_chat_messages_are_ignored` теперь проверяет, что пользователь из группы не записан в `users`.
4. **Тесты.** Добавлены `test_repeated_start_with_new_name_updates_profile_only` (`test_users.py`), `test_repeated_start_stores_changed_name` и доработан `test_failed_registration_keeps_data_and_state` (`test_start_command.py`). Старый `test_repeated_start_returns_same_user_without_duplicate` проверял прежнее правило «имя не меняется». Теперь он проверяет отсутствие дубликата и новые имена.
5. **AC-06 (SPEC-001).** Новый `tests/test_deployment.py`, 5 проверок: наличие файлов, `FROM python:3.12`, `--uid 1000` и `USER postmaster`, `CMD ["python", "-m", "postmaster"]`, `env_file`, `./data:/app/data`, `init`, `stop_grace_period`, а также `.dockerignore` с `.git`, `.env`, `data`, `*.db`.
6. **Документы.** README (строка `/start`), планы SPEC-001…SPEC-003: вопросы переведены в статусы «Закрыто» и «Открыто».

## Что проверено

1. **Тесты и ruff.** Три прогона подряд: 187 passed. Интерпретатор: CPython 3.12.11, собран из исходников в песочнице (OpenSSL 3.0.18, SQLite 3.38.2 из amalgamation на GitHub, zlib 1.3.1). Образ `python:3.12-slim` не использовался, поэтому версия патча в контейнере может отличаться.
2. **AC-01 и AC-02.** `requires-python = ">=3.12"`. Установленные версии: pyTelegramBotAPI 4.37.0, SQLAlchemy 2.1.4, APScheduler 3.11.3, python-dotenv 1.2.4, pydantic 2.14.0. Дополнительно: aiosqlite 0.22.1, pydantic-settings 2.15.0. Lock-файла нет (D8 SPEC-001), поэтому при следующей установке версии могут быть другими.
3. **AC-03.** Консольная точка входа `postmaster` есть. Без `BOT_TOKEN` она завершается кодом 2 с сообщением об ошибке конфигурации.
4. **Схема `users`.** Приложение запущено на временной SQLite с фиктивным токеном. Таблица создана. `PRAGMA table_info` и `PRAGMA index_list` совпадают с SPEC-002 §6. Токен в логе не найден.
5. **AC-06 и dry-run без Docker.** Шаги `Dockerfile` повторены в `/tmp`: `pyproject.toml`, `README.md` и `src` установлены через `pip install .` в чистый venv на Python 3.12.11, затем удалены `src` и `build`. Команда `python -m postmaster` с фиктивным токеном создала `data/postmaster.db` и завершилась кодом 1, потому что Telegram недоступен (сообщение «Запуск невозможен: Telegram API недоступен»). Токен в логе не найден. `docker-compose.yml` разобран через PyYAML: `build`, `env_file`, `volumes`, `restart`, `init`, `stop_grace_period` совпадают с планом.
6. **Мутации.** См. раздел «Мутации».

## SPEC-001 — Foundation

| Критерий | Статус | Основание |
|---|---|---|
| AC-01 Python 3.12+ | ✅ | `requires-python = ">=3.12"`. `test_smoke::test_python_is_312_or_newer` |
| AC-02 Зависимости | ✅ | pyTelegramBotAPI, SQLAlchemy[asyncio], APScheduler, python-dotenv, pydantic (плюс aiosqlite и pydantic-settings). `test_smoke::test_mandatory_dependency_is_installed` (7 случаев) |
| AC-03 Единая точка входа | ✅ | `postmaster = "postmaster.app:main"`. `test_smoke::test_console_script_points_to_app_main`, `test_entrypoint` (запуск через `python -m postmaster`) |
| AC-04 Слои | ✅ | Каталоги bot, handlers, domain, services, publishers, repositories, database, utils. `test_architecture::test_layer_directories_and_root_modules_exist` |
| AC-05 Long Polling | ✅ на fake API | `bot/transport.py`: `bot.polling(non_stop=True)`. `test_polling`, `test_entrypoint`. Реальный `api.telegram.org` не проверен |
| AC-06 Dockerfile и docker-compose.yml присутствуют | ✅ | Файлы есть. `tests/test_deployment.py` проверяет их содержимое. Dry-run шагов `Dockerfile` вне Docker (п. 5 раздела «Что проверено»). Сборка образа не выполнялась: Docker не установлен |
| TC-01 Smoke импорта | ✅ | `test_smoke::test_package_module_imports` (34 модуля) |
| TC-02 Smoke запуска с тестовой конфигурацией | ✅ на fake API | `test_entrypoint::test_full_cycle_polls_and_exits_cleanly_on_sigterm`, `test_app::test_run_starts_and_stops_every_component`. Дополнительно: запуск из установленного пакета с фиктивным токеном, выход с кодом 1 из-за сети (см. п. 5) |
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
| AC-03 Бот отвечает приветствием | ⚠️ личные чаты ✅, группы не поддерживаются | `test_start_command::test_start_greets_new_user_and_waits_for_photo`. В группах `/start` молчит по D14 SPEC-003: `test_dialog_flow::test_group_chat_messages_are_ignored` (проверяет и отсутствие строки в `users`). Решение по умолчанию, нужно подтверждение |
| AC-04 После ответа ожидается фото | ✅ | `test_start_command`: ожидание состояния `WAITING_PHOTO` |
| TC-01 Новый пользователь | ✅ | `test_start_command::test_start_greets_new_user_and_waits_for_photo` |
| TC-02 Повторный `/start` | ✅ | `test_start_command::test_repeated_start_greets_again_without_duplicate`, `test_repeated_start_stores_changed_name` |
| TC-03 Пользователь без username | ✅ | `test_start_command::test_user_without_username_is_registered`, `test_users::test_user_without_username_is_stored_with_empty_username` |
| TC-04 Уникальность `telegram_user_id` | ✅ | `test_users::test_database_rejects_second_row_with_same_telegram_user_id`. В схеме `UNIQUE (telegram_user_id)`. Мутация M6 обнаружена |
| BR-01 `telegram_user_id` уникален | ✅ | Проверяется тем же тестом, что TC-04 |
| BR-02 Повторный `/start` не создаёт пользователя | ✅ | `test_users::test_repeated_start_returns_same_user_without_duplicate`, `test_repeated_start_with_new_name_updates_profile_only` |
| BR-03 После `/start` — `WAITING_PHOTO` | ✅ | `test_dialog_service::test_start_sets_waiting_photo_and_empty_draft` |
| BR-04 Пояс по умолчанию | ✅ | `test_users::test_effective_timezone_falls_back_to_default_when_user_has_none`, `test_config::test_default_timezone_is_read_from_environment` |
| §6 Таблица `users` | ✅ | Реальный запуск: все 7 полей и их типы совпадают с SPEC-002 §6. Уникальность через autoindex. `updated_at` меняется при обновлении профиля (D3) |

## SPEC-003 — FSM сценария создания публикации

| Критерий | Статус | Основание |
|---|---|---|
| AC-01 Три состояния | ✅ | `test_dialog_fsm::test_dialog_has_exactly_three_states` |
| AC-02 Переходы только по сценарию | ✅ | `test_dialog_fsm::test_transition_table_matches_scenario_exactly`, `test_only_scenario_transitions_are_allowed` (9 случаев). Мутации M1–M3 обнаружены |
| AC-03 После успешного создания поста — `IDLE` | ⚠️ в объёме SPEC-003 | Состояние `IDLE` ✅: `test_dialog_service::test_valid_date_creates_post_and_returns_to_idle`. Запись поста в БД отложена до SPEC-008 (решение D9, по умолчанию) |
| AC-04 Новое фото на шаге даты | ✅ | `test_dialog_service::test_new_photo_on_date_step_replaces_previous_and_asks_again`, `test_dialog_flow::test_repeated_photo_replaces_previous_and_asks_date_again`. Мутация M1 обнаружена |
| TC-01 Каждый переход | ✅ | `test_dialog_fsm`: таблица переходов и все пары «состояние — событие» |
| TC-02 Недопустимое сообщение в каждом состоянии | ✅ | `test_dialog_service::test_invalid_message_keeps_state_and_draft` (8 случаев), `test_dialog_flow` (подсказки). Мутация M2 обнаружена |
| TC-03 Повторная фотография в `WAITING_DATETIME` | ✅ | `test_dialog_flow::test_repeated_photo_replaces_previous_and_asks_date_again` |
| BR-01 `/start` переводит в `WAITING_PHOTO` | ✅ | `test_dialog_service::test_start_from_any_state_begins_new_scenario` (3 случая) |
| BR-02 Фото переводит в `WAITING_DATETIME` | ✅ | `test_dialog_service::test_photo_moves_to_waiting_datetime_and_keeps_file` |
| BR-03 Корректная дата и создание поста → `IDLE` | ⚠️ в объёме SPEC-003 | То же, что AC-03. Мутация M3 обнаружена |
| BR-04 Замена фото на шаге даты | ✅ | То же, что AC-04 |
| §7 п. 4: сбой не нарушает данные | ✅ | `test_dialog_service::test_failed_post_creation_keeps_draft_for_retry`. Мутация M7 обнаружена |
| Блокировка на пользователя (D11) | ✅ после исправления | `test_dialog_service::test_parallel_photos_are_handled_one_at_a_time` (мутация M4), `test_dialog_service::test_parallel_dates_create_one_post` (мутация M11) |

## Решения и открытые вопросы

### Решения по умолчанию (подтвердить или отменить)

Спецификации молчат по этим пунктам. Решения записаны в планах, каждое можно отменить отдельным изменением.

| Вопрос | Решение | Где |
|---|---|---|
| `/start` и сценарий в группах | Только личные чаты | SPEC-002 AC-03, SPEC-003 D14, тест `test_group_chat_messages_are_ignored` |
| Запись поста (AC-03, BR-03 SPEC-003) | Пост сохраняется в SPEC-008 вместе с таблицей `posts` | SPEC-003 D9 |
| Обновление `username` и `first_name` при повторном `/start` | Да, `updated_at` меняется, пояс и `created_at` нет | SPEC-002 D3 (пересмотрено) |
| Ответ при ошибке регистрации | Короткий ответ, состояние не меняется | SPEC-002 D5 (пересмотрено) |
| Часовой пояс по умолчанию | `UTC`, пояс задаёт оператор в `.env` | SPEC-002 D2 |
| Состояние `WAITING_PHOTO` и сценарий | В памяти процесса, перезапуск сбрасывает сценарий (указано в README) | SPEC-002 D1, SPEC-003 D2 |
| Альбомы | Каждое фото заменяет предыдущее, пост создаётся из последней фотографии | SPEC-003 BR-04 |
| Тексты подсказок и подтверждения | Оставлены как есть, правятся в `handlers/dialog.py` | SPEC-003 D12 |
| Маскировка токена, PRAGMA SQLite, uid 1000 в Docker | Остаются в SPEC-001, уже реализованы и проверены | SPEC-001 D9 |

### Открытые вопросы (нужен ответ владельца проекта)

1. **`POST_MODE` и политика `PROCESSING`.** Определений нет ни в одном документе репозитория. Нужны до соответствующих функциональных SPEC. SPEC-001 план, §6 п. 4.
2. **Формат и правила даты.** Формат `ДД.ММ.ГГГГ ЧЧ:ММ` в поясе пользователя временный (D8). Нужна спецификация ввода даты и часовых поясов или решение владельца. SPEC-003 план, §6 п. 2.
3. **Таблицы `users` и `posts` (SPEC-007).** Текста SPEC-007 в репозитории нет. Схема `users` совпадает с SPEC-002 §6. Если SPEC-007 её меняет, понадобится миграция (SPEC-027). SPEC-003 план, §6 п. 4.

### Закрыто без изменений

- Площадка развёртывания: Docker на VPS (D7 SPEC-001), подтверждено пользователем.
- Lock-файл зависимостей и Alembic: вводятся вместе с SPEC-027 (D8 SPEC-001).

## Мутации

Каждая мутация применяется к одному файлу, запускается полный набор тестов, затем файл возвращается к исходному состоянию. Скрипты лежат вне репозитория в `/tmp`. M1–M11 получены в первой проверке. M12–M18 получены в обновлении, там файл восстанавливается из памяти, потому что изменения ещё не закоммичены. Код мест, которых касаются M1–M11, в обновлении не менялся.

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
| M12 | Профиль не обновляется при повторном `/start` | 3 failed | — |
| M13 | Запись профиля даже при неизменённых данных | 1 failed | — |
| M14 | `first_name` не записывается при обновлении | 3 failed | — |
| M15 | `updated_at` не записывается при обновлении | 1 failed | — |
| M16 | Часовой пояс сбрасывается при обновлении профиля | 1 failed | — |
| M17 | Ответ `START_FAILED` не отправляется | 1 failed | — |
| M18 | Dockerfile работает от root (`USER root`) | 1 failed | — |

## Исправления первой проверки (2026-10-08)

1. **`test_parallel_photos_are_handled_one_at_a_time`.** Тест проходил без блокировки (мутация M4: 179 passed). Причина: хранилище в памяти не уступает управление event loop, поэтому два вызова выполнялись подряд и гонка не возникала. Добавлен `YieldingDialogStore`, который уступает управление перед чтением и записью состояния. Теперь M4 обнаруживается.
2. **Новый тест `test_parallel_dates_create_one_post`.** Блокировка при создании поста (мутация M11: 179 passed) тестами не проверялась. Тест проверяет, что две корректные даты, присланные подряд, создают один пост, а вторая получает `INVALID_MESSAGE`.

Код приложения в этих исправлениях не менялся.

## Что не проверено

- Реальный `api.telegram.org`: сеть среды его не открывает. AC-05 и TC-02 проверены на fake API. В dry-run сетевой отказ ожидаем и не является результатом проверки живого Telegram.
- Сборка Docker-образа и запуск контейнера: Docker в среде не установлен, образ `python:3.12-slim` не загружался.
- Поведение в группах на живом Telegram: проверено только на fake API.
- Тексты SPEC-004…SPEC-038 и SPEC-007 в репозитории отсутствуют. Сверка выполнена только по SPEC-001…SPEC-003.
- Windows не проверялся. DST относится к SPEC-006.

## Изменённые файлы

- Код: `src/postmaster/services/user_service.py`, `src/postmaster/repositories/user_repository.py`, `src/postmaster/handlers/start.py`.
- Тесты: `tests/test_users.py`, `tests/test_start_command.py`, `tests/test_dialog_flow.py`, `tests/test_deployment.py` (новый).
- Документы: `README.md`, `docs/plans/SPEC-001-plan.md`, `docs/plans/SPEC-002-plan.md`, `docs/plans/SPEC-003-plan.md`, `docs/reviews/SPEC-001-003-review.md`.
- Первая проверка: `tests/test_dialog_service.py` (`YieldingDialogStore`, тест на параллельные даты).
