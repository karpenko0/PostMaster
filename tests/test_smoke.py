"""TC-01: пакет импортируется целиком, зависимости установлены (AC-01, AC-02, AC-03)."""

import importlib
import importlib.metadata
import pkgutil
import sys
import tomllib
from pathlib import Path

import pytest

import postmaster

ROOT = Path(__file__).resolve().parents[1]
MODULES = sorted(m.name for m in pkgutil.walk_packages(postmaster.__path__, "postmaster."))

# Имя дистрибутива из pyproject.toml и имя модуля для импорта.
MANDATORY_DEPENDENCIES = {
    "pyTelegramBotAPI": "telebot",
    "SQLAlchemy": "sqlalchemy",
    "aiosqlite": "aiosqlite",
    "APScheduler": "apscheduler",
    "python-dotenv": "dotenv",
    "pydantic": "pydantic",
    "pydantic-settings": "pydantic_settings",
}


def test_python_is_312_or_newer() -> None:
    assert sys.version_info >= (3, 12)


def test_pyproject_declares_python_312() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=3.12"


@pytest.mark.parametrize("module_name", MODULES)
def test_package_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


@pytest.mark.parametrize(("distribution", "module_name"), MANDATORY_DEPENDENCIES.items())
def test_mandatory_dependency_is_installed(distribution: str, module_name: str) -> None:
    importlib.import_module(module_name)
    assert importlib.metadata.version(distribution)


def test_console_script_points_to_app_main() -> None:
    scripts = importlib.metadata.entry_points(group="console_scripts")
    targets = [entry.value for entry in scripts if entry.name == "postmaster"]
    assert targets == ["postmaster.app:main"]
