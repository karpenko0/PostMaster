# План разработки SPEC-015 «Telegram Publisher»

- Спецификация: [docs/specs/SPEC-015.md](../specs/SPEC-015.md)
- Статус: ✅ реализовано и проверено

## 1. Цель

Реализовать `TelegramPublisher` для автоматической публикации фотографий в целевой Telegram-канал (`post.target_id`) при `publication_type=TELEGRAM` и `status=PROCESSING` через сохранённый `telegram_file_id` без скачивания медиа на диск, с передачей `caption` и возвратом `PublishResult` в `PublicationService`.

## 2. Принятые решения

| № | Решение | Обоснование |
|---|---|---|
| D1 | Расширить `TelegramClient.send_photo` и `BotTelegramClient.send_photo` типом `chat_id: str \| int` | §10, §31–33: `target_id` хранится в модели `Post` как `VARCHAR` (`str`), а для личного чата в SPEC-014 используется `int` |
| D2 | Реализовать `TelegramPublisher(Publisher)` и перечисление `TelegramErrorCode` (`ERR-015-01 … ERR-015-10`) в `src/postmaster/publishers/telegram.py` | §13–21, §34–41: проверяет `publication_type == TELEGRAM`, `status == PROCESSING`, непустой `target_id` и `telegram_file_id` до обращения к Telegram Bot API |
| D3 | Классифицировать ошибки Telegram API на терминальные (`TELEGRAM_PERMISSION_DENIED`, `TELEGRAM_TARGET_UNAVAILABLE`, `INVALID_FILE_ID`, `retryable=False`) и временные (`TELEGRAM_API_TIMEOUT`, `NETWORK_ERROR`, `TELEGRAM_API_UNAVAILABLE`, `retryable=True`) | §34–39, §52–54, §57, §62–63: предотвращает ложный `PUBLISHED` (INV-015-09) и передаёт признак временного сбоя на уровень `PublicationService` |
| D4 | Очищать `error_message` через `redact()` (`postmaster.utils.logging_config`) и маскировать `telegram_file_id` и `caption` | §46–49: исключает утечку Bot Token и секретов в логи и `posts.error_message` |
| D5 | Подключить `TelegramPublisher` в `Application.from_settings` для `PublicationType.TELEGRAM` | §22, §59: обеспечивает полный сквозной цикл автопубликации в канал по расписанию |

## 3. Этапы

1. Сохранить спецификацию в `docs/specs/SPEC-015.md`.
2. Обновить `TelegramClient` / `BotTelegramClient` в `src/postmaster/publishers/base.py`.
3. Реализовать `TelegramErrorCode` и `TelegramPublisher` в `src/postmaster/publishers/telegram.py` и экспортировать их в `src/postmaster/publishers/__init__.py`.
4. Зарегистрировать `TelegramPublisher` в `PublicationService` в `src/postmaster/app.py`.
5. Покрыть unit, integration и E2E-сценарии в `tests/test_spec015.py`.
