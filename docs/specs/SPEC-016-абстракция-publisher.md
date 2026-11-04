# SPEC-016. Абстракция Publisher

**Проект:** Telegram-бот «PostMaster»  
**Этап:** MVP  
**Версия:** 1.0  
**Статус:** к разработке

## 1. Назначение

Единый интерфейс publish(post)->PublishResult; PublisherResolver: REMINDER→ReminderPublisher, TELEGRAM→TelegramPublisher.

## 2. Scope

Спецификация охватывает только функциональность, необходимую для указанного модуля и уже определённую требованиями PostMaster. Дополнительные продуктовые возможности за пределами исходного ТЗ не добавляются.

## 3. Основной сценарий

`PublicationService → Resolver → concrete Publisher → PublishResult`

## 4. Бизнес-правила

- Единый интерфейс publish(post)->PublishResult.
- PublisherResolver: REMINDER→ReminderPublisher, TELEGRAM→TelegramPublisher..

## 5. Контракты и данные

Модуль обязан использовать существующие сущности `User`, `Post`, Repository, Scheduler и Publisher только в той мере, в которой они относятся к данной SPEC. Публичный REST API для MVP не требуется: взаимодействие инициируется Telegram handlers и внутренними application-service контрактами.

## 6. Ошибки и отказоустойчивость

Ошибки должны быть обработаны контролируемо: не приводить к ложному успешному результату, утечке секретов, изменению чужих данных или потере уже сохранённого Post. Если модуль взаимодействует с БД, БД остаётся источником истины; если с Telegram API — ошибка передаётся через общий error flow.

## 7. Критерии приёмки

Scheduler/Recovery/MissedJob не знают concrete Publisher; неизвестный type не публикуется.

## 8. Тестирование

Обязательные проверки: resolver reminder/telegram/unknown, PublicationService success/failure.

Тесты должны проверять как happy path, так и основные ошибочные/граничные сценарии.

## 9. Definition of Done

- [ ] функциональность реализована в соответствии с этой SPEC;
- [ ] бизнес-правила соблюдены;
- [ ] ошибки обрабатываются контролируемо;
- [ ] данные не теряются и не дублируются;
- [ ] пользовательская изоляция и безопасность соблюдены там, где применимо;
- [ ] unit/integration tests для модуля проходят;
- [ ] документация соответствует реализации.
