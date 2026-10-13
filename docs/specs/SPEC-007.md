# SPEC-007. Модель данных пользователей

**Проект:** Telegram-бот «PostMaster»
**Этап:** MVP
**Версия:** 1.0
**Статус:** к разработке

> Рабочая структурированная редакция переданного ТЗ: повторяющиеся примеры/схемы сокращены, идентификаторы требований сохранены. Полный текст предоставлен пользователем в диалоге.

## 1–5. Назначение, цель, scope и зависимости

Устойчивое соответствие Telegram `user_id` ↔ `users.telegram_user_id` ↔ внутренний `users.id`. Таблица пользователей, поиск/создание/обновление, уникальность, имя/username/timezone, даты и связь с публикациями. Вне scope: email, телефон, пароль, OAuth, роли, платежи, геолокация, удаление/блокировка аккаунта, автоопределение и интерфейс изменения timezone (SPEC-041). Используется SPEC-002/006/008/009/017/018/024.

## 6–7. Идентификация и истории

Внешний ключ идентификации — неизменяемый Telegram ID; username не идентификатор. US-007-01 — регистрация через `/start`; US-007-02 — повторный `/start` использует тот же `users.id`; US-007-03 — изменившиеся Telegram-поля обновляются; US-007-04 — публикации принадлежат только их владельцу, по `posts.user_id = users.id`.

## 8–20. Таблица и модель users

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL UNIQUE,
    username VARCHAR NULL,
    first_name VARCHAR NULL,
    timezone VARCHAR NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
```

`id` — внутренний PK для будущих внешних ключей; `telegram_user_id` — BIGINT и UNIQUE с индексом. `username`, `first_name`, `timezone` могут быть NULL. При отсутствии timezone действует `DEFAULT_TIMEZONE=Europe/Moscow` (SPEC-006); Telegram Update не меняет timezone. `created_at` не меняется после создания, `updated_at` обновляется при изменении профиля.

## 21–22. Публикации

Кардинальность User 1:N Posts (включая 0 постов). В `posts` будущий `user_id` должен ссылаться на `users.id` (`FOREIGN KEY`). Изоляция операций — через `posts.user_id = current_user.id`, не через внешнее имя/ID Telegram. Физическая модель posts — SPEC-008, создание — SPEC-009.

## 23–28. `/start` и обновление профиля

При `/start` поиск по `telegram_user_id`: если нет — одна новая запись (username/first_name из Telegram или NULL, timezone=NULL, даты=now); если есть — использовать тот же id. Изменения username и first_name, в том числе исчезновение username, записывать вместе с updated_at. При таком обновлении не менять timezone и created_at.

## 29. Бизнес-правила

- BR-007-01/02/03/04: один row на Telegram ID, username не ID и не обязателен.
- BR-007-05/06: first_name может отсутствовать; при timezone=NULL применяется default из SPEC-006.
- BR-007-07…10: повторное создание исключено; Telegram-поля могут обновляться, created_at неизменен, updated_at обновляется при изменении.
- BR-007-11/12: будущие posts ссылаются на внутренний users.id и изолированы по этому id.

## 30–38. Repository, сервис и транзакции

Операции create/get_by_telegram_id/update и get_or_create (имена методов допускают эквивалентные реализации). Handler → UserService → UserRepository → SQLAlchemy → DB, без SQL в handler. Конкурирующие обновления не создают дубль: UNIQUE в БД — окончательная защита, повторный поиск использует созданный row.

## 39–48. Индекс, примеры, безопасность

UNIQUE-индекс по `telegram_user_id` для частого запроса поиска. Допустимы username=NULL, timezone=NULL, first_name=NULL. При обновлении Telegram username/id внутренний id неизменен. Пользователь не должен читать чужую публикацию только по post_id; проверять `posts.user_id = current_user.id` (SPEC-024).

## 49. Ошибки

- ERR-007-01 USER_CREATE_FAILED: создание в БД не удалось; не выдавать фиктивный users.id, залогировать.
- ERR-007-02 DUPLICATE_TELEGRAM_USER: использовать существующую запись, не добавлять вторую.
- ERR-007-03 USER_NOT_FOUND: при обязательном существовании; `/start` может создать пользователя.

## 50. Логирование

`INFO User created telegram_user_id=123456789`, `INFO Existing user resolved telegram_user_id=123456789`, `INFO User profile updated user_id=15`. Без Bot Token и лишних персональных данных.

## 51. Acceptance Criteria

- AC-007-01…03: `/start` создаёт row, повторный не добавляет, UNIQUE исключает дубли.
- AC-007-04/05: username=NULL и first_name=NULL не мешают регистрации.
- AC-007-06/07: новый username сохраняется, users.id и telegram_user_id неизменны.
- AC-007-08/09: timezone=NULL → default, установленный timezone не теряется при изменении Telegram-полей.
- AC-007-10: posts.user_id = users.id.
- AC-007-11/12: правильный поиск по Telegram ID и отдельные id для разных пользователей.

## 52. Unit Tests

TC-007-01: новая модель User. TC-007-02: существующий User без повторного create. TC-007-03: username=None. TC-007-04: изменённый username. TC-007-05: timezone сохраняется. TC-007-06: get_by_telegram_id возвращает нужный User.

## 53–54. Integration и Concurrency

IT-007-01…03: `/start` → handler → service → repository → SQLite; два `/start` → COUNT=1; изменение username обновляет row. IT-007-04: созданный пост с `posts.user_id=users.id`. CT-007-01: два одновременных get/create дают ровно один users row.

## 55–57. E2E

Новый пользователь `/start` → row с timezone=NULL → запрос фото; существующий `/start` → тот же users.id; изменённый/удалённый username → тот же users.id и связанные публикации (последнее при наличии posts).

## 58. Инварианты

INV-007-01: Telegram ID уникален. INV-007-02: username не идентификатор. INV-007-03: users.id неизменен при обновлении. INV-007-04: posts привязаны через users.id. INV-007-05: created_at неизменен. INV-007-06: timezone не теряется при изменении имени.

## 59–60. Definition of Done и результат

Таблица users с нужными типами/nullable/UNIQUE и датами; создание/поиск/обновление без дублей; nullable Telegram-поля; сохранение timezone; внутренний ID; будущая связь User 1:N Posts; unit, integration, concurrency и E2E тесты. Модель — основа SPEC-008/009/017/018/024.
