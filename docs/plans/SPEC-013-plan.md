# План разработки SPEC-013 «Защита от двойной публикации»

- Спецификация: [docs/specs/SPEC-013.md](../specs/SPEC-013.md)
- Статус: ✅ реализовано и проверено

## 1. Цель

Исключить повторную или одновременную публикацию одного и того же `Post` при запуске из любого источника (Scheduler, Recovery, Missed Job, конкурентные воркеры) с помощью атомарного условного перехода `PENDING → PROCESSING` в БД и централизованного выполнения в `PublicationService`.

## 2. Принятые решения

| № | Решение | Обоснование |
|---|---|---|
| D1 | `PostRepository.claim_post_for_processing` выполняет один условный `UPDATE posts SET status='PROCESSING', updated_at=:now WHERE id=:post_id AND status='PENDING'` без предварительного `SELECT` | §14–16, §40–42: исключает гонку чтения перед записью и взаимную блокировку апгрейда транзакции в SQLite |
| D2 | Финальные переходы `mark_published`, `mark_failed` и `release_post_for_retry` выполняются условным `UPDATE ... WHERE id=:post_id AND status='PROCESSING'` | §21–30, INV-013-06: защищает от перезаписи неактуального статуса и возвращает `bool` по `rowcount == 1` |
| D3 | `PublicationService.execute(post_id)` (и алиас `execute_post`) централизует поток: `claim` → `get_by_id` → выбор `Publisher` → `publish` → финальный статус | §17–20: Scheduler, Recovery и Missed Job не переводят запись в `PROCESSING` напрямую |
| D4 | Разделение окончательных и временных ошибок: по умолчанию `TemporaryPublicationError` (или атрибут `is_temporary`/`retryable`) и передаваемый предикат `is_temporary_error` возвращают пост в `PENDING`; остальные исключения переводят пост в `FAILED` | §25–30, §60, подготовка к интеграции с retry-политикой SPEC-022 |
| D5 | `SchedulerService` принимает опциональный `publication_service` и делегирует `_on_post_due(post_id)` в `publication_service.execute(post_id)`; без него сохраняется поведение SPEC-009 до подключения конкретных Publisher (SPEC-014/015) | §19, обратная совместимость с существующими тестами SPEC-009 |

## 3. Этапы

1. Сохранить спецификацию в `docs/specs/SPEC-013.md`.
2. Добавить `PublicationErrorCode` и `TemporaryPublicationError` в `src/postmaster/domain/post.py`.
3. Реализовать `claim_post_for_processing`, `mark_published`, `mark_failed`, `release_post_for_retry` в `src/postmaster/repositories/post_repository.py` с логированием по §43–46.
4. Реализовать `PublicationService` в `src/postmaster/services/publication_service.py` и интеграцию в `src/postmaster/services/scheduler_service.py`.
5. Покрыть тестами unit, repository, concurrency (корутины и потоки ОС), integration (Scheduler, Recovery, Missed Job, Cancel) и E2E-сценарии в `tests/test_spec013.py`.
