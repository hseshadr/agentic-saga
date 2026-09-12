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
CI_TRACE = (
    ("uv", "run", "poe", "gate"),
    ("pnpm", "gate"),
    ("uv", "run", "poe", "release-candidate"),
)
SECURITY_TRACE = (("dependency-audit",), ("pnpm", "audit"))
BOOTSTRAP_COMMANDS = frozenset(
    {
        ("uv", "sync", "--frozen", "--all-groups", "--all-extras"),
        ("corepack", "enable"),
        ("pnpm", "install", "--frozen-lockfile"),
        ("pnpm", "exec", "playwright", "install", "--with-deps"),
    }
)


def _adapter_tree() -> ast.Module:
    assert MODULE.is_file(), "the closed Agentic Saga Dagger adapter must exist"
    return ast.parse(MODULE.read_text())


def _adapter_class(tree: ast.Module) -> ast.ClassDef:
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert [node.name for node in classes] == ["AgenticSaga"]
    return classes[0]


def _decorator_name(node: ast.expr) -> str | None:
    value = node.func if isinstance(node, ast.Call) else node
    if isinstance(value, ast.Name):
        return value.id
    return value.attr if isinstance(value, ast.Attribute) else None


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


Event = str | tuple[str, ...]


class _Trace:
    def __init__(self, *, guard_fails: bool = False) -> None:
        self.events: list[Event] = []
        self.guard_fails = guard_fails

    def record(self, event: Event) -> None:
        self.events.append(event)

    def product_trace(self) -> tuple[tuple[str, ...], ...]:
        return tuple(
            event
            for event in self.events
            if isinstance(event, tuple) and event not in BOOTSTRAP_COMMANDS
        )


def _assert_product_trace(trace: _Trace, expected: tuple[tuple[str, ...], ...]) -> None:
    assert trace.product_trace() == expected


class _Container:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    def with_exec(self, command: list[str]) -> _Container:
        self.trace.record(tuple(command))
        return self

    def dependency_audit(self, *_: object, **__: object) -> _Container:
        self.trace.record(("dependency-audit",))
        return self

    async def sync(self) -> _Container:
        self.trace.record("product-sync")
        return self

    def __getattr__(self, _: str) -> Callable[..., _Container]:
        return lambda *_args, **_kwargs: self


class _Guard:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    async def sync(self) -> _Guard:
        self.trace.record("guard-sync")
        if self.trace.guard_fails:
            raise RuntimeError("history unavailable")
        return self


class _Foundation:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    def guard(self, *_: object, **__: object) -> _Guard:
        self.trace.record("guard")
        return _Guard(self.trace)


class _Dag:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    def foundation(self) -> _Foundation:
        return _Foundation(self.trace)

    def python_package(self) -> _Container:
        return _Container(self.trace)

    def container(self) -> _Container:
        return _Container(self.trace)

    def __getattr__(self, _: str) -> Callable[..., _Container]:
        return lambda *_args, **_kwargs: _Container(self.trace)


def _adapter_module(
    monkeypatch: pytest.MonkeyPatch, *, guard_fails: bool = False
) -> tuple[ModuleType, _Trace]:
    assert MODULE.is_file(), "the closed Agentic Saga Dagger adapter must exist"
    trace = _Trace(guard_fails=guard_fails)
    fake_dag = _Dag(trace)
    dagger = _DaggerModule("dagger")
    dagger.function = _decorator
    dagger.object_type = _decorator
    dagger.check = _decorator
    dagger.dag = fake_dag
    monkeypatch.setitem(sys.modules, "dagger", dagger)
    monkeypatch.syspath_prepend(str(MODULE.parents[1]))
    sys.modules.pop("agentic_saga_ci.main", None)
    sys.modules.pop("agentic_saga_ci", None)
    module = importlib.import_module("agentic_saga_ci.main")
    monkeypatch.setattr(module, "dag", fake_dag, raising=False)
    return module, trace


def test_should_detect_all_legal_function_decorator_forms() -> None:
    # Given bare and called local and qualified Dagger decorators.
    tree = ast.parse(
        "@function\ndef ci(): pass\n"
        "@function(cache='never')\ndef security(): pass\n"
        "@dagger.function\ndef qualified(): pass\n"
        "@dagger.function(cache='never')\ndef qualified_called(): pass"
    )
    adapter = ast.ClassDef(
        name="AgenticSaga", bases=[], keywords=[], body=tree.body, decorator_list=[]
    )

    # When the public functions are discovered.
    names = tuple(method.name for method in _public_methods(adapter))

    # Then no legal form can hide an endpoint from the closed-schema contract.
    assert names == ("ci", "security", "qualified", "qualified_called")


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


@pytest.mark.parametrize(
    ("entrypoint", "expected"), (("ci", CI_TRACE), ("security", SECURITY_TRACE))
)
def test_should_complete_the_foundation_guard_before_the_exact_product_trace(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, expected: tuple[tuple[str, ...], ...]
) -> None:
    # Given an adapter connected to observable Foundation and Dagger container boundaries.
    module, trace = _adapter_module(monkeypatch)
    adapter = module.AgenticSaga()

    # When a public entry point executes.
    asyncio.run(getattr(adapter, entrypoint)(object(), "a" * 40, object()))

    # Then Foundation finishes first and the complete delegated trace has no extra product work.
    assert trace.events.index("guard-sync") < next(
        index for index, event in enumerate(trace.events) if isinstance(event, tuple)
    )
    _assert_product_trace(trace, expected)


@pytest.mark.parametrize("entrypoint", ("ci", "security"))
def test_should_stop_all_product_interactions_when_the_foundation_guard_fails(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    # Given an observable Foundation guard that cannot establish private history.
    module, trace = _adapter_module(monkeypatch, guard_fails=True)
    adapter = module.AgenticSaga()

    # When either public entry point is evaluated.
    with pytest.raises(RuntimeError, match="history unavailable"):
        asyncio.run(getattr(adapter, entrypoint)(object(), "a" * 40, object()))

    # Then no audit, container command, or product synchronization is reached.
    assert trace.events == ["guard", "guard-sync"]


def test_should_reject_an_extra_product_command_from_the_closed_ci_trace() -> None:
    # Given a non-shipping mutation that adds copied coverage policy after CI delegation.
    trace = _Trace()
    trace.events = ["guard", "guard-sync", *CI_TRACE, ("uv", "run", "poe", "coverage")]

    # When the public execution contract evaluates its captured trace.
    # Then extra policy cannot evade the exact command boundary.
    with pytest.raises(AssertionError):
        _assert_product_trace(trace, CI_TRACE)
