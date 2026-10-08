"""Базовый класс ORM-моделей. Таблицы users и posts добавляются в SPEC-007 и SPEC-008."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Декларативная база для всех таблиц PostMaster."""
