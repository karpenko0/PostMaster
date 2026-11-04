# План разработки SPEC-014 «Reminder Publisher»

- Спецификация: [docs/specs/SPEC-014.md](../specs/SPEC-014.md)
- Статус: ✅ реализовано и проверено

## 1. Цель

Реализовать `ReminderPublisher` для типа публикации `REMINDER`: отправку владельцу поста в личный чат Telegram текста напоминания и затем фотографии по сохранённому `telegram_file_id` без скачивания медиа на диск, с возвратом `PublishResult` для фиксации финального статуса в `PublicationService` (SPEC-013).

## 2. Принятые решения

| № | Решение | Обоснование |
|---|---|---|
| D1 | Ввести `PublishResult(success: bool, error_code: str | None = None, error_message: str | None = None, retryable: bool = False)` и протокол `TelegramClient` с адаптером `BotTelegramClient(AsyncTeleBot)` в `src/postmaster/publishers/base.py` | §13–14, §45–46: позволяет тестировать `ReminderPublisher` без прямого обращения к сети и без нарушения слоёв архитектуры |
| D2 | Добавить `UserRepository.get_by_id(user_id: int) -> User | None` в `src/postmaster/repositories/user_repository.py`, а в `publishers` определить `UserLookup(Protocol)` на базе `postmaster.domain.user.User` | §21–22 и `tests/test_architecture.py`: слой `publishers` зависит только от `domain` и `utils`, получая `UserRepository` через DI |
| D3 | В `ReminderPublisher.publish(post)` проверять `publication_type == REMINDER`, непустой `telegram_file_id`, наличие `User` и `user.telegram_user_id` до вызова Telegram API; отправлять сначала `REMINDER_TEXT`, затем `send_photo` с `caption` (если задан) или без него | BR-014-01…08, §15–20, §23–25, §42: предотвращает частичную отправку при невалидных входных данных |
| D4 | Не менять статус `Post` внутри `ReminderPublisher` и не управлять `Scheduler`; обрабатывать `PublishResult` в `PublicationService.execute`: `success=True` → `mark_published`, `success=False` → `release_post_for_retry` (для временных сбоев, §37) или `mark_failed` (для терминальных ошибок, §35–36) | BR-014-09…11, AC-014-10…12, §29–37 |
| D5 | В `Application.from_settings` создать `ReminderPublisher(BotTelegramClient(bot), user_repository)`, передать в `PublicationService(post_repository, publishers={PublicationType.REMINDER: reminder_publisher})` и подключить `PublicationService` к `SchedulerService` | §54: обеспечивает полный сквозной сценарий от диалога создания поста до отправки напоминания по расписанию |

## 3. Этапы

1. Сохранить спецификацию в `docs/specs/SPEC-014.md`.
2. Добавить `UserRepository.get_by_id(user_id: int) -> User | None`.
3. Расширить `src/postmaster/publishers/base.py` (`PublishResult`, `TelegramClient`, `BotTelegramClient`) и реализовать `ReminderPublisher` в `src/postmaster/publishers/reminder.py`.
4. Обновить `PublicationService.execute` для обработки `PublishResult` и подключить `ReminderPublisher` + `PublicationService` в `src/postmaster/app.py`.
5. Расширить `tests/fake_telegram.py` поддержкой `sendPhoto` и покрыть unit, integration и E2E-сценарии в `tests/test_spec014.py`.
