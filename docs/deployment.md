# Инструкция по развёртыванию PostMaster на сервере клиента

Документ описывает минимальные системные требования и пошаговую установку
Telegram-бота **PostMaster** (отложенные публикации фотографий) на сервере клиента.
Рекомендуемый способ развёртывания — **Docker Compose**. Также приведён раздел
для запуска напрямую через Python venv.

---

## 1. Описание решения

PostMaster — Telegram-бот, который:

- принимает фотографию от пользователя в личном чате;
- запрашивает дату и время публикации в формате `ДД.ММ.ГГГГ ЧЧ:ММ`;
- хранит данные в SQLite (`data/postmaster.db`);
- по расписанию публикует фото в выбранный Telegram-канал
  или отправляет напоминание пользователю; режим выбирается для каждого поста;
- восстанавливает задания после перезапуска и поддерживает повторные попытки.

Реализованы Long Polling и встроенный health-check. Файлы изображений
не скачиваются на сервер: используется Telegram `file_id`.

---

## 2. Минимальные требования к серверу

### 2.1. Аппаратные ресурсы

| Параметр | Минимум | Рекомендуется |
|----------|---------|---------------|
| CPU | 1 vCPU | 2 vCPU |
| RAM | 512 МБ | 1 ГБ |
| Диск | 2 ГБ свободно | 5 ГБ (с учётом бэкапов SQLite и логов) |
| Сеть | стабильный доступ к `api.telegram.org` (исходящие HTTPS, порт 443) | то же |

Бот не требует публичного IP и входящих портов: используется Long Polling.

### 2.2. Операционная система

- Linux (amd64): Ubuntu 22.04 LTS / 24.04 LTS, Debian 12, AlmaLinux 9 — рекомендуется.
- Windows Server 2019/2022 — поддерживается (Docker Desktop или прямой запуск).
- macOS — поддерживается, не для продакшена.

### 2.3. Программное обеспечение (Docker-вариант)

| Компонент | Версия |
|-----------|--------|
| Docker Engine | 24.0+ |
| Docker Compose | v2 (плагин `docker compose`) |
| Git | любая актуальная |
| Утилита `curl` | для проверок |

### 2.4. Программное обеспечение (вариант без Docker)

| Компонент | Версия |
|-----------|--------|
| Python | 3.12 или новее |
| pip | 23+ (идёт в составе Python 3.12) |
| venv / virtualenv | встроенный модуль `venv` |
| SQLite | 3.35+ (поставляется вместе с Python) |

### 2.5. Доступы и учётные данные

- Токен Telegram-бота, выданный **@BotFather** (команда `/newbot`).
  Никогда не публикуется в Git и не передаётся по нешифрованным каналам.
- Для публикации в Telegram пользователь указывает канал в диалоге; бот должен иметь право публикации.
  Бот должен быть добавлен в канал с правом публикации сообщений.
- SSH-доступ к серверу с правами `sudo` (или учётная запись `root`).

### 2.6. Сетевые требования

- Исходящие соединения к `api.telegram.org:443` (Long Polling + публикации).
- Исходящие соединения к `pypi.org` и `files.pythonhosted.org` (только при первоначальной сборке/установке).
- При использовании прокси — задать `HTTPS_PROXY`/`HTTP_PROXY` в окружении контейнера.

### 2.7. Каталоги на сервере

| Путь | Назначение | Владелец |
|------|------------|----------|
| `/opt/postmaster` | каталог приложения (код, `docker-compose.yml`, `.env`) | `root` или пользователь-деплойщик |
| `/opt/postmaster/data` | SQLite-БД, health-отчёт, бэкапы (`postmaster.db`, `health.json`, `backup.db`) | uid `1000` (пользователь `postmaster` внутри контейнера) |

---

## 3. Подготовка сервера (один раз)

### 3.1. Обновить систему (Ubuntu/Debian)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl ca-certificates git
```

### 3.2. Установить Docker и Compose

```bash
# Удалить старые версии, если есть
sudo apt remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

# Официальный установочный скрипт Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Разрешить текущему пользователю работать с Docker без sudo
sudo usermod -aG docker "$USER"
newgrp docker

# Проверить
docker --version
docker compose version
```

### 3.3. Настроить часовой пояс сервера

Бот использует `DEFAULT_TIMEZONE` из конфигурации (`Europe/Moscow` по умолчанию),
но системные часы сервера должны быть синхронизированы — иначе `HEALTHCHECK`
и расписание публикаций будут давать ложные срабатывания.

```bash
sudo timedatectl set-timezone Europe/Moscow
sudo systemctl enable --now systemd-timesyncd
timedatectl status
```

---

## 4. Развёртывание через Docker Compose (рекомендуемый способ)

### Шаг 1. Получить код проекта

```bash
sudo mkdir -p /opt/postmaster
sudo chown "$USER":"$USER" /opt/postmaster
cd /opt/postmaster

# Вариант A — из Git-репозитория
git clone <URL_РЕПОЗИТОРИЯ> .

# Вариант B — из архива (если клиент передаёт .tar.gz)
# tar -xzf postmaster.tar.gz -C /opt/postmaster --strip-components=1
```

После копирования структура каталога должна содержать
`Dockerfile`, `docker-compose.yml`, `pyproject.toml`, `src/`.

### Шаг 2. Создать файл конфигурации `.env`

```bash
cp .env.example .env
sudo chmod 600 .env      # только владелец может читать — здесь хранится токен
```

Открыть `.env` в редакторе и заполнить обязательные параметры:

```dotenv
# Обязательно
BOT_TOKEN=123456789:AA...                # токен от @BotFather

# Опционально (значения по умолчанию подходят для MVP)
DATABASE_URL=sqlite+aiosqlite:///data/postmaster.db
LOG_LEVEL=INFO
DEFAULT_TIMEZONE=Europe/Moscow
DEFAULT_POST_MODE=REMINDER
# TELEGRAM_TARGET_ALLOWLIST=@channel_one,-1001234567890
HEALTH_FILE=data/health.json
HEALTH_MAX_AGE_SECONDS=90
BACKUP_PATH=data/backup.db
```

> Никогда не коммитить `.env` в Git. Файл уже добавлен в `.gitignore`.

### Шаг 3. Подготовить каталог данных

Контейнер работает от непривилегированного пользователя с `uid=1000`.
Каталог `./data` должен быть доступен ему на запись.

```bash
mkdir -p /opt/postmaster/data
sudo chown -R 1000:1000 /opt/postmaster/data
ls -ld /opt/postmaster/data    # drwxr-xr-x 1000 1000 ...
```

### Шаг 4. Собрать и запустить контейнер

```bash
cd /opt/postmaster
docker compose up -d --build
```

Команда собирает образ по `Dockerfile` и запускает сервис `postmaster` в фоне.

### Шаг 5. Убедиться, что бот работает

```bash
# Статус контейнера (должен быть "healthy" через ~30–60 секунд)
docker compose ps

# Логи (Ctrl+C выходит, поток не прерывает работу контейнера)
docker compose logs -f postmaster

# Разовый health-check внутри контейнера
docker compose exec postmaster python -m postmaster health --print
```

Ожидаемый вывод health-check:

```json
{"process": "ok", "database": "ok", "scheduler": "ok", "generated_at": "..."}
```

### Шаг 6. Проверить связь с Telegram

Открыть чат с ботом в Telegram, отправить `/start`. Бот должен ответить
приветствием (см. README, раздел «Команды бота»). Если ответа нет — проверить
логи (`docker compose logs -f postmaster`) и корректность `BOT_TOKEN`.

### Шаг 7. Управление сервисом

```bash
# Остановить (graceful, 30 секунд на завершение Long Polling и Scheduler)
docker compose down

# Перезапустить после изменения .env
docker compose up -d

# Пересобрать образ после обновления кода
docker compose up -d --build

# Полностью удалить контейнер и сеть (БД в ./data сохраняется)
docker compose down
```

---

## 5. Резервное копирование и обслуживание

### 5.1. Резервная копия БД

Встроенная команда `backup` создаёт файл через Online Backup API SQLite
и проверяет его целостность (`PRAGMA integrity_check`):

```bash
docker compose exec postmaster python -m postmaster backup
docker compose exec postmaster python -m postmaster backup \
    --destination /app/data/backup-$(date +%F).db
```

Для ежедневного бэкапа добавить в `/etc/cron.d/postmaster`:

```cron
15 3 * * *  cd /opt/postmaster && /usr/local/bin/docker compose exec -T postmaster python -m postmaster backup --destination /app/data/backup.db >/var/log/postmaster-backup.log 2>&1
```

Хранить минимум 7 последних копий, периодически выгружать их на внешнее
хранилище (`rsync`, S3, BorgBackup — на стороне клиента).

### 5.2. Ротация логов

Docker ограничивает размер логов контейнера средствами драйвера
(по умолчанию `json-file`). Создать или отредактировать
`/etc/docker/daemon.json`:

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "5"
  }
}
```

Затем перезапустить Docker: `sudo systemctl restart docker`.

### 5.3. Обновление версии бота

```bash
cd /opt/postmaster
git pull            # или распаковать новый архив
docker compose up -d --build
docker compose logs -f postmaster
```

БД в `./data` сохраняется. После смены схемы БД может потребоваться
миграция (см. README, замечание о SPEC-027 и колонке `users.first_name`).

---

## 6. Мониторинг

### 6.1. Health-check

Docker периодически (каждые 30 секунд) вызывает `python -m postmaster health`.
Если отчёт unhealthy три раза подряд — контейнер получает статус `unhealthy`.

```bash
docker compose ps                 # колонка STATUS
docker inspect --format '{{json .State.Health}}' $(docker compose ps -q postmaster)
```

### 6.2. Коды завершения

| Код | Значение |
|-----|----------|
| 0 | Штатная остановка по `SIGTERM` или healthy health-check |
| 1 | Токен отклонён, нет сети Telegram, БД недоступна, Scheduler остановлен |
| 2 | Ошибка конфигурации (`BOT_TOKEN`, `LOG_LEVEL`, `DEFAULT_TIMEZONE`, `DEFAULT_POST_MODE`, `TELEGRAM_TARGET_ALLOWLIST`) |

### 6.3. Системные проверки

```bash
# Использование ресурсов контейнером
docker stats postmaster-postmaster-1

# Место на диске (БД и бэкапы растут медленно, но следить обязательно)
df -h /opt/postmaster
```

---

## 7. Развёртывание без Docker (альтернативный вариант)

Использовать, если на сервере запрещены контейнеры. Все команды выполняются
от имени обычного пользователя с `sudo`-доступом.

### Шаг 1. Установить Python 3.12

```bash
# Ubuntu 24.04
sudo apt install -y python3.12 python3.12-venv python3-pip

# Старше — через deadsnakes PPA
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

### Шаг 2. Развернуть код

```bash
sudo mkdir -p /opt/postmaster
sudo chown "$USER":"$USER" /opt/postmaster
cd /opt/postmaster
git clone <URL_РЕПОЗИТОРИЯ> .
cp .env.example .env && chmod 600 .env
# отредактировать .env: задать BOT_TOKEN
```

### Шаг 3. Создать виртуальное окружение и установить зависимости

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

### Шаг 4. Запустить как systemd-сервис

Создать `/etc/systemd/system/postmaster.service`:

```ini
[Unit]
Description=PostMaster Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=postmaster
Group=postmaster
WorkingDirectory=/opt/postmaster
EnvironmentFile=/opt/postmaster/.env
ExecStart=/opt/postmaster/.venv/bin/python -m postmaster
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

# Безопасность
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/postmaster/data

[Install]
WantedBy=multi-user.target
```

Создать пользователя и каталог данных:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin postmaster
sudo mkdir -p /opt/postmaster/data
sudo chown -R postmaster:postmaster /opt/postmaster /opt/postmaster/.env
```

Активировать и запустить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now postmaster
sudo systemctl status postmaster
sudo journalctl -u postmaster -f
```

Управление:

```bash
sudo systemctl restart postmaster
sudo systemctl stop postmaster
sudo systemctl start postmaster
```

Health-check в этой конфигурации можно запускать вручную или из cron:

```bash
sudo -u postmaster /opt/postmaster/.venv/bin/python -m postmaster health --print
```

---

## 8. Чек-лист после установки

- [ ] Контейнер/сервис в статусе `healthy`.
- [ ] `docker compose exec postmaster python -m postmaster health --print`
      возвращает `{"process": "ok", "database": "ok", "scheduler": "ok"}`.
- [ ] Команда `/start` в Telegram даёт приветственный ответ.
- [ ] Файл `data/postmaster.db` создан и принадлежит `uid 1000`.
- [ ] Файл `data/health.json` обновляется каждые 30 секунд.
- [ ] `.env` имеет права `600` и не находится в Git.
- [ ] Настроено расписание резервного копирования.
- [ ] Настроена ротация логов Docker.
- [ ] Для публикации в Telegram бот добавлен в выбранный канал с правом публикации.

---

## 9. Контакты для эскалации

При возникновении проблем на стороне клиента фиксировать:

1. Точную команду и её вывод (без токена).
2. Содержимое `docker compose logs --tail=200 postmaster` (или
   `journalctl -u postmaster -n 200`).
3. Результат `docker compose exec postmaster python -m postmaster health --print`.
4. Версию Docker / Python на сервере и ОС.

Все эти данные безопасны для передачи — `BOT_TOKEN` в логах не появляется
(см. README, раздел «Настройка»).
