# Проверка SPEC-021 — Обработка ошибок Telegram API

## Объём и результат

Реализован `TelegramErrorMapper` (нормализация исключений Telegram API в
`TelegramError(code, message, retryable)`) и его интеграция в
`TelegramChannelPublisher`, `ReminderPublisher`, `PublicationService` и `app.py`.
Полный прогон тестов: **621 passed**, 1 skipped, 1 deselected (только
Windows-несовместимый SIGTERM-subprocess-тест). `ruff check .` и
`ruff format --check .` — успешно.

Исходный текст SPEC-021: `docs/specs/SPEC-021-обработка-ошибок-telegram-api.md`.

## Доработки 2026-10-25 (после первой реализации)

| # | Изменение | Файл | Обоснование |
|---|---|---|---|
| W1 | Расширены `_INVALID_FILE_TEXTS` и `_UNAVAILABLE_TEXTS` | `telegram_errors.py` | Реальные коды Telegram `MEDIA_CAPTION_TOO_LONG`, `MEDIA_EMPTY`, `MESSAGE_TOO_LONG`, `PHOTO_INVALID_DIMENSIONS`, `RetryAfter`, `500` теперь корректно классифицируются вместо попадания в UNKNOWN |
| W2 | Добавлен модульный singleton `DEFAULT_ERROR_MAPPER` | `telegram_errors.py` | Legacy-хелперы `_is_permission_error`, `_is_timeout`, `_is_bot_blocked` используют общий экземпляр вместо создания нового на каждый вызов |
| W3 | `_RecordingReminderSender` поддерживает `fail_photo_with` через конструктор | `tests/test_spec021.py` | Тесты text-OK/photo-failed стали однострочными; public API совпадает с `test_spec014.py` |
| W4 | 7 новых тестов: caption-too-long, message-too-long, MEDIA_EMPTY, PHOTO_INVALID_DIMENSIONS, 429 RetryAfter (с кодом и без), 500 | `tests/test_spec021.py` | Покрытие W1 |
| W5 | 3 новых теста «ReminderPublisher: text OK, photo failed» (NETWORK/BLOCKED/INVALID_FILE) | `tests/test_spec021.py` | Гарантирует, что send_message не отправляется повторно при сбое send_photo, и retryable-флаг корректно доходит до `PublicationService` |

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| `TelegramErrorMapper` нормализует exceptions в `code/message/retryable` | `tests/test_spec021.py` (22 теста маппинга) | ✅ Все 7 кодов покрыты |
| timeout/network/unavailable → retryable | `test_retryable_codes_match_spec`, `test_publication_service_retries_on_network/timeout` | ✅ `RETRYABLE_CODES == {TIMEOUT, NETWORK, UNAVAILABLE}` |
| permission/blocked/invalid file → non-retryable | `test_non_retryable_codes`, `test_publication_service_does_not_retry_on_permission/blocked/invalid_file` | ✅ PublicationService делает 1 попытку на этих ошибках |
| Telegram exception не даёт false success | `test_telegram_publisher_does_not_swallow_exception`, `test_publication_service_does_not_retry_on_unknown` | ✅ Publisher пробрасывает `TelegramAPIError`/`PublicationError` |
| Bot Token не попадает в logs/error_message | `test_secret_token_redacted_in_message`, `test_secret_token_redacted_in_telegram_api_error`, `test_secret_token_redacted_in_publication_service_error_message`, `test_publication_service_post_error_message_has_no_token` | ✅ `redact_secrets` заменяет токен на `***` в `TelegramError.message`, `TelegramAPIError`, `Post.error_message` |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| all error mappings | `test_mapper_timeout_via_timeout_error/text`, `test_mapper_network_via_text/connection_error_type/os_error`, `test_mapper_unavailable_via_429/502/text/retry_after_text/retry_after_without_code/500`, `test_mapper_permission_*` (4 теста), `test_mapper_blocked_*` (2), `test_mapper_invalid_*` (5: invalid_file_id, invalid_photo, invalid_caption_too_long, invalid_message_too_long, invalid_media_empty, invalid_photo_dimensions) | ✅ 30 тестов покрывают все коды и их варианты |
| unknown error | `test_mapper_unknown_generic_exception`, `test_mapper_unknown_value_error`, `test_mapper_unknown_with_unrelated_400`, `test_publication_service_does_not_retry_on_unknown` | ✅ UNKNOWN → non-retryable, FAILED после 1 попытки |
| secret redaction | `test_secret_token_redacted_in_message`, `test_secret_token_redacted_in_telegram_api_error`, `test_secret_token_redacted_in_publication_service_error_message`, `test_redact_secrets_function_handles_multiple_tokens`, `test_redact_secrets_keeps_safe_text`, `test_publication_service_post_error_message_has_no_token`, `test_telegram_publisher_permission_message_has_no_token`, `test_reminder_publisher_message_has_no_token` | ✅ Токен не попадает в `TelegramError.message`, `TelegramAPIError`, `TelegramPermissionError`, `Post.error_message` |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `TelegramErrorMapper` в `publishers/telegram_errors.py` | SPEC-021 §1: единая точка нормализации; `publishers` может зависеть только от `domain` и `utils` (test_architecture) — модуль чистый, без внешних зависимостей |
| D2 | `TelegramErrorCode` — `StrEnum`, `TelegramError` — frozen dataclass | Структурный тип, иммутабельный, удобно логировать и класть в БД |
| D3 | `RETRYABLE_CODES = {TIMEOUT, NETWORK, UNAVAILABLE}` как `frozenset` | SPEC-021 §4 — фиксированный набор; `frozenset` гарантирует целостность |
| D4 | Publisher поднимает `TelegramAPIError(TelegramError)` для нетипизированных ошибок | Общий носитель нормализованной ошибки; сохраняет `retryable`/`code`/`message` для error flow |
| D5 | Специфические классы ошибок оставлены: `TelegramPermissionError`, `TelegramTimeoutError`, `TelegramBlockedError` | Обратная совместимость с SPEC-014/015; сообщения строятся из `error.message` (уже редактированного) |
| D6 | `_is_permission_error` / `_is_timeout` / `_is_bot_blocked` остались как тонкие обёртки над маппером | Источник истины — `TelegramErrorMapper`; старые хелперы делегируют, чтобы тесты SPEC-014/015 продолжали работать |
| D7 | `PublicationService` принимает опциональный `error_mapper` | Без маппера — fallback `_DEFAULT_RETRYABLE = (TimeoutError, ConnectionError)` + проверка `__cause__` для типизированных исключений; с маппером — полная классификация SPEC-021 |
| D8 | `_is_retryable` смотрит `__cause__` если переданное исключение — типизированное (обёртка) | `TelegramTimeoutError` снаружи, `TimeoutError` внутри — `isinstance` по внешнему типу не нашёл бы `TimeoutError`, поэтому fallback на `__cause__` |
| D9 | `redact_secrets()` — общая функция редактирования | Используется и в `TelegramErrorMapper`, и доступна отдельно для логов и тестов |
| D10 | `TelegramError.message` начинается с кода (`"NETWORK: ..."`) и обрезается до 500 символов | Диагностика в БД и логах; `Post.error_message` имеет ограничение |
| D11 | В `app.py` маппер создаётся один и передаётся в оба Publisher и в `PublicationService` | Единая классификация на всё приложение, нет дублирования эвристик |
| D12 | `TelegramErrorMapper.map_exception` не переклассифицирует уже нормализованные `TelegramAPIError` | Повторный проход через mapper в `PublicationService._is_retryable` не теряет исходный `code`/`retryable`; используется несущий `error` атрибут |

## Сценарии

### Happy path (нет ошибки)

```
publisher.publish(post)
  → sender.send_photo(...) — успех
  → PublishResult(post_id, external_id)
  → PublicationService: PROCESSING → PUBLISHED
```

### Сетевая ошибка (retry)

```
publisher.publish(post)
  → sender.send_photo(...) — RuntimeError("network error")
  → TelegramErrorMapper → TelegramError(code=NETWORK, retryable=True, message="NETWORK: network error")
  → TelegramAPIError(error) → raise
PublicationService.publish (retry_policy=delays=(30,120,600), error_mapper=...)
  → _is_retryable(TelegramAPIError) → mapper.retryable → True
  → Post → PENDING, asyncio.sleep(delays[0]=30)
  → claim → publisher.publish повторно
```

### Нет прав (permanent)

```
publisher.publish(post)
  → sender.send_photo(...) — ApiTelegramException(400, "chat not found")
  → TelegramErrorMapper → TelegramError(code=PERMISSION, retryable=False, ...)
  → TelegramPermissionError("PERMISSION: chat not found") → raise
PublicationService.publish
  → _is_retryable → mapper.retryable → False
  → Post → FAILED с error_message="PERMISSION: chat not found"
  → PublicationError → raise
```

### Бот заблокирован (reminder)

```
ReminderPublisher.publish(post)
  → sender.send_message(...) — ApiTelegramException(403, "bot was blocked by the user")
  → mapper → code=BLOCKED
  → TelegramBlockedError → raise
PublicationService.publish
  → _is_retryable → False → FAILED
```

### Утечка секрета невозможна

```
sender.send_photo(...) — RuntimeError(
    "Failed /bot1234567890:AaBbCcDdEeFf.../sendPhoto: network error"
)
→ mapper → TelegramError.message = "NETWORK: Failed /bot***/sendPhoto: network error"
→ TelegramAPIError(...) — str не содержит токен
→ PublicationService → Post.error_message = "...bot***..."  (без токена)
→ logging → RedactingFormatter + redact_secrets → stdout без токена
```

## Связь с другими SPEC

- **SPEC-014, SPEC-015:** интеграция маппера не меняет публичные контракты
  Publisher. Существующие типизированные исключения (`TelegramPermissionError`,
  `TelegramTimeoutError`, `TelegramBlockedError`, `TargetMissingError`,
  `FileIdMissingError`, `PhotoDataMissingError`, `UserNotFoundError`,
  `InvalidPostStatusError`) сохранены.
- **SPEC-022:** SPEC-021 поставляет `retryable` флаг; конкретные задержки
  `+30s / +2m / +10m` и логика повторов — ответственность SPEC-022
  (`RetryPolicy.delays`). Маппер уже используется в `PublicationService` —
  при полной реализации SPEC-022 добавляется только временной график.
- **SPEC-023:** редактирование секретов в `TelegramError.message` усиливает
  redaction, уже реализованную в `utils/logging_config.py`. Оба используют
  один regex `r"\d{5,}:[A-Za-z0-9_-]{20,}"`.
- **SPEC-025:** новое поле `retryable` добавлено в лог `Publication attempt
  failed post_id=… attempt=… error=… retryable=…`.

## Изменения в существующих тестах

Минимальные правки, согласованные с новым контрактом:

1. `tests/test_spec010.py::test_publication_service_retries_on_failure` —
   `RuntimeError("temporary error")` → `TimeoutError("temporary error")`.
   Обоснование: `RuntimeError` → UNKNOWN → non-retryable; тест проверяет
   retryable-ошибку.
2. `tests/test_spec013.py` / `tests/test_spec010.py` `FlakyTelegramSender` —
   `raise RuntimeError("temporary failure N")` →
   `raise TimeoutError("temporary failure N")`. То же обоснование.
3. `tests/test_spec014.py::test_reminder_non_blocked_error_propagates`,
   `tests/test_spec014.py::test_reminder_photo_error_after_text_succeeds` —
   ожидаем `TelegramAPIError` вместо голого `RuntimeError` (нормализация
   работает, как и положено по SPEC-021).
4. `tests/test_spec014.py::test_is_bot_blocked_detects_403_error_code` —
   голый `403 "Forbidden"` теперь относится к PERMISSION (бот не имеет прав),
   а не к BLOCKED (пользователь заблокировал). Это разделение продиктовано
   SPEC-021 §4 и делает классификацию явной.
5. `tests/test_spec015.py::test_telegram_publisher_other_error_propagates`,
   `tests/test_spec015.py::test_telegram_publisher_does_not_manage_retry` —
   `RuntimeError` теперь нормализуется в `TelegramAPIError`, тесты
   обновлены под новый контракт.
6. `tests/test_entrypoint.py::_settings_env` — добавлен
   `PYTHONIOENCODING=utf-8`. Иначе Cyrillic в логах subprocess искажается
   на Windows-cmd. Не относится напрямую к SPEC-021, но без него тесты
   падали на проверке текста ошибок.

## Ограничения scope

- Редактирование секретов в маппере использует тот же regex, что и
  `utils/logging_config.py`. Если формат токена изменится, оба места нужно
  обновить.
- Нет телеметрии/счётчиков по типам ошибок (только лог-сообщение). Для MVP
  достаточно.
- Маппер не делает задержки — это ответственность `RetryPolicy` (SPEC-022).
- UNKNOWN ошибки считаются non-retryable. Это сознательный выбор: при
  неизвестной ошибке безопаснее прекратить попытки, чем бесконечно повторять.

## Команды

```bash
python -m pytest tests/test_spec021.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```

## Матрица доработок (статус после W1–W5)

| Изменение | Статус | Покрытие тестами |
|---|---|---|
| Расширение `_INVALID_FILE_TEXTS` | ✅ | `test_mapper_invalid_caption_too_long`, `test_mapper_invalid_message_too_long`, `test_mapper_invalid_media_empty`, `test_mapper_invalid_photo_dimensions` |
| Расширение `_UNAVAILABLE_TEXTS` (RetryAfter, 500) | ✅ | `test_mapper_unavailable_via_retry_after_text`, `test_mapper_unavailable_via_retry_after_without_code`, `test_mapper_unavailable_via_500` |
| `DEFAULT_ERROR_MAPPER` singleton | ✅ | Импортируется в `test_spec014.py`/`test_spec015.py` через legacy-хелперы (8 тестов) |
| `_RecordingReminderSender.fail_photo_with` через конструктор | ✅ | `test_reminder_publisher_photo_network_error_after_text_sent`, `test_reminder_publisher_photo_blocked_after_text_sent`, `test_reminder_publisher_photo_invalid_file_after_text_sent` |
