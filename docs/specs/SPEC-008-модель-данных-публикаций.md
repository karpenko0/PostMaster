# SPEC-008. Модель данных публикаций

**Проект:** Telegram-бот «PostMaster»  
**Этап:** MVP  
**Версия:** 1.0  
**Статус:** к разработке

## 1. Назначение

Таблица posts с user_id, file_id, scheduled_at_utc, timezone, status, publication_type, target_id, error_message, timestamps, caption.

## 2. Scope

Спецификация охватывает только функциональность, необходимую для указанного модуля и уже определённую требованиями PostMaster. Дополнительные продуктовые возможности за пределами исходного ТЗ не добавляются.

## 3. Основной сценарий

`User + photo + schedule + type → Post lifecycle`

## 4. Бизнес-правила

- Таблица posts с user_id, file_id, scheduled_at_utc, timezone, status, publication_type, target_id, error_message, timestamps, caption..

## 5. Контракты и данные

Модуль обязан использовать существующие сущности `User`, `Post`, Repository, Scheduler и Publisher только в той мере, в которой они относятся к данной SPEC. Публичный REST API для MVP не требуется: взаимодействие инициируется Telegram handlers и внутренними application-service контрактами.

## 6. Ошибки и отказоустойчивость

Ошибки должны быть обработаны контролируемо: не приводить к ложному успешному результату, утечке секретов, изменению чужих данных или потере уже сохранённого Post. Если модуль взаимодействует с БД, БД остаётся источником истины; если с Telegram API — ошибка передаётся через общий error flow.

## 7. Критерии приёмки

Статусы PENDING/PROCESSING/PUBLISHED/FAILED/CANCELLED; типы REMINDER/TELEGRAM; новый Post=PENDING.

## 8. Тестирование

Обязательные проверки: create/read, status validation, pending queries, persistence.

Тесты должны проверять как happy path, так и основные ошибочные/граничные сценарии.

## 9. Definition of Done

- [ ] функциональность реализована в соответствии с этой SPEC;
- [ ] бизнес-правила соблюдены;
- [ ] ошибки обрабатываются контролируемо;
- [ ] данные не теряются и не дублируются;
- [ ] пользовательская изоляция и безопасность соблюдены там, где применимо;
- [ ] unit/integration tests для модуля проходят;
- [ ] документация соответствует реализации.
