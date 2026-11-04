# SPEC-015. Telegram Publisher

**Проект:** Telegram-бот «PostMaster»  
**Этап:** MVP  
**Версия:** 1.0  
**Статус:** к разработке

---

# 1. Назначение

Данная спецификация описывает механизм автоматической публикации фотографии в Telegram-канал.

Telegram Publisher используется в сценарии, когда пользователь выбрал автоматическую публикацию в Telegram и PostMaster должен в назначенное время самостоятельно отправить подготовленный контент в целевой канал.

Основная задача:

```text
получить Post
→ определить target_id
→ отправить фото в Telegram-канал
→ при наличии caption передать его
→ вернуть результат Publication Service
```

Telegram Publisher не занимается планированием времени, восстановлением задач и управлением статусами до начала выполнения.

---

# 2. Цель

Telegram Publisher должен:

1. работать только с публикациями типа `TELEGRAM`;
2. получать подготовленный `Post`;
3. использовать `target_id` как идентификатор целевого Telegram-канала;
4. использовать `telegram_file_id`;
5. отправлять фотографию через Telegram Bot API;
6. использовать `caption`, если он задан;
7. не скачивать фотографию на сервер;
8. корректно обрабатывать отсутствие target;
9. корректно обрабатывать отсутствие `file_id`;
10. обрабатывать ошибки Telegram API;
11. вернуть результат Publication Service;
12. не изменять бизнес-статус самостоятельно вне согласованного publication-flow.

---

# 3. Scope

В рамках SPEC-015 реализуются:

- `TelegramPublisher`;
- обработка `publication_type=TELEGRAM`;
- получение `target_id`;
- отправка фотографии;
- передача `caption`;
- использование Telegram `file_id`;
- вызов Telegram Bot API;
- обработка прав бота;
- обработка недоступного канала;
- обработка invalid `file_id`;
- обработка network/API ошибок;
- возврат `PublishResult`;
- логирование.

---

# 4. Out of Scope

В рамках SPEC-015 не реализуются:

- Scheduler;
- Recovery;
- Missed Jobs;
- atomic claim;
- создание Telegram-канала;
- добавление бота в канал;
- изменение прав администратора;
- автоматический поиск channel ID;
- VK API;
- публикация видео;
- публикация документов;
- публикация альбомов;
- несколько фотографий;
- редактирование уже опубликованного сообщения;
- удаление опубликованного сообщения;
- AI-генерация caption;
- retry policy — SPEC-022.

---

# 5. Зависимости

SPEC-015 зависит от:

- **SPEC-008** — модель `Post`;
- **SPEC-013** — защита от двойной публикации;
- **SPEC-016** — Publisher abstraction.

Взаимодействует с:

- **SPEC-020** — caption;
- **SPEC-021** — Telegram API errors;
- **SPEC-022** — retry;
- **SPEC-025** — логирование.

---

# 6. Условие применения

Telegram Publisher выбирается только когда:

```text
post.publication_type = TELEGRAM
```

---

# 7. Предусловия

Перед вызовом Telegram Publisher должны выполняться условия:

```text
post.status = PROCESSING
```

и:

```text
post.publication_type = TELEGRAM
```

Также должны быть заполнены:

```text
post.telegram_file_id
post.target_id
```

---

# 8. User Stories

## US-015-01. Автоматическая публикация в канал

**Как** SMM-менеджер  
**Я хочу** запланировать фотографию для автоматической публикации  
**Чтобы** мне не приходилось вручную публиковать её в Telegram-канале.

### Acceptance

В назначенное время фотография появляется в целевом Telegram-канале.

---

## US-015-02. Публикация с подписью

**Как** пользователь  
**Я хочу**, чтобы вместе с фотографией отправлялся подготовленный текст  
**Чтобы** публикация не требовала ручного редактирования.

### Acceptance

Если у Post заполнен:

```text
caption
```

он передаётся вместе с фотографией.

---

## US-015-03. Работа без локального файла

**Как** система  
**Я хочу** использовать `telegram_file_id`  
**Чтобы** не хранить изображения на сервере.

### Acceptance

Telegram Publisher не скачивает фотографию перед отправкой.

---

# 9. Модель входных данных

Минимально необходимые поля `Post`:

```text
id
telegram_file_id
publication_type
target_id
caption
status
```

---

# 10. Поле target_id

Для Telegram Publisher:

```text
target_id
```

содержит идентификатор Telegram-канала.

Пример:

```text
-1001234567890
```

Тип в модели данных:

```text
VARCHAR
```

Publisher передаёт его Telegram API как `chat_id`.

---

# 11. Почему target_id обязателен

В отличие от Reminder Publisher, Telegram Publisher не отправляет сообщение владельцу публикации.

Ему необходимо знать конкретный канал назначения.

Поэтому:

```text
publication_type=TELEGRAM
```

требует:

```text
target_id != NULL
```

---

# 12. Telegram channel permissions

Чтобы автоматическая публикация работала, бот должен иметь права администратора в целевом канале.

Минимально необходимая возможность:

```text
публикация сообщений
```

В контексте PostMaster это включает возможность отправить фото в канал.

---

# 13. Основной Publisher contract

Рекомендуемый интерфейс:

```python
class TelegramPublisher(Publisher):

    def publish(
        self,
        post: Post
    ) -> PublishResult:
        ...
```

---

# 14. PublishResult

Используется общий DTO:

```python
class PublishResult:
    success: bool
    error_code: str | None
    error_message: str | None
```

---

# 15. Основной алгоритм

```text
получить Post
↓
проверить publication_type
↓
проверить target_id
↓
проверить telegram_file_id
↓
подготовить caption
↓
вызвать Telegram Bot API
↓
получить результат
↓
вернуть PublishResult
```

---

# 16. Telegram API call

Логически:

```python
bot.send_photo(
    chat_id=post.target_id,
    photo=post.telegram_file_id
)
```

Если caption существует:

```python
bot.send_photo(
    chat_id=post.target_id,
    photo=post.telegram_file_id,
    caption=post.caption
)
```

---

# 17. Использование file_id

Основной источник изображения:

```text
post.telegram_file_id
```

Publisher не должен:

```text
скачать фото
→ сохранить на диск
→ открыть файл
→ загрузить снова
```

Правильно:

```text
telegram_file_id
→ Telegram API
```

---

# 18. Поддержка caption

Если:

```text
post.caption != NULL
```

caption передаётся в `send_photo`.

Если:

```text
post.caption = NULL
```

фотография публикуется без текста.

---

# 19. Publication type validation

Перед вызовом Telegram API необходимо проверить:

```text
post.publication_type == TELEGRAM
```

Если тип другой:

```text
INVALID_PUBLICATION_TYPE
```

и вызов Telegram API не выполняется.

---

# 20. Status validation

Ожидаемое состояние:

```text
PROCESSING
```

Telegram Publisher не должен работать с:

```text
PENDING
PUBLISHED
FAILED
CANCELLED
```

Atomic transition выполняется ранее в SPEC-013.

---

# 21. Разделение ответственности

Telegram Publisher не должен выполнять:

```text
PENDING → PROCESSING
```

и не должен самовольно выполнять:

```text
PROCESSING → PUBLISHED
```

Финальное изменение статуса выполняет Publication Service после анализа `PublishResult`.

---

# 22. Правильный flow

```text
Scheduler / Missed Job
      ↓
PublicationService
      ↓
SPEC-013 claim
      ↓
PROCESSING
      ↓
PublisherResolver
      ↓
TelegramPublisher
      ↓
Telegram API
      ↓
PublishResult
      ↓
PublicationService
      ↓
PUBLISHED / FAILED / PENDING retry
```

---

# 23. Publisher не работает с Scheduler

Telegram Publisher не должен:

- создавать job;
- удалять job;
- менять run time;
- работать с APScheduler.

---

# 24. Publisher не работает с timezone

В момент публикации timezone уже не нужна для выполнения.

Scheduler ранее использовал:

```text
scheduled_at_utc
```

Telegram Publisher получает готовый Post.

---

# 25. Бизнес-правила

## BR-015-01

Telegram Publisher используется только для:

```text
publication_type=TELEGRAM
```

---

## BR-015-02

`target_id` обязателен.

---

## BR-015-03

`telegram_file_id` обязателен.

---

## BR-015-04

Файл отправляется через существующий Telegram `file_id`.

---

## BR-015-05

Локальное хранение изображения не требуется.

---

## BR-015-06

Если `caption` существует, он отправляется вместе с фото.

---

## BR-015-07

Telegram Publisher должен вызываться только после atomic claim.

---

## BR-015-08

Ожидаемый статус перед вызовом:

```text
PROCESSING
```

---

## BR-015-09

Publisher возвращает результат Publication Service.

---

## BR-015-10

Финальный статус устанавливается publication layer.

---

# 26. Sequence Flow — успешная публикация

```text
PublicationService
      │
      │ Post PROCESSING
      ▼
TelegramPublisher
      │
      │ validate
      ▼
Telegram Client
      │
      │ send_photo()
      ▼
Telegram Bot API
      │
      │ success
      ▼
PublishResult(success=True)
      │
      ▼
PublicationService
      │
      ▼
PUBLISHED
```

---

# 27. Sequence Flow — нет target_id

```text
TelegramPublisher
      ↓
target_id check
      ↓
NULL
      ↓
TARGET_REQUIRED
      ↓
PublishResult(success=False)
```

Telegram API не вызывается.

---

# 28. Sequence Flow — нет file_id

```text
TelegramPublisher
      ↓
telegram_file_id check
      ↓
NULL
      ↓
FILE_ID_MISSING
      ↓
PublishResult(success=False)
```

---

# 29. Sequence Flow — нет прав

```text
TelegramPublisher
      ↓
Telegram API
      ↓
permission error
      ↓
PublishResult(success=False)
      ↓
PublicationService
```

---

# 30. Sequence Flow — канал недоступен

```text
send_photo()
      ↓
Telegram API
      ↓
channel not found / inaccessible
      ↓
error
```

Publisher возвращает неуспешный результат.

---

# 31. Telegram Client abstraction

Рекомендуемый контракт:

```python
class TelegramClient:

    def send_photo(
        self,
        chat_id: str | int,
        file_id: str,
        caption: str | None = None
    ) -> None:
        ...
```

---

# 32. Причина abstraction

Это позволяет:

- не привязывать domain/service слой к `pyTelegramBotAPI`;
- mock-ировать Telegram API;
- писать unit tests без сети;
- централизованно обрабатывать Telegram errors.

---

# 33. Взаимодействие с pyTelegramBotAPI

Реализация TelegramClient может использовать:

```python
bot.send_photo(
    chat_id=chat_id,
    photo=file_id,
    caption=caption
)
```

---

# 34. Ошибки

## ERR-015-01. INVALID_PUBLICATION_TYPE

Причина:

```text
publication_type != TELEGRAM
```

Результат:

- Telegram API не вызывается;
- `success=False`.

---

## ERR-015-02. INVALID_POST_STATUS

Причина:

```text
status != PROCESSING
```

Результат:

- отправка не выполняется.

---

## ERR-015-03. TARGET_REQUIRED

Причина:

```text
target_id = NULL
```

---

## ERR-015-04. FILE_ID_MISSING

Причина:

```text
telegram_file_id = NULL
```

---

## ERR-015-05. TELEGRAM_PERMISSION_DENIED

Бот не имеет необходимых прав в канале.

---

## ERR-015-06. TELEGRAM_TARGET_UNAVAILABLE

Целевой канал недоступен.

---

## ERR-015-07. INVALID_FILE_ID

Telegram отклонил сохранённый `file_id`.

---

## ERR-015-08. TELEGRAM_API_TIMEOUT

Telegram API не ответил вовремя.

---

## ERR-015-09. NETWORK_ERROR

Ошибка сетевого взаимодействия.

---

## ERR-015-10. TELEGRAM_API_UNAVAILABLE

Telegram API временно недоступен.

---

# 35. Обработка отсутствия прав

Если бот не имеет права публиковать в канал:

```text
success=False
error_code=TELEGRAM_PERMISSION_DENIED
```

Publisher не должен считать операцию успешной.

---

# 36. Обработка недоступного канала

Если `target_id` указан, но Telegram API не может использовать этот канал:

```text
success=False
error_code=TELEGRAM_TARGET_UNAVAILABLE
```

---

# 37. Обработка invalid file_id

Если Telegram отклоняет:

```text
telegram_file_id
```

возвращается:

```text
INVALID_FILE_ID
```

---

# 38. Временные ошибки

Ошибки вида:

```text
timeout
network error
Telegram unavailable
```

передаются выше.

Решение о повторной попытке принимает SPEC-022.

---

# 39. Publisher не определяет retry самостоятельно

Недопустимо внутри TelegramPublisher делать:

```text
while true:
    retry
```

или создавать собственный Scheduler job.

Retry policy централизована отдельно.

---

# 40. Успешный результат

После успешного вызова:

```text
PublishResult(
    success=True,
    error_code=None,
    error_message=None
)
```

---

# 41. Ошибочный результат

Пример:

```text
PublishResult(
    success=False,
    error_code="TELEGRAM_PERMISSION_DENIED",
    error_message="..."
)
```

---

# 42. Работа с БД

TelegramPublisher напрямую не обязан обновлять таблицу `posts`.

Чтение необходимых данных выполняется до вызова либо через переданный объект Post.

Изменение статуса выполняет PublicationService.

---

# 43. Поля Post

Используются:

```text
id
telegram_file_id
publication_type
target_id
caption
status
```

---

# 44. Поле published_at

Publisher сам не устанавливает:

```text
published_at
```

После `success=True` Publication Service выполняет:

```text
PROCESSING → PUBLISHED
```

и устанавливает `published_at`.

---

# 45. error_message

Publisher возвращает техническую ошибку Publication Service.

Дальнейшее сохранение:

```text
posts.error_message
```

выполняется на уровне service/repository.

---

# 46. Логирование начала

```text
INFO Telegram publication started post_id=42 target_id=-100...
```

---

# 47. Логирование успеха

```text
INFO Telegram publication completed post_id=42
```

---

# 48. Логирование ошибки

```text
ERROR Telegram publication failed post_id=42 error_code=...
```

---

# 49. Требования к логам

Не логировать:

- Bot Token;
- access tokens;
- бинарное содержимое фотографий;
- секреты.

`post_id` и `target_id` могут использоваться для технической диагностики согласно принятой политике логирования.

---

# 50. Acceptance Criteria

## AC-015-01 — корректный type

**Given**:

```text
publication_type=TELEGRAM
status=PROCESSING
```

**When** вызывается TelegramPublisher  
**Then** начинается Telegram publish flow.

---

## AC-015-02 — автоматическая публикация

**Given**:

```text
target_id=-1001234567890
telegram_file_id=AgAC...
```

**When** Telegram API отвечает успешно  
**Then** фотография появляется в канале.

---

## AC-015-03 — file_id

Для отправки используется именно:

```text
post.telegram_file_id
```

---

## AC-015-04 — local storage

Publisher не требует локального файла изображения.

---

## AC-015-05 — caption exists

**Given**:

```text
caption="Текст публикации"
```

**Then** caption передаётся вместе с фотографией.

---

## AC-015-06 — caption missing

**Given**:

```text
caption=NULL
```

**Then** фотография публикуется без caption.

---

## AC-015-07 — target missing

**Given**:

```text
target_id=NULL
```

**Then**:

- Telegram API не вызывается;
- возвращается `TARGET_REQUIRED`.

---

## AC-015-08 — file missing

**Given**:

```text
telegram_file_id=NULL
```

**Then**:

- отправка не выполняется;
- возвращается `FILE_ID_MISSING`.

---

## AC-015-09 — wrong publication type

**Given**:

```text
publication_type=REMINDER
```

**Then** TelegramPublisher не вызывает Telegram API.

---

## AC-015-10 — wrong status

**Given**:

```text
status=PENDING
```

**Then** непосредственная публикация через TelegramPublisher не должна считаться допустимым execution flow.

---

## AC-015-11 — permission error

Если бот не имеет права отправлять сообщения:

```text
success=False
```

---

## AC-015-12 — target unavailable

Если канал недоступен:

```text
success=False
```

---

## AC-015-13 — API timeout

Timeout возвращается как ошибка в Publication Service.

---

## AC-015-14 — success result

При успешной отправке:

```text
PublishResult.success=True
```

---

## AC-015-15 — final status ownership

TelegramPublisher не устанавливает финальный статус самостоятельно.

---

# 51. Unit Tests

## TC-015-01 — successful publish

Вход:

```text
publication_type=TELEGRAM
status=PROCESSING
target_id=-100...
file_id=AgAC...
```

Mock Telegram:

```text
success
```

Ожидается:

```text
send_photo called once
success=True
```

---

## TC-015-02 — caption

Передать:

```text
caption="Hello"
```

Проверить:

```text
send_photo(... caption="Hello")
```

---

## TC-015-03 — no caption

Проверить вызов без caption.

---

## TC-015-04 — missing target

Ожидается:

```text
TARGET_REQUIRED
```

и:

```text
send_photo not called
```

---

## TC-015-05 — missing file

Ожидается:

```text
FILE_ID_MISSING
```

---

## TC-015-06 — wrong type

Вход:

```text
REMINDER
```

Telegram client не вызывается.

---

## TC-015-07 — wrong status

Вход:

```text
PENDING
```

Публикация не выполняется.

---

# 52. Unit Test — permission denied

Mock:

```text
send_photo → Telegram permission exception
```

Ожидается:

```text
success=False
error_code=TELEGRAM_PERMISSION_DENIED
```

---

# 53. Unit Test — timeout

Mock:

```text
send_photo → timeout
```

Ожидается ошибка, пригодная для передачи retry layer.

---

# 54. Unit Test — invalid file_id

Mock Telegram возвращает file error.

Ожидание:

```text
INVALID_FILE_ID
```

---

# 55. Integration Tests

## IT-015-01

Создать:

```text
Post:
publication_type=TELEGRAM
status=PROCESSING
target_id=-100...
telegram_file_id=AgAC...
```

Вызвать Publisher с mock TelegramClient.

Проверить:

```text
chat_id
file_id
```

---

## IT-015-02

Проверить передачу caption.

---

## IT-015-03

Проверить ошибку target.

---

## IT-015-04

Проверить ошибку Telegram API.

---

# 56. Integration Test с PublicationService

1. Post=`PENDING`.
2. SPEC-013 выполняет claim.
3. Post=`PROCESSING`.
4. Resolver выбирает `TelegramPublisher`.
5. Publisher отправляет фото.
6. Возвращает `success=True`.
7. PublicationService выполняет:
   ```text
   PROCESSING → PUBLISHED
   ```
8. Заполняется `published_at`.

---

# 57. Integration Test с ошибкой

1. Post=`PROCESSING`.
2. TelegramClient возвращает permission error.
3. Publisher возвращает `success=False`.
4. PublicationService получает ошибку.
5. Дальнейший статус определяется SPEC-021/022.

---

# 58. Integration Test с SPEC-013

1. Два execution flow пытаются выполнить один Post.
2. Только один получает atomic claim.
3. TelegramPublisher вызывается один раз.
4. В канале не появляется дублирующий пост.

---

# 59. E2E-сценарий — автоматическая публикация

1. Пользователь создаёт Telegram-публикацию.
2. В `posts` сохраняется:
   ```text
   publication_type=TELEGRAM
   target_id=-100...
   status=PENDING
   ```
3. Наступает время.
4. Scheduler вызывает PublicationService.
5. SPEC-013 выполняет:
   ```text
   PENDING → PROCESSING
   ```
6. PublisherResolver выбирает TelegramPublisher.
7. TelegramPublisher вызывает `send_photo`.
8. Telegram подтверждает отправку.
9. Publisher возвращает success.
10. Post становится:
    ```text
    PUBLISHED
    ```
11. `published_at` заполнен.

---

# 60. E2E-сценарий — публикация после Recovery

1. Post запланирован.
2. Приложение перезапускается.
3. SPEC-011 восстанавливает job.
4. Наступает время.
5. Post проходит SPEC-013.
6. TelegramPublisher публикует фото.
7. Post получает `PUBLISHED`.

---

# 61. E2E-сценарий — Missed Job

1. Telegram Post запланирован на 15:00.
2. Сервер выключен.
3. Запускается в 15:20.
4. SPEC-011/012 определяет missed Post.
5. SPEC-013 получает claim.
6. TelegramPublisher отправляет фотографию.
7. `scheduled_at_utc` остаётся исходным.
8. `published_at` отражает фактическое время.

---

# 62. E2E-сценарий — бот потерял права

1. Post=`PROCESSING`.
2. Бот больше не администратор канала.
3. `send_photo` возвращает ошибку.
4. Publisher возвращает:
   ```text
   success=False
   ```
5. Post не получает ложный `PUBLISHED`.

---

# 63. E2E-сценарий — invalid target

1. `target_id` указывает на недоступный канал.
2. TelegramPublisher вызывает API.
3. API возвращает ошибку.
4. Publisher сообщает об ошибке.
5. PublicationService обрабатывает failure.

---

# 64. Инварианты

```text
INV-015-01:
TelegramPublisher работает только с TELEGRAM.
```

```text
INV-015-02:
публикация требует target_id.
```

```text
INV-015-03:
публикация требует telegram_file_id.
```

```text
INV-015-04:
Publisher вызывается только после atomic claim.
```

```text
INV-015-05:
изображение отправляется через Telegram file_id.
```

```text
INV-015-06:
TelegramPublisher не управляет Scheduler.
```

```text
INV-015-07:
TelegramPublisher не выполняет recovery.
```

```text
INV-015-08:
финальный статус устанавливает PublicationService.
```

```text
INV-015-09:
при Telegram API ошибке нельзя устанавливать PUBLISHED.
```

---

# 65. Definition of Done

SPEC-015 считается реализованной, если:

- [ ] создан `TelegramPublisher`;
- [ ] реализован общий Publisher interface;
- [ ] поддерживается только `publication_type=TELEGRAM`;
- [ ] проверяется `status=PROCESSING`;
- [ ] `target_id` обязателен;
- [ ] `telegram_file_id` обязателен;
- [ ] используется Telegram Bot API;
- [ ] фото отправляется через `file_id`;
- [ ] локальная загрузка файла не требуется;
- [ ] поддерживается caption;
- [ ] публикация работает без caption;
- [ ] обрабатывается отсутствие target;
- [ ] обрабатывается отсутствие file_id;
- [ ] обрабатывается отсутствие прав;
- [ ] обрабатывается недоступный канал;
- [ ] обрабатывается invalid file_id;
- [ ] обрабатываются timeout/network/API errors;
- [ ] возвращается `PublishResult`;
- [ ] Publisher не выполняет retry самостоятельно;
- [ ] Publisher не управляет Scheduler;
- [ ] Publisher не меняет финальный статус напрямую;
- [ ] начало, успех и ошибки логируются;
- [ ] unit tests проходят;
- [ ] integration tests проходят;
- [ ] PublicationService integration проходит;
- [ ] SPEC-013 integration проходит;
- [ ] E2E-сценарии проходят.

---

# 66. Результат SPEC-015

После реализации автоматическая Telegram-публикация работает по цепочке:

```text
Post
publication_type=TELEGRAM
        ↓
Scheduler / Missed Job
        ↓
PublicationService
        ↓
SPEC-013 Atomic Claim
        ↓
PROCESSING
        ↓
PublisherResolver
        ↓
TelegramPublisher
        ↓
Telegram Bot API
        ↓
Target Channel
        ↓
📸 Photo + Caption
        ↓
PublishResult
        ↓
PUBLISHED / error flow
```

Следующая спецификация объединяет оба способа публикации единым контрактом:

```text
SPEC-016 — Абстракция Publisher
```
