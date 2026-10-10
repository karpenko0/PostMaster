"""Формулировки SPEC-004 в коде совпадают со спецификацией (§16, §25, §26).

Тексты ответов и ключи FSM — часть критериев приёмки (AC-004-07, AC-004-08, E2E-004-01).
Тесты читают docs/specs/SPEC-004.md и сравнивают её кодовые блоки с константами приложения,
поэтому правка текста без правки спецификации (или наоборот) ломает проверку.
"""

from pathlib import Path

from postmaster.handlers.dialog import PHOTO_ACCEPTED_TEXT, PHOTO_REQUEST
from postmaster.services.dialog_service import (
    PHOTO_FILE_ID_KEY,
    PHOTO_FILE_SIZE_KEY,
    PHOTO_HEIGHT_KEY,
    PHOTO_UNIQUE_ID_KEY,
    PHOTO_WIDTH_KEY,
)

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "specs" / "SPEC-004.md"


def spec_fenced_after(marker: str) -> str:
    """Текст первого блока ``` после маркера в спецификации (без ограждения и языка)."""
    spec = SPEC_PATH.read_text(encoding="utf-8")
    assert spec.count(marker) == 1, marker
    section = spec.split(marker, 1)[1]
    fenced = section.split("```", 2)[1]
    return fenced.removeprefix("text").strip()


def test_photo_accepted_text_matches_spec() -> None:
    # §25, AC-004-07, E2E-004-01: сообщение после принятия фото задано дословно.
    assert PHOTO_ACCEPTED_TEXT == spec_fenced_after("# 25. Ответ пользователю")


def test_photo_request_text_matches_spec() -> None:
    # §26, AC-004-08: «Отправь фотографию…» — дословный ответ на неверный тип сообщения.
    assert PHOTO_REQUEST == spec_fenced_after("Ответ:")


def test_fsm_draft_keys_match_spec() -> None:
    # §16: в состоянии пользователя доступны данные фотографии с этими именами.
    spec_keys = set(spec_fenced_after("# 16. Данные FSM").splitlines())
    assert spec_keys == {
        PHOTO_FILE_ID_KEY,
        PHOTO_UNIQUE_ID_KEY,
        PHOTO_FILE_SIZE_KEY,
        PHOTO_WIDTH_KEY,
        PHOTO_HEIGHT_KEY,
    }
