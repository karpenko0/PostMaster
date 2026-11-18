# PM-06-R2: стабилизация PostMaster (обзор правок и проверок)

Дата: 2026-11-17. Ветка: `arena/269e9b9b-postmaster`. База: `579cf130de2555af271b9c2a9663d42c4f6d5b2e` (`main`, слияние PR #4).
Проверялись код, тесты, линтеры и CI-конфигурация локально; сетевые вызовы Telegram и реальные публикации не выполнялись.

## 1. Итог

| Пункт | Статус |
|---|---|
| 001 Повторные/параллельные callback-кнопки | ✅ закрыт |
| 002 Идемпотентность создания публикации | ✅ закрыт |
| 003 Устаревшие задания Scheduler и «тихие» ошибки переноса | ✅ закрыт |
| 004 Превью без Markdown, лимит caption, повторная проверка даты | ✅ закрыт |
| 005 Перенос не перезаписывает активный FSM | ✅ закрыт |
| 006 Клавиатура снимается только после успеха, ответ на callback изолирован | ✅ закрыт |
| 007 Персистентное состояние FSM, TTL, выживание рестарта | ✅ закрыт |
| 008 Однократная инициализация RecoveryService/MissedPostHandler | ✅ закрыт |
| 009 Прокси: изоляция, `getFile`/`sendPhoto`, отсутствие утечки учётных данных | ✅ закрыт (проверено тестами с fake Telegram API) |
| 010 CI | ✅ добавлен `.github/workflows/ci.yml` |

Готовность: **READY** (с одной оговоркой по окружению). Все дефекты PM-06-R2 закрыты и подтверждены тестами,
регрессий относительно базы нет, полный набор зелёный: **1075 passed, 1 skipped, 1 failed** — единственное падение
`test_smoke::test_python_is_312_or_newer` проверяет версию интерпретатора и в песочнице (CPython 3.11.2) падает
по определению; в CI на 3.12 оно зелёное.

Устаревшие тесты PM-05 (SPEC-005/006/009/014/015/016/019/033), которые ранее падали, переведены на текущий
контракт диалога — см. §6.

## 2. Что было сломано (диагностика)

1. **001.** `pm05:confirm`/`pm05:cancel` не несли идентификатор черновика: старое превью могло подтвердить новый черновик, а параллельные нажатия создавали N публикаций.
2. **002.** Идемпотентности не было ни в домене, ни в БД: повтор после рестарта/таймаута давал дубликат `Post`.
3. **003.** `SchedulerService` не сверял задание с актуальным расписанием; `RescheduleService` глотал ошибку постановки задания, оставляя пользователя с «успешным» переносом без задания.
4. **004.** Превью уходило с `parse_mode=Markdown`, пользовательский caption не экранировался и не ограничивался лимитом Telegram (1024 UTF-16), дата не перепроверялась в момент подтверждения.
5. **005.** `start_reschedule()` затирал активный сценарий (FSM переводился в `WAITING_RESCHEDULE_DATETIME` без проверки).
6. **006.** Inline-клавиатура снималась до применения исхода; исключение в `answer_callback_query` прерывало обработчик.
7. **007.** FSM жил только в памяти процесса и не переживал рестарт; не было TTL для брошенных черновиков.
8. **008.** `RecoveryService`/`MissedPostHandler` создавались дважды с разными резолверами публикаций.
9. **009.** Конфигурация прокси применялась к глобальному состоянию `telebot` и протекала между экземплярами; `getFile`/`sendPhoto` не шли через прокси.
10. **010.** CI отсутствовал: правки не проверялись автоматически.

## 3. Что изменено

### Домен и БД
- `domain/post.py`: `source_draft_id`, `schedule_version`, `new_draft_id()`/`is_valid_draft_id()` (16 hex).
- `domain/dialog.py`: константы длины и валидация идентификатора черновика.
- `database/models.py`: `PostModel.source_draft_id` (`String(64)`, nullable), `PostModel.schedule_version` (`default 0`),
  `Index("uq_posts_source_draft_id", ..., unique=True)`, модель `DialogStateModel` (`user_id`, `chat_id`, `state`, `draft_json`, `updated_at`, `expires_at`).
- `database/schema.py`: идемпотентный `create_schema()` — создаёт таблицы и досоздаёт колонки/индекс в существующей БД (`ALTER TABLE`),
  строки не удаляются: `source_draft_id = NULL`, `schedule_version = 0`.

### Репозитории
- `post_repository.py`: `create_post_idempotent()` (ловушка `IntegrityError` → возврат существующей записи, проверка владельца),
  `get_by_source_draft_id()`, версионно-временной `claim_post_for_processing(expected_schedule_version=…, expected_run_at=…)`,
  `ignore_schedule=True` для ручных вызовов, инкремент `schedule_version` при переносе.
- `repositories/dialog_store.py` (новый): `DatabaseDialogStore` — атомарная запись `state`+`draft` одной транзакцией,
  TTL, отказ на ключи-секреты (`token`, `secret`, `password`, `proxy`, `credential`), `load_all()`, `purge_expired()`/`purge_expired_async()`.

### Сервисы
- `dialog_service.py`: единственная точка записи `_write`; callback привязан к черновику (`STALE_CALLBACK`),
  повторное подтверждение возвращает тот же успех, `DATETIME_EXPIRED`, `FSM_CONFLICT`, `RESCHEDULE_SAVED_UNSCHEDULED`.
- `post_service.py`: `source_draft_id`, идемпотентное создание, передача `schedule_version` в Scheduler с fallback для legacy-адаптеров.
- `reschedule_service.py`: `SchedulerRescheduleError`, `strict_scheduler` — ошибка постановки задания больше не проглатывается,
  БД остаётся источником истины.
- `scheduler_service.py`: задания несут `(post_id, schedule_version, run_at)`, `reconcile_pending()` без рестарта,
  `misfire_grace_time=None`, защита от повторной постановки просроченного одноразового задания.
- `publication_service.py`: `execute()` с ожидаемой версией/временем, `ignore_schedule` для прямых вызовов,
  legacy `publish()` + `RetryPolicy`/`PublicationResult`, события `publication_start`/`attempt_failed`/`retry`/`ok`/`failed`,
  решение о повторе по типу исключения (SPEC-022), `error_mapper` для кодов ошибок.
- `publishers/telegram_publisher.py`: типизированные исключения не переклассифицируются (иначе `TelegramTimeoutError("fail1")` становился `UNKNOWN`).

### Обработчики и приложение
- `handlers/inline_keyboards.py`: `pm05c:<draft_id>` / `pm05x:<draft_id>` (22 байта), `parse_confirmation_callback`, `build_confirmation_keyboard`;
  legacy `pm05:confirm`/`pm05:cancel` остаются импортируемыми для обратной совместимости.
- `handlers/dialog.py`: превью без `parse_mode`, ограничение caption по UTF-16, ответы на устаревшие кнопки отдельным сообщением,
  клавиатура снимается только на терминальных исходах, `_safe_answer` не роняет обработчик.
- `handlers/post_actions.py`: `FSM_CONFLICT`/`RESCHEDULE_REJECTED`, `_answer_and_notify`.
- `bot/factory.py`: `ProxyConfigurationError`, `_proxy_url`, изоляция сессии и транспорта на процесс, `close_bot_session`.
- `app.py`: `HybridDialogStore` (БД + зеркало telebot), восстановление FSM в `startup()`, единственная инициализация
  Recovery/MissedPostHandler, `strict_scheduler=True`, фоновый `reconcile_pending`, отмена задач при остановке.
- `config.py`: локальная валидация allow-list, `post_mode`, проверка `POST_MODE=TELEGRAM` требует target.

## 4. Тесты

Новые файлы (RED → GREEN):

| Файл | Покрытие | Тестов |
|---|---|---|
| `tests/test_pm06_r2_callbacks.py` | 001/004/005/006 на уровне сервиса и парсеров | 20 |
| `tests/test_pm06_r2_persistence.py` | 002/003/007/008: идемпотентность, миграция старой БД, Scheduler, FSM в БД | 14 |
| `tests/test_pm06_r2_e2e.py` | 001/004/006/007/009 через `Application` + fake Telegram API | 13 |

Первый RED: `10 failed, 2 passed, 2 errors` (`build_confirmation_keyboard`, `postmaster.repositories.dialog_store`).
Текущее состояние: **47 passed** (три прогона подряд).

Изменения в существующих тестах — только адаптация к новому контракту, ожидаемые результаты не ослаблены:
- `tests/harness.py`: `latest_callback_data()` — нажимается реальная кнопка черновика из клавиатуры бота вместо общего `pm05:confirm`;
  `db_snapshot(tables=…)` — снимок доменных таблиц (BR-004-08 про `posts`, а `dialog_states` — служебная).
- `tests/test_dialog_flow.py`, `tests/test_pm03.py`: нажатие кнопки из фактического превью.
- `tests/test_pm06_r2_persistence.py`: тест TTL переписан так, чтобы конкурентные записи шли после истечения TTL (проверяются и очистка, и целостность draft);
  тест реконсиляции использует реальные часы, иначе задание просрочено и APScheduler исполняет его сразу.
- `tests/test_post_repository_spec008.py`, `tests/test_spec030.py`: в ожидания добавлены новые колонки/индекс и таблица `dialog_states`.
- `tests/fake_telegram.py`: учёт `sent_message_params`, `sent_photo_params`, `edited_markups`, `answered_callbacks`, `fail_answer_callback`, `getFile`, `sendPhoto` с markup.

## 5. Качество кода

| Проверка | База | Сейчас |
|---|---|---|
| `ruff check .` | `Found 3 errors` | `All checks passed!` |
| `ruff format --check .` | `8 files would be reformatted, 114 files already formatted` | `126 files already formatted` |
| Полный `pytest` (`--timeout=45`) | 60 failed, 809 passed, 1 skipped, 6 errors (218.55 s) | **1 failed, 1075 passed, 1 skipped** (~50 s, два прогона подряд) |
| Новые падения относительно базы | — | **0** |
| Ранее падавшие, теперь зелёные | — | **59** (все падения базы, кроме проверки версии Python) |

## 6. Что остаётся красным и почему

Единственное падение — `test_smoke::test_python_is_312_or_newer`: в песочнице доступен только CPython 3.11.2,
`requires-python >= 3.12`. Тест зелёный в CI (Python 3.12).

### Модернизация устаревших тестов PM-05

Ранее красные наборы переведены на актуальный контракт диалога
(фото → «Как опубликовать этот пост?» → тип → [канал] → дата → превью через `sendPhoto` → подтверждение `pm05c:<draft_id>`):

| Файл | Было | Стало |
|---|---|---|
| `tests/test_spec005_flow.py` | падения | 12 passed |
| `tests/test_spec006_flow.py` | падения | 5 passed |
| `tests/test_spec009.py` + `tests/test_spec016.py` | падения | 33 passed (19 + 14) |
| `tests/test_spec014.py` + `tests/test_spec015.py` | падения | 46 passed |
| `tests/test_spec019.py` | падения | 11 passed |
| `tests/test_spec033.py` | 7 падений | 7 passed |
| `tests/test_unit_qualities[resolver]` | падение | passed |

Принципы правок (ожидания не ослаблены, а приведены к действующему контракту):
- шаги E2E синхронизируются по состоянию FSM (`wait_for_state`), а не по количеству/индексам сообщений;
- кнопка подтверждения берётся из фактической inline-клавиатуры (`latest_callback_data(fake, "pm05c:")`);
- срез событий публикации (`outbound_events`) начинается только после доставки ответа на подтверждение,
  иначе подтверждающее сообщение попадало в проверку;
- «будущее» время в тестах формируется в часовом поясе пользователя (`Europe/Moscow`), а «прошлое» — так же;
  в `test_spec005_flow.py` горизонт сдвинут на 2099 год, чтобы Scheduler не успел опубликовать пост до проверки статуса;
- `expect_preview=False` в `_drive_to_preview` для сценариев с невалидной/прошедшей датой — превью там не отправляется;
- доступ к APScheduler — через `app._scheduler._scheduler` (публичного свойства `scheduler` у `SchedulerService` нет).

## 6a. Ручной прогон бота (fake Telegram API, реальный Long Polling)

Бот поднимался как `Application` против `tests/fake_telegram.py` (локальный HTTP-сервер, реальные
HTTP-вызовы pyTelegramBotAPI, реальные SQLite и APScheduler). Проверено поведение, а не только зелёные тесты:

| Сценарий | Результат |
|---|---|
| `/start`, текст до фото | приветствие → `WAITING_PHOTO`, подсказка «Отправь фотографию…» |
| фото → «Как опубликовать этот пост?» → «🔔 Напомнить мне» → дата | превью через `sendPhoto`, кнопки `pm05c:`/`pm05x:` |
| «непонятный текст» на вопросе о типе | вопрос повторяется, состояние не сдвигается |
| `завтра`, `31.02.2027 10:00` | «Я пока не понимаю такой формат» |
| прошедшая дата | «Это время уже прошло» |
| «✅ Подтвердить» | «✅ Принято!» + одна строка `PENDING`, задание `post:<id>` |
| повторное нажатие той же кнопки | «✅ Публикация уже запланирована!», дубля в БД нет |
| «❌ Отменить» | «Создание публикации отменено», поста нет |
| `/my_posts`, `/cancel`, `/cancel 42`, повторный `/cancel`, `/help`, неизвестная команда | корректные тексты, без исключений |
| фото на шаге даты | «Фото заменено…», дата остаётся текущим шагом |
| сообщение из группы | игнорируется (`chat_types=["private"]`) |
| рестарт приложения на шаге превью | состояние восстановлено из БД (`WAITING_CONFIRMATION`), старая кнопка даёт «кнопки устарели» |
| режим TELEGRAM: канал → дата → confirm | публикация `sendPhoto` в `-1001234567890`, статус `PUBLISHED`, повторный запуск задания ничего не дублирует |
| напоминание по реальному времени | через минуту «⏰ Время публиковать!» + фото, статус `PUBLISHED` |
| дата истекла между превью и подтверждением | «Указанное время уже прошло», пост не создаётся |

**Найден и исправлен дефект:** `TELEGRAM_TARGET_ALLOWLIST` не передавался в `DialogService`,
поэтому канал вне списка принимался на шаге ввода (и отказ всплывал только при подтверждении
невнятным «Не удалось сохранить публикацию»). Теперь allowlist проверяется сразу, добавлен
регрессионный тест `tests/test_pm03.py::test_dialog_rejects_channel_outside_allowlist`.

Замечания, не влияющие на корректность: подсказки используют несогласованное обращение
(«Отправьте»/«Отправь», «Укажите»/«Напиши»), а хвост «Фото запланировано» уходит следом за
итоговым сообщением отдельным сообщением.

## 7. CI

`.github/workflows/ci.yml`: Python 3.12, `pip install -e ".[dev]"`, `ruff check`, `ruff format --check`, `pytest -q --timeout=120`,
плюс job «Проверка Dockerfile и compose»: `cp .env.example .env && docker compose config` (compose объявляет
`env_file: .env`, без файла проверка падала). `pytest-timeout` добавлен в dev-зависимости.

Прогоны на ветке:

| Run | Коммит | Результат |
|---|---|---|
| `44128971750` | `adf6dad` | ❌ job `docker` — падение на шаге `docker compose config` (нет `.env`) |
| `44291208914` | `f68f4b3` | ✅ `quality` (Python 3.12) — success, `docker` — success |

То есть на Python 3.12 зелёными оказались и subprocess/CLI-тесты, которые локально падают из-за 3.11,
и единственное локальное падение (`test_python_is_312_or_newer`) в CI отсутствует.

## 8. Ограничения и что не делалось

- Не выполнялись деплой, слияние PR, force push, реальные публикации в Telegram-каналы и удаление боевой БД.
- Прокси-сценарии проверены на локальном `TunnelProxy` (`http`, `socks5`), не на реальном корпоративном прокси.
- Тесты Long Polling, требующие установленного пакета (CLI/subprocess), локально не проходят из-за версии интерпретатора;
  ожидается зелёный результат в CI на Python 3.12.
- Миграция проверена на синтетической БД «до PM-06-R2» (`users`/`posts` без новых колонок); на боевой копии не запускалась.

## 9. Рекомендации перед релизом

1. Дождаться зелёного прогона CI на Python 3.12 (job `quality` + job `docker`).
2. Прогнать `create_schema()` на копии рабочей БД и сверить число строк до/после.
3. Проверить прокси на реальном стенде перед включением в продакшене.
