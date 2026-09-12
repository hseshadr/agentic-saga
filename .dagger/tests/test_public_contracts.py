from __future__ import annotations

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
FOUNDATION_SHA = "5cf3b7550442bb06d1cce1f146e48c064dcf511c"
FOUNDATION = "github.com/hseshadr/ci/modules/portfolio-foundation"
PYTHON_PACKAGE = "github.com/hseshadr/ci/modules/python-package"
PYTHON_IMAGE = (
    "python:3.13.14-bookworm@sha256:"
    "8b9a8b28d9cc221c6ab5d40e9cfcd99429959f6a8f5171612a99147975ab043f"
)
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.11.32@sha256:"
    "df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c"
)
NODE_IMAGE = (
    "node:24.6.0-bookworm-slim@sha256:"
    "9b741b28148b0195d62fa456ed84dd6c953c1f17a3761f3e6e6797a754d9edff"
)
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
    ("container-sync",),
    ("pnpm", "gate"),
    ("container-sync",),
    ("uv", "run", "poe", "release-candidate"),
    ("container-sync",),
)
SECURITY_TRACE = (
    ("dependency-audit",),
    ("container-sync",),
    ("pnpm", "audit"),
    ("container-sync",),
)
CI_RUNTIME_TRACE = (
    ("uv", "sync", "--frozen", "--all-groups", "--all-extras"),
    *CI_TRACE[:2],
    ("corepack", "enable"),
    ("pnpm", "install", "--frozen-lockfile"),
    ("pnpm", "exec", "playwright", "install", "--with-deps"),
    *CI_TRACE[2:4],
    ("uv", "sync", "--frozen", "--all-groups", "--all-extras"),
    ("corepack", "enable"),
    ("pnpm", "install", "--frozen-lockfile"),
    ("pnpm", "exec", "playwright", "install", "--with-deps"),
    *CI_TRACE[4:],
)
SECURITY_RUNTIME_TRACE = (
    *SECURITY_TRACE[:2],
    ("corepack", "enable"),
    ("pnpm", "install", "--frozen-lockfile"),
    *SECURITY_TRACE[2:],
)
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


def _runtime_commands(trace: _Trace) -> tuple[tuple[str, ...], ...]:
    return tuple(event for event in trace.events if isinstance(event, tuple))


def _constants(tree: ast.Module) -> dict[str, object]:
    return {
        node.target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    }


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
        self.git_calls: list[tuple[str, object, str, int, bool]] = []
        self.images: list[str] = []

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


async def _generated_module_product_mutation(dag: _Dag) -> None:
    await dag.python_package().build("coverage").directory().sync()


class _Container:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    def with_exec(self, command: list[str]) -> _Container:
        self.trace.record(tuple(command))
        return self

    def from_(self, image: str) -> _Container:
        self.trace.images.append(image)
        return self

    async def sync(self) -> _Container:
        self.trace.record(("container-sync",))
        return self

    def __getattr__(self, name: str) -> Callable[..., _Container]:
        configuration = {
            "directory",
            "file",
            "with_directory",
            "with_env_variable",
            "with_file",
            "with_secret_variable",
            "with_workdir",
        }
        if name in configuration:
            return lambda *_args, **_kwargs: self
        raise AttributeError(f"unknown container operation: {name}")


class _PythonPackage:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    def dependency_audit(self, *_: object, **__: object) -> _Container:
        self.trace.record(("dependency-audit",))
        return _Container(self.trace)

    def __getattr__(self, name: str) -> object:
        raise AttributeError(f"unknown python-package operation: {name}")


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


class _GitCommit:
    def __init__(self, trace: _Trace, repository: str, auth: object, commit_sha: str) -> None:
        self.trace = trace
        self.repository = repository
        self.auth = auth
        self.commit_sha = commit_sha

    def tree(self, *, depth: int, include_tags: bool) -> object:
        self.trace.git_calls.append(
            (self.repository, self.auth, self.commit_sha, depth, include_tags)
        )
        self.trace.record("git-tree")
        return object()


class _GitRepository:
    def __init__(self, trace: _Trace, repository: str, auth: object) -> None:
        self.trace = trace
        self.repository = repository
        self.auth = auth

    def commit(self, commit_sha: str) -> _GitCommit:
        return _GitCommit(self.trace, self.repository, self.auth, commit_sha)


class _Dag:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    def foundation(self) -> _Foundation:
        return _Foundation(self.trace)

    def python_package(self) -> _PythonPackage:
        return _PythonPackage(self.trace)

    def container(self) -> _Container:
        return _Container(self.trace)

    def cache_volume(self, _: str) -> object:
        raise AssertionError("shared mutable caches are forbidden")

    def git(self, repository: str, *, http_auth_header: object) -> _GitRepository:
        return _GitRepository(self.trace, repository, http_auth_header)

    def __getattr__(self, name: str) -> object:
        raise AttributeError(f"unknown Dagger operation: {name}")


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


def test_should_pin_exact_runtime_images_and_python_base() -> None:
    # Given the adapter source and generated-module build configuration.
    tree = _adapter_tree()
    assignments = _constants(tree)
    pyproject = (ROOT / ".dagger" / "pyproject.toml").read_text()

    # When the three runtime identities are inspected.
    # Then each is immutable and the module build uses the same Git-capable Python image.
    expected = {"PYTHON_IMAGE": PYTHON_IMAGE, "UV_IMAGE": UV_IMAGE, "NODE_IMAGE": NODE_IMAGE}
    assert assignments.items() >= expected.items()
    assert f'base-image = "{PYTHON_IMAGE}"' in pyproject


def test_should_commit_lock_and_ignore_generated_sdk() -> None:
    # Given the generated module's reproducibility inputs.
    config = cast(dict[str, object], json.loads((ROOT / "dagger.json").read_text()))
    included = cast(list[str], config["include"])
    lock = ROOT / ".dagger" / "uv.lock"

    # When committed inputs and ignore boundaries are inspected.
    # Then the lock is tracked while generated and local state stays excluded.
    assert lock.is_file() and ".dagger/uv.lock" in included
    ignores = set((ROOT / ".gitignore").read_text().splitlines())
    assert {".dagger/.venv/", ".dagger/sdk/"} <= ignores


def test_should_not_attach_shared_mutable_caches_to_product_containers() -> None:
    # Given the adapter's complete syntax tree.
    tree = _adapter_tree()

    # When Dagger method calls are inspected.
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    # Then no reusable writable cache can enter a repository-controlled command.
    assert calls.isdisjoint({"cache_volume", "with_mounted_cache"})


@pytest.mark.parametrize(
    ("entrypoint", "expected_images", "expected_commands"),
    (
        (
            "ci",
            (UV_IMAGE, PYTHON_IMAGE, NODE_IMAGE, UV_IMAGE, PYTHON_IMAGE, NODE_IMAGE),
            CI_RUNTIME_TRACE,
        ),
        ("security", (NODE_IMAGE,), SECURITY_RUNTIME_TRACE),
    ),
)
def test_should_bootstrap_pinned_runtimes_before_each_product_command(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    expected_images: tuple[str, ...],
    expected_commands: tuple[tuple[str, ...], ...],
) -> None:
    # Given an adapter with every image and runtime command observable.
    module, trace = _adapter_module(monkeypatch)

    # When either closed public lane executes.
    asyncio.run(getattr(module.AgenticSaga(), entrypoint)(object(), "a" * 40, object()))

    # Then exact immutable images and frozen bootstraps precede product commands.
    assert tuple(trace.images) == expected_images
    assert _runtime_commands(trace) == expected_commands


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


def test_should_checkout_authenticated_canonical_history_after_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an exact source identity and an opaque typed authorization header.
    module, trace = _adapter_module(monkeypatch)
    auth = object()
    commit_sha = "a" * 40

    # When the release-bearing CI lane executes.
    asyncio.run(module.AgenticSaga().ci(object(), commit_sha, auth))

    # Then canonical full history is fetched only after the verified guard.
    assert trace.git_calls == [
        ("https://github.com/hseshadr/agentic-saga.git", auth, commit_sha, 0, True)
    ]
    assert trace.events.index("guard-sync") < trace.events.index("git-tree")


def test_should_reject_an_extra_product_command_from_the_closed_ci_trace() -> None:
    # Given a non-shipping mutation that adds copied coverage policy after CI delegation.
    trace = _Trace()
    trace.events = ["guard", "guard-sync", *CI_TRACE, ("uv", "run", "poe", "coverage")]

    # When the public execution contract evaluates its captured trace.
    # Then extra policy cannot evade the exact command boundary.
    with pytest.raises(AssertionError):
        _assert_product_trace(trace, CI_TRACE)


def test_should_reject_unknown_generated_module_product_operations() -> None:
    # Given the reported generated-module build/directory/sync bypass.
    trace = _Trace()

    # When a public adapter attempts that unsupported product operation.
    with pytest.raises(AttributeError, match="build"):
        asyncio.run(_generated_module_product_mutation(_Dag(trace)))

    # Then no unknown product interaction can silently enter the exact trace.
    assert trace.events == []


def test_should_tolerate_known_nonexecuting_container_configuration() -> None:
    # Given fixed-image/container configuration required before a delegated command.
    trace = _Trace()
    container = _Dag(trace).container()

    # When the adapter configures, but does not execute, that container.
    configured = (
        container.from_("node:24")
        .with_directory("/src", object())
        .with_workdir("/src")
        .with_env_variable("CI", "1")
    )

    # Then configuration stays outside the exact product trace.
    assert configured is container and trace.events == []
