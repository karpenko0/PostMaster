# Проверка SPEC-013 — защита от двойной публикации

## Объём и результат

Проверена реализация `claim_for_processing` в `PostRepository` и его использование в
`PublicationService`. Полный прогон на Python 3.14.6: **407 passed**, 1 skipped.
`ruff check .`, `ruff format --check .` — успешно.

Исходный текст SPEC-013: `docs/specs/SPEC-013-защита-от-двойной-публикации.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Atomic UPDATE PENDING → PROCESSING | `test_claim_atomic_pending_to_processing` | ✅ Один условный UPDATE с `WHERE status = PENDING` |
| rowcount=1 даёт право на execution | `test_concurrent_claims_only_one_succeeds`, `test_second_claim_returns_none` | ✅ Из 10 конкурирующих вызовов ровно один возвращает Post |
| Final transitions PROCESSING → PUBLISHED/FAILED/PENDING(retry) | `test_publication_service_failed_status_on_error`, `test_publication_service_retry_resets_to_pending`, `test_publication_service_all_attempts_failed` | ✅ Все три финальных перехода работают |
| Publisher вызывается только после успешного claim | `test_publication_service_calls_publisher_only_after_successful_claim`, `test_publication_service_concurrent_publish_calls_publisher_once` | ✅ Из двух конкурирующих publish Publisher вызван 1 раз |
| Cancel и execute конкурируют атомарно | `test_cancel_race_with_claim` | ✅ Cancel и claim используют одну и ту же условную конструкцию `WHERE status = PENDING` |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| atomic claim | `test_claim_atomic_pending_to_processing` | ✅ |
| second claim | `test_second_claim_returns_none`, `test_concurrent_claims_only_one_succeeds` | ✅ |
| concurrency | `test_concurrent_claims_only_one_succeeds`, `test_publication_service_concurrent_publish_calls_publisher_once` | ✅ |
| cancel race | `test_cancel_race_with_claim` | ✅ |
| final status | `test_publication_service_failed_status_on_error`, `test_publication_service_retry_resets_to_pending`, `test_publication_service_all_attempts_failed` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `claim_for_processing` — отдельный метод, а не флаг в `update_status` | §1 SPEC-013: «Atomic UPDATE PENDING→PROCESSING; rowcount=1 даёт право на execution» — это отдельная операция, а не общий переход статуса |
| D2 | `claim_for_processing` возвращает `Post \| None`, а не `bool` | Успешный claim сразу даёт доменный объект без второго запроса; None однозначно означает «не забрали» |
| D3 | `PublicationService` НЕ делает pre-check `if post.status != PENDING` | SPEC-013 §1: единственный источник истины — `rowcount=1`. Pre-check создаёт TOCTOU-окно |
| D4 | `PostClaimLostError` отдельно от `PostNotPendingError` | Различаем «пост не существует», «пост отменён/выполняется» и «другой исполнитель уже забрал» |
| D5 | Retry делает повторный `claim_for_processing` | После возврата в PENDING нужен новый atomic claim — иначе нарушается §1 |
| D6 | Cancel и claim используют один и тот же `WHERE status = PENDING` | Обе операции конкурируют за единственный PENDING row; rowcount=1 гарантирует взаимное исключение |

## Сценарии

### Happy path

```
1. PublicationService.publish(post_id)
2. get_by_id(post_id) — проверка существования
3. claim_for_processing(post_id) — atomic UPDATE PENDING→PROCESSING
4. rowcount=1 → исполнитель получил право
5. Publisher.publish(post) — отправка
6. update_status(PUBLISHED) — финальный переход
```

### Конкурирующие исполнители

```
T1: claim_for_processing(id=1) → rowcount=1 → claimed=Post(...)
T2: claim_for_processing(id=1) → rowcount=0 → None → PostClaimLostError
```

### Cancel race

```
T1: cancel_post(id=1, user_id) → UPDATE WHERE status=PENDING → rowcount=1 → CANCELLED
T2: claim_for_processing(id=1) → rowcount=0 → None → PostClaimLostError
```

(или наоборот — ровно один из двух выигрывает)

### Retry

```
1. claim → PROCESSING
2. Publisher.publish() → ошибка
3. update_status(PENDING) — промежуточный переход для retry
4. claim → PROCESSING (повторный claim)
5. Publisher.publish() → успех
6. update_status(PUBLISHED)
```

## Ограничения scope

- `claim_for_processing` не различает «отменён» и «уже забрали» — оба возвращают None.
  Для разделения этих причин нужно дополнительное чтение, но это вне scope SPEC-013.
- SQLite обеспечивает сериализацию через глобальную блокировку; в PostgreSQL
  поведение аналогично благодаря `UPDATE ... WHERE` с условием.
- `PostClaimLostError` не прерывает работу всего приложения — только текущий вызов.

## Команды

```bash
python -m pytest tests/test_spec013.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
