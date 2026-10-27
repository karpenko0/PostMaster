# Проверка SPEC-031 — Unit-тесты

## Объём и результат

SPEC-031 — мета-спецификация о качестве unit-тестов, а не о новой
функциональности. Она требует:

- §1/§4: изолированное покрытие parser/timezone/FSM/resolver/publishers/
  error mapper/retry/formatting/validation, внешние зависимости mock;
- §7: без сети, deterministic time, production secrets не используются;
- §8: unit suite для happy/error paths.

Покрытие модулей к моменту SPEC-031 уже было реализовано в SPEC-005,
006, 014–016, 021, 022, 025, 026, 029. Добавлен слой **guard-тестов**,
который превращает требования §7 в автоматически проверяемые инварианты.

Добавлено:

- `tests/test_unit_qualities.py` — 23 теста (guard-слой).
- `docs/reviews/SPEC-031-review.md` — этот отчёт.

Полный прогон: **848 passed, 1 skipped**. `ruff check .` и
`ruff format --check .` — успешно.

## Матрица приёмки

| Критерий §7 | Доказательство | Вывод |
|---|---|---|
| Без сети | `test_no_external_telegram_host_in_unit_tests`, `test_no_requests_or_urllib_in_unit_tests`, `test_runtime_external_network_is_blockable` | ✅ Внешних обращений нет; есть локальный fake Telegram API на 127.0.0.1 |
| Deterministic time | `test_parse_publication_datetime_accepts_now_argument`, `test_format_local_datetime_does_not_call_utcnow`, `test_now_argument_in_validation_is_utc_aware` | ✅ Парсер принимает `now` извне; форматирование — чистая функция |
| Production secrets не используются | `test_fake_token_matches_expected_format`, `test_no_real_token_in_source`, `test_all_tokens_in_tests_use_fake_or_none`, `test_clean_env_does_not_leak_real_token_from_dotenv` | ✅ Только fake-токены; `.env` проекта не подтягивается |

## §4 — Покрытие модулей

Матрица проверяется параметризованным тестом
`test_category_has_unit_test_covering_target`: для каждой категории
указан тест-файл и целевой символ, который обязан в нём упоминаться.

| Категория §4 | Модуль | Тест-файл | Целевой символ |
|---|---|---|---|
| parser | `domain/schedule.py` | `test_schedule_spec005.py` | `parse_publication_datetime` |
| timezone | `domain/timezone.py` | `test_timezone_spec006.py` | `convert_local_to_utc` |
| FSM | `services/dialog_service.py` | `test_dialog_fsm.py` | `DialogState` |
| resolver | `publishers/resolver.py` | `test_spec016.py` | `DefaultPublisherResolver` |
| publishers | `publishers/*` | `test_spec014.py`, `test_spec015.py` | `ReminderPublisher` + Telegram-издатель |
| error mapper | `publishers/telegram_errors.py` | `test_spec021.py` | `TelegramErrorMapper` |
| retry | `services/publication_service.py` | `test_spec022.py` | `RetryPolicy` |
| formatting | `utils/logging_config.py` | `test_logging.py` | `RedactingFormatter` |
| validation (config) | `config.py` | `test_config.py` | `load_settings` |
| validation (schedule) | `domain/schedule.py` | `test_schedule_spec005.py` | `parse_publication_datetime` |

Дополнительно: `test_all_publisher_implementations_have_unit_tests` —
явная проверка наличия тестов обоих издателей.

## §8 — happy/error paths

| Путь | Покрытие |
|---|---|
| happy | `test_parse_publication_datetime_accepts_now_argument` (будущее время принимается), `test_format_local_datetime_does_not_call_utcnow` (корректный формат) |
| error | `test_now_argument_in_validation_is_utc_aware` (naive `now` → ValueError), `test_parse_publication_datetime_accepts_now_argument` (прошлое → DateTimeValidationError) |
| граница | `test_unit_tests_use_sqlite_only` (БД только в `tmp_path`), `test_unit_test_rootdir_is_project_root` (корень проекта распознан) |

## Архитектурные решения

| # | Решение | Обоснование |
|---|---|---|
| D1 | Guard-тесты — **статический анализ** кода тестов, а не runtime-перехват всего сокета | Runtime-блокировка сломала бы `test_polling`/`test_entrypoint`, которым нужен локальный fake-сервер на 127.0.0.1. Статика надёжнее и детерминированнее |
| D2 | Допустимые исключения из «no api.telegram.org» перечислены явно | `test_app`/`test_logging`/`test_spec025` используют URL как **строку-данные** (проверка редактирования секретов, `RuntimeError` без реального запроса). Исключение задокументировано в docstring, чтобы новый файл с реальным обращением не прошёл молча |
| D3 | `test_runtime_external_network_is_blockable` — **демонстрация** перехвата, а не запрет | Показывает, что блокировка добавляется одной fixture; не ломает существующие loopback-тесты |
| D4 | `test_clean_env_does_not_leak_real_token_from_dotenv` допускает `ConfigError` | `load_settings(env_file=None)` при отсутствии `BOT_TOKEN` бросает `ConfigError` — это и есть доказательство, что `.env` не читается. Тест гибок: если `.env` нет или в нём нет токена — `skip` |
| D5 | Fake-токены в redaction-тестах (SPEC-021) добавлены в allowlist явно | `1234567890:AaBbCcDdEeFfGgHhIiJjKkLl` и `9876543210:ZzYyXxWwVvUuTtSsRrQq` — синтетические значения с повторяющимся паттерном, не встречающимся в настоящих токенах |
| D6 | Реальный токен бота в `.env` проекта **не попадает** в тесты | `load_settings(env_file=None)` не вызывает `load_dotenv`; subprocess-тесты всегда передают `BOT_TOKEN` в env явно, и `load_dotenv(override=False)` не перезаписывает переменные окружения |

## Обнаруженный риск и его смягчение

**Риск:** `tests/conftest.py::clean_env` удаляет `BOT_TOKEN` из окружения.
Если бы какой-либо тест вызвал `load_settings()` без `env_file=None`,
`load_dotenv` прочитал бы проектный `.env` и подставил настоящий токен.

**Проверено:** в тестах `load_settings()` без аргументов вызывается только
в `test_config.py:57` — это сам тест на чтение `.env` по назначению.
Все остальные вызовы используют `env_file=None` или `monkeypatch.setenv`.

**Смягчение:** добавлен guard-тест `test_clean_env_does_not_leak_real_token_from_dotenv`,
фиксирующий это свойство. При появлении нового вызова `load_settings()` без
`env_file` guard не сработает напрямую, но отдельный тест на утечку останется.

## Связь с другими SPEC

- **SPEC-005/006:** детерминированное время заложено в контракт
  `parse_publication_datetime(value, timezone, now)` — `now` передаётся явно.
  Guard-тесты фиксируют это как инвариант.
- **SPEC-014/015:** издатели (Reminder + Telegram) покрыты раздельно.
- **SPEC-021:** error mapper; synthetic-токены проверены guard-тестом.
- **SPEC-022:** retry-политика.
- **SPEC-023/025/026:** секреты в логах, редактирование, отсутствие утечек —
  guard-тест `test_all_tokens_in_tests_use_fake_or_none` покрывает
  регрессию «настоящий токен попал в тест».
- **SPEC-028:** тесты не используют production-пути — БД только в `tmp_path`
  (`test_unit_tests_use_sqlite_only`).
- **SPEC-030:** backup-тесты используют `tmp_path`, сеть не нужна.

## Изменения в существующих файлах

Нет. Добавлены только новые файлы — существующий код и тесты не менялись,
это соответствует природе SPEC-031 (guard над уже имеющимся покрытием).

## Новые файлы

- `tests/test_unit_qualities.py` — 23 теста.
- `docs/reviews/SPEC-031-review.md` — этот отчёт.

## Ограничения scope

- Guard-тесты проверяют **наличие** покрытия (файл + целевой символ), а не
  его полноту. Полнота измеряется через `pytest --cov`, что не входит в
  критерии SPEC-031.
- Статический анализ не ловит сетевой вызов, сделанный через сокет напрямую
  (без `requests`/`urllib`/`telebot`). Для этого нужен runtime-guard с
  allowlist для loopback — см. D3.
- Реальный Bot API токен в `.env` проекта внедрён в репозиторий? Проверено:
  `.env` в `.gitignore` (SPEC-023) и не отслеживается Git.
- Unit-тесты не проверяют Docker-образ и реальный Telegram-сервер — это зона
  SPEC-028 (интеграционные проверки) и ручного стенда.

## Команды

```bash
python -m pytest tests/test_unit_qualities.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
