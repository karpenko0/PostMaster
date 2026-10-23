# Проверка SPEC-014 — Reminder Publisher

## Объём и результат

Проверена реализация `ReminderPublisher`: отправка текста и фото владельцу поста,
обработка ошибок отсутствующего пользователя/файла, заблокированного бота и других
ошибок Telegram API. Полный прогон на Python 3.14.6: **423 passed**, 1 skipped.
`ruff check .`, `ruff format --check .` — успешно.

Исходный текст SPEC-014: `docs/specs/SPEC-014-reminder-publisher.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Получает владельца Post | `test_reminder_success_sends_text_and_photo`, `test_reminder_with_user_service_resolver` | ✅ Через `ReminderRecipientResolver` |
| Отправляет текст «Время публиковать!» | `test_reminder_success_sends_text_and_photo`, `test_reminder_uses_correct_text_template` | ✅ Шаблон `REMINDER_TEXT_TEMPLATE` |
| Отправляет фото по telegram_file_id | `test_reminder_success_sends_text_and_photo`, `test_reminder_success_without_caption` | ✅ `send_photo` после `send_message` |
| Caption добавляется при наличии | `test_reminder_success_sends_text_and_photo`, `test_reminder_success_without_caption` | ✅ Caption передаётся в `send_photo` |
| Не скачивает файл | `test_reminder_success_sends_text_and_photo` | ✅ Используется только `telegram_file_id` |
| Работает только после PROCESSING | `test_spec013.py::test_concurrent_claims_only_one_succeeds` | ✅ Гарантировано атомарным `claim_for_processing` (SPEC-013) |
| Возвращает PublishResult | `test_reminder_returns_publish_result`, `test_reminder_success_sends_text_and_photo` | ✅ `PublishResult(post_id, external_id)` |
| Final status меняет PublicationService | `test_publication_service_calls_publisher_only_after_successful_claim` | ✅ ReminderPublisher не меняет статус — это делает PublicationService |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| success | `test_reminder_success_sends_text_and_photo`, `test_reminder_returns_publish_result` | ✅ |
| no caption | `test_reminder_success_without_caption` | ✅ |
| missing user | `test_reminder_missing_user_raises_error` | ✅ |
| missing file | `test_reminder_missing_file_id_raises_error`, `test_reminder_missing_file_id_whitespace_raises_error` | ✅ |
| blocked bot | `test_reminder_blocked_bot_on_send_message`, `test_reminder_blocked_bot_on_send_photo` | ✅ |
| send errors | `test_reminder_non_blocked_error_propagates`, `test_reminder_photo_error_after_text_succeeds` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `PublishResult` в `publishers/base.py` | §7 SPEC-014: «возвращает PublishResult». Общий контракт для всех Publisher'ов (Telegram/Reminder) |
| D2 | `TelegramReminderSender` имеет два метода: `send_message` и `send_photo` | §1 SPEC-014: «отправляет текст … и фото по telegram_file_id» |
| D3 | Текст отправляется до фото | Текст «Время публиковать!» + фото вместе формируют напоминание; порядок помогает пользователю |
| D4 | `UserNotFoundError` и `PhotoDataMissingError` — отдельные исключения | §8 SPEC-014 «missing user/file» — разные причины ошибок |
| D5 | `_is_bot_blocked` детектирует 403 и тексты ошибок | §8 SPEC-014 «blocked bot» — специфическая ошибка Telegram API |
| D6 | Прочие ошибки Telegram пробрасываются без обёртки | §6 SPEC-014: «ошибка передаётся через общий error flow» |
| D7 | Caption=None → `send_photo(..., caption=None)` | Telegram API поддерживает None caption |

## Сценарии

### Happy path

```
ReminderPublisher.publish(post):
1. post.telegram_file_id не пустой — иначе PhotoDataMissingError
2. recipient_resolver.get_telegram_user_id(post.user_id) → telegram_user_id
   или None → UserNotFoundError
3. format_local_datetime(post.scheduled_at_utc, post.timezone) → local_time
4. send_message(chat_id=telegram_user_id, text="🔔 Время публиковать!...")
   или TelegramBlockedError / пробрасывание ошибки
5. send_photo(chat_id=telegram_user_id, photo_file_id, caption)
   или TelegramBlockedError / пробрасывание ошибки
6. return PublishResult(post_id=post.id, external_id=...)
```

### Blocked bot

```
Telegram API: 403 Forbidden: bot was blocked by the user
→ _is_bot_blocked(exc) == True
→ raise TelegramBlockedError(...)
```

### Не-403 ошибка

```
Telegram API: network timeout / 400 Bad Request
→ _is_bot_blocked(exc) == False
→ raise exc  # пробрасывается
```

## Ограничения scope

- ReminderPublisher не меняет статус Post — это делает PublicationService.
- Если `send_photo` упал после успешного `send_message`, текст уже доставлен пользователю,
  но пост переходит в FAILED. Это сделано сознательно: одна атомарная операция
  «доставить напоминание» невозможна через два запроса, и частичная доставка лучше
  полного молчания.
- `_is_bot_blocked` использует эвристику по тексту ошибки (не только `error_code`),
  потому что `ApiTelegramException` не всегда доступен в тестах.

## Команды

```bash
python -m pytest tests/test_spec014.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
