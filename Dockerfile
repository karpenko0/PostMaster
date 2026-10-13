# Образ PostMaster (AC-06, SPEC-028). Сборка: docker compose build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Непривилегированный пользователь с uid 1000. Каталог данных должен принадлежать ему.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin postmaster \
    && mkdir -p /app/data \
    && chown postmaster:postmaster /app/data

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && rm -rf /app/src /app/build

USER postmaster

# Логи пишутся в stdout. Остановка по SIGTERM: init в compose передаёт сигнал процессу.
CMD ["python", "-m", "postmaster"]
