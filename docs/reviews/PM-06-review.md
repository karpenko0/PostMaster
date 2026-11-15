# PM-06 — Отчёт о стабилизации доработок PM-03/04/05

Ветка: `arena/6a51f52e-postmaster` (база `2eeaaf8`, main).
Коммиты: `096acf7` (основные исправления), `98d9ac3` (служебный BOT_TOKEN для миграций).
Push выполнен только в рабочую ветку; merge/deploy не выполнялись.

## 1. Этап 1 — аудит: воспроизводимость дефектов

До изменений полный прогон дал **772 passed, 59 failed, 6 collection errors** —
то есть часть тестов репозитория была сломана ещё до PM-06 (несовместимость старых
тестов SPEC-009/010/013/033 с контрактами PM-03/05: `SchedulerService(publication_executor=…)`,
удалённый `DefaultPublisherResolver`, шаг выбора типа публикации, `Settings(post_mode=…)`).
Это зафиксировано как baseline и использовалось для контроля регрессий.

| ID | Дефект | Подтверждён | Причина в коде |
|----|--------|-------------|----------------|
| BUG-01 | Устаревшие кнопки действуют на другой черновик | Да | `callback_data = "pm05:confirm"/"pm05:cancel"` без привязки к черновику; `confirm_post`/`cancel_creation` не сверяли черновик |
| BUG-02 | Дубликаты публикаций | Да | Post создавался до записи `confirmed_post_id` в FSM; защиты в БД не было (только in-process lock) |
| BUG-03 | Падение/порча preview на пользовательском тексте | Да | caption/канал вставлялись в `parse_mode="Markdown"` без экранирования; fallback повторял ту же разметку; не было лимита caption |
| BUG-04 | Scheduler публикует по старому времени | Да | задание не помнило своё время, `remove_job` терял результат `remove_job()` APScheduler (всегда `None`), ошибка синхронизации при переносе глоталась |
| BUG-05 | Потеря черновика при старте переноса | Да | `start_reschedule` безусловно перезаписывал state+draft |
| BUG-06 | Клавиатура удаляется после ошибки | Да | `edit_message_reply_markup(None)` вызывался до/независимо от результата |
| BUG-07 | Потеря FSM и черновиков после рестарта | Да | `StateMemoryStorage` в памяти процесса |
| — | Повторная валидация времени при подтверждении | Да | отсутствовала |
| — | Проверка владельца/статуса до старта переноса | Да | отсутствовала (только при вводе даты) |
| — | Пагинация `/my_posts` > 20 | Нет | уже реализована (`MY_POSTS_LIMIT`, `hidden`, `TRUNCATION_NOTICE`) — добавлен регрессионный тест |
| — | Ошибки Telegram API при редактировании клавиатуры | Да | не обёрнуты, могли ронять обработчик |
| — | Утечка `DialogService._locks` | Да | `defaultdict` рос без очистки |
| — | Proxy для Telegram (PR #4) | Нет реализации | PR #4 закрыт без merge, другого способа в `main` нет (см. §7) |

Изменения схемы БД (с миграцией и проверкой отката):
`posts.draft_id` (nullable, уникальный индекс `ux_posts_draft_id`), новая таблица
`dialog_states`, служебная таблица `schema_version`.

## 2. Этап 2 — исправления

| ID | Дефект | Причина | Исправление | Тест | Результат |
|----|--------|---------|-------------|------|-----------|
| BUG-01 | Кнопки старого превью подтверждают/отменяют новый черновик | callback не привязан к черновику | `draft_id` в черновике; `callback_data = pm05:<confirm|cancel>:<token>` (16 симв., ≤64 байт); `confirm_post(..., draft_id=)`, `cancel_creation(..., draft_id=)`, исход `STALE_CALLBACK`; неверный/повторный/неизвестный callback обрабатывается отдельно | `test_bug01_*` (7), E2E `test_pm06_e2e_stale_button_cannot_confirm_new_draft` | PASS |
| BUG-02 | Дубликаты публикаций при повторах/рестарте/гонках | Post создавался раньше фиксации в FSM | `posts.draft_id` + уникальный индекс; `PostRepository.create_post(draft_id=…)` возвращает существующий Post при конфликте; `PostService`/`DialogService` передают `draft_id`; старые записи с `NULL` совместимы | `test_bug02_*` (6) | PASS |
| BUG-03 | Ошибки разметки/лимита caption, потеря черновика | нет экранирования и fallback | `escape_markdown` (`_ * `` ` `` [`), лимит caption 1024 без разрыва UTF-8, цепочка фото(Markdown) → фото(plain) → текст(Markdown) → текст(plain), ≤2 попыток, `PREVIEW_FAILED_TEXT` при неудаче | `test_bug03_*` (6 параметров + 4), E2E ошибки sendPhoto/sendMessage | PASS |
| BUG-04 | Публикация по устаревшему времени | у задания нет версии времени | задание помнит `expected_run_at`; `_on_post_due` сверяет статус PENDING и `scheduled_at_utc` с БД, при расхождении перерегистрирует задание; `RescheduleService.reschedule_with_status` → `RescheduleResult.scheduler_synced`, повторная попытка, ошибка не маскируется; `remove_job` различает успех и `JobLookupError` | `test_bug04_*` (6) | PASS |
| BUG-05 | Перенос уничтожает активный черновик | нет проверки состояния | `start_reschedule` разрешён из IDLE/WAITING_PHOTO/WAITING_CONFIRMATION/повторного переноса, отклоняется (`RESCHEDULE_CONFLICT`) при вводе фото/типа/канала/даты; проверка владельца и PENDING до старта | `test_bug05_*` (4), `test_pm06_reschedule_rejects_*` (2), E2E | PASS |
| BUG-06 | Тупик после ошибки | клавиатура снималась всегда | клавиатура снимается только для завершённых исходов, при восстановимой ошибке остаётся; `answerCallbackQuery` с понятным текстом, ошибки Telegram обёрнуты | `test_bug06_callback_outcomes_define_keyboard_lifecycle`, E2E двойного нажатия | PASS |
| BUG-07 | Потеря FSM после рестарта | состояние в памяти | таблица `dialog_states`, `SqlAlchemyStateStorage` (реализует `StateStorageBase`) + `DialogStateRepository`; state и draft в одной транзакции; TTL 24 ч и очистка при старте; общее состояние для нескольких экземпляров; секретов в черновике нет | `test_bug07_*` (6), E2E рестарт | PASS |
| Доп. | Нет проверки времени при подтверждении | превью могло «провиснуть» | `confirm_post` повторно проверяет `parsed_utc > now`, иначе `DATETIME_INVALID` без создания Post, черновик сохранён | `test_pm06_confirm_revalidates_datetime` | PASS |

## 3. Изменённые файлы

Код:
- `src/postmaster/domain/dialog.py` — `draft_token`, `token_matches`.
- `src/postmaster/domain/post.py` — `Post.draft_id`.
- `src/postmaster/database/models.py` — `posts.draft_id` + `ux_posts_draft_id`, `DialogStateModel`.
- `src/postmaster/database/schema.py` — `create_all` + миграции.
- `src/postmaster/database/migrations.py` — **новый**, версионированные миграции и откат.
- `src/postmaster/migrate.py` — **новый**, CLI `upgrade` / `downgrade` / `version`.
- `src/postmaster/repositories/post_repository.py` — идемпотентное создание, `get_by_draft_id`.
- `src/postmaster/repositories/dialog_state_repository.py` — **новый**, порт `DialogStore` на БД.
- `src/postmaster/repositories/state_storage.py` — **новый**, `StateStorageBase` на БД.
- `src/postmaster/services/dialog_service.py` — `draft_id` в исходах, `confirm_post`/`cancel_creation`/`start_reschedule`, `STALE_CALLBACK`, `ALREADY_CONFIRMED`, `RESCHEDULE_CONFLICT`, `_prune_locks`.
- `src/postmaster/services/post_service.py` — `draft_id`, `find_by_draft_id`.
- `src/postmaster/services/reschedule_service.py` — `check_pending`, `reschedule_with_status`, `RescheduleResult`.
- `src/postmaster/services/scheduler_service.py` — версия задания, `_is_job_actual`, `_resync_job`, `resync_post`, корректный `remove_job`.
- `src/postmaster/handlers/dialog.py` — экранирование, лимиты, `_send_preview`/`_safe_send`/`_safe_answer`, callback `pm05:<action>:<token>`, новые тексты.
- `src/postmaster/handlers/post_actions.py` — ответ на конфликт переноса.
- `src/postmaster/bot/factory.py` — возможность передать персистентное хранилище состояний.
- `src/postmaster/app.py` — персистентный FSM, `DialogStateRepository`, очистка TTL при старте.
- `src/postmaster/config.py` — `TOKEN_NOT_REQUIRED` для служебных команд.
- `README.md` — раздел «Стабилизация PM-06», proxy-PR #4, CI.
- `.github/workflows/ci.yml` — **новый**.

Новые миграции: версия 1 — `posts.draft_id` + `ux_posts_draft_id` (обратимая, данные сохраняются);
таблицы `dialog_states` и `schema_version` создаются `create_all`.

Тесты: **новый** `tests/test_pm06.py` (55 тестов: unit, интеграционные, конкурентные, E2E, миграции);
обновлены `tests/fake_telegram.py` (клавиатура в `sendPhoto`), `tests/harness.py` (`meaningful_draft`),
`tests/test_architecture.py`, `tests/test_dialog_service.py`, `tests/test_dialog_flow.py`,
`tests/test_pm05.py`, `tests/test_post_repository_spec008.py`, `tests/test_spec030.py`,
`tests/test_pm04.py`/`tests/test_spec033.py` (только `ruff format`). Существующие тесты не удалялись
и не отключались; правки — минимальная адаптация к новому контракту с комментариями.

## 4. Этап 3 — проверки

Окружение: Python 3.11.2 в песочнице (проект требует ≥3.12). Установлены зависимости из
`pyproject.toml`; пакет установлен с `--ignore-requires-python`, чтобы работали subprocess-тесты
(`python -m postmaster`). Один тест (`test_smoke.py::test_python_is_312_or_newer`) поэтому падает
в песочнице и проходит в CI на 3.12.

Итоговый полный прогон: **847 passed, 1 skipped, 43 failed, 6 collection errors**.
Baseline до изменений: **772 passed, 59 failed, 6 collection errors**.
**Новых падений относительно baseline — 0; 16 ранее падавших тестов теперь проходят.**

`ruff check .` — All checks passed. `ruff format --check .` — 125 files already formatted.

Регрессионный набор PM: `tests/test_pm03.py test_pm04.py test_pm05.py test_pm06.py
test_dialog_flow.py test_dialog_service.py test_dialog_store.py` → **147 passed**.

Миграции: `upgrade → version(=1) → downgrade(=0) → upgrade(=1)` на чистой БД и на «старой»
схеме без `draft_id` (данные сохраняются), покрыты `test_migration_adds_draft_id_to_existing_schema`
и `test_migration_cli_roundtrip_without_bot_token`.

### Неуспешные тесты (все — существовавшие до PM-06)

Причины: контракты PM-03/05 разошлись со старыми тестами и старым кодом ещё в `2eeaaf8`.

- `test_spec010/020/021/022/025/032` (6 collection errors): импорт `DefaultPublisherResolver`,
  `PublicationError`, `RetryPolicy`, `SchedulerService(publication_executor=…)` — этих API нет в `main`.
- `test_spec009/011/013/018` (10): `SchedulerService(publication_executor=…)`, ожидание прямого
  `PostService` из диалога.
- `test_spec005_flow`/`test_spec006_flow` (15): диалог больше не вызывает `PostService` на шаге даты
  (PM-05 вводит шаг превью).
- `test_spec014/015/016` (5) и `test_spec033` (6): сценарии без шага выбора типа публикации (PM-03).
- `test_spec019` (3): ожидают draft без PM-06 маркеров и без шага типа.
- `test_spec026/026 config` (2), `test_architecture` (1): `Settings(post_mode=…)` удалён, `config → domain`
  импорт `parse_allowlist` (предсуществовало).
- `test_unit_qualities[resolver]` (1): требует `DefaultPublisherResolver` в тесте SPEC-016.
- `test_smoke::test_python_is_312_or_newer` — ограничение песочницы (Python 3.11).

Пропущен: **1** (существовавший skip).

## 5. Команды для локальной проверки

```bash
pip install -e ".[dev]" && pip install aiohttp

ruff check .
ruff format --check .
pytest -q
pytest -q tests/test_pm03.py tests/test_pm04.py tests/test_pm05.py tests/test_pm06.py

DATABASE_URL=sqlite+aiosqlite:///data/migrate-check.db python -m postmaster.migrate upgrade
DATABASE_URL=sqlite+aiosqlite:///data/migrate-check.db python -m postmaster.migrate version
DATABASE_URL=sqlite+aiosqlite:///data/migrate-check.db python -m postmaster.migrate downgrade
DATABASE_URL=sqlite+aiosqlite:///data/migrate-check.db python -m postmaster.migrate upgrade
```

## 6. Действия перед production deployment

1. Сделать backup SQLite (`python -m postmaster backup`) — миграция добавляет колонку и индекс.
2. Применить миграции: `python -m postmaster.migrate upgrade` (то же выполнит старт приложения).
   На чистой БД `create_schema` + миграции идемпотентны.
3. Проверить `schema_version=1` и наличие `ux_posts_draft_id`; при необходимости откат —
   `python -m postmaster.migrate downgrade` (индекс и колонка удаляются, таблица публикаций сохраняется).
4. Перезапустить бота: при старте выполняется очистка просроченных черновиков и Recovery заданий.
5. Убедиться, что `DATABASE_URL` указывает на постоянный диск (общий для всех экземпляров):
   персистентный FSM требует общей БД для нескольких инстансов.
6. Оставить `BOT_TOKEN` в окружении/.env; `TOKEN_NOT_REQUIRED` — только для служебных команд.

## 7. Известные оставшиеся риски

- **Исторические падения тестов (43 + 6)** не относятся к PM-06 и не исправлялись: это
  несовместимость старых тестов с PM-03/05. Рекомендуется отдельная задача на их актуализацию;
  до неё CI (`pytest -q`) будет красным, поэтому регрессионные job-ы разделены, а PM-набор зелёный.
- **Proxy для Telegram API не реализован**: PR #4 закрыт без merge, в `main` нет ни `BOT_PROXY_URL`,
  ни `aiohttp-socks`; функциональность не добавлена (требует подтверждения требований и новой
  зависимости). Обходной путь — proxy на уровне инфраструктуры.
- **TTL 24 часа** — эвристика: очень долгий сценарий может быть очищен. Значение вынесено
  параметром `ttl` в `SqlAlchemyStateStorage`/`DialogStateRepository`.
- **Версия задания Scheduler** живёт в памяти задания APScheduler: после рестарта версия ставится
  Recovery по БД, поэтому рассинхронизации нет, но при нескольких процессах с APScheduler задания
  дублируются по времени; защита — атомарный claim в `PostRepository` (PENDING→PROCESSING).
- **Очистка `_locks`** удаляет только свободные блокировки и не является распределённой:
  межпроцессная сериализация обеспечивается БД (уникальный индекс, условные UPDATE).
- Поведение при `parse_mode` ошибке Telegram определяется эвристикой (повтор без разметки), а не
  разбором кода ошибки — это осознанный компромисс без новой зависимости.
