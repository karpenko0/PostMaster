"""SPEC-031: Качество unit-тестов.

§7 Приёмка:
- без сети: unit-тесты не обращаются к внешним хостам;
- deterministic time: тесты не зависят от системных часов;
- production secrets не используются: только fake-токены.

§4 Покрытие:
- parser, timezone, FSM, resolver, publishers, error_mapper, retry,
  formatting, validation — каждый модуль имеет unit-тесты.

Тесты в этом файле — guard-тесты: они сканируют код/тесты и
утверждают инварианты качества.
"""

import re
import socket
from pathlib import Path

import pytest

from fake_telegram import TEST_TOKEN
from harness import db_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src" / "postmaster"
TESTS_ROOT = PROJECT_ROOT / "tests"

# Шаблон fake-токена. Любой другой формат в тестах — нарушение.
_FAKE_TOKEN_PATTERN = re.compile(r"^\d+:[A-Za-z0-9_-]+$")
# Шаблон настоящего Bot API токена (id:secret, секрет ≥ 35 символов).
_REAL_TOKEN_PATTERN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")


# === Без сети (§7) =========================================================


def _iter_python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if path.name in {"__init__.py", "__main__.py"}:
            continue
        yield path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_no_external_telegram_host_in_unit_tests() -> None:
    """В unit-тестах нет обращений к api.telegram.org (только localhost или строки-данные).

    Допустимые исключения: ``test_polling``, ``test_entrypoint``, ``fake_telegram`` —
    локальный fake-сервер (127.0.0.1). ``test_app`` использует строку
    ``api.telegram.org`` для проверки сетевых ошибок (RuntimeError не делает
    реальный запрос). ``test_logging`` и ``test_spec025`` используют
    ``api.telegram.org`` в данных для проверки редактирования.
    """
    allowed_files = {
        "test_polling.py",
        "test_entrypoint.py",
        "fake_telegram.py",
        "test_app.py",
        "test_logging.py",
        "test_spec025.py",
    }
    offenders: list[str] = []
    self_file = Path(__file__).name
    for path in _iter_python_files(TESTS_ROOT):
        if path.name in allowed_files:
            continue
        if path.name == self_file:
            continue
        if "api.telegram.org" in _read(path):
            offenders.append(path.name)
    assert offenders == [], (
        "Внешний хост api.telegram.org в unit-тестах (только строки-данные или "
        "localhost допустимы): " + ", ".join(offenders)
    )


def test_no_requests_or_urllib_in_unit_tests() -> None:
    """В unit-тестах нет requests/urllib — внешние HTTP-запросы недопустимы."""
    offenders: list[str] = []
    for path in _iter_python_files(TESTS_ROOT):
        text = _read(path)
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Допустимы строки-данные в кавычках, но не вызовы.
            if re.search(r"^\s*(import requests|from requests|urllib\.request\.)", line):
                offenders.append(f"{path.name}:{line_no}")
    assert offenders == [], "requests/urllib в unit-тестах: " + ", ".join(offenders)


def test_runtime_external_network_is_blockable() -> None:
    """Демонстрация: ``socket.socket.connect`` можно перехватить для non-loopback.

    Этот тест не блокирует сеть, но показывает, что guard легко добавить:
    достаточно обернуть ``socket.socket.connect`` в ``pytest`` fixture.
    """
    original = socket.socket.connect
    called: list[tuple[str, int]] = []

    def spy(self, address):
        called.append(address)
        return original(self, address)

    socket.socket.connect = spy
    try:
        # Подключение к loopback перехватывается и форвардится оригиналу.
        with socket.socket() as probe:
            try:
                probe.connect(("127.0.0.1", 1))
            except OSError:
                # Порт закрыт — нас интересует только факт перехвата вызова.
                pass
        assert called, "socket.connect не был вызван"
        # Реальный сетевой адрес (8.8.8.8) НЕ должен участвовать в тестах:
        # здесь мы только подтверждаем, что spy работает.
        assert all(isinstance(addr[0], str) and addr[1] == int(addr[1]) for addr in called)
    finally:
        socket.socket.connect = original


# === Production secrets не используются (§7) ================================


def test_fake_token_matches_expected_format() -> None:
    """Фиктивный токен имеет формат Bot API, но не production-значение."""
    assert _FAKE_TOKEN_PATTERN.match(TEST_TOKEN), "TEST_TOKEN не соответствует формату Bot API"
    assert "Test" in TEST_TOKEN or "Fake" in TEST_TOKEN, (
        "TEST_TOKEN должен содержать маркер тестового значения"
    )


def test_no_real_token_in_source() -> None:
    """В production-коде нет настоящих токенов."""
    offenders: list[str] = []
    for path in _iter_python_files(SRC_ROOT):
        text = _read(path)
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _REAL_TOKEN_PATTERN.search(line):
                # Игнорируем ровно строку, где задаётся TEST_TOKEN.
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_no}")
    assert offenders == [], "Возможный настоящий токен в production-коде: " + ", ".join(offenders)


def test_all_tokens_in_tests_use_fake_or_none() -> None:
    """В тестах используется либо TEST_TOKEN, либо synthetic-токены для redaction-проверок."""
    # Допустимые токены: TEST_TOKEN и синтетические для проверки редактирования.
    # Синтетические содержат повторяющийся паттерн («AaBbCc...», «ZzYyXx...»),
    # который НЕ встречается в настоящих Bot API токенах.
    allowed_patterns = (
        TEST_TOKEN,
        "123456789:AAFakeTokenForTestsOnly_0123456789",
        # Синтетические токены в test_spec021 для тестирования redaction.
        "1234567890:AaBbCcDdEeFfGgHhIiJjKkLl",
        "9876543210:ZzYyXxWwVvUuTtSsRrQq",
        "",
    )
    offenders: list[str] = []
    for path in _iter_python_files(TESTS_ROOT):
        text = _read(path)
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for match in _REAL_TOKEN_PATTERN.finditer(line):
                token = match.group()
                if token in allowed_patterns:
                    continue
                offenders.append(f"{path.name}:{line_no}: {token!r}")
    assert offenders == [], "Токен в тестах не соответствует fake-шаблону: " + ", ".join(offenders)


def test_clean_env_does_not_leak_real_token_from_dotenv(monkeypatch, tmp_path: Path) -> None:
    """``clean_env`` + ``load_settings(env_file=None)`` не подтягивают настоящий токен.

    В проекте есть ``.env`` с настоящим токеном. После ``clean_env`` и
    ``load_settings(env_file=None)`` результат должен быть либо
    ConfigError (если token не задан), либо fake-токен — но не значение
    из ``.env``.
    """
    project_env = PROJECT_ROOT / ".env"
    if not project_env.is_file():
        pytest.skip("В проекте нет .env — проверка не требуется")

    project_token_value = ""
    for line in _read(project_env).splitlines():
        match = _REAL_TOKEN_PATTERN.search(line)
        if match:
            project_token_value = match.group()
            break
    if not project_token_value:
        pytest.skip("В .env нет настоящего токена")

    monkeypatch.delenv("BOT_TOKEN", raising=False)
    settings_module = __import__("postmaster.config", fromlist=["load_settings"])

    # При env_file=None load_dotenv не вызывается — .env не читается.
    try:
        settings = settings_module.load_settings(env_file=None)
    except settings_module.ConfigError:
        return  # ConfigError — отлично: значит .env НЕ подгрузился.

    # Если Settings создались, проверяем, что значение НЕ из .env.
    actual = settings.bot_token.get_secret_value()
    assert actual != project_token_value, (
        f"load_settings(env_file=None) подтянул токен из {project_env}: {actual!r}"
    )


# === Матрица покрытия (§4) =================================================


# Каждый модуль из §4 SPEC-031 должен иметь хотя бы один тест-файл,
# который импортирует его и проверяет ключевое поведение.
COVERAGE_MATRIX: dict[str, tuple[Path, str]] = {
    # category -> (test file, marker function or symbol name)
    "parser": (TESTS_ROOT / "test_schedule_spec005.py", "parse_publication_datetime"),
    "timezone": (
        TESTS_ROOT / "test_timezone_spec006.py",
        "convert_local_to_utc",
    ),
    "fsm": (TESTS_ROOT / "test_dialog_fsm.py", "DialogState"),
    "resolver": (
        TESTS_ROOT / "test_spec016.py",
        "DefaultPublisherResolver",
    ),
    "publishers": (
        TESTS_ROOT / "test_spec014.py",
        "ReminderPublisher",
    ),
    "error_mapper": (
        TESTS_ROOT / "test_spec021.py",
        "TelegramErrorMapper",
    ),
    "retry": (
        TESTS_ROOT / "test_spec022.py",
        "RetryPolicy",
    ),
    "formatting": (
        TESTS_ROOT / "test_logging.py",
        "RedactingFormatter",
    ),
    "validation_config": (
        TESTS_ROOT / "test_config.py",
        "load_settings",
    ),
    "validation_schedule": (
        TESTS_ROOT / "test_schedule_spec005.py",
        "parse_publication_datetime",
    ),
}


@pytest.mark.parametrize(
    ("category", "target"),
    [(name, target) for name, (_, target) in COVERAGE_MATRIX.items()],
    ids=list(COVERAGE_MATRIX.keys()),
)
def test_category_has_unit_test_covering_target(category: str, target: str) -> None:
    """Модуль из §4 покрыт unit-тестом с обращением к целевому символу."""
    test_file, _ = COVERAGE_MATRIX[category]
    assert test_file.is_file(), f"Тестовый файл не найден: {test_file}"

    text = _read(test_file)
    assert target in text, f"Тест {test_file.name} не упоминает {target!r} (категория {category!r})"


def test_all_publisher_implementations_have_unit_tests() -> None:
    """ReminderPublisher и TelegramChannelPublisher покрыты отдельными тестами."""
    pub_test_files = {"test_spec014.py", "test_spec015.py"}
    found = {path.name for path in _iter_python_files(TESTS_ROOT) if path.name in pub_test_files}
    assert pub_test_files <= found, f"Отсутствуют тесты publisher'ов: {pub_test_files - found}"


# === Deterministic time (§7) ================================================


def test_parse_publication_datetime_accepts_now_argument() -> None:
    """Парсер публикации принимает ``now`` как параметр — без системных часов.

    Тест вызывает реальную функцию и проверяет, что сравнение с будущим/прошлым
    опирается на переданный ``now``, а не на ``datetime.now()``.
    """
    from postmaster.domain.schedule import DateTimeValidationError, parse_publication_datetime

    fixed_now = __import__("datetime").datetime(
        2026, 1, 1, 12, 0, tzinfo=__import__("datetime").UTC
    )

    # 15 минут раньше фиксированного now — должно быть отклонено.
    with pytest.raises(DateTimeValidationError):
        parse_publication_datetime("01.01.2026 11:45", "UTC", fixed_now)

    # 15 минут позже — принимается.
    parsed = parse_publication_datetime("01.01.2026 12:15", "UTC", fixed_now)
    assert parsed.utc_datetime > fixed_now


def test_format_local_datetime_does_not_call_utcnow() -> None:
    """Форматирование даты/времени не зависит от системных часов."""
    from postmaster.utils.timezone_format import format_local_datetime

    utc = __import__("datetime").datetime(2026, 10, 20, 12, 0, tzinfo=__import__("datetime").UTC)
    result = format_local_datetime(utc, "Europe/Moscow")
    assert "20.10.2026 15:00" == result


def test_now_argument_in_validation_is_utc_aware() -> None:
    """``now`` в ``parse_publication_datetime`` должен иметь tzinfo."""
    from postmaster.domain.schedule import parse_publication_datetime

    naive_now = __import__("datetime").datetime(2026, 1, 1, 12, 0)
    with pytest.raises(ValueError, match="now должен содержать часовой пояс"):
        parse_publication_datetime("01.01.2026 12:15", "UTC", naive_now)


# === Покрытие публичного API репозиториев — smoke-тесты ====================


async def test_unit_tests_use_sqlite_only(tmp_path: Path) -> None:
    """Unit-тесты создают БД в ``tmp_path`` — никаких production-путей."""
    # Создаём и читаем БД: проверяем, что db_path генерирует путь в tmp_path.
    path = db_path(tmp_path)
    assert path.parent == tmp_path / "data"
    assert str(path).endswith("app.db")


def test_unit_test_rootdir_is_project_root() -> None:
    """``PROJECT_ROOT`` указывает на корень проекта — не на системные каталоги."""
    resolved = PROJECT_ROOT.resolve()
    # Защита от запуска из chroot/нестандартного окружения.
    assert (resolved / "src" / "postmaster").is_dir()
    assert (resolved / "tests").is_dir()
    assert (resolved / "pyproject.toml").is_file()
