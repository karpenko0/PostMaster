# SPEC-014. Reminder Publisher

**Проект:** Telegram-бот «PostMaster»  
**Этап:** MVP  
**Версия:** 1.0  
**Статус:** к разработке

---

# 1. Назначение

Данная спецификация описывает механизм отправки пользователю напоминания о необходимости вручную опубликовать ранее загруженную фотографию.

Reminder Publisher используется в сценарии, когда автоматическая публикация в целевую социальную сеть не выполняется, а PostMaster должен в назначенное время вернуть пользователю его фотографию и напомнить о публикации.

Основная задача:

```text
получить Post
→ определить владельца
→ отправить напоминание
→ отправить фотографию по telegram_file_id
→ вернуть результат выполнения
```

---

# 2. Цель

Reminder Publisher должен:

1. работать с публикациями типа `REMINDER`;
2. получать подготовленный `Post`;
3. использовать `telegram_file_id`;
4. определить Telegram ID владельца публикации;
5. отправить пользователю текстовое напоминание;
6. отправить пользователю сохранённую фотографию;
7. не скачивать фотографию на сервер;
8. использовать Telegram Bot API;
9. сообщить Publication Service об успешном выполнении;
10. сообщить Publication Service об ошибке;
11. не управлять Scheduler;
12. не изменять статус Post напрямую вне согласованного publication-flow.

---

# 3. Scope

В рамках SPEC-014 реализуются:

- `ReminderPublisher`;
- поддержка `publication_type=REMINDER`;
- получение владельца публикации;
- получение `telegram_user_id`;
- отправка сообщения-напоминания;
- отправка фотографии;
- использование Telegram `file_id`;
- поддержка `caption`, если он существует;
- обработка Telegram API ошибок;
- возврат результата публикационному слою;
- логирование результата.

---

# 4. Out of Scope

В SPEC-014 не реализуются:

- планирование времени;
- Scheduler;
- Recovery;
- Missed Jobs;
- atomic claim;
- Telegram-канал;
- VK;
- другие социальные сети;
- скачивание фотографии;
- изменение изображения;
- генерация caption;
- AI;
- редактирование публикации;
- повторные попытки по времени — SPEC-022.

---

# 5. Зависимости

SPEC-014 зависит от:

- **SPEC-007** — пользователь;
- **SPEC-008** — модель публикации;
- **SPEC-013** — защита от двойной публикации;
- **SPEC-016** — Publisher abstraction.

Взаимодействует с:

- **SPEC-021** — обработка ошибок Telegram API;
- **SPEC-022** — retry.

---

# 6. Условие применения

Reminder Publisher выбирается, когда:

```text
post.publication_type = REMINDER
```

---

# 7. Пользовательский сценарий

Пользователь заранее отправил фотографию и создал публикацию.

Например:

```text
publication_type = REMINDER
scheduled_at_utc = 2026-10-07 12:00 UTC
timezone = Europe/Moscow
status = PENDING
```

Когда наступает время:

```text
PublicationService
→ atomic claim
→ status=PROCESSING
→ ReminderPublisher
```

Пользователь получает:

```text
⏰ Время публиковать!

Копируй и вставляй в соцсеть.
```

После чего получает фотографию.

---

# 8. User Stories

## US-014-01. Получение напоминания

**Как** SMM-менеджер  
**Я хочу** получить напоминание в назначенное время  
**Чтобы** не забыть вручную опубликовать контент.

### Acceptance

Пользователь получает сообщение:

```text
⏰ Время публиковать!

Копируй и вставляй в соцсеть.
```

---

## US-014-02. Получение исходной фотографии

**Как** пользователь  
**Я хочу** вместе с напоминанием получить сохранённое фото  
**Чтобы** сразу использовать его для ручной публикации.

### Acceptance

Бот отправляет фото через сохранённый `telegram_file_id`.

---

## US-014-03. Работа без файлового хранилища

**Как** система  
**Я хочу** повторно использовать Telegram `file_id`  
**Чтобы** не хранить изображения локально.

### Acceptance

При выполнении Reminder Publisher не требуется скачивание файла.

---

# 9. Входные данные

Publisher получает объект `Post`.

Минимально необходимые поля:

```text
id
user_id
telegram_file_id
publication_type
caption
status
```

---

# 10. Дополнительные данные пользователя

Для отправки напоминания требуется:

```text
users.telegram_user_id
```

Связь:

```text
Post.user_id
    ↓
User.id
    ↓
User.telegram_user_id
```

---

# 11. Проверка типа публикации

Перед выполнением должно выполняться:

```text
post.publication_type == REMINDER
```

Если передан другой тип, Reminder Publisher не должен выполнять публикацию.

---

# 12. Предусловие статуса

Reminder Publisher должен вызываться только после успешного claim из SPEC-013.

Ожидаемое состояние:

```text
status=PROCESSING
```

Publisher сам не должен переводить:

```text
PENDING → PROCESSING
```

---

# 13. Publisher interface

Рекомендуемый контракт:

```python
class ReminderPublisher(Publisher):

    def publish(
        self,
        post: Post
    ) -> PublishResult:
        ...
```

---

# 14. PublishResult

Допустимый DTO:

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
получить User
↓
получить telegram_user_id
↓
отправить reminder text
↓
отправить photo
↓
вернуть success
```

---

# 16. Reminder text

Текст из требований:

```text
⏰ Время публиковать!

Копируй и вставляй в соцсеть.
```

---

# 17. Отправка фотографии

Используется:

```text
post.telegram_file_id
```

Пример логического вызова:

```python
bot.send_photo(
    chat_id=user.telegram_user_id,
    photo=post.telegram_file_id
)
```

---

# 18. Caption

Если:

```text
post.caption != NULL
```

caption может быть передан вместе с фотографией.

Пример:

```python
bot.send_photo(
    chat_id=user.telegram_user_id,
    photo=post.telegram_file_id,
    caption=post.caption
)
```

Если:

```text
caption = NULL
```

фото отправляется без подписи.

---

# 19. Порядок сообщений

Основной порядок:

```text
1. reminder text
2. photo
```

То есть сначала пользователь понимает контекст, затем получает контент.

---

# 20. Использование file_id

Publisher не должен делать:

```text
download file
→ save local
→ upload again
```

Правильно:

```text
telegram_file_id
→ Telegram Bot API
```

---

# 21. Repository interaction

Reminder Publisher может получить User через:

```python
user_repository.get_by_id(post.user_id)
```

---

# 22. User Repository contract

Минимально:

```python
get_by_id(
    user_id: int
) -> User | None
```

---

# 23. Поведение при отсутствии пользователя

Если:

```text
User = None
```

Reminder Publisher не может определить target chat.

Результат:

```text
success=False
error_code=USER_NOT_FOUND
```

---

# 24. Поведение при отсутствии telegram_user_id

Если у User отсутствует:

```text
telegram_user_id
```

публикация не может быть отправлена.

Результат:

```text
TARGET_USER_MISSING
```

---

# 25. Поведение при отсутствии file_id

Если:

```text
post.telegram_file_id = NULL
```

Publisher не должен пытаться вызвать Telegram API с пустым значением.

Результат:

```text
FILE_ID_MISSING
```

---

# 26. Бизнес-правила

## BR-014-01

Reminder Publisher используется только для:

```text
publication_type=REMINDER
```

---

## BR-014-02

Отправка выполняется владельцу Post.

---

## BR-014-03

Target определяется через:

```text
posts.user_id
→ users.id
→ users.telegram_user_id
```

---

## BR-014-04

Используется сохранённый:

```text
telegram_file_id
```

---

## BR-014-05

Файл не требуется скачивать на сервер.

---

## BR-014-06

Сначала отправляется текст напоминания.

---

## BR-014-07

После текста отправляется фотография.

---

## BR-014-08

При наличии caption он передаётся вместе с фотографией.

---

## BR-014-09

Reminder Publisher не управляет Scheduler.

---

## BR-014-10

Reminder Publisher не выполняет atomic claim.

---

## BR-014-11

Reminder Publisher возвращает результат в Publication Service.

---

# 27. Sequence Flow — успешное выполнение

```text
PublicationService
      │
      │ Post PROCESSING
      ▼
ReminderPublisher
      │
      ▼
UserRepository
      │
      │ telegram_user_id
      ▼
Telegram Bot API
      │
      ├── send_message()
      └── send_photo()
      │
      ▼
PublishResult(success=True)
```

---

# 28. Sequence Flow — пользователь не найден

```text
ReminderPublisher
      ↓
UserRepository
      ↓
None
      ↓
PublishResult(
  success=False,
  USER_NOT_FOUND
)
```

---

# 29. Sequence Flow — Telegram error

```text
ReminderPublisher
      ↓
Telegram Bot API
      ↓
ERROR
      ↓
PublishResult(
  success=False
)
      ↓
PublicationService
      ↓
SPEC-021 / SPEC-022
```

---

# 30. Работа со статусом Post

Reminder Publisher сам не должен произвольно выполнять:

```text
PROCESSING → PUBLISHED
```

Финальный статус устанавливает Publication Service после получения результата Publisher.

Правильная схема:

```text
ReminderPublisher
→ success
→ PublicationService
→ mark_published()
```

---

# 31. Успешный результат

Если и текст, и фото отправлены успешно:

```text
PublishResult.success = True
```

После этого publication layer переводит:

```text
PROCESSING → PUBLISHED
```

---

# 32. Частичная ошибка

Возможен сценарий:

```text
reminder text sent
photo failed
```

Общее выполнение считается неуспешным, так как основной контент не доставлен полностью.

Publisher возвращает:

```text
success=False
```

---

# 33. Почему частичное выполнение считается ошибкой

Бизнес-сценарий требует:

```text
напоминание + фото
```

Если фотография не доставлена, пользователь не получил полный ожидаемый результат.

---

# 34. Telegram API ошибки

Обрабатываются:

- timeout;
- network error;
- bot blocked;
- invalid `file_id`;
- Telegram API unavailable.

Подробная классификация — SPEC-021.

---

# 35. bot blocked

Если пользователь заблокировал бота, Telegram API может вернуть ошибку доставки.

Результат:

```text
success=False
```

Дальнейший статус определяется Publication Service и error/retry policy.

---

# 36. invalid file_id

Если Telegram больше не принимает сохранённый `file_id`:

```text
error_code=INVALID_FILE_ID
```

---

# 37. network error

Временная ошибка сети передаётся в retry layer.

---

# 38. Логирование начала

```text
INFO Reminder publication started post_id=42 user_id=15
```

---

# 39. Логирование текста

Не требуется логировать полный текст пользовательского caption.

---

# 40. Логирование успеха

```text
INFO Reminder publication completed post_id=42
```

---

# 41. Логирование ошибки

```text
ERROR Reminder publication failed post_id=42 error_code=...
```

---

# 42. Ошибки

## ERR-014-01. INVALID_PUBLICATION_TYPE

Post имеет:

```text
publication_type != REMINDER
```

Результат:

- Telegram API не вызывается;
- возвращается ошибка.

---

## ERR-014-02. USER_NOT_FOUND

`user_id` не соответствует существующему User.

---

## ERR-014-03. TARGET_USER_MISSING

Не найден `telegram_user_id`.

---

## ERR-014-04. FILE_ID_MISSING

Отсутствует `telegram_file_id`.

---

## ERR-014-05. MESSAGE_SEND_FAILED

Не удалось отправить текст напоминания.

---

## ERR-014-06. PHOTO_SEND_FAILED

Не удалось отправить фотографию.

---

# 43. Поведение при MESSAGE_SEND_FAILED

Если текст не отправлен:

- отправка считается неуспешной;
- дальнейшая обработка передаётся error/retry layer.

---

# 44. Поведение при PHOTO_SEND_FAILED

Если текст отправился, но фото нет:

- публикация считается неуспешной;
- retry policy решает дальнейшее поведение.

---

# 45. API-контракт Telegram client

Допустимый abstraction:

```python
class TelegramClient:

    def send_message(
        self,
        chat_id: int,
        text: str
    ) -> None:
        ...

    def send_photo(
        self,
        chat_id: int,
        file_id: str,
        caption: str | None = None
    ) -> None:
        ...
```

---

# 46. Причина abstraction

Это позволяет:

- тестировать Publisher без реального Telegram;
- mock-ировать Telegram API;
- отделить бизнес-логику Publisher от библиотеки `pyTelegramBotAPI`.

---

# 47. Acceptance Criteria

## AC-014-01 — REMINDER type

**Given**:

```text
publication_type=REMINDER
status=PROCESSING
```

**When** вызывается Publisher  
**Then** Reminder flow выполняется.

---

## AC-014-02 — reminder text

Пользователь получает:

```text
⏰ Время публиковать!

Копируй и вставляй в соцсеть.
```

---

## AC-014-03 — photo

После текста пользователь получает фото из:

```text
telegram_file_id
```

---

## AC-014-04 — no download

Для отправки изображения локальный файл не создаётся.

---

## AC-014-05 — caption

Если `caption` заполнен, он передаётся вместе с фото.

---

## AC-014-06 — caption NULL

Если caption отсутствует, фото всё равно отправляется.

---

## AC-014-07 — user resolution

Telegram chat определяется по:

```text
user.telegram_user_id
```

---

## AC-014-08 — user missing

Если User отсутствует, Telegram API не вызывается.

---

## AC-014-09 — file missing

Если `telegram_file_id` отсутствует, фото не отправляется.

---

## AC-014-10 — success result

Если оба Telegram-вызова успешны:

```text
PublishResult.success=True
```

---

## AC-014-11 — Telegram error

Если любой обязательный вызов завершён ошибкой:

```text
PublishResult.success=False
```

---

## AC-014-12 — status ownership

Reminder Publisher сам не выполняет окончательный статусный переход Post вне Publication Service.

---

# 48. Unit Tests

## TC-014-01 — successful reminder

Вход:

```text
REMINDER
valid user
valid file_id
```

Ожидается:

```text
send_message = 1
send_photo = 1
success=True
```

---

## TC-014-02 — wrong publication type

Вход:

```text
TELEGRAM
```

Ожидается:

```text
send_message = 0
send_photo = 0
success=False
```

---

## TC-014-03 — user missing

Repository:

```text
None
```

Telegram client не вызывается.

---

## TC-014-04 — file missing

```text
telegram_file_id=None
```

Ожидается ошибка до `send_photo`.

---

## TC-014-05 — caption present

Проверить передачу caption в `send_photo`.

---

## TC-014-06 — caption absent

Проверить отправку фото без caption.

---

# 49. Unit Test — message error

Mock:

```text
send_message → exception
```

Ожидается:

```text
success=False
```

---

# 50. Unit Test — photo error

Mock:

```text
send_message → success
send_photo → exception
```

Ожидается:

```text
success=False
```

---

# 51. Integration Tests

## IT-014-01

Создать User.

Создать:

```text
Post:
publication_type=REMINDER
status=PROCESSING
```

Вызвать Publisher с Telegram API mock.

Проверить правильный:

```text
chat_id
file_id
```

---

## IT-014-02

Проверить интеграцию:

```text
Post.user_id
→ UserRepository
→ telegram_user_id
```

---

## IT-014-03

Проверить caption.

---

# 52. Integration Test с PublicationService

1. Post=`PENDING`.
2. PublicationService получает atomic claim.
3. Post=`PROCESSING`.
4. Resolver выбирает ReminderPublisher.
5. Publisher возвращает `success=True`.
6. PublicationService переводит:
   ```text
   PROCESSING → PUBLISHED
   ```

---

# 53. Integration Test ошибки

1. Post=`PROCESSING`.
2. Telegram mock возвращает ошибку.
3. ReminderPublisher возвращает `success=False`.
4. Publication Service передаёт ошибку в error/retry flow.

---

# 54. E2E-сценарий — обычное напоминание

1. Пользователь отправляет фото.
2. Выбирается Reminder mode.
3. Post создаётся как `PENDING`.
4. Наступает время.
5. SPEC-013 переводит Post в `PROCESSING`.
6. Reminder Publisher получает Post.
7. Получает пользователя.
8. Отправляет:
   ```text
   ⏰ Время публиковать!
   ```
9. Отправляет фото.
10. Возвращает success.
11. Post становится `PUBLISHED`.

---

# 55. E2E-сценарий — missed reminder

1. Reminder запланирован.
2. Сервер выключен.
3. Время проходит.
4. SPEC-011/012 обнаруживает missed Post.
5. SPEC-013 выполняет claim.
6. ReminderPublisher отправляет сообщение и фото.
7. Post становится `PUBLISHED`.

---

# 56. E2E-сценарий — blocked bot

1. Пользователь заблокировал бота.
2. Reminder Publisher пытается отправить сообщение.
3. Telegram API возвращает ошибку.
4. Publisher возвращает failure.
5. Publication layer обрабатывает ошибку по SPEC-021/022.

---

# 57. Инварианты

```text
INV-014-01:
ReminderPublisher работает только с REMINDER.
```

```text
INV-014-02:
target всегда определяется через владельца Post.
```

```text
INV-014-03:
фото отправляется по telegram_file_id.
```

```text
INV-014-04:
локальное файловое хранилище для reminder не требуется.
```

```text
INV-014-05:
Publisher не выполняется без предварительного execution claim.
```

```text
INV-014-06:
Publisher не управляет Scheduler.
```

```text
INV-014-07:
финальный статус устанавливает publication layer.
```

---

# 58. Definition of Done

SPEC-014 считается реализованной, если:

- [ ] создан `ReminderPublisher`;
- [ ] он реализует общий Publisher interface;
- [ ] обрабатывает только `REMINDER`;
- [ ] получает User по `post.user_id`;
- [ ] использует `telegram_user_id`;
- [ ] отправляет текст напоминания;
- [ ] отправляет фото;
- [ ] использует `telegram_file_id`;
- [ ] не скачивает фото;
- [ ] поддерживает caption;
- [ ] работает без caption;
- [ ] обрабатывает отсутствие User;
- [ ] обрабатывает отсутствие `file_id`;
- [ ] обрабатывает Telegram API error;
- [ ] возвращает `PublishResult`;
- [ ] не выполняет atomic claim самостоятельно;
- [ ] не управляет Scheduler;
- [ ] не меняет финальный статус напрямую вне publication layer;
- [ ] логирует начало, успех и ошибку;
- [ ] unit tests проходят;
- [ ] integration tests проходят;
- [ ] PublicationService integration проходит;
- [ ] E2E-сценарии проходят.

---

# 59. Результат SPEC-014

После реализации Reminder Mode работает по цепочке:

```text
Post
publication_type=REMINDER
        ↓
SPEC-013 Atomic Claim
        ↓
PROCESSING
        ↓
PublicationService
        ↓
ReminderPublisher
        ↓
User.telegram_user_id
        ↓
Telegram
        ↓
⏰ Напоминание
        ↓
📸 Фото
        ↓
PublishResult
        ↓
PUBLISHED / error flow
```

Следующая спецификация реализует второй MVP-вариант:

```text
SPEC-015 — Telegram Publisher
```

Он отвечает за автоматическую публикацию фотографии непосредственно в Telegram-канал.
