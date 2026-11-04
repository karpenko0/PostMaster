# План разработки SPEC-016 «Абстракция Publisher»

- Спецификация: [docs/specs/SPEC-016.md](../specs/SPEC-016.md)
- Статус: ✅ реализовано и проверено

## 1. Цель

Сформировать единый контракт публикационного слоя (`Publisher`, `PublishResult`, `PublisherResolver`), чтобы `SchedulerService`, `Recovery`, `MissedJobService` и `PublicationService` не зависели от деталей конкретного способа доставки (`ReminderPublisher`, `TelegramPublisher`, а в дальнейшем `VKPublisher`).

## 2. Принятые решения

| № | Решение | Обоснование |
|---|---|---|
| D1 | Реализовать `PublisherResolver` в `src/postmaster/publishers/resolver.py` с методом `resolve(publication_type, *, post_id=None) -> Publisher` и методом `register(publication_type, publisher)` | §10–12, §16, §20: хранит заранее созданные через DI экземпляры `ReminderPublisher` и `TelegramPublisher` без пересоздания при вызове и позволяет регистрировать новые реализации (`VKPublisher`) без правок `SchedulerService` |
| D2 | Выделить доменные исключения `PublisherNotFoundError` (`ERR-016-01: PUBLISHER_NOT_FOUND`) и `InvalidPublicationTypeError` (`ERR-016-02: INVALID_PUBLICATION_TYPE`) с логированием по §22 | §12, §21, §22: гарантирует, что при неизвестном или некорректном `publication_type` вызов `publish()` не выполняется (`AC-016-04`, `BR-016-08`) |
| D3 | Интегрировать `PublisherResolver` в `PublicationService` через параметр `publisher_resolver` (и свойство `publisher_resolver`) с сохранением обратной совместимости | §13–16, §25: `PublicationService` выполняет atomic claim, запрашивает `Publisher` у `PublisherResolver`, вызывает `publish(post)` и по `PublishResult` вызывает `mark_published` либо error flow |
| D4 | В `Application.from_settings` создавать `ReminderPublisher`, `TelegramPublisher`, передавать их в `PublisherResolver` и инжектировать `PublisherResolver` в `PublicationService` | §16, §27: формирует эталонную цепочку DI `Application → Publishers → PublisherResolver → PublicationService → SchedulerService` |

## 3. Этапы

1. Сохранить спецификацию в `docs/specs/SPEC-016.md`.
2. Реализовать `PublisherResolver`, `PublisherResolverErrorCode`, `PublisherNotFoundError` и `InvalidPublicationTypeError` в `src/postmaster/publishers/resolver.py` и экспортировать их в `src/postmaster/publishers/__init__.py`.
3. Обновить `PublicationService` (`src/postmaster/services/publication_service.py`) и `Application.from_settings` (`src/postmaster/app.py`) для работы через `PublisherResolver`.
4. Покрыть unit, integration и E2E-сценарии в `tests/test_spec016.py`.
