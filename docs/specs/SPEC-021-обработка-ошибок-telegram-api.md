# SPEC-021. Обработка ошибок Telegram API

**Проект:** Telegram-бот «PostMaster»  
**Этап:** MVP  
**Версия:** 1.0  
**Статус:** к разработке

## 1. Назначение

TelegramErrorMapper нормализует exceptions в code/message/retryable; timeout/network/unavailable retryable, permission/blocked/invalid file non-retryable.

## 2. Scope

Спецификация охватывает только функциональность, необходимую для указанного модуля и уже определённую требованиями PostMaster. Дополнительные продуктовые возможности за пределами исходного ТЗ не добавляются.

## 3. Основной сценарий

`Telegram exception → ErrorMapper → normalized error → retry/FAILED`

## 4. Бизнес-правила

- TelegramErrorMapper нормализует exceptions в code/message/retryable.
- timeout/network/unavailable retryable, permission/blocked/invalid file non-retryable..

## 5. Контракты и данные

Модуль обязан использовать существующие сущности `User`, `Post`, Repository, Scheduler и Publisher только в той мере, в которой они относятся к данной SPEC. Публичный REST API для MVP не требуется: взаимодействие инициируется Telegram handlers и внутренними application-service контрактами.

## 6. Ошибки и отказоустойчивость

Ошибки должны быть обработаны контролируемо: не приводить к ложному успешному результату, утечке секретов, изменению чужих данных или потере уже сохранённого Post. Если модуль взаимодействует с БД, БД остаётся источником истины; если с Telegram API — ошибка передаётся через общий error flow.

## 7. Критерии приёмки

Telegram exception не даёт false success; Bot Token не попадает в logs/error_message.

## 8. Тестирование

Обязательные проверки: all error mappings, unknown error, secret redaction.

Тесты должны проверять как happy path, так и основные ошибочные/граничные сценарии.

## 9. Definition of Done

- [ ] функциональность реализована в соответствии с этой SPEC;
- [ ] бизнес-правила соблюдены;
- [ ] ошибки обрабатываются контролируемо;
- [ ] данные не теряются и не дублируются;
- [ ] пользовательская изоляция и безопасность соблюдены там, где применимо;
- [ ] unit/integration tests для модуля проходят;
- [ ] документация соответствует реализации.
