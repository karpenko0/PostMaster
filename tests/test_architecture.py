"""TC-03: границы слоёв, запрет циклов и внешних библиотек не там, где нельзя (AC-04, BR-01…BR-04).

Граф импортов строится через ast, поэтому проверка не запускает код приложения. Детектор
проверяется на синтетических пакетах с заведомо неверными зависимостями.
"""

import ast
import textwrap
from pathlib import Path

import pytest

import postmaster

PACKAGE = "postmaster"
PACKAGE_DIR = Path(postmaster.__file__).resolve().parent

LAYER_DIRECTORIES = (
    "bot",
    "handlers",
    "domain",
    "services",
    "publishers",
    "repositories",
    "database",
    "utils",
)
ROOT_MODULES = ("config.py", "app.py", "__main__.py")

ALL_LAYERS = frozenset(
    {"bot", "handlers", "domain", "services", "publishers", "repositories", "database", "utils"}
    | {"config"}
)

# Слои, от которых может зависеть каждый слой (SPEC-001, план раздел 2 и 4).
# Корневой пакет postmaster доступен всем слоям, поэтому в матрице не указан.
ALLOWED_LAYER_DEPENDENCIES: dict[str, frozenset[str]] = {
    "utils": frozenset(),
    "domain": frozenset(),
    "database": frozenset(),
    "bot": frozenset(),
    "config": frozenset(),
    "publishers": frozenset({"domain", "utils"}),
    "repositories": frozenset({"domain", "database", "utils"}),
    "services": frozenset({"domain", "repositories", "publishers", "utils"}),
    "handlers": frozenset({"domain", "services", "utils"}),
    "app": ALL_LAYERS,
    "__main__": frozenset({"app"}),
    "root": frozenset(),
}

# Внешние библиотеки и слои, где их можно импортировать (BR-01, BR-02, BR-04).
THIRD_PARTY_ALLOWED_LAYERS: dict[str, frozenset[str]] = {
    "telebot": frozenset({"bot", "handlers", "publishers", "app"}),
    "apscheduler": frozenset({"services", "app"}),
    "sqlalchemy": frozenset({"database", "repositories", "app"}),
    "aiosqlite": frozenset({"database"}),
    "dotenv": frozenset({"config", "app"}),
    "pydantic_settings": frozenset({"config", "app"}),
}

Graph = dict[str, set[str]]


def layer_of(module: str) -> str:
    parts = module.split(".")
    return "root" if len(parts) == 1 else parts[1]


def _resolve_base(node: ast.ImportFrom, module: str, is_package: bool) -> str | None:
    if node.level == 0:
        return node.module
    parts = module.split(".")
    package_parts = parts if is_package else parts[:-1]
    anchor = package_parts[: len(package_parts) - (node.level - 1)]
    if not anchor:
        return None
    return ".".join([*anchor, node.module] if node.module else anchor)


def _imported_names(node: ast.AST, module: str, is_package: bool) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        base = _resolve_base(node, module, is_package)
        if base is None:
            return []
        return [base, *(f"{base}.{alias.name}" for alias in node.names)]
    return []


def _internal_target(name: str, known: set[str]) -> str | None:
    """Самый длинный префикс имени, который является модулем пакета."""
    parts = name.split(".")
    for size in range(len(parts), 0, -1):
        candidate = ".".join(parts[:size])
        if candidate in known:
            return candidate
    return None


def collect_graph(package_dir: Path) -> tuple[Graph, Graph]:
    """Возвращает (внутренние импорты, внешние импорты) по каждому модулю пакета."""
    sources: dict[str, tuple[Path, bool]] = {}
    for path in sorted(package_dir.rglob("*.py")):
        parts = list(path.relative_to(package_dir.parent).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        sources[".".join(parts)] = (path, is_package)

    known = set(sources)
    internal: Graph = {module: set() for module in known}
    external: Graph = {module: set() for module in known}
    for module, (path, is_package) in sources.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for name in _imported_names(node, module, is_package):
                target = _internal_target(name, known)
                if target is None:
                    external[module].add(name)
                elif target != module:
                    internal[module].add(target)
    return internal, external


def layer_violations(internal: Graph) -> list[str]:
    problems: list[str] = []
    for module, targets in sorted(internal.items()):
        source_layer = layer_of(module)
        if source_layer not in ALLOWED_LAYER_DEPENDENCIES:
            problems.append(f"{module}: слой «{source_layer}» не описан в матрице зависимостей")
            continue
        # Один импорт может дать несколько целей (пакет и модуль), поэтому группируем по слою.
        forbidden = {
            layer_of(target)
            for target in targets
            if layer_of(target) not in (source_layer, "root")
            and layer_of(target) not in ALLOWED_LAYER_DEPENDENCIES[source_layer]
        }
        for target_layer in sorted(forbidden):
            problems.append(
                f"{module}: слой «{source_layer}» не может зависеть от слоя «{target_layer}»"
            )
    return problems


def third_party_violations(external: Graph) -> list[str]:
    problems: list[str] = []
    for module, names in sorted(external.items()):
        layer = layer_of(module)
        for name in sorted(names):
            top = name.split(".")[0]
            allowed = THIRD_PARTY_ALLOWED_LAYERS.get(top)
            if allowed is not None and layer not in allowed:
                problems.append(f"{module} импортирует {top}, а в слое «{layer}» это запрещено")
    return problems


def find_cycle(graph: Graph) -> list[str] | None:
    """Возвращает один цикл (первый узел совпадает с последним) или None."""
    white, grey, black = 0, 1, 2
    color = dict.fromkeys(graph, white)
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = grey
        path.append(node)
        for target in sorted(graph.get(node, ())):
            if color.get(target, white) == grey:
                return [*path[path.index(target) :], target]
            if color.get(target, white) == white:
                found = visit(target)
                if found is not None:
                    return found
        path.pop()
        color[node] = black
        return None

    for node in sorted(graph):
        if color[node] == white:
            found = visit(node)
            if found is not None:
                return found
    return None


def layer_graph(internal: Graph) -> Graph:
    graph: Graph = {}
    for module, targets in internal.items():
        source = layer_of(module)
        graph.setdefault(source, set())
        for target in targets:
            target_layer = layer_of(target)
            if target_layer not in (source, "root"):
                graph[source].add(target_layer)
                graph.setdefault(target_layer, set())
    return graph


@pytest.fixture(scope="module")
def project_graph() -> tuple[Graph, Graph]:
    return collect_graph(PACKAGE_DIR)


def test_layer_directories_and_root_modules_exist() -> None:
    for layer in LAYER_DIRECTORIES:
        assert (PACKAGE_DIR / layer / "__init__.py").is_file(), layer
    for module in ROOT_MODULES:
        assert (PACKAGE_DIR / module).is_file(), module


def test_modules_respect_layer_dependency_matrix(project_graph: tuple[Graph, Graph]) -> None:
    internal, _external = project_graph
    assert layer_violations(internal) == []


def test_third_party_libraries_stay_in_their_layers(project_graph: tuple[Graph, Graph]) -> None:
    _internal, external = project_graph
    assert third_party_violations(external) == []


def test_no_circular_dependencies_between_modules(project_graph: tuple[Graph, Graph]) -> None:
    internal, _external = project_graph
    assert find_cycle(internal) is None


def test_no_circular_dependencies_between_layers(project_graph: tuple[Graph, Graph]) -> None:
    internal, _external = project_graph
    assert find_cycle(layer_graph(internal)) is None


def test_business_rules_on_direct_dependencies(project_graph: tuple[Graph, Graph]) -> None:
    internal, external = project_graph
    handler_targets = {
        layer_of(target)
        for module, targets in internal.items()
        if layer_of(module) == "handlers"
        for target in targets
    }
    service_external = {
        name
        for module, names in external.items()
        if layer_of(module) == "services"
        for name in names
    }
    # BR-01 и BR-02: обработчики не работают с репозиториями и базой данных.
    assert not handler_targets & {"repositories", "database"}
    # BR-04: сервисы, включая Scheduler, не импортируют Telegram API.
    assert not any(name.startswith("telebot") for name in service_external)


def _synthetic(root: Path, files: dict[str, str]) -> tuple[Graph, Graph]:
    package_dir = root / PACKAGE
    for relative, source in files.items():
        path = package_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return collect_graph(package_dir)


def test_detector_accepts_clean_synthetic_package(tmp_path: Path) -> None:
    internal, external = _synthetic(
        tmp_path,
        {
            "__init__.py": "",
            "domain/__init__.py": "",
            "domain/model.py": "",
            "services/__init__.py": "",
            "services/post_service.py": "from postmaster.domain import model\n",
            "handlers/__init__.py": "",
            "handlers/start.py": "from postmaster.services.post_service import PostService\n",
        },
    )

    assert layer_violations(internal) == []
    assert third_party_violations(external) == []
    assert find_cycle(internal) is None


def test_detector_reports_forbidden_layer_dependency(tmp_path: Path) -> None:
    internal, _external = _synthetic(
        tmp_path,
        {
            "__init__.py": "",
            "handlers/__init__.py": "",
            "handlers/bad.py": "from postmaster.repositories import user_repo\n",
            "repositories/__init__.py": "",
            "repositories/user_repo.py": "",
        },
    )

    problems = layer_violations(internal)

    assert len(problems) == 1
    assert "postmaster.handlers.bad" in problems[0]
    assert "«repositories»" in problems[0]


def test_detector_reports_forbidden_third_party_import(tmp_path: Path) -> None:
    _internal, external = _synthetic(
        tmp_path,
        {
            "__init__.py": "",
            "services/__init__.py": "",
            "services/scheduler_service.py": "import telebot\n",
        },
    )

    problems = third_party_violations(external)

    assert len(problems) == 1
    assert "telebot" in problems[0]


def test_detector_finds_module_cycle(tmp_path: Path) -> None:
    internal, _external = _synthetic(
        tmp_path,
        {
            "__init__.py": "",
            "services/__init__.py": "",
            "services/a.py": "from postmaster.services import b\n",
            "services/b.py": "from postmaster.services import a\n",
        },
    )

    cycle = find_cycle(internal)

    assert cycle is not None
    assert "postmaster.services.a" in cycle
    assert "postmaster.services.b" in cycle


def test_detector_resolves_relative_imports(tmp_path: Path) -> None:
    internal, _external = _synthetic(
        tmp_path,
        {
            "__init__.py": "",
            "handlers/__init__.py": "",
            "handlers/bad.py": "from ..repositories import user_repo\n",
            "repositories/__init__.py": "",
            "repositories/user_repo.py": "",
        },
    )

    assert "postmaster.repositories.user_repo" in internal["postmaster.handlers.bad"]
    assert layer_violations(internal)


def test_cycle_finder_on_small_graphs() -> None:
    assert find_cycle({}) is None
    assert find_cycle({"a": set(), "b": {"a"}}) is None
    assert find_cycle({"a": {"b"}, "b": {"a"}}) is not None
