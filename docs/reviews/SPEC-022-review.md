# Проверка SPEC-022 — Retry-механизм

## Объём и результат

Проверена реализация retry-механизма: задержки +30s/+2m/+10m, повтор только для
retryable ошибок, atomic claim на каждой попытке, FAILED после исчерпания.
Полный прогон на Python 3.14.6: **608 passed**, 1 skipped. `ruff check .`,
`ruff format --check .` — успешно.

Исходный текст SPEC-022: `docs/specs/SPEC-022-retry-механизм.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Retry только для retryable errors | `test_permanent_error_marks_failed_immediately`, `test_blocked_bot_error_not_retried` | ✅ Permission/Blocked → FAILED сразу |
| Задержки +30s, +2m, +10m | `test_retry_policy_default_delays`, `test_retry_policy_delay_after_attempt` | ✅ `RetryPolicy.DEFAULT_DELAYS = (30, 120, 600)` |
| Затем FAILED | `test_retry_limit_persistent_error_marks_failed` | ✅ 4 попытки, затем FAILED |
| Каждый retry через atomic claim | `test_retry_uses_atomic_claim_on_each_attempt`, `test_concurrent_retries_use_atomic_claim` | ✅ После каждой неудачи — reset PENDING → re-claim PROCESSING |
| Новый Post не создаётся | `test_retry_does_not_create_new_post` | ✅ Работаем с тем же post_id |
| scheduled_at_utc не меняется | `test_retry_does_not_change_scheduled_at_utc` | ✅ Не модифицируется |
| Success → PUBLISHED | `test_retry1/2/3_succeeds_after_failures`, `test_success_on_first_attempt_no_retries` | ✅ |
| Permanent error → FAILED | `test_permanent_error_marks_failed_immediately` | ✅ |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| retry1 | `test_retry1_succeeds_after_first_failure` | ✅ |
| retry2 | `test_retry2_succeeds_after_two_failures` | ✅ |
| retry3 | `test_retry3_succeeds_after_three_failures` | ✅ |
| limit | `test_retry_limit_persistent_error_marks_failed` | ✅ 4 попытки → FAILED |
| permanent error | `test_permanent_error_marks_failed_immediately`, `test_blocked_bot_error_not_retried` | ✅ |
| cancel between retries | `test_cancel_between_retries_stops_publisher_calls` | ✅ |
| concurrency | `test_concurrent_retries_use_atomic_claim` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | RetryPolicy хранит `delays: tuple[int, ...]` вместо одного `delay_seconds` | SPEC-022 §4: разные задержки для каждого повтора (+30s, +2m, +10m) |
| D2 | `max_attempts = 1 + len(delays)` — суммарное число попыток | Первая попытка без delay + три retry |
| D3 | `RetryPolicy(delays=())` отключает повторы | Простой способ выключить retry для тестов и специальных случаев |
| D4 | `_DEFAULT_RETRYABLE = (TimeoutError, ConnectionError, TelegramTimeoutError)` — консервативный default | Не повторяем неизвестные ошибки — избегаем бесконечных ретраев на программных ошибках |
| D5 | `error_mapper.map_exception` используется, если передан | Более точная классификация (TIMEOUT/NETWORK/UNAVAILABLE/PERMISSION/BLOCKED) |
| D6 | `TelegramErrorMapper.map_exception` возвращает уже нормализованную ошибку из `TelegramAPIError.error` | Не переклассифицируем то, что уже нормализовано в Publisher'е |
| D7 | Application.from_settings создаёт PublicationService с `RetryPolicy()` (default delays) | Спецификация §4: задержки по умолчанию именно +30s, +2m, +10m |
| D8 | Ошибка TelegramTimeoutError теперь в `_DEFAULT_RETRYABLE` | Publisher оборачивает TimeoutError в TelegramTimeoutError — без этого retry не сработал бы без mapper'а |

## Сценарии

### Happy path: 1-я попытка успешна

```
attempt=1: Publisher.publish() → success
→ status: PUBLISHED (без retry)
```

### Retry path: timeout, потом success

```
attempt=1: Publisher → TelegramTimeoutError → retryable=True, attempt<delays
  → sleep delays[0] = 30s (или 0s в тестах)
  → update_status PENDING
  → claim_for_processing → success
attempt=2: Publisher → success
→ status: PUBLISHED
```

### Permanent error: сразу FAILED

```
attempt=1: Publisher → TelegramPermissionError → retryable=False
→ status: FAILED (без retry)
```

### Retry exhausted: 4 попытки → FAILED

```
attempts 1..4: Publisher → TelegramTimeoutError
  1→sleep 30s, 2→sleep 2m, 3→sleep 10m
  attempt=4: max_attempts → should_retry=False
→ status: FAILED
```

### Cancel between retries

```
attempt=1: Publisher → TelegramTimeoutError → retryable=True
  → status: PENDING
  → sleep delays[0] (начался)
  → user cancels post → status: CANCELLED
  → sleep завершился
  → claim_for_processing → rowcount=0 (CANCELLED != PENDING)
  → PostClaimLostError → propagate
```

## Ограничения scope

- Используется `asyncio.sleep` для задержки между повторами — на production это
  блокирует event loop на суммарно 12 минут (30s + 2m + 10m). Для MVP приемлемо,
  т.к. только один пост обрабатывается атомарно. В будущем можно перейти на
  Scheduler-based retry: сохранить PENDING + schedule_post(now + delay).
- Retry работает только для первой публикации поста. Если перезапуск приложения
  перепланировал overdue посты через RecoveryService, retry там не происходит —
  считается одной попыткой.
- Лимит retries жёстко задан в `RetryPolicy.DEFAULT_DELAYS`. Конфигурировать через
  переменные окружения можно, но это вне scope MVP.

## Команды

```bash
python -m pytest tests/test_spec022.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
