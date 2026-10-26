# Проверка SPEC-026 — Конфигурация приложения

## Объём и результат

SPEC-026 была реализована ранее: `Settings` на базе `pydantic-settings`
централизует `BOT_TOKEN`, `DATABASE_URL`, `DEFAULT_TIMEZONE`, `LOG_LEVEL`,
`POST_MODE` и `TELEGRAM_TARGET_ID`; `load_settings()` читает `.env` без
перезаписи переменных окружения и валидирует конфигурацию на старте.
Добавлены:

- Валидация `DATABASE_URL`: значение из одних пробелов отклоняется, URL
  нормализуется (`strip()`).
- `DatabaseUrlError` в `database/engine.py` — понятная ошибка при
  некорректной строке подключения вместо `ArgumentError` из SQLAlchemy.
- `tests/test_spec026.py` — 26 тестов: обязательные проверки §8 + приёмка §7.

Полный прогон тестов: **715 passed**, 1 skipped. `ruff check .` и
`ruff format --check .` — успешно.

Исходный текст SPEC-026: `docs/specs/SPEC-026-конфигурация-приложения.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Environment имеет приоритет | `test_environment_takes_precedence_over_env_file`, `test_env_file_does_not_override_existing_environment` | ✅ `load_dotenv(env_file, override=False)` — переменная окружения не перетирается файлом |
| Default timezone Europe/Moscow | `test_default_timezone_is_europe_moscow`, `test_default_timezone_is_read_from_environment` | ✅ `DEFAULT_TIMEZONE = "Europe/Moscow"` |
| Сервисы получают Settings через init/DI | `test_services_receive_settings_via_constructor`, `test_user_service_uses_injected_default_timezone`, `test_only_config_module_reads_environment` | ✅ Значения передаются в конструктор; `os.environ`/`os.getenv` не встречаются вне `config.py` |
| Settings централизует токен, DB URL, timezone и валидирует startup | `test_valid_settings_are_loaded`, `test_settings_defaults_are_applied`, `test_application_from_settings_uses_validated_config`, `test_main_exits_with_code_2_on_invalid_config` | ✅ Валидация в `load_settings()`; `main()` завершается кодом 2 |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| valid settings | `test_valid_settings_are_loaded`, `test_settings_defaults_are_applied`, `test_env_file_provides_settings` | ✅ |
| missing token | `test_missing_token_raises_config_error`, `test_empty_token_raises_config_error`, `test_missing_token_does_not_echo_any_value`, `test_config_error_lists_all_invalid_fields` | ✅ |
| DB URL | `test_database_url_is_read_from_environment`, `test_empty_database_url_is_rejected`, `test_whitespace_database_url_is_rejected`, `test_database_url_with_credentials_is_accepted` | ✅ |
| timezone default | `test_default_timezone_is_europe_moscow`, `test_default_timezone_is_read_from_environment`, `test_invalid_default_timezone_is_rejected` (5 параметров) | ✅ |

## Централизованная конфигурация

| Переменная | Поле `Settings` | Правило | Дефолт |
|---|---|---|---|
| `BOT_TOKEN` | `bot_token: SecretStr` | формат `\d{5,}:[A-Za-z0-9_-]{20,}` | обязателен |
| `DATABASE_URL` | `database_url: str` | непустое, без пробелов | `sqlite+aiosqlite:///data/postmaster.db` |
| `DEFAULT_TIMEZONE` | `default_timezone: str` | существующий IANA-пояс | `Europe/Moscow` |
| `LOG_LEVEL` | `log_level: LogLevel` | один из DEBUG/INFO/WARNING/ERROR/CRITICAL | `INFO` |
| `POST_MODE` | `post_mode: Literal["REMINDER","TELEGRAM"]` | при `TELEGRAM` требуется `TELEGRAM_TARGET_ID` | `REMINDER` |
| `TELEGRAM_TARGET_ID` | `telegram_target_id: str \| None` | обязателен при `POST_MODE=TELEGRAM` | `None` |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `DATABASE_URL` проверяется на непустоту в `config.py`, синтаксис URL — в `database/engine.py` | Архитектурное правило (`test_architecture`): слой `config` не импортирует `sqlalchemy`. Первая попытка поместить `make_url` в `config.py` нарушила правило — проверка перенесена в слой `database`, где `sqlalchemy` разрешён |
| D2 | `DatabaseUrlError` вместо `ArgumentError` SQLAlchemy | Понятная ошибка: пользователь видит «DATABASE_URL не является корректной строкой подключения», а не внутренний трейсбек SQLAlchemy. Текст не содержит значения URL (в нём могут быть credentials) |
| D3 | Значение `DATABASE_URL` нормализуется через `strip()` | Пробелы по краям — частая ошибка в `.env`; молчаливое принятие приводит к неочевидным ошибкам подключения |
| D4 | Guard-тест `test_only_config_module_reads_environment` | Контрактная защита от регрессии: если кто-то прочитает `os.environ` в сервисе, тест упадёт. Закрепляет §1/§4 «централизованный Settings» |
| D5 | Тесты DI проверяют `UserService`, а не `Application` целиком | `Application.from_settings` проверяется отдельным e2e-тестом; здесь важна именно передача значения через конструктор |
| D6 | `_StubUserRepository` в тестах | `UserService.timezone_of` требует `find_by_telegram_id`; заглушка позволяет проверить использование внедрённого дефолта без БД |

## Связь с другими SPEC

- **SPEC-001 / SPEC-002:** `BOT_TOKEN` и `DEFAULT_TIMEZONE` — обязательные и дефолтные параметры из BR-04.
- **SPEC-023:** `SecretStr` скрывает токен в `repr`/`str`/`model_dump_json`; `Settings.secret_values` отдаёт секреты для redaction в логах.
- **SPEC-025:** `configure_logging(settings.log_level, secrets=settings.secret_values)` — уровень логирования тоже приходит из конфигурации.
- **SPEC-027:** `create_database_engine(settings.database_url)` — слой `database` отвечает за разбор строки подключения и за `DatabaseUrlError`.
- **SPEC-020 / SPEC-016:** `POST_MODE` и `TELEGRAM_TARGET_ID` управляют выбором publisher'а (`DefaultPublisherResolver`).

## Изменения в существующих файлах

1. `src/postmaster/config.py`:
   - добавлен валидатор `_check_database_url` (непустое значение, `strip()`);
   - в docstring модуля добавлена ссылка на SPEC-026.
2. `src/postmaster/database/engine.py`:
   - добавлен `DatabaseUrlError`;
   - `create_database_engine` перехватывает `ArgumentError` SQLAlchemy и сообщает
     понятную ошибку без значения URL.

## Новые файлы

- `tests/test_spec026.py` — 26 тестов.
- `docs/reviews/SPEC-026-review.md` — этот отчёт.

## Ограничения scope

- `Settings` не поддерживает несколько конфигурационных профилей (dev/prod) —
  разные значения задаются через переменные окружения.
- `default_timezone` хранится строкой, а не объектом `ZoneInfo`; проверка корректности
  IANA-названия выполняется валидатором. Тип `ZoneInfo` потребовал бы каскадных
  изменений в домене и репозиториях.
- `LOG_LEVEL` и `POST_MODE` не вынесены в отдельный объект — они уже покрыты
  `Literal`/валидаторами Pydantic.
- Проверка достижимости БД выполняется в `app.startup()` (`check_database`),
  а не в `Settings`: это сетевая проверка, а не проверка конфигурации.

## Команды

```bash
python -m pytest tests/test_spec026.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
