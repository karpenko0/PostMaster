"""Базовый класс ORM-моделей. Модели таблиц находятся в database/models.py."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Декларативная база для всех таблиц PostMaster."""
