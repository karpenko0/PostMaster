"""Нормализация идентификаторов Telegram-каналов (SPEC-015, SPEC-026).

Модуль остаётся в слое ``domain`` и не импортирует другие слои проекта: это чистая
функция валидации, нужная и ``config`` (проверка allowlist), и ``services``.
Вынесение её в ``utils`` сделало бы ``utils`` зависимым от ``config`` и наоборот,
поэтому матрица зависимостей SPEC-001 (``tests/test_architecture.py``) дополнена
разрешённым ребром ``config → domain`` — как единственным осознанным исключением.
"""

import re

__all__ = ["normalize_target", "parse_allowlist"]


def normalize_target(value: str) -> str:
    """Приводит @username или числовой id канала к каноническому виду."""
    value = value.strip()
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value, flags=re.ASCII):
        return value.lower()
    if re.fullmatch(r"-?[0-9]+", value, flags=re.ASCII) and int(value) != 0:
        return str(int(value))
    raise ValueError("invalid Telegram channel identifier")


def parse_allowlist(value: str) -> frozenset[str]:
    """Разбирает список разрешённых каналов через запятую в набор канонических id."""
    return frozenset(normalize_target(item) for item in value.split(",") if item.strip())
