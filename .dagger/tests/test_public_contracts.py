from __future__ import annotations

import ast
import asyncio
import importlib
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

ROOT = Path(__file__).parents[2]
MODULE = ROOT / ".dagger" / "src" / "agentic_saga_ci" / "main.py"
FOUNDATION_SHA = "5cf3b7550442bb06d1cce1f146e48c064dcf511c"
FOUNDATION = "github.com/hseshadr/ci/modules/portfolio-foundation"
PYTHON_PACKAGE = "github.com/hseshadr/ci/modules/python-package"
PYTHON_312_IMAGE = (
    "python:3.12.12-bookworm@sha256:"
    "c0abd0758831ad99b7a29e0c1a875da9c4abb9a2e3f21e2eeb585dbcadfb6cd0"
)
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
MATRIX_TRACE = (
    ("uv", "run", "poe", "gate"),
    ("container-sync",),
    ("uv", "run", "poe", "release-candidate"),
    ("container-sync",),
    ("uv", "run", "python", "scripts/measure_release.py"),
    ("container-sync",),
)
CI_TRACE = (
    *MATRIX_TRACE,
    *MATRIX_TRACE,
    ("pnpm", "gate"),
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
    *MATRIX_TRACE[:2],
    ("uv", "sync", "--frozen", "--all-groups", "--all-extras"),
    ("corepack", "enable"),
    ("pnpm", "install", "--frozen-lockfile"),
    ("pnpm", "exec", "playwright", "install", "--with-deps"),
    *MATRIX_TRACE[2:],
    ("uv", "sync", "--frozen", "--all-groups", "--all-extras"),
    *MATRIX_TRACE[:2],
    ("uv", "sync", "--frozen", "--all-groups", "--all-extras"),
    ("corepack", "enable"),
    ("pnpm", "install", "--frozen-lockfile"),
    ("pnpm", "exec", "playwright", "install", "--with-deps"),
    *MATRIX_TRACE[2:],
    ("corepack", "enable"),
    ("pnpm", "install", "--frozen-lockfile"),
    ("pnpm", "exec", "playwright", "install", "--with-deps"),
    ("pnpm", "gate"),
    ("container-sync",),
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
GENERATED_PATHS = (".dagger/sdk/generated.py", ".dagger/.venv/pyvenv.cfg")
GENERATED_PREFIXES = (".dagger/sdk/", ".dagger/.venv/")


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


def _git_binary() -> str:
    executable = shutil.which("git")
    assert executable is not None, "Git executable must exist on PATH"
    assert Path(executable).is_absolute(), "Git executable must resolve to an absolute path"
    return executable


def _git(root: Path, *arguments: str) -> tuple[str, ...]:
    result = subprocess.run(  # noqa: S603 - resolved absolute Git interrogates a test repository.
        [_git_binary(), *arguments], cwd=root, check=True, capture_output=True, text=True
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def _assert_vcs_boundaries(root: Path) -> None:
    lock = _git(root, "ls-files", "--", ".dagger/uv.lock")
    generated = _git(root, "ls-files", "--", *GENERATED_PREFIXES)
    ignored = _git(root, "check-ignore", "--no-index", *GENERATED_PATHS)
    assert lock == (".dagger/uv.lock",)
    assert generated == ()
    assert ignored == GENERATED_PATHS


def _decorator(value: object | None = None, **_: object) -> object:
    return (lambda target: target) if value is None else value


class _DaggerModule(ModuleType):
    def __getattr__(self, _: str) -> object:
        return object


Event = str | tuple[str, ...]


@dataclass(frozen=True)
class _FileArtifact:
    snapshot: _Snapshot
    path: str


@dataclass(frozen=True)
class _DirectoryArtifact:
    snapshot: _Snapshot
    path: str


@dataclass(frozen=True)
class _FileMount:
    path: str
    source: _FileArtifact


@dataclass(frozen=True)
class _DirectoryMount:
    path: str
    source: object
    include: tuple[str, ...] | None


@dataclass(frozen=True)
class _Snapshot:
    image: str | None = None
    files: tuple[_FileMount, ...] = ()
    directories: tuple[_DirectoryMount, ...] = ()
    workdir: str | None = None
    environment: tuple[tuple[str, str], ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    secret_mounts: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()


class _Trace:
    def __init__(self, *, guard_fails: bool = False, mutation: str | None = None) -> None:
        self.events: list[Event] = []
        self.guard_fails = guard_fails
        self.mutation = mutation
        self.git_calls: list[tuple[str, object, str, int, bool]] = []
        self.images: list[str] = []
        self.product_snapshots: list[_Snapshot] = []
        self.sync_snapshots: list[_Snapshot] = []
        self.guard_calls: list[tuple[object, str, str, object]] = []
        self.audit_calls: list[tuple[object, str, str, object]] = []
        self.audit_syncs = 0
        self.git_tree: object | None = None

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
    def __init__(self, trace: _Trace, snapshot: _Snapshot | None = None) -> None:
        self.trace = trace
        self.snapshot = _Snapshot() if snapshot is None else snapshot

    def _next(self, snapshot: _Snapshot) -> _Container:
        return _Container(self.trace, snapshot)

    def _release_mutation(self, command: tuple[str, ...]) -> _Snapshot:
        if command != ("uv", "run", "poe", "release-candidate"):
            return self.snapshot
        if self.trace.mutation == "wrong-workdir":
            return replace(
                self.snapshot, workdir="/", operations=(*self.snapshot.operations, "with_workdir")
            )
        if self.trace.mutation == "secret-mount":
            return replace(
                self.snapshot,
                secret_mounts=("/run/git-auth",),
                operations=(*self.snapshot.operations, "with_mounted_secret"),
            )
        return self.snapshot

    def with_exec(self, command: list[str]) -> _Container:
        value = tuple(command)
        state = self._release_mutation(value)
        state = replace(
            state, commands=(*state.commands, value), operations=(*state.operations, "with_exec")
        )
        result = self._next(state)
        self.trace.record(value)
        if value not in BOOTSTRAP_COMMANDS:
            self.trace.product_snapshots.append(state)
        return result

    def from_(self, image: str) -> _Container:
        self.trace.images.append(image)
        return self._next(_Snapshot(image=image, operations=("from",)))

    def file(self, path: str) -> _FileArtifact:
        return _FileArtifact(self.snapshot, path)

    def directory(self, path: str) -> _DirectoryArtifact:
        return _DirectoryArtifact(self.snapshot, path)

    def with_file(self, path: str, source: _FileArtifact) -> _Container:
        state = replace(
            self.snapshot,
            files=(*self.snapshot.files, _FileMount(path, source)),
            operations=(*self.snapshot.operations, "with_file"),
        )
        return self._next(state)

    def with_directory(
        self, path: str, source: object, *, include: list[str] | None = None
    ) -> _Container:
        mount = _DirectoryMount(path, source, None if include is None else tuple(include))
        state = replace(
            self.snapshot,
            directories=(*self.snapshot.directories, mount),
            operations=(*self.snapshot.operations, "with_directory"),
        )
        return self._next(state)

    def with_workdir(self, path: str) -> _Container:
        state = replace(
            self.snapshot, workdir=path, operations=(*self.snapshot.operations, "with_workdir")
        )
        return self._next(state)

    def with_env_variable(self, name: str, value: str) -> _Container:
        state = replace(
            self.snapshot,
            environment=(*self.snapshot.environment, (name, value)),
            operations=(*self.snapshot.operations, "with_env_variable"),
        )
        return self._next(state)

    async def sync(self) -> _Container:
        self.trace.record(("container-sync",))
        self.trace.sync_snapshots.append(self.snapshot)
        return self

    def __getattr__(self, name: str) -> object:
        raise AttributeError(f"unknown container operation: {name}")


class _Audit:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    async def sync(self) -> _Audit:
        self.trace.record(("container-sync",))
        self.trace.audit_syncs += 1
        return self


class _PythonPackage:
    def __init__(self, trace: _Trace) -> None:
        self.trace = trace

    def dependency_audit(
        self, *, source: object, repository: str, commit_sha: str, http_auth_header: object
    ) -> _Audit:
        self.trace.record(("dependency-audit",))
        self.trace.audit_calls.append((source, repository, commit_sha, http_auth_header))
        return _Audit(self.trace)

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

    def guard(
        self, *, source: object, repository: str, commit_sha: str, http_auth_header: object
    ) -> _Guard:
        self.trace.record("guard")
        self.trace.guard_calls.append((source, repository, commit_sha, http_auth_header))
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
        self.trace.git_tree = object()
        return self.trace.git_tree


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

    def git(self, repository: str, *, http_auth_header: object) -> _GitRepository:
        return _GitRepository(self.trace, repository, http_auth_header)

    def __getattr__(self, name: str) -> object:
        raise AttributeError(f"unknown Dagger operation: {name}")


def _fake_dagger(fake_dag: _Dag) -> _DaggerModule:
    dagger = _DaggerModule("dagger")
    dagger.function = _decorator
    dagger.object_type = _decorator
    dagger.check = _decorator
    dagger.dag = fake_dag
    return dagger


def _load_adapter(
    monkeypatch: pytest.MonkeyPatch, fake_dag: _Dag, module_root: Path | None
) -> ModuleType:
    monkeypatch.setitem(sys.modules, "dagger", _fake_dagger(fake_dag))
    monkeypatch.syspath_prepend(str(MODULE.parents[1] if module_root is None else module_root))
    sys.modules.pop("agentic_saga_ci.main", None)
    sys.modules.pop("agentic_saga_ci", None)
    module = importlib.import_module("agentic_saga_ci.main")
    monkeypatch.setattr(module, "dag", fake_dag, raising=False)
    return module


def _adapter_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    guard_fails: bool = False,
    mutation: str | None = None,
    module_root: Path | None = None,
) -> tuple[ModuleType, _Trace]:
    assert MODULE.is_file(), "the closed Agentic Saga Dagger adapter must exist"
    trace = _Trace(guard_fails=guard_fails, mutation=mutation)
    fake_dag = _Dag(trace)
    return _load_adapter(monkeypatch, fake_dag, module_root), trace


def _mutated_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, original: str, replacement: str
) -> tuple[ModuleType, _Trace]:
    package = tmp_path / "agentic_saga_ci"
    package.mkdir()
    (package / "__init__.py").write_text("")
    source = MODULE.read_text()
    assert source.count(original) == 1
    (package / "main.py").write_text(source.replace(original, replacement))
    return _adapter_module(monkeypatch, module_root=tmp_path)


@dataclass(frozen=True)
class _RuntimeView:
    image: str | None
    files: tuple[tuple[str, _ArtifactView], ...]
    directories: tuple[tuple[str, str | _ArtifactView, tuple[str, ...] | None], ...]
    workdir: str | None
    environment: tuple[tuple[str, str], ...]
    commands: tuple[tuple[str, ...], ...]
    secret_mounts: tuple[str, ...]
    operations: tuple[str, ...]


@dataclass(frozen=True)
class _ArtifactView:
    path: str
    origin: _RuntimeView


_Artifact = _FileArtifact | _DirectoryArtifact


def _artifact_view(artifact: _Artifact, source: object, ancestors: frozenset[int]) -> _ArtifactView:
    return _ArtifactView(artifact.path, _runtime_view(artifact.snapshot, source, ancestors))


def _directory_source_view(
    value: object, source: object, ancestors: frozenset[int]
) -> str | _ArtifactView:
    if value is source:
        return "source"
    assert isinstance(value, _DirectoryArtifact), "unknown directory source"
    return _artifact_view(value, source, ancestors)


def _file_views(
    snapshot: _Snapshot, source: object, ancestors: frozenset[int]
) -> tuple[tuple[str, _ArtifactView], ...]:
    return tuple(
        (item.path, _artifact_view(item.source, source, ancestors)) for item in snapshot.files
    )


def _directory_views(
    snapshot: _Snapshot, source: object, ancestors: frozenset[int]
) -> tuple[tuple[str, str | _ArtifactView, tuple[str, ...] | None], ...]:
    return tuple(
        (item.path, _directory_source_view(item.source, source, ancestors), item.include)
        for item in snapshot.directories
    )


def _runtime_view(
    snapshot: _Snapshot, source: object, ancestors: frozenset[int] = frozenset()
) -> _RuntimeView:
    assert id(snapshot) not in ancestors, "cyclic artifact ancestry is forbidden"
    nested = ancestors | {id(snapshot)}
    return _RuntimeView(
        snapshot.image,
        _file_views(snapshot, source, nested),
        _directory_views(snapshot, source, nested),
        snapshot.workdir,
        snapshot.environment,
        snapshot.commands,
        snapshot.secret_mounts,
        snapshot.operations,
    )


UV_SYNC = ("uv", "sync", "--frozen", "--all-groups", "--all-extras")
COREPACK = ("corepack", "enable")
PNPM_INSTALL = ("pnpm", "install", "--frozen-lockfile")
PLAYWRIGHT = ("pnpm", "exec", "playwright", "install", "--with-deps")
UV_ORIGIN = _RuntimeView(UV_IMAGE, (), (), None, (), (), (), ("from",))
NODE_ORIGIN = _RuntimeView(NODE_IMAGE, (), (), None, (), (), (), ("from",))
UV_FILE = (("/usr/local/bin/uv", _ArtifactView("/uv", UV_ORIGIN)),)
SOURCE_DIRECTORY = (("/src", "source", None),)
PYTHON_ENV = (("UV_PROJECT_ENVIRONMENT", "/opt/venv"),)
NODE_ENV = (("CI", "1"),)
MEASUREMENT_ENV = (("AGENTIC_SAGA_RELEASE_WHEELHOUSE", "/src/dist/release/wheelhouse"),)
PYTHON_GATE_OPERATIONS = (
    "from",
    "with_file",
    "with_directory",
    "with_workdir",
    "with_env_variable",
    "with_exec",
    "with_exec",
)


def _python_gate_view(image: str) -> _RuntimeView:
    return _RuntimeView(
        image,
        UV_FILE,
        SOURCE_DIRECTORY,
        "/src",
        PYTHON_ENV,
        (UV_SYNC, ("uv", "run", "poe", "gate")),
        (),
        PYTHON_GATE_OPERATIONS,
    )


FRONTEND_GATE_VIEW = _RuntimeView(
    NODE_IMAGE,
    (),
    SOURCE_DIRECTORY,
    "/src/web/flight-recorder",
    NODE_ENV,
    (COREPACK, PNPM_INSTALL, PLAYWRIGHT, ("pnpm", "gate")),
    (),
    (
        "from",
        "with_directory",
        "with_workdir",
        "with_env_variable",
        "with_exec",
        "with_exec",
        "with_exec",
        "with_exec",
    ),
)
RELEASE_DIRECTORIES = (
    *SOURCE_DIRECTORY,
    (
        "/usr/local",
        _ArtifactView("/usr/local", NODE_ORIGIN),
        tuple(["bin/corepack", "bin/node", "bin/npm", "bin/npx", "lib/node_modules/**"]),
    ),
)
RELEASE_OPERATIONS = (
    *PYTHON_GATE_OPERATIONS[:-1],
    "with_directory",
    "with_workdir",
    "with_env_variable",
    "with_exec",
    "with_exec",
    "with_exec",
    "with_workdir",
)


def _release_view(image: str, product: tuple[tuple[str, ...], ...]) -> _RuntimeView:
    return _RuntimeView(
        image,
        UV_FILE,
        RELEASE_DIRECTORIES,
        "/src",
        (*PYTHON_ENV, *NODE_ENV),
        (UV_SYNC, COREPACK, PNPM_INSTALL, PLAYWRIGHT, *product),
        (),
        (*RELEASE_OPERATIONS, *("with_exec",) * len(product)),
    )


def _measured_release_view(image: str) -> _RuntimeView:
    release = _release_view(image, MEASURED_CANDIDATE)
    operations = (
        *RELEASE_OPERATIONS,
        "with_exec",
        "with_env_variable",
        "with_exec",
    )
    return replace(
        release,
        environment=(*release.environment, *MEASUREMENT_ENV),
        operations=operations,
    )


SECURITY_VIEW = _RuntimeView(
    NODE_IMAGE,
    (),
    SOURCE_DIRECTORY,
    "/src/web/flight-recorder",
    NODE_ENV,
    (COREPACK, PNPM_INSTALL, ("pnpm", "audit")),
    (),
    (
        "from",
        "with_directory",
        "with_workdir",
        "with_env_variable",
        "with_exec",
        "with_exec",
        "with_exec",
    ),
)
RELEASE_CANDIDATE = (("uv", "run", "poe", "release-candidate"),)
MEASURED_CANDIDATE = (
    *RELEASE_CANDIDATE,
    ("uv", "run", "python", "scripts/measure_release.py"),
)
EXPECTED_CI_VIEWS = (
    _python_gate_view(PYTHON_312_IMAGE),
    _release_view(PYTHON_312_IMAGE, RELEASE_CANDIDATE),
    _measured_release_view(PYTHON_312_IMAGE),
    _python_gate_view(PYTHON_IMAGE),
    _release_view(PYTHON_IMAGE, RELEASE_CANDIDATE),
    _measured_release_view(PYTHON_IMAGE),
    FRONTEND_GATE_VIEW,
)
EXPECTED_MATRIX_PRODUCTS = (
    (PYTHON_312_IMAGE, ("uv", "run", "poe", "gate")),
    (PYTHON_312_IMAGE, ("uv", "run", "poe", "release-candidate")),
    (PYTHON_312_IMAGE, ("uv", "run", "python", "scripts/measure_release.py")),
    (PYTHON_IMAGE, ("uv", "run", "poe", "gate")),
    (PYTHON_IMAGE, ("uv", "run", "poe", "release-candidate")),
    (PYTHON_IMAGE, ("uv", "run", "python", "scripts/measure_release.py")),
    (NODE_IMAGE, ("pnpm", "gate")),
)
UV_EXTRACTION = 'uv = dag.container().from_(UV_IMAGE).file("/uv")'
UV_OVERWRITE = (
    'uv = dag.container().from_(UV_IMAGE).with_file("/uv", '
    'dag.container().from_(image).file("/etc/passwd")).file("/uv")'
)
NODE_EXTRACTION = 'node = dag.container().from_(NODE_IMAGE).directory("/usr/local")'
NODE_OVERLAY = (
    'node = dag.container().from_(NODE_IMAGE).with_directory("/usr/local", '
    'base.directory(SOURCE_ROOT)).directory("/usr/local")'
)


def _assert_runtime_lineage(
    trace: _Trace, source: object, expected: tuple[_RuntimeView, ...]
) -> None:
    actual = tuple(_runtime_view(snapshot, source) for snapshot in trace.product_snapshots)
    assert actual == expected
    assert len(trace.sync_snapshots) == len(trace.product_snapshots)
    assert all(
        product is synchronized
        for product, synchronized in zip(trace.product_snapshots, trace.sync_snapshots, strict=True)
    )


def test_should_restore_each_supported_runtime_release_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the closed public CI entry point and both supported Python runtimes.
    module, trace = _adapter_module(monkeypatch)

    # When the complete candidate proof executes.
    asyncio.run(module.AgenticSaga().ci(object(), "a" * 40, object()))

    # Then each runtime owns a gate, immutable candidate, and packaged measured proof.
    actual = tuple((snapshot.image, snapshot.commands[-1]) for snapshot in trace.product_snapshots)
    assert actual == EXPECTED_MATRIX_PRODUCTS


def test_should_resolve_offline_measurement_from_verified_wheelhouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given each release candidate has built a hash-pinned offline wheelhouse.
    module, trace = _adapter_module(monkeypatch)

    # When the nested release measurements execute.
    asyncio.run(module.AgenticSaga().ci(object(), "a" * 40, object()))

    # Then uv must resolve only from that candidate wheelhouse, never an ambient cache or index.
    measured = tuple(
        snapshot
        for snapshot in trace.product_snapshots
        if snapshot.commands[-1] == ("uv", "run", "python", "scripts/measure_release.py")
    )
    assert len(measured) == len((PYTHON_312_IMAGE, PYTHON_IMAGE))
    assert all(
        snapshot.environment[-len(MEASUREMENT_ENV) :] == MEASUREMENT_ENV for snapshot in measured
    )


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

    # When the four runtime identities are inspected.
    # Then each is immutable and the module build uses the same Git-capable Python image.
    expected = {
        "PYTHON_IMAGES": (PYTHON_312_IMAGE, PYTHON_IMAGE),
        "UV_IMAGE": UV_IMAGE,
        "NODE_IMAGE": NODE_IMAGE,
    }
    assert assignments.items() >= expected.items()
    assert f'base-image = "{PYTHON_IMAGE}"' in pyproject


def test_should_commit_lock_and_ignore_generated_sdk() -> None:
    # Given the generated module's reproducibility and VCS boundaries.
    config = cast(dict[str, object], json.loads((ROOT / "dagger.json").read_text()))
    included = cast(list[str], config["include"])

    # When the real Git index and ignore engine are queried.
    _assert_vcs_boundaries(ROOT)

    # Then the tracked lock remains an explicit module input.
    assert ".dagger/uv.lock" in included


def _poisoned_repository(tmp_path: Path, poisoned_path: str) -> Path:
    repository = tmp_path / "repository"
    (repository / ".dagger/sdk").mkdir(parents=True)
    (repository / ".dagger/.venv").mkdir()
    (repository / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    (repository / ".dagger/uv.lock").write_text("lock")
    (repository / ".dagger/sdk/generated.py").write_text("generated")
    (repository / ".dagger/.venv/pyvenv.cfg").write_text("generated")
    poisoned = repository / poisoned_path
    poisoned.parent.mkdir(parents=True, exist_ok=True)
    poisoned.write_text("poison")
    _git(repository, "init", "--quiet")
    _git(repository, "add", ".gitignore", ".dagger/uv.lock")
    _git(repository, "add", "--force", poisoned_path)
    return repository


@pytest.mark.parametrize(
    "poisoned_path",
    (".dagger/sdk/client/internal.py", ".dagger/.venv/lib/python3.13/site-packages/bad.py"),
)
def test_should_reject_generated_state_forced_into_a_git_index(
    tmp_path: Path, poisoned_path: str
) -> None:
    # Given an isolated repository with production ignores and one poisoned index entry.
    repository = _poisoned_repository(tmp_path, poisoned_path)

    # When the closed VCS boundary evaluates the isolated index.
    # Then force-added generated SDK state is rejected without touching the real worktree.
    with pytest.raises(AssertionError):
        _assert_vcs_boundaries(repository)


def test_should_execute_resolved_git_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a sentinel absolute Git executable returned by the active PATH.
    executable = str(ROOT / "sentinel-git")
    commands: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda _: executable)

    def capture(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", capture)

    # When the VCS boundary interrogates the worktree.
    _git(ROOT, "status", "--short")

    # Then argv[0] is exactly the absolute executable selected by the PATH resolver.
    assert commands[0][0] == executable


def test_should_reject_relative_git_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a relative executable returned by the active PATH resolver.
    monkeypatch.setattr(shutil, "which", lambda _: "bin/git")

    # When the VCS boundary tries to execute Git.
    # Then the executable must be rejected before subprocess execution.
    with pytest.raises(AssertionError, match="absolute path"):
        _git(ROOT, "status", "--short")


def test_should_fail_closed_when_git_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given no Git executable on the active PATH.
    monkeypatch.setattr(shutil, "which", lambda _: None)

    # When the VCS boundary tries to execute Git.
    # Then missing tooling cannot silently fall back to a fixed host path.
    with pytest.raises(AssertionError, match="Git executable"):
        _git(ROOT, "status", "--short")


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
            (
                UV_IMAGE,
                PYTHON_312_IMAGE,
                UV_IMAGE,
                PYTHON_312_IMAGE,
                NODE_IMAGE,
                UV_IMAGE,
                PYTHON_IMAGE,
                UV_IMAGE,
                PYTHON_IMAGE,
                NODE_IMAGE,
                NODE_IMAGE,
            ),
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


def test_should_capture_complete_immutable_product_snapshot_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a public CI execution through the observable Dagger boundary.
    module, trace = _adapter_module(monkeypatch)

    # When the release-bearing lane runs.
    asyncio.run(module.AgenticSaga().ci(object(), "a" * 40, object()))

    # Then each product command retains a distinct complete runtime ancestry.
    assert trace.git_tree is not None
    _assert_runtime_lineage(trace, trace.git_tree, EXPECTED_CI_VIEWS)


def test_should_capture_complete_security_boundary_and_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given exact source identity and an opaque typed authorization secret.
    module, trace = _adapter_module(monkeypatch)
    source, auth, commit_sha = object(), object(), "a" * 40

    # When the public security lane runs.
    asyncio.run(module.AgenticSaga().security(source, commit_sha, auth))

    # Then both shared boundaries and the frontend audit retain exact values and ancestry.
    boundary = (source, "hseshadr/agentic-saga", commit_sha, auth)
    assert trace.guard_calls == [boundary]
    assert trace.audit_calls == [boundary] and trace.audit_syncs == 1
    _assert_runtime_lineage(trace, source, (SECURITY_VIEW,))


@pytest.mark.parametrize(
    ("original", "replacement"),
    ((UV_EXTRACTION, UV_OVERWRITE), (NODE_EXTRACTION, NODE_OVERLAY)),
)
def test_should_reject_tainted_runtime_artifact_ancestry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, original: str, replacement: str
) -> None:
    # Given production source that extracts a runtime artifact after an unsafe overlay.
    module, trace = _mutated_adapter(monkeypatch, tmp_path, original, replacement)
    asyncio.run(module.AgenticSaga().ci(object(), "a" * 40, object()))

    # When complete public-CI runtime ancestry is compared.
    # Then neither overwritten uv nor repository-overlaid Node contents can false-green.
    assert trace.git_tree is not None
    with pytest.raises(AssertionError):
        _assert_runtime_lineage(trace, trace.git_tree, EXPECTED_CI_VIEWS)


def test_should_fail_closed_on_cyclic_artifact_ancestry() -> None:
    # Given a deliberately corrupted file artifact whose origin contains itself.
    origin = _Snapshot()
    artifact = _FileArtifact(origin, "/uv")
    cyclic = replace(origin, files=(_FileMount("/uv", artifact),))
    object.__setattr__(artifact, "snapshot", cyclic)

    # When recursive ancestry is rendered for comparison.
    # Then a cycle is rejected rather than looping or skipping nested mounts.
    with pytest.raises(AssertionError, match="cyclic artifact ancestry"):
        _runtime_view(cyclic, object())


@pytest.mark.parametrize("mutation", ("wrong-workdir", "secret-mount"))
def test_should_reject_unsafe_release_container_lineage_mutations(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    # Given a public CI execution with one controlled unsafe lineage mutation.
    module, trace = _adapter_module(monkeypatch, mutation=mutation)

    # When the exact runtime contract inspects its product and sync snapshots.
    asyncio.run(module.AgenticSaga().ci(object(), "a" * 40, object()))

    # Then wrong workdirs and mounted credentials cannot false-green.
    assert trace.git_tree is not None
    with pytest.raises(AssertionError):
        _assert_runtime_lineage(trace, trace.git_tree, EXPECTED_CI_VIEWS)


@pytest.mark.parametrize(
    "operation",
    ("with_mounted_secret", "with_secret_variable", "with_mounted_cache", "with_user"),
)
def test_should_fail_closed_on_unmodeled_configuration_and_secret_operations(
    operation: str,
) -> None:
    # Given an immutable container fake with a closed configuration surface.
    container = _Dag(_Trace()).container()

    # When an unknown mount, secret, cache, or identity operation is requested.
    # Then the fake cannot discard it and allow a false-green product trace.
    with pytest.raises(AttributeError, match=operation):
        getattr(container, operation)("unexpected")


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
    assert configured is not container and container.snapshot == _Snapshot()
    assert trace.events == []
