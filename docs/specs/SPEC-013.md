# SPEC-013. Защита от двойной публикации

**Проект:** Telegram-бот «PostMaster»  
**Этап:** MVP  
**Версия:** 1.0  
**Статус:** к разработке

---

# 1. Назначение

Данная спецификация описывает механизм защиты PostMaster от повторной публикации одного и того же `Post`.

Защита необходима, поскольку один и тот же Post потенциально может быть запущен несколькими путями:

- обычным Scheduler;
- Recovery после перезапуска;
- Missed Job Handler;
- повторным Scheduler job;
- конкурентными обработчиками.

Основная задача SPEC-013 — гарантировать, что конкретная публикация получит право на выполнение только один раз.

---

# 2. Цель

Система должна:

1. не допускать одновременную публикацию одного Post несколькими обработчиками;
2. использовать статус `PROCESSING` как признак того, что выполнение уже началось;
3. выполнять атомарный переход:

```text
PENDING → PROCESSING
```

4. разрешать Publisher только тому обработчику, который успешно выполнил этот переход;
5. не выполнять `PROCESSING` повторно;
6. не выполнять `PUBLISHED`;
7. не выполнять `CANCELLED`;
8. не выполнять `FAILED` обычным execution flow;
9. после успешной публикации переводить Post в `PUBLISHED`;
10. при ошибке переводить Post в `FAILED` либо обратно в `PENDING`, если ошибка признана временной в retry-flow.

---

# 3. Scope

В рамках SPEC-013 реализуются:

- атомарный захват Post;
- переход `PENDING → PROCESSING`;
- проверка количества изменённых строк;
- защита от конкурентного выполнения;
- запрет повторного запуска Publisher;
- переход `PROCESSING → PUBLISHED`;
- переход `PROCESSING → FAILED`;
- возврат `PROCESSING → PENDING` в retry-сценарии;
- обновление `updated_at`;
- заполнение `published_at`;
- сохранение `error_message`;
- интеграция с Scheduler, Recovery и Missed Job;
- тестирование конкуренции.

---

# 4. Out of Scope

В SPEC-013 не реализуются:

- Scheduler — SPEC-010;
- Recovery — SPEC-011;
- Missed Jobs — SPEC-012;
- конкретный Reminder Publisher — SPEC-014;
- Telegram Publisher — SPEC-015;
- retry-расписание — SPEC-022;
- распределённые блокировки;
- Redis Lock;
- внешние очереди;
- Kafka;
- RabbitMQ.

---

# 5. Зависимости

SPEC-013 зависит от:

- **SPEC-008** — модель Post;
- **SPEC-010** — Scheduler;
- **SPEC-011** — Recovery;
- **SPEC-012** — Missed Jobs.

Взаимодействует с:

- **SPEC-014** — Reminder Publisher;
- **SPEC-015** — Telegram Publisher;
- **SPEC-021** — обработка ошибок;
- **SPEC-022** — retry.

---

# 6. Проблема двойного выполнения

Без защиты возможен сценарий:

```text
15:00 Scheduler запускает Post 42
15:00 Recovery тоже запускает Post 42
15:00 MissedJobService тоже запускает Post 42
```

Если каждый обработчик просто проверит:

```text
status == PENDING
```

а потом вызовет Publisher, два процесса могут одновременно увидеть одинаковое состояние.

Результат:

```text
дублирующая публикация
```

---

# 7. Основной принцип

Проверка должна быть не:

```text
прочитать PENDING
↓
потом отдельно изменить статус
```

а атомарной:

```sql
UPDATE posts
SET status = 'PROCESSING'
WHERE id = :post_id
AND status = 'PENDING';
```

---

# 8. Критерий владения выполнением

После `UPDATE`:

```text
affected_rows = 1
```

означает:

```text
этот обработчик получил право на выполнение
```

Если:

```text
affected_rows = 0
```

означает:

```text
Post уже не PENDING
или другой обработчик успел раньше
```

Publisher вызывать нельзя.

---

# 9. User Stories

## US-013-01. Защита от дублей

**Как** пользователь  
**Я хочу**, чтобы один запланированный Post публиковался только один раз  
**Чтобы** в канале не появлялись дубли.

### Acceptance

Даже при двух одновременных попытках только один обработчик вызывает Publisher.

---

## US-013-02. Защита после рестарта

**Как** пользователь  
**Я хочу**, чтобы Recovery не дублировал публикацию, если она уже выполняется  
**Чтобы** перезапуск не создавал повторные посты.

### Acceptance

Если Post уже `PROCESSING`, Recovery/Missed Job не запускают Publisher.

---

## US-013-03. Корректный финальный статус

**Как** пользователь  
**Я хочу**, чтобы после успешной публикации система зафиксировала её завершение  
**Чтобы** она больше не выполнялась.

### Acceptance

После успеха:

```text
PROCESSING → PUBLISHED
```

---

# 10. Статусы, связанные со SPEC-013

Используются:

```text
PENDING
PROCESSING
PUBLISHED
FAILED
CANCELLED
```

---

# 11. Допустимые переходы

Основной happy path:

```text
PENDING
   ↓
PROCESSING
   ↓
PUBLISHED
```

Ошибка:

```text
PENDING
   ↓
PROCESSING
   ↓
FAILED
```

Временная ошибка:

```text
PENDING
   ↓
PROCESSING
   ↓
PENDING
```

Отмена до старта:

```text
PENDING
   ↓
CANCELLED
```

---

# 12. Недопустимые переходы

Без отдельного retry-flow недопустимы:

```text
PUBLISHED → PROCESSING
CANCELLED → PROCESSING
FAILED → PROCESSING
PROCESSING → PROCESSING
```

---

# 13. Atomic Claim API

Рекомендуемый Repository-контракт:

```python
claim_post_for_processing(
    post_id: int
) -> bool
```

---

# 14. Реализация claim

Логически:

```sql
UPDATE posts
SET
    status = 'PROCESSING',
    updated_at = :now_utc
WHERE id = :post_id
AND status = 'PENDING';
```

---

# 15. Результат claim

Если:

```text
rowcount == 1
```

вернуть:

```text
True
```

Если:

```text
rowcount == 0
```

вернуть:

```text
False
```

---

# 16. Почему нельзя сначала читать статус

Неправильный сценарий:

```text
Worker A: SELECT → PENDING
Worker B: SELECT → PENDING
Worker A: publish
Worker B: publish
```

Правильный:

```text
Worker A: UPDATE WHERE status=PENDING → 1 row
Worker B: UPDATE WHERE status=PENDING → 0 rows
```

Только Worker A продолжает.

---

# 17. Execution flow

Правильная последовательность:

```text
execute_post(post_id)
    ↓
atomic claim
    ↓
claimed?
 ┌──┴──┐
 │     │
no    yes
 │     │
return │
       ▼
load current Post
       ↓
Publication Service
       ↓
success / failure
       ↓
final status
```

---

# 18. Повторная загрузка после claim

После успешного claim допустимо перечитать Post из БД, чтобы Publication Service получил актуальную модель.

---

# 19. Publication Service

Scheduler, Recovery и Missed Job не должны сами менять Post в `PROCESSING`.

Желательно централизовать это в publication layer.

Например:

```python
publication_service.execute(post_id)
```

---

# 20. Контракт PublicationService

Допустимый интерфейс:

```python
class PublicationService:

    def execute(
        self,
        post_id: int
    ) -> None:
        ...
```

Алгоритм:

```text
claim PENDING → PROCESSING
↓
если claim false → return
↓
load Post
↓
resolve Publisher
↓
publish
↓
update final status
```

---

# 21. Успешная публикация

После успешного Publisher:

```text
status = PUBLISHED
published_at = now_utc
updated_at = now_utc
error_message = NULL
```

---

# 22. Repository API для успеха

Рекомендуемый контракт:

```python
mark_published(
    post_id: int,
    published_at: datetime
) -> None
```

---

# 23. Логический SQL успеха

```sql
UPDATE posts
SET
    status = 'PUBLISHED',
    published_at = :published_at,
    error_message = NULL,
    updated_at = :updated_at
WHERE id = :post_id
AND status = 'PROCESSING';
```

---

# 24. Почему WHERE status=PROCESSING

Это защищает от случайного финального изменения неактуального Post.

---

# 25. Ошибка публикации

Если Publisher завершился окончательной ошибкой:

```text
PROCESSING → FAILED
```

---

# 26. Repository API для ошибки

```python
mark_failed(
    post_id: int,
    error_message: str
) -> None
```

---

# 27. Логический SQL ошибки

```sql
UPDATE posts
SET
    status = 'FAILED',
    error_message = :error_message,
    updated_at = :updated_at
WHERE id = :post_id
AND status = 'PROCESSING';
```

---

# 28. Временная ошибка

Если SPEC-022 определяет ошибку как временную:

```text
PROCESSING → PENDING
```

---

# 29. Repository API возврата в PENDING

```python
release_post_for_retry(
    post_id: int,
    error_message: str | None
) -> None
```

---

# 30. Логический SQL retry release

```sql
UPDATE posts
SET
    status = 'PENDING',
    error_message = :error_message,
    updated_at = :updated_at
WHERE id = :post_id
AND status = 'PROCESSING';
```

---

# 31. Отмена и race condition

Возможен сценарий:

```text
User нажимает cancel
и одновременно Scheduler запускает Post
```

Если Scheduler уже успешно перевёл:

```text
PENDING → PROCESSING
```

то `/cancel`, работающий только с `PENDING`, не должен считать такую запись успешно отменённой.

Это предотвращает состояние:

```text
Publisher уже работает
но Post стал CANCELLED
```

---

# 32. Главное правило отмены

Для MVP безопасная бизнес-модель:

```text
отменяется только PENDING Post
```

---

# 33. Atomic cancel

Логически:

```sql
UPDATE posts
SET
    status = 'CANCELLED',
    updated_at = :now
WHERE id = :post_id
AND user_id = :user_id
AND status = 'PENDING';
```

Если `rowcount=0`, отмена не выполнена.

---

# 34. Связь с SPEC-018

SPEC-018 должна использовать атомарную отмену PENDING Post, а не безусловный `UPDATE`.

---

# 35. Бизнес-правила

## BR-013-01

Publisher можно вызвать только после успешного atomic claim.

---

## BR-013-02

Atomic claim разрешён только из `PENDING`.

---

## BR-013-03

Если claim изменил 0 строк, Publisher не вызывается.

---

## BR-013-04

После успешного claim статус становится `PROCESSING`.

---

## BR-013-05

Успех переводит `PROCESSING → PUBLISHED`.

---

## BR-013-06

Окончательная ошибка переводит `PROCESSING → FAILED`.

---

## BR-013-07

Временная ошибка может перевести `PROCESSING → PENDING` в рамках retry-flow.

---

## BR-013-08

`PUBLISHED` никогда не возвращается в обычный execution flow.

---

## BR-013-09

`CANCELLED` никогда не переводится в `PROCESSING`.

---

## BR-013-10

Один Post может иметь только одного успешного владельца execution.

---

# 36. Sequence Flow — успешный claim

```text
Worker A
   │
   │ UPDATE ... WHERE status=PENDING
   ▼
Database
   │
   │ rowcount=1
   ▼
PROCESSING
   │
   ▼
Publisher
   │
   ▼
PUBLISHED
```

---

# 37. Sequence Flow — конкурентный Worker

```text
Worker B
   │
   │ UPDATE ... WHERE status=PENDING
   ▼
Database
   │
   │ rowcount=0
   ▼
SKIP
```

---

# 38. Sequence Flow — два одновременных запуска

```text
            Post=PENDING
               │
        ┌──────┴──────┐
        │             │
     Worker A       Worker B
        │             │
 atomic claim     atomic claim
        │             │
    row=1          row=0
        │             │
   Publisher         STOP
        │
   PUBLISHED
```

---

# 39. Sequence Flow — ошибка Publisher

```text
PENDING
  ↓
PROCESSING
  ↓
Publisher error
  ↓
FAILED
```

или при временной ошибке:

```text
PENDING
  ↓
PROCESSING
  ↓
temporary error
  ↓
PENDING
```

---

# 40. Работа с транзакциями

Atomic claim должен выполняться как единая операция БД.

Нельзя делать:

```text
SELECT status
↓
if PENDING
↓
UPDATE status
```

между которыми другой worker может вмешаться.

---

# 41. SQLite

Для MVP используется SQLite.

Даже при SQLite логика атомарного conditional UPDATE обязательна.

Это обеспечивает корректность при конкурентных обработчиках в рамках допустимой модели MVP.

---

# 42. PostgreSQL compatibility

Та же модель:

```sql
UPDATE ... WHERE status='PENDING'
```

должна без изменения бизнес-логики работать после перехода на PostgreSQL.

---

# 43. Логирование claim

Успешный claim:

```text
INFO Post claimed for processing post_id=42
```

Неуспешный:

```text
INFO Post claim skipped post_id=42
```

---

# 44. Логирование успеха

```text
INFO Post published post_id=42
```

---

# 45. Логирование ошибки

```text
ERROR Post publication failed post_id=42
```

---

# 46. Логирование retry release

```text
WARNING Post returned to pending for retry post_id=42
```

---

# 47. Ошибки

## ERR-013-01. CLAIM_FAILED

Atomic claim вернул 0 строк.

Это не обязательно техническая ошибка.

Может означать:

- Post уже `PROCESSING`;
- Post уже `PUBLISHED`;
- Post `CANCELLED`;
- Post `FAILED`;
- Post не существует.

Результат:

```text
Publisher не вызывается
```

---

## ERR-013-02. POST_NOT_FOUND_AFTER_CLAIM

Если claim успешен, но Post затем не удалось получить, ошибка логируется.

---

## ERR-013-03. FINAL_STATUS_UPDATE_FAILED

Publisher сработал, но финальный status update не удался.

Это критическая ошибка состояния и должна логироваться.

---

# 48. Acceptance Criteria

## AC-013-01 — успешный claim

**Given**:

```text
status=PENDING
```

**When** вызывается `claim_post_for_processing`  
**Then**:

```text
status=PROCESSING
```

и метод возвращает `True`.

---

## AC-013-02 — повторный claim

**Given**:

```text
status=PROCESSING
```

**When** вызывается claim  
**Then** возвращается `False`.

---

## AC-013-03 — PUBLISHED claim

`PUBLISHED` не переводится в `PROCESSING`.

---

## AC-013-04 — CANCELLED claim

`CANCELLED` не переводится в `PROCESSING`.

---

## AC-013-05 — FAILED claim

`FAILED` не переводится в `PROCESSING` обычным flow.

---

## AC-013-06 — конкурентный запуск

**Given** два обработчика одновременно пытаются захватить один `PENDING` Post  
**Then** только один получает `True`.

---

## AC-013-07 — один Publisher

При конкурентном запуске Publisher вызывается ровно один раз.

---

## AC-013-08 — success

После успешного Publisher:

```text
status=PUBLISHED
published_at != NULL
```

---

## AC-013-09 — final error

После окончательной ошибки:

```text
status=FAILED
error_message != NULL
```

---

## AC-013-10 — temporary error

Если retry-flow признаёт ошибку временной:

```text
PROCESSING → PENDING
```

---

## AC-013-11 — updated_at

Каждый статусный переход обновляет:

```text
updated_at
```

---

## AC-013-12 — cancel race

Если Post уже `PROCESSING`, обычная отмена PENDING Post не должна перевести его в `CANCELLED`.

---

# 49. Unit Tests

## TC-013-01

Вход:

```text
PENDING
```

Ожидание:

```text
claim=True
status=PROCESSING
```

---

## TC-013-02

Вход:

```text
PROCESSING
```

Ожидание:

```text
claim=False
```

---

## TC-013-03

Вход:

```text
PUBLISHED
```

Ожидание:

```text
claim=False
```

---

## TC-013-04

Вход:

```text
CANCELLED
```

Ожидание:

```text
claim=False
```

---

# 50. Repository Tests

## RT-013-01 — atomic claim

Создать PENDING Post.

Выполнить claim.

Проверить:

```text
rowcount=1
status=PROCESSING
```

---

## RT-013-02 — second claim

Повторить claim.

Проверить:

```text
rowcount=0
```

---

## RT-013-03 — mark published

Из `PROCESSING`:

```text
→ PUBLISHED
```

Проверить `published_at`.

---

## RT-013-04 — mark failed

Проверить:

```text
PROCESSING → FAILED
```

---

## RT-013-05 — retry release

Проверить:

```text
PROCESSING → PENDING
```

---

# 51. Concurrency Test

## CT-013-01 — два worker

1. Создать один:

```text
Post=PENDING
```

2. Одновременно запустить два claim.
3. Первый результат:

```text
True
```

4. Второй:

```text
False
```

или наоборот.

Итог:

```text
ровно один True
```

---

# 52. Concurrency Test с PublicationService

1. Создать один `PENDING`.
2. Два потока вызывают:

```text
publication_service.execute(post_id)
```

3. Atomic claim допускает один поток.
4. Mock Publisher вызывается:

```text
1 раз
```

---

# 53. Integration Test с Scheduler

1. Scheduler запускает Post.
2. Дополнительно вручную запускается второй execution.
3. Только один процесс получает claim.
4. Publisher вызывается один раз.

---

# 54. Integration Test с Recovery

1. Scheduler job существует.
2. Recovery ошибочно/параллельно инициирует тот же Post.
3. Atomic claim разрешает только одно выполнение.

---

# 55. Integration Test с Missed Job

1. MissedJobService запускает Post.
2. Одновременно обычный Scheduler запускает тот же Post.
3. Только один вызывает Publisher.

---

# 56. Integration Test с Cancel

1. Post=`PENDING`.
2. Одновременно:
   - execution делает claim;
   - пользователь делает cancel.
3. Допустим только один успешный переход из `PENDING`.

Результат должен быть либо:

```text
PROCESSING
```

либо:

```text
CANCELLED
```

но не оба.

---

# 57. E2E-сценарий — нормальная публикация

1. Post=`PENDING`.
2. Наступает время.
3. PublicationService делает atomic claim.
4. Post=`PROCESSING`.
5. Publisher успешно работает.
6. Post=`PUBLISHED`.
7. `published_at` заполнен.
8. Повторный execution ничего не публикует.

---

# 58. E2E-сценарий — два запуска

1. Post=`PENDING`.
2. Scheduler и Recovery почти одновременно вызывают execution.
3. Scheduler получает claim.
4. Recovery получает `False`.
5. Scheduler публикует.
6. Post=`PUBLISHED`.
7. Внешняя публикация произошла один раз.

---

# 59. E2E-сценарий — ошибка

1. Post=`PENDING`.
2. Claim успешен.
3. Post=`PROCESSING`.
4. Publisher возвращает окончательную ошибку.
5. Post=`FAILED`.
6. `error_message` заполнен.
7. Обычный Scheduler повторно его не выполняет.

---

# 60. E2E-сценарий — retry

1. Post=`PENDING`.
2. Claim успешен.
3. Publisher получает временную ошибку.
4. Retry policy разрешает повторную попытку.
5. Post возвращается в:

```text
PENDING
```

6. Следующая попытка снова должна пройти через atomic claim.

---

# 61. Инварианты

```text
INV-013-01:
Publisher нельзя вызвать без успешного claim.
```

```text
INV-013-02:
только PENDING может стать PROCESSING.
```

```text
INV-013-03:
один Post имеет максимум одного активного execution owner.
```

```text
INV-013-04:
PUBLISHED никогда не выполняется повторно обычным flow.
```

```text
INV-013-05:
CANCELLED никогда не выполняется.
```

```text
INV-013-06:
финальный успех фиксируется только из PROCESSING.
```

```text
INV-013-07:
конкурентный запуск не должен приводить к двойному Publisher call.
```

---

# 62. Definition of Done

SPEC-013 считается реализованной, если:

- [ ] реализован atomic claim;
- [ ] claim выполняется условным UPDATE;
- [ ] claim работает только для `PENDING`;
- [ ] успешный claim переводит в `PROCESSING`;
- [ ] rowcount определяет владельца execution;
- [ ] Publisher вызывается только после `claim=True`;
- [ ] `PROCESSING` повторно не выполняется;
- [ ] `PUBLISHED` повторно не выполняется;
- [ ] `CANCELLED` не выполняется;
- [ ] `FAILED` не запускается обычным flow;
- [ ] успех переводит `PROCESSING → PUBLISHED`;
- [ ] заполняется `published_at`;
- [ ] ошибка переводит `PROCESSING → FAILED`;
- [ ] временная ошибка может вернуть в `PENDING`;
- [ ] `updated_at` обновляется;
- [ ] cancel работает атомарно для `PENDING`;
- [ ] unit tests проходят;
- [ ] repository tests проходят;
- [ ] concurrency tests проходят;
- [ ] Scheduler integration tests проходят;
- [ ] Recovery integration tests проходят;
- [ ] Missed Job integration tests проходят;
- [ ] E2E-сценарии проходят.

---

# 63. Результат SPEC-013

После реализации PostMaster получает центральную гарантию идемпотентного выполнения:

```text
любое количество execution попыток
            ↓
       Atomic Claim
            ↓
      только один True
            ↓
        Publisher
            ↓
        PUBLISHED
```

То есть даже если одновременно сработают:

```text
Scheduler
Recovery
Missed Job
```

один и тот же `Post` не должен быть опубликован дважды.

Следующие спецификации используют этот механизм непосредственно перед отправкой контента:

```text
SPEC-014 — Reminder Publisher
SPEC-015 — Telegram Publisher
```
