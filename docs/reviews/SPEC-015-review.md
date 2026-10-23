# Проверка SPEC-015 — Telegram Publisher

## Объём и результат

Проверена реализация `TelegramChannelPublisher`: валидация preconditions, отправка
photo+caption в target_id, обработка permission/timeout ошибок Telegram API. Полный
прогон на Python 3.14.6: **448 passed**, 1 skipped. `ruff check .`, `ruff format --check .`
— успешно.

Исходный текст SPEC-015: `docs/specs/SPEC-015-telegram-publisher.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Публикует photo+caption в target_id через Telegram Bot API | `test_telegram_publisher_success`, `test_telegram_publisher_without_caption` | ✅ `send_photo` с caption=None или caption |
| Требует PROCESSING | `test_telegram_publisher_requires_processing_status`, `test_telegram_publisher_published_status_rejected` | ✅ `InvalidPostStatusError` |
| Требует target_id | `test_telegram_publisher_target_missing`, `test_telegram_publisher_target_whitespace_missing` | ✅ `TargetMissingError` |
| Требует file_id | `test_telegram_publisher_file_id_missing`, `test_telegram_publisher_file_id_whitespace_missing` | ✅ `FileIdMissingError` |
| Бот имеет право публикации | `test_telegram_publisher_permission_chat_not_found`, `test_telegram_publisher_permission_bot_kicked`, `test_telegram_publisher_permission_not_admin`, `test_telegram_publisher_permission_chat_deactivated` | ✅ 400/403 с permission-текстом → `TelegramPermissionError` |
| Publisher не управляет Scheduler | `test_telegram_publisher_does_not_manage_retry` | ✅ Нет вызовов Scheduler в коде Publisher'а |
| Publisher не управляет retry | `test_telegram_publisher_does_not_manage_retry` | ✅ Одна попытка, ошибка пробрасывается |
| Publisher не управляет status | `test_telegram_publisher_does_not_manage_status` | ✅ Статус остаётся PROCESSING после успеха |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| success | `test_telegram_publisher_success`, `test_telegram_publisher_returns_publish_result_with_external_id` | ✅ |
| caption | `test_telegram_publisher_success`, `test_telegram_publisher_without_caption` | ✅ |
| target missing | `test_telegram_publisher_target_missing`, `test_telegram_publisher_target_whitespace_missing` | ✅ |
| file missing | `test_telegram_publisher_file_id_missing`, `test_telegram_publisher_file_id_whitespace_missing` | ✅ |
| permission | 4 теста permission (chat not found, bot kicked, not admin, chat deactivated) | ✅ |
| timeout | `test_telegram_publisher_timeout_via_timeout_error`, `test_telegram_publisher_timeout_via_text` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `_validate_post()` вызывается до `send_photo` | §1 SPEC-015: «требует PROCESSING, target_id и file_id» — все три проверки до отправки |
| D2 | `InvalidPostStatusError` (а не ValueError) | Различаем «нет прав на публикацию в канал» и «нельзя публиковать пост в этом статусе» |
| D3 | `TargetMissingError`, `FileIdMissingError` — отдельные исключения | §8 SPEC-015 «target/file missing» — разные причины |
| D4 | `_PERMISSION_ERROR_CODES = {400, 403}` + текстовые подстроки | Telegram API возвращает 400/403 для permission-ошибок; тексты помогают отделить от других 400 |
| D5 | `TelegramTimeoutError` для `TimeoutError` и текста «timeout» | §8 SPEC-015 «timeout» — специфический класс ошибки |
| D6 | Не-403/не-timeout ошибки пробрасываются без обёртки | §6 SPEC-015: «ошибка передаётся через общий error flow» |
| D7 | Publisher не делает retry и не меняет статус | §7 SPEC-015: «Publisher не управляет Scheduler/retry/status» |

## Сценарии

### Happy path

```
TelegramChannelPublisher.publish(post):
1. _validate_post(post):
   - status == PROCESSING
   - target_id непустой
   - telegram_file_id непустой
2. send_photo(chat_id=target_id, photo_file_id, caption)
3. return PublishResult(post_id, external_id=message_id)
```

### Permission denied

```
Telegram API: 400 "Bad Request: chat not found"
→ _is_permission_error(exc) == True (400 + 'chat not found')
→ raise TelegramPermissionError(...)
```

### Timeout

```
asyncio.TimeoutError
→ _is_timeout(exc) == True
→ raise TelegramTimeoutError(...)
```

### Invalid post

```
post.status == PENDING
→ _validate_post: InvalidPostStatusError
```

## Ограничения scope

- `_is_permission_error` использует эвристику по кодам и текстам. Точное определение
  зависит от версии Telegram Bot API. Если Telegram добавит новые тексты ошибок,
  может потребоваться обновление списков.
- Publisher не валидирует формат target_id (например, что это `-100...` число).
  Это ответственность конфигурации (SPEC-026).
- `ProcessPool` через claim_for_processing гарантирует, что Publisher получает только
  посты в PROCESSING. Если кто-то вызовет Publisher напрямую с PENDING постом,
  валидация D1 заблокирует отправку.

## Команды

```bash
python -m pytest tests/test_spec015.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
