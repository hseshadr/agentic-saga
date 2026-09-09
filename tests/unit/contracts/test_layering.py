from __future__ import annotations

import ast
from collections.abc import Iterable
from importlib.util import resolve_name
from pathlib import Path
from typing import get_type_hints

import agentic_saga
from agentic_saga.contracts import runtime as runtime_contracts
from agentic_saga.execution import composition as composition_module
from agentic_saga.kernel.policy import PolicyEngine

CORE_PACKAGES = frozenset({"contracts", "kernel", "storage", "execution", "evidence"})
EXPECTED_GRAPH = {
    "contracts": frozenset(),
    "kernel": frozenset({"contracts"}),
    "storage": frozenset({"contracts", "kernel"}),
    "execution": frozenset({"contracts", "kernel"}),
    "evidence": frozenset({"contracts", "kernel"}),
}
TOPOLOGICAL_RANK = {
    "contracts": 0,
    "kernel": 1,
    "storage": 2,
    "execution": 2,
    "evidence": 2,
}
_PACKAGE_ROOT = Path(__file__).parents[3] / "src" / "agentic_saga"


def _package_imports(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(("agentic_saga", *path.relative_to(_PACKAGE_ROOT).parts[:-1]))
    dependencies = (_core_package(name) for name in _imported_modules(tree, package))
    return frozenset(item for item in dependencies if item is not None)


def _imported_modules(tree: ast.AST, package: str) -> tuple[str, ...]:
    imported = tuple(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    from_imported = tuple(
        module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for module in _from_imported_modules(node, package)
    )
    return imported + from_imported


def _from_imported_modules(node: ast.ImportFrom, package: str) -> tuple[str, ...]:
    base = _resolve_from_import(node, package)
    if base != "agentic_saga":
        return (base,)
    imported = tuple(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return (base, *imported)


def _resolve_from_import(node: ast.ImportFrom, package: str) -> str:
    if node.level:
        relative = "." * node.level + (node.module or "")
        return resolve_name(relative, package)
    if node.module is None:
        raise ValueError("absolute import has no module")
    return node.module


def _core_package(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != "agentic_saga" or parts[1] not in CORE_PACKAGES:
        return None
    return parts[1]


def production_package_import_graph() -> dict[str, frozenset[str]]:
    graph: dict[str, set[str]] = {package: set() for package in CORE_PACKAGES}
    for path in _PACKAGE_ROOT.glob("**/*.py"):
        package = path.relative_to(_PACKAGE_ROOT).parts[0]
        if package in graph:
            graph[package].update(_package_imports(path) - {package})
    return {package: frozenset(dependencies) for package, dependencies in graph.items()}


def bidirectional_edges(
    graph: dict[str, frozenset[str]], packages: Iterable[str]
) -> set[tuple[str, str]]:
    return {
        _ordered_edge(source, target)
        for source in packages
        for target in graph[source]
        if source in graph.get(target, ())
    }


def _ordered_edge(source: str, target: str) -> tuple[str, str]:
    return (source, target) if source < target else (target, source)


def rank_violations(
    graph: dict[str, frozenset[str]], ranks: dict[str, int]
) -> set[tuple[str, str]]:
    return {
        (source, target)
        for source, dependencies in graph.items()
        for target in dependencies
        if ranks[target] >= ranks[source]
    }


def cycle_edges(graph: dict[str, frozenset[str]]) -> set[tuple[str, str]]:
    return {
        (source, target)
        for source, dependencies in graph.items()
        for target in dependencies
        if source in _reachable_packages(graph, target)
    }


def _reachable_packages(graph: dict[str, frozenset[str]], start: str) -> set[str]:
    reached: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current not in reached:
            reached.add(current)
            pending.extend(graph.get(current, ()))
    return reached


def test_core_package_dependencies_are_one_way() -> None:
    # Given production imports among the five core packages
    graph = production_package_import_graph()

    # When opposite-direction package edges are identified
    edges = bidirectional_edges(graph, CORE_PACKAGES)

    # Then no package pair owns dependencies in both directions
    assert edges == set(), graph


def test_core_package_dependencies_match_allowed_adjacency() -> None:
    # Given production imports among the core packages
    graph = production_package_import_graph()

    # When the complete adjacency is compared with the layer contract
    # Then peers and higher layers are absent from every dependency set
    assert graph == EXPECTED_GRAPH


def test_core_package_dependencies_follow_topological_ranks() -> None:
    # Given the production package graph and its architectural ranks
    graph = production_package_import_graph()

    # When edges are checked from consumer to dependency
    violations = rank_violations(graph, TOPOLOGICAL_RANK)

    # Then every dependency has a strictly lower rank
    assert violations == set()


def test_core_package_dependency_graph_has_no_long_cycles() -> None:
    # Given the complete production package graph
    graph = production_package_import_graph()

    # When transitive paths are inspected
    cycles = cycle_edges(graph)

    # Then no cycle of any length exists
    assert cycles == set()


def test_cycle_probe_detects_a_three_package_cycle() -> None:
    # Given a synthetic three-package cycle
    graph = {
        "contracts": frozenset({"execution"}),
        "kernel": frozenset({"contracts"}),
        "execution": frozenset({"kernel"}),
    }

    # When transitive paths are inspected
    cycles = cycle_edges(graph)

    # Then every edge participating in the long cycle is reported
    assert cycles == {
        ("contracts", "execution"),
        ("execution", "kernel"),
        ("kernel", "contracts"),
    }


def test_imported_symbol_uses_resolved_base_module_for_package_edge() -> None:
    # Given a symbol imported from a relative core module
    tree = ast.parse("from ..storage import KernelStore")

    # When the imported module is resolved
    modules = _imported_modules(tree, "agentic_saga.execution")

    # Then the dependency uses the storage module without treating the symbol as a module
    assert modules == ("agentic_saga.storage",)


def _assert_root_import_exposes_forbidden_storage_edge(source: str) -> None:
    tree = ast.parse(source)
    modules = _imported_modules(tree, "agentic_saga.execution")
    dependencies = frozenset(filter(None, map(_core_package, modules)))
    graph = {**EXPECTED_GRAPH, "execution": EXPECTED_GRAPH["execution"] | dependencies}
    assert dependencies == frozenset({"storage"})
    assert graph != EXPECTED_GRAPH
    assert rank_violations(graph, TOPOLOGICAL_RANK) == {("execution", "storage")}


def test_relative_root_import_exposes_forbidden_execution_storage_edge() -> None:
    # Given a relative root import that names a core package as an alias
    source = "from .. import storage"

    # When the import is classified as a package dependency
    # Then it exposes the forbidden execution-to-storage edge
    _assert_root_import_exposes_forbidden_storage_edge(source)


def test_absolute_root_import_exposes_forbidden_execution_storage_edge() -> None:
    # Given an absolute root import that names a core package as an alias
    source = "from agentic_saga import storage"

    # When the import is classified as a package dependency
    # Then it exposes the forbidden execution-to-storage edge
    _assert_root_import_exposes_forbidden_storage_edge(source)


def test_should_keep_definition_types_in_the_kernel_layer() -> None:
    # Given
    definition_types = (
        "SagaDefinition",
        "DefinitionCatalog",
        "DefinitionConflict",
        "DefinitionUnavailable",
        "DefinitionPolicy",
    )

    # When
    contract_exports = tuple(name for name in definition_types if hasattr(runtime_contracts, name))

    # Then
    assert agentic_saga.SagaDefinition.__module__ == "agentic_saga.kernel.definitions"
    assert contract_exports == ()


def test_should_compose_with_the_concrete_definition_policy() -> None:
    # Given / When
    policy_type = get_type_hints(agentic_saga.SagaDefinition)["policy"]

    # Then
    assert policy_type is PolicyEngine
    assert not hasattr(composition_module, "_policy_engine")
