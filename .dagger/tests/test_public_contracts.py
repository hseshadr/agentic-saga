from __future__ import annotations

# ruff: noqa: S101
import ast
import json
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).parents[2]
MODULE = ROOT / ".dagger" / "src" / "agentic_saga_ci" / "main.py"
FOUNDATION_SHA = "95c99bd46c97797235da7149776cb1c7a580289e"
FOUNDATION = "github.com/hseshadr/ci/modules/portfolio-foundation"
PYTHON_PACKAGE = "github.com/hseshadr/ci/modules/python-package"


def _adapter_tree() -> ast.Module:
    assert MODULE.is_file(), "the closed Agentic Saga Dagger adapter must exist"
    return ast.parse(MODULE.read_text())


def _adapter_class(tree: ast.Module) -> ast.ClassDef:
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert [node.name for node in classes] == ["AgenticSaga"]
    return classes[0]


def _decorator_names(node: ast.AsyncFunctionDef | ast.FunctionDef) -> tuple[str, ...]:
    return tuple(ast.unparse(decorator) for decorator in node.decorator_list)


def _public_methods(node: ast.ClassDef) -> Iterator[ast.AsyncFunctionDef | ast.FunctionDef]:
    for member in node.body:
        if isinstance(member, (ast.AsyncFunctionDef, ast.FunctionDef)) and (
            "function" in _decorator_names(member)
        ):
            yield member


def _signature(node: ast.AsyncFunctionDef | ast.FunctionDef) -> tuple[object, ...]:
    parameters = tuple(
        (argument.arg, ast.unparse(argument.annotation))
        for argument in node.args.args
        if argument.arg != "self"
    )
    return node.name, parameters, ast.unparse(node.returns)


def _called_private_methods(node: ast.AsyncFunctionDef | ast.FunctionDef) -> tuple[str, ...]:
    calls = (
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "self"
    )
    return tuple(calls)


def _method(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    methods = {
        method.name: method
        for method in _adapter_class(tree).body
        if isinstance(method, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    assert name in methods
    return methods[name]


def _source_for_reachable_methods(tree: ast.Module, name: str) -> str:
    pending = [name]
    visited: set[str] = set()
    sources: list[str] = []
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        method = _method(tree, current)
        sources.append(ast.unparse(method))
        pending.extend(_called_private_methods(method))
    return "\n".join(sources)


def _dependencies() -> list[dict[str, str]]:
    config = ROOT / "dagger.json"
    assert config.is_file(), "the Dagger module configuration must exist"
    return json.loads(config.read_text())["dependencies"]


def test_should_expose_only_the_closed_typed_ci_and_security_boundary() -> None:
    # Given the public Dagger object.
    tree = _adapter_tree()

    # When its function schema is inspected.
    actual = tuple(_signature(method) for method in _public_methods(_adapter_class(tree)))

    # Then callers can provide only exact source, identity, and a typed Git header.
    assert actual == (
        (
            "ci",
            (
                ("source", "dagger.Directory"),
                ("commit_sha", "str"),
                ("git_auth_header", "dagger.Secret"),
            ),
            "str",
        ),
        (
            "security",
            (
                ("source", "dagger.Directory"),
                ("commit_sha", "str"),
                ("git_auth_header", "dagger.Secret"),
            ),
            "str",
        ),
    )


def test_should_pin_the_shared_modules_to_the_merged_private_history_commit() -> None:
    # Given the adapter's external module dependencies.
    dependencies = _dependencies()

    # When their resolved source identities are inspected.
    expected = (
        {"name": "foundation", "source": f"{FOUNDATION}@{FOUNDATION_SHA}", "pin": FOUNDATION_SHA},
        {
            "name": "python-package",
            "source": f"{PYTHON_PACKAGE}@{FOUNDATION_SHA}",
            "pin": FOUNDATION_SHA,
        },
    )

    # Then both share one immutable, merged Foundation identity.
    assert tuple(dependencies) == expected


def test_should_complete_the_foundation_guard_before_ci_product_commands() -> None:
    # Given the closed CI orchestration.
    source = _source_for_reachable_methods(_adapter_tree(), "ci")

    # When its ordered execution boundary is inspected.
    guard = source.index("guard")
    commands = ("poe gate", "pnpm gate", "release-candidate")
    products = tuple(source.index(command) for command in commands)

    # Then source and history validation precede every product-owned command.
    assert guard < min(products)


def test_should_delegate_ci_to_the_existing_authoritative_commands() -> None:
    # Given the closed CI orchestration.
    source = _source_for_reachable_methods(_adapter_tree(), "ci")

    # When its product proof is inspected.
    commands = ("uv run poe gate", "pnpm gate", "uv run poe release-candidate")

    # Then the adapter delegates rather than reimplementing repository quality logic.
    assert all(command in source for command in commands)


def test_should_delegate_security_to_the_shared_locked_and_frontend_audits() -> None:
    # Given the scheduled security orchestration.
    source = _source_for_reachable_methods(_adapter_tree(), "security")

    # When its retained audit boundaries are inspected.
    required = ("dependency_audit", "pnpm audit")

    # Then it uses the shared locked Python audit and the existing frontend audit.
    assert all(boundary in source for boundary in required)


def test_should_keep_coverage_budgets_and_release_algorithms_out_of_the_adapter() -> None:
    # Given the complete closed adapter source.
    source = ast.unparse(_adapter_tree()).lower()

    # When implementation-owned product policy is inspected.
    copied_policy = ("coverage", "budget", "measure_release", "release_runner")

    # Then the adapter retains only delegation boundaries, not duplicate policy.
    assert not any(policy in source for policy in copied_policy)
