# Проверка SPEC-027 — SQLite и Repository layer

## Объём и результат

SPEC-027 была реализована ранее в нескольких инкрементах (SPEC-007 users,
SPEC-008 posts, SPEC-013 атомарный claim, SPEC-018 атомарная отмена).
Функциональность соответствует спецификации; добавлен консолидирующий
тестовый файл, закрывающий все обязательные проверки §8 и приёмку §7
в одном месте.

- `tests/test_spec027.py` — 27 тестов.

Полный прогон тестов: **742 passed**, 1 skipped. `ruff check .` и
`ruff format --check .` — успешно.

Исходный текст SPEC-027: `docs/specs/SPEC-027-sqlite-и-repository-layer.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Handlers не выполняют SQL | `test_handlers_do_not_import_sqlalchemy`, `test_handlers_do_not_contain_sql_statements`, `test_handlers_depend_only_on_services` | ✅ Handlers обращаются только к сервисам; SQL, `session_factory` и `repositories` в слое handlers отсутствуют |
| Commit Post до Scheduler | `test_post_is_committed_before_scheduler_registration`, `test_scheduler_failure_keeps_committed_post` | ✅ В момент вызова `schedule_post` строка уже зафиксирована в БД (проверено отдельным подключением к файлу SQLite); при ошибке Scheduler запись сохраняется и восстанавливается через Recovery |
| Restart сохраняет данные | `test_data_survives_engine_reopen`, `test_schema_is_not_dropped_on_reopen` | ✅ Данные и статусы переживают закрытие/открытие БД; повторный `create_schema` ничего не удаляет |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| CRUD | `test_user_crud`, `test_post_crud`, `test_update_status_on_missing_post_raises`, `test_invalid_transition_is_rejected` | ✅ 4 теста |
| persistence reopen | `test_data_survives_engine_reopen`, `test_schema_is_not_dropped_on_reopen` | ✅ 2 теста |
| unique user | `test_duplicate_telegram_user_id_is_rejected`, `test_database_enforces_unique_telegram_user_id`, `test_duplicate_detection_against_raw_constraint` | ✅ 3 теста: на уровне репозитория и на уровне БД |
| atomic claim/cancel | `test_claim_is_atomic_and_single_winner`, `test_concurrent_claim_has_single_winner`, `test_cancel_is_atomic_and_single_winner`, `test_cancel_after_claim_does_not_succeed`, `test_claim_after_cancel_does_not_succeed` | ✅ 5 тестов, включая конкурентные |

Дополнительно: constraints, транзакции, индексы — 9 тестов
(`test_database_rejects_invalid_status`, `test_database_rejects_published_without_timestamp`,
`test_database_rejects_telegram_post_without_target`, `test_foreign_key_is_enforced`,
`test_repository_rejects_post_for_unknown_user`, `test_indexes_exist_for_queue_and_user_listing`,
`test_failed_write_leaves_no_partial_data`, `test_status_update_is_a_single_atomic_statement`).

## Соответствие бизнес-правилам §4

| Бизнес-правило | Реализация |
|---|---|
| SQLAlchemy+SQLite persistent DB | `database/engine.py` — `create_database_engine` с `aiosqlite`, WAL, `busy_timeout=5000`, `foreign_keys=ON` |
| UserRepository/PostRepository | `repositories/user_repository.py`, `repositories/post_repository.py` — наружу только доменные объекты |
| Transactions | Каждый вызов репозитория — собственная сессия с `session.begin()`; при ошибке транзакция откатывается целиком |
| Constraints | 6 `CheckConstraint` на `posts` (status, publication_type, file_id, unique_id, timezone, telegram_target, published_at) + `UNIQUE(telegram_user_id)` на `users` + `FOREIGN KEY posts.user_id → users.id` с `ondelete=RESTRICT` |
| Atomic status updates | Один условный `UPDATE ... WHERE id=? AND status=<прежний>` с проверкой `rowcount == 1`: `claim_for_processing`, `update_status`, `cancel_post` |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | Консолидирующий тестовый файл вместо дублирования существующих | Функциональность SPEC-027 уже покрыта (`test_post_repository_spec008.py`, `test_users.py`, `test_database.py`). Новый файл собирает обязательные проверки §8 в одном месте — это читаемый чек-лист соответствия |
| D2 | Атомарность проверяется конкурентными вызовами (`asyncio.gather` из 5 claim'ов/cancel'ов) | Проверка не только «второй вызов возвращает None», но и реальная конкуренция: ровно один победитель |
| D3 | Порядок «commit до Scheduler» проверяется отдельным синхронным подключением к файлу SQLite | `schedule_post` — синхронный callback, а `get_by_id` — асинхронный. Чтение из другого соединения доказывает, что транзакция уже зафиксирована, а не просто инициирована |
| D4 | Guard-тесты на исходниках handlers вместо только архитектурного теста | `test_architecture.py` проверяет слои целиком; здесь — точечная проверка §7 «Handlers не выполняют SQL»: нет импортов SQLAlchemy, нет SQL-строк, нет импорта репозиториев |
| D5 | Проверка импорта repositories ограничена строками импортов | Слово «repositories» встречается в докстроках handlers («Handler не обращается к repositories») — это документация контракта, а не нарушение |
| D6 | `IntegrityError` вместо `Exception` в тесте транзакционности | Точная причина отката: нарушение `CheckConstraint`. `pytest.raises(Exception)` проходил бы и при любой другой ошибке |
| D7 | Alembic-миграции не добавляются | SPEC-027 не требует миграций, в зависимостях проекта Alembic нет. `create_all` создаёт только отсутствующие таблицы и не трогает существующие (проверено `test_schema_is_not_dropped_on_reopen`). Миграции — задача этапа развития схемы |

## Связь с другими SPEC

- **SPEC-007 / SPEC-008:** `users` и `posts` — предмет этой SPEC.
- **SPEC-013:** `claim_for_processing` — атомарный claim, `PostClaimLostError` при проигрыше.
- **SPEC-018:** `cancel_post` — атомарная отмена с owner-фильтром (заодно реализует изоляцию SPEC-024).
- **SPEC-011:** Recovery восстанавливает PENDING-посты после перезапуска — работает на данных, переживших reopen.
- **SPEC-009:** `PostService.create_scheduled_post` — коммит в БД до регистрации задания; при ошибке Scheduler запись сохраняется (`SchedulerRegistrationError`), чтобы Recovery её подхватил.
- **SPEC-024:** `get_user_post`, `list_pending_by_user` — фильтрация по `user_id` в SQL.
- **SPEC-026:** `DATABASE_URL` приходит из `Settings`; `database/engine.py` отвечает за разбор URL и понятную ошибку `DatabaseUrlError`.

## Новые файлы

- `tests/test_spec027.py` — 27 тестов.
- `docs/reviews/SPEC-027-review.md` — этот отчёт.

Изменения в существующих файлах приложения не потребовались: SPEC-027
соответствует текущей реализации.

## Ограничения scope

- Миграций схемы (Alembic) нет: `create_all` не умеет менять существующие
  таблицы. Для MVP с фиксированной схемой это достаточно; при изменении
  колонок потребуется миграция.
- SQLite: единственный процесс-писатель. При масштабировании на несколько
  инстансов нужен PostgreSQL — строка подключения уже параметризована.
- `busy_timeout=5000` и WAL снимают типичные конфликты polling/Scheduler,
  но не устраняют их полностью: при высокой конкуренции возможен
  `OperationalError`, который сервисы обрабатывают через error flow.
- Тесты конкуренции используют один event loop и одну БД — это проверка
  атомарности на уровне SQL, а не нагрузочный тест.

## Команды

```bash
python -m pytest tests/test_spec027.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
