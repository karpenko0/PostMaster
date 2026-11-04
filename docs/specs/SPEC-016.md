# SPEC-016. Абстракция Publisher

**Проект:** Telegram-бот «PostMaster»  
**Этап:** MVP  
**Версия:** 1.0  
**Статус:** к разработке

---

# 1. Назначение

SPEC-016 описывает единый контракт публикационного слоя PostMaster.

Абстракция Publisher нужна, чтобы Scheduler, Recovery и PublicationService не зависели от конкретного способа доставки контента.

В MVP поддерживаются:

```text
REMINDER → ReminderPublisher
TELEGRAM → TelegramPublisher
```

В дальнейшем архитектура должна позволить добавить:

```text
VK → VKPublisher
```

без изменения Scheduler и Recovery.

---

# 2. Цель

Система должна:

1. иметь единый интерфейс Publisher;
2. выбирать Publisher по `publication_type`;
3. использовать единый формат результата;
4. отделять бизнес-логику от Telegram API;
5. поддерживать Reminder и Telegram;
6. позволять добавлять новые Publisher без изменения Scheduler.

---

# 3. Scope

В рамках SPEC-016 реализуются:

- интерфейс `Publisher`;
- метод `publish(post)`;
- DTO `PublishResult`;
- `PublisherResolver`;
- сопоставление `publication_type → Publisher`;
- обработка неизвестного типа;
- интеграция с `PublicationService`;
- регистрация ReminderPublisher и TelegramPublisher.

---

# 4. Out of Scope

Не входят:

- Reminder Publisher — SPEC-014;
- Telegram Publisher — SPEC-015;
- VK Publisher — SPEC-039;
- Scheduler — SPEC-010;
- Recovery — SPEC-011;
- Missed Jobs — SPEC-012;
- atomic claim — SPEC-013;
- retry — SPEC-022.

---

# 5. Архитектура

Правильная цепочка:

```text
Scheduler / Recovery / Missed Job
            ↓
    PublicationService
            ↓
     PublisherResolver
            ↓
        Publisher
       /         \
Reminder      Telegram
Publisher     Publisher
            ↓
      PublishResult
```

Scheduler не должен содержать вызовы конкретных API.

---

# 6. User Stories

## US-016-01. Единый интерфейс

**Как** разработчик  
**Я хочу** использовать единый контракт Publisher  
**Чтобы** все способы доставки работали одинаково для PublicationService.

### Acceptance

Каждый Publisher реализует:

```text
publish(post) -> PublishResult
```

---

## US-016-02. Выбор Publisher

**Как** система  
**Я хочу** выбирать Publisher по `publication_type`  
**Чтобы** PublicationService не содержал Telegram-specific логику.

---

## US-016-03. Расширяемость

**Как** разработчик  
**Я хочу** добавить новый Publisher без изменения Scheduler  
**Чтобы** архитектура оставалась расширяемой.

---

# 7. Интерфейс Publisher

Рекомендуемый контракт:

```python
class Publisher(Protocol):

    def publish(
        self,
        post: Post
    ) -> PublishResult:
        ...
```

Допускается использование `ABC` вместо `Protocol`.

---

# 8. PublishResult

Общий DTO:

```python
class PublishResult:
    success: bool
    error_code: str | None
    error_message: str | None
```

При успехе:

```text
success=True
error_code=None
error_message=None
```

При ошибке:

```text
success=False
error_code=<код ошибки>
```

---

# 9. PublicationType

Для MVP:

```python
class PublicationType(str, Enum):
    REMINDER = "REMINDER"
    TELEGRAM = "TELEGRAM"
```

Будущее расширение:

```text
VK
```

---

# 10. PublisherResolver

Контракт:

```python
class PublisherResolver:

    def resolve(
        self,
        publication_type: PublicationType
    ) -> Publisher:
        ...
```

Для MVP:

```text
REMINDER → ReminderPublisher
TELEGRAM → TelegramPublisher
```

---

# 11. Пример Resolver

```python
class PublisherResolver:

    def __init__(
        self,
        reminder_publisher: ReminderPublisher,
        telegram_publisher: TelegramPublisher,
    ):
        self._publishers = {
            PublicationType.REMINDER: reminder_publisher,
            PublicationType.TELEGRAM: telegram_publisher,
        }

    def resolve(self, publication_type):
        return self._publishers[publication_type]
```

---

# 12. Неизвестный publication_type

Если Publisher не зарегистрирован:

```text
PUBLISHER_NOT_FOUND
```

или:

```text
INVALID_PUBLICATION_TYPE
```

Публикация не выполняется.

---

# 13. PublicationService

PublicationService использует Resolver.

Логика:

```text
atomic claim
↓
PENDING → PROCESSING
↓
load Post
↓
resolve Publisher
↓
publish(post)
↓
PublishResult
↓
success / error
```

---

# 14. Пример PublicationService

```python
def execute(post_id: int) -> None:

    claimed = post_repository.claim_post_for_processing(post_id)

    if not claimed:
        return

    post = post_repository.get_by_id(post_id)

    publisher = publisher_resolver.resolve(
        post.publication_type
    )

    result = publisher.publish(post)

    if result.success:
        post_repository.mark_published(
            post_id=post.id,
            published_at=now_utc()
        )
    else:
        handle_publication_error(post, result)
```

---

# 15. Разделение ответственности

## Publisher

Отвечает только за доставку контента.

Publisher не должен:

- создавать Scheduler jobs;
- выполнять Recovery;
- менять время публикации;
- выполнять atomic claim;
- управлять FSM;
- самостоятельно выбирать другой Publisher.

## PublisherResolver

Отвечает только за выбор реализации.

## PublicationService

Отвечает за:

- запуск execution flow;
- выбор Publisher через Resolver;
- обработку `PublishResult`;
- изменение финального статуса.

---

# 16. Dependency Injection

Publisher должны передаваться через DI:

```text
Application
    ↓
ReminderPublisher
TelegramPublisher
    ↓
PublisherResolver
    ↓
PublicationService
```

Resolver не должен создавать Publisher при каждом вызове.

---

# 17. Бизнес-правила

## BR-016-01

Все Publisher реализуют единый интерфейс.

## BR-016-02

Вход метода `publish` — объект `Post`.

## BR-016-03

Результат — `PublishResult`.

## BR-016-04

Publisher выбирается по `publication_type`.

## BR-016-05

Scheduler не выбирает конкретный Publisher.

## BR-016-06

Recovery и MissedJobService также не выбирают конкретный Publisher.

## BR-016-07

Финальный статус устанавливает PublicationService.

## BR-016-08

Неизвестный `publication_type` не должен приводить к публикации.

---

# 18. Sequence Flow — REMINDER

```text
PublicationService
      ↓
PublisherResolver
      ↓
REMINDER
      ↓
ReminderPublisher
      ↓
PublishResult
```

---

# 19. Sequence Flow — TELEGRAM

```text
PublicationService
      ↓
PublisherResolver
      ↓
TELEGRAM
      ↓
TelegramPublisher
      ↓
PublishResult
```

---

# 20. Будущее расширение

Для VK достаточно добавить:

```text
PublicationType.VK
```

и зарегистрировать:

```text
VK → VKPublisher
```

При этом не должны изменяться:

- Scheduler;
- Recovery;
- MissedJobService;
- основной execution flow.

---

# 21. Ошибки

## ERR-016-01. PUBLISHER_NOT_FOUND

Для `publication_type` не найден Publisher.

Результат:

- `publish()` не вызывается;
- ошибка передаётся в publication error flow.

---

## ERR-016-02. INVALID_PUBLICATION_TYPE

Получен неподдерживаемый тип публикации.

---

# 22. Логирование

Допустимо:

```text
INFO Publisher resolved post_id=42 type=TELEGRAM
```

Ошибка:

```text
ERROR Publisher not found post_id=42 publication_type=...
```

Полный объект Post логировать не требуется.

---

# 23. Acceptance Criteria

## AC-016-01

ReminderPublisher и TelegramPublisher реализуют:

```text
publish(post) -> PublishResult
```

---

## AC-016-02

Для:

```text
REMINDER
```

Resolver возвращает ReminderPublisher.

---

## AC-016-03

Для:

```text
TELEGRAM
```

Resolver возвращает TelegramPublisher.

---

## AC-016-04

Неизвестный тип не приводит к вызову Publisher.

---

## AC-016-05

Scheduler вызывает PublicationService, а не конкретный Publisher.

---

## AC-016-06

Recovery и MissedJobService не зависят от конкретной реализации Publisher.

---

## AC-016-07

Все Publisher возвращают единый `PublishResult`.

---

## AC-016-08

Финальный статус обновляется PublicationService.

---

## AC-016-09

Добавление нового Publisher не требует изменения Scheduler.

---

# 24. Unit Tests

## TC-016-01

```text
REMINDER
→ ReminderPublisher
```

## TC-016-02

```text
TELEGRAM
→ TelegramPublisher
```

## TC-016-03

Неизвестный тип:

```text
→ PUBLISHER_NOT_FOUND
```

## TC-016-04

Проверить общий метод:

```text
publish(post)
```

для обоих Publisher.

---

# 25. PublicationService Tests

При `PublishResult.success=True` должен вызываться:

```text
mark_published()
```

При:

```text
success=False
```

должен запускаться error flow.

---

# 26. Integration Tests

## IT-016-01

```text
REMINDER
→ Resolver
→ ReminderPublisher
```

## IT-016-02

```text
TELEGRAM
→ Resolver
→ TelegramPublisher
```

## IT-016-03

Проверить интеграцию:

```text
PENDING
→ PROCESSING
→ PublisherResolver
→ Publisher
→ PUBLISHED
```

---

# 27. E2E-сценарий

### REMINDER

```text
Scheduler
→ PublicationService
→ Atomic Claim
→ Resolver
→ ReminderPublisher
→ PublishResult
→ PUBLISHED
```

### TELEGRAM

```text
Scheduler
→ PublicationService
→ Atomic Claim
→ Resolver
→ TelegramPublisher
→ PublishResult
→ PUBLISHED
```

---

# 28. Инварианты

```text
INV-016-01:
все Publisher имеют единый контракт.
```

```text
INV-016-02:
Publisher выбирается по publication_type.
```

```text
INV-016-03:
Scheduler, Recovery и MissedJobService не знают деталей конкретного Publisher.
```

```text
INV-016-04:
неизвестный тип не должен приводить к случайной публикации.
```

```text
INV-016-05:
финальный статус устанавливается publication layer.
```

---

# 29. Definition of Done

SPEC-016 считается реализованной, если:

- [ ] создан интерфейс `Publisher`;
- [ ] реализован `publish(post)`;
- [ ] создан `PublishResult`;
- [ ] создан `PublisherResolver`;
- [ ] REMINDER → ReminderPublisher;
- [ ] TELEGRAM → TelegramPublisher;
- [ ] неизвестный тип обрабатывается контролируемо;
- [ ] PublicationService использует Resolver;
- [ ] Scheduler не вызывает конкретный Publisher;
- [ ] Recovery не вызывает конкретный Publisher;
- [ ] MissedJobService не вызывает конкретный Publisher;
- [ ] final status изменяется в PublicationService;
- [ ] Publisher подключаются через DI;
- [ ] архитектура готова к VKPublisher;
- [ ] unit tests проходят;
- [ ] integration tests проходят;
- [ ] E2E-сценарии проходят.

---

# 30. Результат SPEC-016

После реализации публикационный слой выглядит так:

```text
             PublicationService
                    ↓
             PublisherResolver
                    ↓
       ┌────────────┴────────────┐
       ↓                         ↓
ReminderPublisher       TelegramPublisher
       └────────────┬────────────┘
                    ↓
               PublishResult
                    ↓
             PublicationService
                    ↓
        PUBLISHED / error flow
```

Архитектура позволяет позднее добавить:

```text
VK → VKPublisher
```

без изменения Scheduler, Recovery и Missed Job flow.
