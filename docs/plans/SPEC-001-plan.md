# План разработки SPEC-001 «Foundation и базовая архитектура»

- Спецификация: [docs/specs/SPEC-001.md](../specs/SPEC-001.md)
- Ветка: `arena/1b9daf2a-postmaster`
- Статусы: ⬜ не начато · 🔄 в работе · ✅ выполнено · ⚠️ выполнено с ограничением (см. раздел 5)

## 1. Цель

Создать каркас PostMaster, в который последующие спецификации (SPEC-002…038) добавляют функции по слоям, не нарушая правила BR-01…BR-05.

## 2. Решения, принятые при планировании

Пользователь подтвердил решения D1–D8 без изменений. Все они соответствуют SPEC-001: D1 — §5 задаёт `async def publish`; D7 — §2 требует Dockerfile и docker-compose.yml; D8 — Alembic и lock-файл не описаны в SPEC-001, поэтому §10 не позволяет добавлять их сейчас.

| № | Решение | Обоснование |
|---|---|---|
| D1 | Асинхронный стек: `AsyncTeleBot`, `AsyncIOScheduler`, SQLAlchemy async + aiosqlite | §5 SPEC-001 задаёт `async def publish`. Бот, Scheduler и БД работают в одном event loop |
| D2 | Пакет `src/postmaster/` (src-layout), слои — подпакеты `bot`, `handlers`, `domain`, `services`, `publishers`, `repositories`, `database`, `utils` | Имена `utils`, `database`, `services` слишком общие для импорта верхнего уровня и конфликтуют с пакетами PyPI |
| D3 | Единственный транспорт MVP — Long Polling за протоколом `Transport`. Webhook добавляется новой реализацией | BR-05 и п. 19 SPEC-001 |
| D4 | Настройки через `pydantic-settings` из переменных окружения. `.env` загружается через python-dotenv с `override=False` | SPEC-026: переменные окружения важнее файла. Секреты не попадают в `repr` и в тексты ошибок |
| D5 | В SPEC-001 три параметра: `BOT_TOKEN`, `DATABASE_URL`, `LOG_LEVEL` | Ограничение п. 10 SPEC-001. Остальные параметры SPEC-026 добавятся в соответствующих SPEC |
| D6 | Логи в stdout. Секреты маскируются во всех записях, включая трассировки и логгер `TeleBot` | SPEC-023, SPEC-025. Библиотека может печатать URL с токеном |
| D7 | Образ на `python:3.12-slim`, непривилегированный пользователь (uid 1000), `init: true`, `stop_grace_period` | Корректная остановка по SIGTERM и least privilege |
| D8 | Alembic и lock-файл зависимостей вне SPEC-001 | Не описаны в спецификации. Фиксируются как следующий шаг вместе с SPEC-027 |

## 3. Этапы и задачи

### Этап 0. Подготовка

| ID | Задача | Статус |
|---|---|---|
| 0.1 | Сохранить спецификацию в `docs/specs/SPEC-001.md` | ✅ |
| 0.2 | Получить интерпретатор Python 3.12 для проверок (AC-01) | ⚠️ собран из исходников CPython 3.12.15, см. раздел 5 |
| 0.3 | Составить план и зафиксировать решения (этот документ) | ✅ |

### Этап 1. Каркас проекта (AC-02, AC-03, AC-04)

| ID | Задача | Статус |
|---|---|---|
| 1.1 | `pyproject.toml`: метаданные, `requires-python >=3.12`, зависимости, dev-зависимости, точка входа `postmaster`, настройки pytest и ruff | ✅ |
| 1.2 | Структура `src/postmaster/`: 8 слоёв и корневые модули `config.py`, `app.py`, `__main__.py` с докстрингами о назначении | ✅ |
| 1.3 | `.gitignore` (`.env`, `data/`, кэши), `.dockerignore`, `.env.example` | ✅ |

### Этап 2. Конфигурация и логирование (AC-02; SPEC-023, SPEC-025, SPEC-026)

| ID | Задача | Статус |
|---|---|---|
| 2.1 | `config.py`: класс `Settings` (`BOT_TOKEN` — `SecretStr` с проверкой формата, `DATABASE_URL`, `LOG_LEVEL`). `load_settings()` с python-dotenv. Ошибки без значений переменных | ✅ |
| 2.2 | `utils/logging_config.py`: `configure_logging`, `RedactingFormatter`, `redact`. Маскировка токена в тексте и трассировке. Логгер `TeleBot` идёт через общий путь | ✅ |
| 2.3 | Тесты конфигурации и логирования | ✅ |

### Этап 3. Слои данных, Telegram и сервисов (BR-01…BR-05)

| ID | Задача | Статус |
|---|---|---|
| 3.1 | `database/`: `Base` (DeclarativeBase), `create_database_engine` (создание каталога SQLite, WAL, `busy_timeout`, `foreign_keys`), `create_session_factory`, `check_database` | ✅ |
| 3.2 | `bot/factory.py`: `create_bot` на `AsyncTeleBot`, `close_bot_session`. `bot/transport.py`: протокол `Transport` и `PollingTransport` с остановкой через отмену задачи | ✅ |
| 3.3 | `handlers/registry.py`: `register_handlers` (обработчиков пока нет, появятся в SPEC-002) | ✅ |
| 3.4 | `services/`: `SchedulerService` (обёртка над `AsyncIOScheduler`). Контракты `PostService`, `PublicationService`, `UserService` | ✅ |
| 3.5 | `publishers/base.py`: абстрактный `Publisher` с `async def publish(self, post)` | ✅ |
| 3.6 | `repositories/`, `domain/`: пакеты с докстрингами о назначении (BR-02) | ✅ |

### Этап 4. Точка входа и жизненный цикл (US-02, AC-03, AC-05)

| ID | Задача | Статус |
|---|---|---|
| 4.1 | `app.py`: класс `Application` (`startup`, `run`, `stop`, `shutdown`). Проверка токена через `get_me` перед polling. Обработка SIGINT/SIGTERM. Функция `main()` | ✅ |
| 4.2 | `__main__.py`: запуск через `python -m postmaster` | ✅ |

### Этап 5. Тесты (TC-01…TC-03)

| ID | Задача | Статус |
|---|---|---|
| 5.1 | TC-01: импорт всех модулей пакета и обязательных зависимостей; версия Python ≥ 3.12 (`tests/test_smoke.py`) | ✅ |
| 5.2 | TC-02: запуск и остановка приложения с тестовой конфигурацией (БД во временном каталоге, без сети); `run()` с подменёнными компонентами; отказ при неверном токене без утечки токена (`tests/test_app.py`) | ✅ |
| 5.3 | TC-03: архитектурные правила (матрица зависимостей слоёв, запрет циклов, ограничения на импорт `telebot`, `apscheduler`, `sqlalchemy`) и самопроверка детектора нарушений (`tests/test_architecture.py`) | ✅ |
| 5.4 | Long Polling: локальный fake Telegram Bot API (aiohttp). Обновление доходит до обработчика, `stop()` завершает `run()` (AC-05) (`tests/test_polling.py`) | ✅ |
| 5.5 | Контракты из §5 SPEC-001 существуют, `Publisher` абстрактен (`tests/test_contracts.py`) | ✅ |

### Этап 6. Docker (AC-06; SPEC-028)

| ID | Задача | Статус |
|---|---|---|
| 6.1 | `Dockerfile`: `python:3.12-slim`, `pip install .`, пользователь uid 1000, `CMD ["python", "-m", "postmaster"]` | ✅ |
| 6.2 | `docker-compose.yml`: сборка, `env_file: .env`, том `./data:/app/data`, `restart: unless-stopped`, `init: true`, `stop_grace_period` | ✅ |
| 6.3 | Проверка: сборка образа и `docker compose config` недоступны в среде выполнения. Проверены эквивалентные шаги (см. раздел 5) | ⚠️ |

### Этап 7. Сквозная проверка запуска (AC-03, AC-05)

| ID | Задача | Статус |
|---|---|---|
| 7.1 | Запуск без `.env` и с неверным токеном: понятная ошибка, код выхода ≠ 0, токен не выводится (`tests/test_entrypoint.py`, `tests/test_app.py`) | ✅ |
| 7.2 | Запуск полного цикла против fake Telegram API: `get_me`, Long Polling, получение обновления, SIGTERM, корректная остановка (код выхода 0). Автоматизировано в `tests/test_entrypoint.py`; против настоящего api.telegram.org не проверялось (см. раздел 5) | ✅ |
| 7.3 | Проверка логов: токен не встречается ни в одной строке вывода процесса (`tests/test_entrypoint.py`) | ✅ |

### Этап 8. Документация и поставка

| ID | Задача | Статус |
|---|---|---|
| 8.1 | `README.md`: запуск локально и в Docker, настройки, тесты, архитектурные правила, путь к Webhook | ✅ |
| 8.2 | Прогон `ruff check`, `ruff format --check`, `pytest` на Python 3.12 | ✅ |
| 8.3 | Коммиты в `arena/1b9daf2a-postmaster` и push в origin. Статусы в этом плане обновлены | ✅ |

## 4. Матрица проверки

| Требование | Как проверяется | Статус |
|---|---|---|
| AC-01 Python 3.12+ | `requires-python >=3.12`. Установка и тесты в venv на CPython 3.12.15. TC-01 проверяет версию | ✅ |
| AC-02 Зависимости подключены | Установка `pip install .` в чистый venv. TC-01 импортирует все библиотеки | ✅ |
| AC-03 Единая точка входа | `python -m postmaster` и консольная команда `postmaster` ведут в `postmaster.app:main`. Проверяют `test_smoke.py` и `test_entrypoint.py` | ✅ |
| AC-04 Слои разделены | Структура каталогов. TC-03 проверяет зависимости между слоями | ✅ |
| AC-05 Long Polling | Тест с fake Telegram API (`test_polling.py`, `test_entrypoint.py`) | ✅ |
| AC-06 Docker-файлы | Файлы присутствуют, `docker-compose.yml` разобран парсером YAML. Сборка образа не выполнялась (см. раздел 5) | ⚠️ |
| TC-01 Импорт | `tests/test_smoke.py` | ✅ |
| TC-02 Запуск с тестовой конфигурацией | `tests/test_app.py`, `tests/test_entrypoint.py` | ✅ |
| TC-03 Циклы и границы слоёв | `tests/test_architecture.py` | ✅ |
| BR-01 Логика не в handlers | TC-03: handlers не импортируют repositories и database | ✅ |
| BR-02 Данные через Repository | TC-03: только repositories, database и app импортируют SQLAlchemy | ✅ |
| BR-03 Публикация через Publisher | Контракт `Publisher` (`test_contracts.py`); TC-03: publishers не зависят от Telegram-сервисов | ✅ |
| BR-04 Scheduler через сервисы | TC-03: services без импорта `telebot`, `SchedulerService` не знает о Telegram | ✅ |
| BR-05 Long Polling | `PollingTransport` — единственная реализация транспорта | ✅ |

## 5. Риски, ограничения и находки

- **Docker недоступен в среде выполнения.** Образ не собирался, `docker compose config` не запускался. Эквивалентные шаги: установка пакета в чистый venv на Python 3.12 без редактируемого режима, копирование только файлов из `COPY`, запуск из каталога без `src` (как в контейнере, `WORKDIR /app`), проверка `docker-compose.yml` парсером YAML. Окончательная проверка — `docker compose up --build` на машине разработчика.
- **Интерпретатор Python 3.12 собран из исходников.** Установка через `uv` и загрузка с GitHub Releases в среде недоступны. Использованы официальный тег `v3.12.15` с codeload.github.com, OpenSSL 3.0.20 и системные библиотеки SQLite и zlib.
- **Нет доступа к api.telegram.org.** Реальный бот не проверялся. Long Polling проверен на локальном fake Telegram API, который отвечает по тому же HTTP-протоколу. Отказ токена и недоступность сети проверены на fake-сервере и на закрытом порту.
- **Площадка развёртывания (SPEC-038) не выбрана.** Dockerfile рассчитан на Docker-хост. PythonAnywhere не запускает пользовательские Docker-образы.
- **Остановка Long Polling.** pyTelegramBotAPI не предоставляет публичного метода остановки. Опрос останавливается отменой задачи. Это подтверждено чтением исходников 4.37.0 и тестом `test_polling.py`.

Находки при реализации (учтены в коде, важны для следующих SPEC):

- В pyTelegramBotAPI 4.37.0 асинхронные исключения (`telebot.asyncio_helper.ApiTelegramException`) — отдельные классы, не совпадающие с синхронными (`telebot.apihelper`). Проверка по `except` должна использовать асинхронный модуль.
- HTTP-сессия библиотеки общая для всех ботов и не закрывается сама после `get_me`. `Application.shutdown()` вызывает `close_bot_session()`, иначе aiohttp выводит предупреждение «Unclosed client session».
- Текст `ApiHTTPException` и некоторых сетевых исключений может содержать URL с токеном. Поэтому сообщения при запуске формируются без текста исключения, а логи маскируются.
- В Ruff 0.16 форматтер обрабатывает Python-блоки внутри Markdown. Каталог `docs/` исключён из `ruff format`, чтобы тексты спецификаций не менялись.

## 6. Открытые вопросы

1. Площадка развёртывания: VPS с Docker Compose или PythonAnywhere на платном плане (SPEC-038). Решение оставлено без изменений: Docker на VPS (D7).
2. Lock-файл зависимостей и Alembic: решение оставлено без изменений, вводятся вместе с SPEC-027 (D8).
3. Вопросы из анализа, которые ещё не закрыты: `POST_MODE` и политика `PROCESSING`. Они нужны до соответствующих функциональных SPEC.
