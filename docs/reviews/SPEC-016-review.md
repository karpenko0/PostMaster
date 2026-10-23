# Проверка SPEC-016 — Абстракция Publisher

## Объём и результат

Проверена абстракция Publisher: единый интерфейс `publish(post) -> PublishResult`,
протокол `PublisherResolver` для выбора Publisher по типу публикации, и decoupling
Scheduler/Recovery/MissedJob от конкретных реализаций. Полный прогон на Python 3.14.6:
**470 passed**, 1 skipped. `ruff check .`, `ruff format --check .` — успешно.

Исходный текст SPEC-016: `docs/specs/SPEC-016-абстракция-publisher.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Единый интерфейс `publish(post) -> PublishResult` | `test_publisher_is_abstract`, `test_publish_result_is_dataclass`, `test_concrete_publishers_return_publish_result` | ✅ `Publisher` (ABC) + `PublishResult` (dataclass) |
| PublisherResolver: REMINDER → ReminderPublisher | `test_resolver_routes_reminder_to_reminder_publisher` | ✅ `DefaultPublisherResolver.resolve(REMINDER)` |
| PublisherResolver: TELEGRAM → TelegramPublisher | `test_resolver_routes_telegram_to_telegram_publisher` | ✅ `DefaultPublisherResolver.resolve(TELEGRAM)` |
| Scheduler/Recovery/MissedJob не знают concrete Publisher | `test_scheduler_uses_publication_executor_protocol`, `test_recovery_uses_missed_post_gateway_protocol`, `test_missed_post_handler_uses_publication_executor_protocol` | ✅ Используют протоколы `PublicationExecutor`, `MissedPostGateway` |
| Неизвестный type не публикуется | `test_resolver_unknown_type_rejected`, `test_publication_service_unknown_type_marks_failed` | ✅ `UnknownPublicationTypeError` → пост в FAILED |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| resolver reminder | `test_resolver_routes_reminder_to_reminder_publisher` | ✅ |
| resolver telegram | `test_resolver_routes_telegram_to_telegram_publisher` | ✅ |
| resolver unknown | `test_resolver_unknown_type_rejected`, `test_resolver_unknown_publication_type_keeps_post_safe` | ✅ |
| service success | `test_publication_service_uses_resolver_to_route_reminder`, `test_publication_service_uses_resolver_to_route_telegram`, `test_publication_service_routes_valid_type_after_resolver_update` | ✅ |
| service failure | `test_publication_service_unknown_type_marks_failed`, `test_publication_service_publisher_not_configured_marks_failed` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `Publisher` и `PublishResult` живут в `publishers/base.py` | SPEC-016 §1: «Единый интерфейс publish(post)->PublishResult» — это контракт слоя publishers |
| D2 | `PublisherResolver` перенесён из `services/publication_service.py` в `publishers/resolver.py` | Резолвер выбирает Publisher — это часть абстракции publishers, а не services |
| D3 | Типизированные исключения `UnknownPublicationTypeError` и `PublisherNotConfiguredError` | SPEC-016 §7: «неизвестный type не публикуется» — разные причины отказа требуют разных исключений |
| D4 | `UnknownPublicationTypeError` хранит `publication_type` для диагностики | Тесты и логирование получают конкретное значение |
| D5 | PublicationService ловит только новые исключения, а не общий `PublicationError` | Старый код использовал `PublicationError` как generic — теперь каждое исключение имеет конкретный тип |
| D6 | Scheduler/Recovery/MissedJob используют протоколы, не concrete classes | SPEC-016 §7: decoupling через structural typing (Protocol) |
| D7 | `PublisherNotConfiguredError` — отдельный класс от `UnknownPublicationTypeError` | Различаем «тип не поддерживается» (навсегда) и «publisher не зарегистрирован» (конфигурация) |

## Сценарии

### Happy path

```
PublicationService.publish(post_id):
1. claim_for_processing(post_id) → Post(status=PROCESSING)
2. resolver.resolve(post.publication_type) → Publisher
3. Publisher.publish(post) → PublishResult
4. update_status(PUBLISHED, published_at=now)
```

### Unknown type

```
PublicationService.publish(post_id):
1. claim_for_processing(post_id) → Post(status=PROCESSING)
2. resolver.resolve(post.publication_type) → UnknownPublicationTypeError
3. _mark_failed(post_id, "неизвестный тип публикации: VK")
4. raise PublicationError(post_id, ...)
```

### Publisher not configured

```
PublicationService.publish(post_id):
1. claim_for_processing(post_id) → Post(status=PROCESSING)
2. resolver.resolve(PublicationType.TELEGRAM)
   → PublisherNotConfiguredError (telegram_publisher=None)
3. _mark_failed(post_id, "publisher для типа TELEGRAM не настроен")
4. raise PublicationError(post_id, ...)
```

### Scheduler doesn't know about Publishers

```
SchedulerService._on_post_due(post_id):
1. post = await post_repository.get_by_id(post_id)
2. if post.status != PENDING: return
3. await publication_executor.publish(post_id)
```

Scheduler не импортирует конкретные классы Publisher, только протокол PublicationExecutor.

## Ограничения scope

- DefaultPublisherResolver — единственная реализация. Альтернативы (например, динамическая
  регистрация через entry points) — вне scope MVP.
- Проверка на «неизвестный тип» через `PublicationType` enum: пока что в БД стоит
  CHECK constraint, который запрещает строки вне enum. Но резолвер защищает от
  случая расширения enum без обновления резолвера.
- `PublisherNotConfiguredError` валится в `PublicationError` в PublicationService.
  Это сознательное решение — приложение единообразно реагирует на любую ошибку
  публикации (FAILED + лог).

## Команды

```bash
python -m pytest tests/test_spec016.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
