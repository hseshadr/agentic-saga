from __future__ import annotations

# ruff: noqa: S101
import ast
import asyncio
import importlib
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

ROOT = Path(__file__).parents[2]
MODULE = ROOT / ".dagger" / "src" / "agentic_saga_ci" / "main.py"
FOUNDATION_SHA = "95c99bd46c97797235da7149776cb1c7a580289e"
FOUNDATION = "github.com/hseshadr/ci/modules/portfolio-foundation"
PYTHON_PACKAGE = "github.com/hseshadr/ci/modules/python-package"
PublicMethod = ast.AsyncFunctionDef | ast.FunctionDef
Parameter = tuple[str, str]
Signature = tuple[
    str,
    tuple[Parameter, ...],
    tuple[Parameter, ...],
    Parameter | None,
    tuple[Parameter, ...],
    Parameter | None,
    tuple[int, int],
    str,
]
PUBLIC_INPUTS = (
    ("source", "dagger.Directory"),
    ("commit_sha", "str"),
    ("git_auth_header", "dagger.Secret"),
)
PUBLIC_LANES = (("ci", "_ci_commands"), ("security", "_security_commands"))


def _adapter_tree() -> ast.Module:
    assert MODULE.is_file(), "the closed Agentic Saga Dagger adapter must exist"
    return ast.parse(MODULE.read_text())


def _adapter_class(tree: ast.Module) -> ast.ClassDef:
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert [node.name for node in classes] == ["AgenticSaga"]
    return classes[0]


def _decorator_name(node: ast.expr) -> str | None:
    value = node.func if isinstance(node, ast.Call) else node
    return value.id if isinstance(value, ast.Name) else None


def _public_methods(node: ast.ClassDef) -> Iterator[PublicMethod]:
    for member in node.body:
        if isinstance(member, (ast.AsyncFunctionDef, ast.FunctionDef)) and (
            "function" in {_decorator_name(item) for item in member.decorator_list}
        ):
            yield member


def _parameter(node: ast.arg | None) -> Parameter | None:
    if node is None:
        return None
    annotation = "None" if node.annotation is None else ast.unparse(node.annotation)
    return node.arg, annotation


def _parameters(nodes: list[ast.arg], *, remove_self: bool = False) -> tuple[Parameter, ...]:
    filtered = nodes[1:] if remove_self and nodes and nodes[0].arg == "self" else nodes
    return tuple(cast(Parameter, _parameter(node)) for node in filtered)


def _signature(node: PublicMethod) -> Signature:
    arguments = node.args
    defaults = len(arguments.defaults), len(arguments.kw_defaults)
    return (
        node.name,
        _parameters(arguments.posonlyargs),
        _parameters(arguments.args, remove_self=True),
        _parameter(arguments.vararg),
        _parameters(arguments.kwonlyargs),
        _parameter(arguments.kwarg),
        defaults,
        ast.unparse(node.returns),
    )


def _method(tree: ast.Module, name: str) -> PublicMethod:
    methods = {
        method.name: method
        for method in _adapter_class(tree).body
        if isinstance(method, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    assert name in methods
    return methods[name]


def _called_private_methods(node: PublicMethod) -> tuple[str, ...]:
    calls = (
        child.func.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "self"
    )
    return tuple(calls)


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


def _literal_command_sequences(tree: ast.Module, name: str) -> tuple[tuple[str, ...], ...]:
    source = ast.parse(_source_for_reachable_methods(tree, name))
    sequences = (
        tuple(item.value for item in node.elts)
        for node in ast.walk(source)
        if isinstance(node, (ast.List, ast.Tuple))
        and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts
        )
    )
    return tuple(sequences)


def _dependencies() -> list[dict[str, str]]:
    config = ROOT / "dagger.json"
    assert config.is_file(), "the Dagger module configuration must exist"
    parsed = cast(dict[str, list[dict[str, str]]], json.loads(config.read_text()))
    return parsed["dependencies"]


def _decorator(value: object | None = None, **_: object) -> object:
    return (lambda target: target) if value is None else value


class _DaggerModule(ModuleType):
    def __getattr__(self, _: str) -> object:
        return object


def _adapter_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    assert MODULE.is_file(), "the closed Agentic Saga Dagger adapter must exist"
    dagger = _DaggerModule("dagger")
    dagger.function = _decorator
    dagger.object_type = _decorator
    dagger.check = _decorator
    monkeypatch.setitem(sys.modules, "dagger", dagger)
    monkeypatch.syspath_prepend(str(MODULE.parents[1]))
    sys.modules.pop("agentic_saga_ci.main", None)
    sys.modules.pop("agentic_saga_ci", None)
    return importlib.import_module("agentic_saga_ci.main")


def _lane_recorder(events: list[str], name: str) -> Callable[..., object]:
    async def lane(*_: object) -> None:
        events.append(name)

    return lane


def _failing_guard(events: list[str]) -> Callable[..., object]:
    async def guard(*_: object) -> None:
        events.append("guard")
        raise RuntimeError("history unavailable")

    return guard


def test_should_detect_bare_and_called_function_decorators() -> None:
    # Given both legal Dagger decorator forms.
    tree = ast.parse("@function\ndef ci(): pass\n@function(cache='never')\ndef security(): pass")
    adapter = ast.ClassDef(
        name="AgenticSaga", bases=[], keywords=[], body=tree.body, decorator_list=[]
    )

    # When the public functions are discovered.
    names = tuple(method.name for method in _public_methods(adapter))

    # Then neither form can hide an endpoint from the closed-schema contract.
    assert names == ("ci", "security")


def test_should_capture_nonpositional_public_inputs() -> None:
    # Given a public function attempting hidden argument forms.
    tree = ast.parse(
        "@function\ndef ci(self, source: Directory, /, *argv: str, path: str, "
        "**extra: str) -> str: pass"
    )
    method = cast(PublicMethod, tree.body[0])

    # When every argument category is represented in its signature.
    actual = _signature(method)

    # Then positional-only, variadic, keyword-only, and keyword-spread inputs remain visible.
    assert actual[1:6] == (
        (("self", "None"), ("source", "Directory")),
        (),
        ("argv", "str"),
        (("path", "str"),),
        ("extra", "str"),
    )


def test_should_expose_only_the_closed_typed_ci_and_security_boundary() -> None:
    # Given the public Dagger object.
    tree = _adapter_tree()

    # When its complete function schema is inspected.
    actual = tuple(_signature(method) for method in _public_methods(_adapter_class(tree)))

    # Then callers can provide only exact source, identity, and a typed Git header.
    expected = (
        ("ci", (), PUBLIC_INPUTS, None, (), None, (0, 0), "str"),
        ("security", (), PUBLIC_INPUTS, None, (), None, (0, 0), "str"),
    )
    assert actual == expected


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


@pytest.mark.parametrize(("entrypoint", "lane"), PUBLIC_LANES)
def test_should_wait_for_each_guard_before_its_product_lane(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, lane: str
) -> None:
    # Given a controlled adapter orchestration fixture.
    module = _adapter_module(monkeypatch)
    adapter = module.AgenticSaga.__new__(module.AgenticSaga)
    events: list[str] = []
    monkeypatch.setattr(adapter, "_guard", _lane_recorder(events, "guard"))
    monkeypatch.setattr(adapter, lane, _lane_recorder(events, "command"))

    # When the public entry point runs its product lane.
    asyncio.run(getattr(adapter, entrypoint)(object(), "a" * 40, object()))

    # Then the guard completed before any command was started.
    assert events == ["guard", "command"]


@pytest.mark.parametrize(("entrypoint", "lane"), PUBLIC_LANES)
def test_should_stop_each_product_lane_when_its_guard_fails(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, lane: str
) -> None:
    # Given a guard that fails before its controlled product lane.
    module = _adapter_module(monkeypatch)
    adapter = module.AgenticSaga.__new__(module.AgenticSaga)
    events: list[str] = []
    monkeypatch.setattr(adapter, "_guard", _failing_guard(events))
    monkeypatch.setattr(adapter, lane, _lane_recorder(events, "command"))

    # When the public entry point is evaluated.
    with pytest.raises(RuntimeError, match="history unavailable"):
        asyncio.run(getattr(adapter, entrypoint)(object(), "a" * 40, object()))

    # Then no product command was reached.
    assert events == ["guard"]


def test_should_delegate_ci_to_the_existing_authoritative_commands() -> None:
    # Given the closed CI orchestration.
    commands = _literal_command_sequences(_adapter_tree(), "ci")

    # When its product proof command boundaries are inspected.
    expected = (
        ("uv", "run", "poe", "gate"),
        ("pnpm", "gate"),
        ("uv", "run", "poe", "release-candidate"),
    )

    # Then the adapter delegates the complete existing CI proof without reproducing it.
    assert all(command in commands for command in expected)


def test_should_delegate_security_to_the_shared_locked_and_frontend_audits() -> None:
    # Given the scheduled security orchestration.
    tree = _adapter_tree()
    commands = _literal_command_sequences(tree, "security")
    source = _source_for_reachable_methods(tree, "security")

    # When its audit command boundaries are inspected.
    expected = "dependency_audit", ("pnpm", "audit")

    # Then it uses the shared locked Python audit and the existing frontend audit.
    assert expected[0] in source and expected[1] in commands
