# Проверка SPEC-023 — Безопасность и управление секретами

## Объём и результат

SPEC-023 в основном была реализована ранее: `Settings` с `SecretStr` для токена,
`.gitignore`/`dockerignore` исключают `.env`, `RedactingFormatter` маскирует токены
в логах, `ConfigError` не эхом выводит значения. Добавлены:

- `Settings.secret_values` — централизованный список секретов для редактирования
  в логах (токен + пароль из `DATABASE_URL` при наличии).
- `app.main()` использует `settings.secret_values` вместо ручного `[token]`.
- `tests/test_spec023.py` — 28 тестов, покрывающих §7/§8 SPEC-023.

Полный прогон тестов: **646 passed**, 1 skipped. `ruff check .` и
`ruff format --check .` — успешно.

Исходный текст SPEC-023: `docs/specs/SPEC-023-безопасность-и-управление-секретами.md`.

## Матрица приёмки

| Проверяемый критерий | Доказательство | Вывод |
|---|---|---|
| Нет hardcoded secret в `src/` | `test_no_hardcoded_tokens_in_source` | ✅ Regex-сканер всех `*.py` в `src/postmaster/` |
| Secrets не в Git | `test_gitignore_excludes_env_but_keeps_example` | ✅ `.gitignore` содержит `.env`/`.env.*` и `!.env.example` |
| Secrets не в `.env.example` | `test_env_example_has_no_real_token` | ✅ Файл содержит `BOT_TOKEN=` (пусто) и подсказки |
| Secrets не в Dockerfile | `test_no_hardcoded_tokens_in_dockerfile` | ✅ |
| Secrets не в `docker-compose.yml` | `test_docker_compose_uses_env_file`, `test_no_hardcoded_tokens_in_docker_compose` | ✅ Используется `env_file: .env` |
| `.dockerignore` исключает `.env` | `test_dockerignore_excludes_env_files` | ✅ |
| Secrets не в logs | `test_token_redacted_in_log_output`, `test_database_url_password_redacted` | ✅ `RedactingFormatter` + `Settings.secret_values` |
| Missing token останавливает startup | `test_missing_token_raises_config_error`, `test_main_exits_with_code_2_when_token_missing`, `test_subprocess_does_not_leak_token_to_stdout` | ✅ `ConfigError` → `SystemExit(2)`; `main()` в subprocess без `BOT_TOKEN` → код 2, токена в выводе нет |

## Обязательные проверки §8

| Проверка | Тест | Статус |
|---|---|---|
| valid token | `test_valid_token_is_loaded_from_environment`, `test_valid_token_via_dotenv_file` | ✅ |
| missing token | `test_missing_token_raises_config_error`, `test_missing_token_error_does_not_echo_value` | ✅ `ConfigError` без утечки |
| empty token | `test_empty_token_raises_config_error`, `test_whitespace_token_raises_config_error`, `test_malformed_token_raises_config_error` | ✅ |
| log redaction | `test_token_redacted_in_log_output`, `test_database_url_password_redacted`, `test_token_not_in_settings_repr/json` | ✅ `RedactingFormatter` + `secret_values` |
| env override | `test_environment_overrides_env_file`, `test_env_file_is_optional` | ✅ |

## Решения по реализации

| # | Решение | Обоснование |
|---|---|---|
| D1 | `Settings.secret_values` — property, возвращает tuple всех секретов | SPEC-023 §4 BR: «централизованный Settings». Единая точка истины для redaction: и токен, и `DATABASE_URL` с `user:password@` |
| D2 | `_URL_CREDENTIALS_PATTERN` распознаёт `user:password@host` в `DATABASE_URL` | Пароль БД — тоже секрет. Если URL содержит credentials, `secret_values` возвращает URL целиком, чтобы `redact_secrets` замаскировал и логин, и пароль |
| D3 | `secret_values` исключает пустые значения и удаляет дубликаты | Устойчивость к повторным конфигурациям и пустым полям |
| D4 | `app.main()` использует `settings.secret_values` вместо `[bot_token.get_secret_value()]` | Один источник истины, не нужно синхронизировать вручную при добавлении новых секретных полей |
| D5 | Regex-сканеры для `src/`, `Dockerfile`, `docker-compose.yml` ищут `_TOKEN_PATTERN` | Гарантия, что будущий разработчик не вставит токен в код/конфигурацию (SPEC-023 §7) |
| D6 | `test_docker_compose_uses_env_file` парсит YAML regex'ом, без зависимости `pyyaml` | В проекте нет `pyyaml` в dev-зависимостях |
| D7 | Subprocess-тест `main()` без `BOT_TOKEN` проверяет код 2 и отсутствие токена в выводе | Защита от регрессии: секрет не должен оказаться ни в stdout, ни в stderr |
| D8 | `monkeypatch.chdir(tmp_path)` в startup-тестах | В проекте есть локальный `.env` для разработки, без chdir `load_settings()` нашёл бы его |

## Связь с другими SPEC

- **SPEC-001 / SPEC-002 / SPEC-026:** `Settings` — общая точка конфигурации, `secret_values` дополняет её без поломки контракта.
- **SPEC-021:** `TelegramErrorMapper` использует тот же `_TOKEN_PATTERN`, что и `Settings.secret_values`. Покрытие секретов согласованно.
- **SPEC-025:** `RedactingFormatter` принимает `secrets=settings.secret_values` — те же значения редактируются в stdout/stderr.
- **SPEC-027:** Если SQLite не используется и `DATABASE_URL` содержит `user:password@`, пароль автоматически редактируется в логах.
- **SPEC-028 (Docker):** `.dockerignore` исключает `.env`/`.env.*`; `docker-compose.yml` читает секреты из `env_file` (а не из `environment`).
- **SPEC-038 (Deployment):** Боевые секреты должны приходить из переменных окружения оркестратора (k8s Secret, systemd `EnvironmentFile=`), а не из `.env` в репозитории. `secret_values` это покрывает.

## Изменения в существующих файлах

1. `src/postmaster/config.py`:
   - добавлен `_URL_CREDENTIALS_PATTERN`;
   - добавлен `Settings.secret_values` (property);
   - docstring обновлён ссылкой на SPEC-023.
2. `src/postmaster/app.py`:
   - `main()` использует `settings.secret_values` вместо ручного списка.

## Новые файлы

- `tests/test_spec023.py` — 28 тестов, все обязательные проверки §8 + приёмочные §7.
- `docs/reviews/SPEC-023-review.md` — этот отчёт.

## Ограничения scope

- SPEC-023 не описывает ротацию токенов и хранение в Secret Manager (k8s/Vault).
  Это выходит за рамки MVP.
- Если в будущем добавится ещё одно секретное поле (например, S3-ключ),
  его нужно будет включить в `secret_values` явно — единого реестра секретов
  по типу поля сейчас нет.
- Тест `test_subprocess_does_not_leak_token_to_stdout` поднимает полный subprocess
  — занимает ~1 секунду. Альтернатива (мок `main()`) была бы быстрее, но не
  проверяла бы реальный путь запуска.

## Команды

```bash
python -m pytest tests/test_spec023.py -v -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m ruff format --check .
```
