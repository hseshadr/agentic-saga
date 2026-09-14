from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest
from dagger import Container, Directory, dag

from agentic_saga_ci import main

ROOT = Path(__file__).parents[2]
MODULE = ROOT / ".dagger" / "src" / "agentic_saga_ci" / "main.py"
CONFIG = ROOT / "dagger.json"
WORKFLOWS = ROOT / ".github" / "workflows"
FOUNDATION_SHA = "5cf3b7550442bb06d1cce1f146e48c064dcf511c"
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
DAGGER_ACTION_SHA = "27b130bf0f79a7f6fbbbe0fbca6760dc9bb40a77"
PYTHON_IMAGES = (
    "python:3.12.12-bookworm@sha256:"
    "c0abd0758831ad99b7a29e0c1a875da9c4abb9a2e3f21e2eeb585dbcadfb6cd0",
    "python:3.13.14-bookworm@sha256:"
    "8b9a8b28d9cc221c6ab5d40e9cfcd99429959f6a8f5171612a99147975ab043f",
)
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.11.32@sha256:"
    "df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c"
)
NODE_IMAGE = (
    "node:24.6.0-bookworm-slim@sha256:"
    "9b741b28148b0195d62fa456ed84dd6c953c1f17a3761f3e6e6797a754d9edff"
)
WHEELHOUSES = (
    "/src/dist/release/wheelhouses/3.12",
    "/src/dist/release/wheelhouses/3.13",
)
PUBLIC_INPUTS = (
    ("source", "dagger.Directory"),
    ("commit_sha", "str"),
    ("git_auth_header", "dagger.Secret"),
)
GENERATED_PATHS = (".dagger/sdk/generated.py", ".dagger/.venv/pyvenv.cfg")
GENERATED_PREFIXES = (".dagger/sdk/", ".dagger/.venv/")

PublicMethod = ast.AsyncFunctionDef | ast.FunctionDef
Parameter = tuple[str, str]
Signature = tuple[str, tuple[Parameter, ...], str]


def _tree(source: str | None = None) -> ast.Module:
    return ast.parse(MODULE.read_text() if source is None else source)


def _decorator_name(node: ast.expr) -> str | None:
    value = node.func if isinstance(node, ast.Call) else node
    if isinstance(value, ast.Name):
        return value.id
    return value.attr if isinstance(value, ast.Attribute) else None


def _adapter_class(tree: ast.Module) -> ast.ClassDef:
    classes = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgenticSaga"
    ]
    assert len(classes) == 1
    return classes[0]


def _public_methods(adapter: ast.ClassDef) -> Iterator[PublicMethod]:
    for member in adapter.body:
        if isinstance(member, (ast.AsyncFunctionDef, ast.FunctionDef)) and (
            "function" in {_decorator_name(item) for item in member.decorator_list}
        ):
            yield member


def _signature(method: PublicMethod) -> Signature:
    arguments = method.args.args
    inputs = arguments[1:] if arguments and arguments[0].arg == "self" else arguments
    annotations = tuple((item.arg, ast.unparse(item.annotation)) for item in inputs)
    return method.name, annotations, ast.unparse(method.returns)


def _literal_constants(source: str) -> dict[str, object]:
    return {
        node.target.id: ast.literal_eval(node.value)
        for node in _tree(source).body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    }


def _assert_public_schema(source: str) -> None:
    actual = tuple(_signature(method) for method in _public_methods(_adapter_class(_tree(source))))
    expected = (("ci", PUBLIC_INPUTS, "str"), ("security", PUBLIC_INPUTS, "str"))
    assert actual == expected, "only ci and security may be public Dagger functions"


def _assert_immutable_runtime_contract(source: str) -> None:
    constants = _literal_constants(source)
    expected = {
        "PYTHON_IMAGES": PYTHON_IMAGES,
        "UV_IMAGE": UV_IMAGE,
        "NODE_IMAGE": NODE_IMAGE,
        "RUNTIME_WHEELHOUSES": WHEELHOUSES,
    }
    assert constants.items() >= expected.items(), (
        "runtime identities must remain immutable and versioned"
    )


def _assert_module_dependencies(config: str) -> None:
    parsed = cast(dict[str, object], json.loads(config))
    expected = [
        {
            "name": "foundation",
            "source": f"github.com/hseshadr/ci/modules/portfolio-foundation@{FOUNDATION_SHA}",
            "pin": FOUNDATION_SHA,
        },
        {
            "name": "python-package",
            "source": f"github.com/hseshadr/ci/modules/python-package@{FOUNDATION_SHA}",
            "pin": FOUNDATION_SHA,
        },
    ]
    assert parsed["dependencies"] == expected, (
        "shared Dagger modules must use one immutable revision"
    )


def _assert_workflow_boundary(name: str, workflow: str) -> None:
    argument = "ci" if name == "dagger.yml" else "security"
    assert f"uses: actions/checkout@{CHECKOUT_SHA}" in workflow
    assert f"uses: dagger/dagger-for-github@{DAGGER_ACTION_SHA}" in workflow
    assert 'version: "0.21.8"' in workflow
    assert f"args: {argument} --source=. --commit-sha=${{{{ github.sha }}}}" in workflow
    assert "--git-auth-header=env:DAGGER_GIT_HTTP_AUTH_HEADER" in workflow


def _git_binary() -> str:
    executable = shutil.which("git")
    assert executable is not None and Path(executable).is_absolute()
    return executable


def _git(root: Path, *arguments: str) -> tuple[str, ...]:
    result = subprocess.run(  # noqa: S603 - resolved Git only interrogates test repositories.
        [_git_binary(), *arguments], cwd=root, check=True, capture_output=True, text=True
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def _assert_generated_paths_are_untracked(root: Path) -> None:
    tracked = _git(root, "ls-files", "--", *GENERATED_PREFIXES)
    ignored = _git(root, "check-ignore", "--no-index", *GENERATED_PATHS)
    assert tracked == (), "generated Dagger SDK and virtualenv files cannot enter Git"
    assert ignored == GENERATED_PATHS, "generated Dagger SDK and virtualenv files must be ignored"


def test_should_expose_only_ci_and_security_with_the_closed_typed_boundary() -> None:
    # Given the adapter source.
    source = MODULE.read_text()

    # When public Dagger signatures are inspected.
    # Then only the two supported typed entry points are exported.
    _assert_public_schema(source)


def test_should_reject_an_unapproved_public_dagger_function() -> None:
    # Given a copied adapter source with an extra endpoint.
    source = MODULE.read_text().replace(
        "    @function\n    async def security(",
        "    @function\n    async def preview(self) -> str:\n"
        "        return 'not approved'\n\n"
        "    @function\n    async def security(",
    )

    # When the closed public schema is applied.
    # Then the extra endpoint is rejected.
    with pytest.raises(AssertionError, match="only ci and security"):
        _assert_public_schema(source)


def test_should_pin_each_runtime_and_keep_separate_wheelhouses() -> None:
    # Given the adapter's immutable literals.
    source = MODULE.read_text()

    # When runtime identities are checked.
    # Then Python 3.12 and 3.13 retain pinned images and ABI-specific wheelhouses.
    _assert_immutable_runtime_contract(source)


@pytest.mark.parametrize(
    ("original", "replacement"),
    (("@sha256:", "@tag:"), (WHEELHOUSES[1], WHEELHOUSES[0])),
)
def test_should_reject_unpinned_or_shared_runtime_mutations(
    original: str, replacement: str
) -> None:
    # Given a copied adapter source with an unsafe immutable-literal change.
    source = MODULE.read_text().replace(original, replacement, 1)

    # When its immutable runtime contract is checked.
    # Then dropped Python 3.13, unpinned images, and a shared wheelhouse fail closed.
    with pytest.raises(AssertionError, match="runtime identities"):
        _assert_immutable_runtime_contract(source)


def test_should_pin_shared_modules_to_one_private_history_revision() -> None:
    # Given the Dagger module configuration.
    config = CONFIG.read_text()

    # When shared module identities are checked.
    # Then each dependency remains pinned to the same reviewed revision.
    _assert_module_dependencies(config)


def test_should_reject_an_unpinned_shared_module() -> None:
    # Given a copied Dagger configuration with a floating Foundation source.
    config = CONFIG.read_text().replace(f"@{FOUNDATION_SHA}", "@main", 1)

    # When its dependency identities are checked.
    # Then a mutable shared module reference is rejected.
    with pytest.raises(AssertionError, match="shared Dagger modules"):
        _assert_module_dependencies(config)


@pytest.mark.parametrize("name", ("dagger.yml", "dagger-security.yml"))
def test_should_preserve_thin_pinned_workflow_ingress(name: str) -> None:
    # Given one repository-owned Dagger ingress workflow.
    workflow = (WORKFLOWS / name).read_text()

    # When its checkout, action, and public invocation are checked.
    # Then CI remains a pinned thin shell around the Dagger graph.
    _assert_workflow_boundary(name, workflow)


@pytest.mark.parametrize(
    ("original", "replacement"),
    ((CHECKOUT_SHA, "v4"), (DAGGER_ACTION_SHA, "v8.4.1")),
)
def test_should_reject_an_unpinned_workflow_action(original: str, replacement: str) -> None:
    # Given a copied workflow with a floating action reference.
    workflow = (WORKFLOWS / "dagger.yml").read_text().replace(original, replacement, 1)

    # When the ingress contract is checked.
    # Then neither checkout nor Dagger itself can float.
    with pytest.raises(AssertionError):
        _assert_workflow_boundary("dagger.yml", workflow)


def test_should_keep_generated_dagger_state_out_of_the_git_index() -> None:
    # Given the real worktree and its ignore rules.
    # When generated module state is checked through Git.
    # Then generated SDK and virtualenv content are not tracked.
    _assert_generated_paths_are_untracked(ROOT)


def test_should_reject_generated_dagger_state_forced_into_a_fixture_index(tmp_path: Path) -> None:
    # Given an isolated repository where generated state was force-added.
    repository = tmp_path / "repository"
    (repository / ".dagger/sdk").mkdir(parents=True)
    (repository / ".dagger/.venv").mkdir()
    (repository / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    (repository / ".dagger/sdk/generated.py").write_text("generated")
    (repository / ".dagger/.venv/pyvenv.cfg").write_text("generated")
    _git(repository, "init", "--quiet")
    _git(repository, "add", ".gitignore")
    _git(repository, "add", "--force", *GENERATED_PATHS)

    # When the same Git-index boundary is applied.
    # Then tracked generated content is rejected even though it is ignored for new files.
    with pytest.raises(AssertionError, match="cannot enter Git"):
        _assert_generated_paths_are_untracked(repository)


@dataclass
class _AsyncOutput:
    label: str
    syncs: int = 0

    async def sync(self) -> _AsyncOutput:
        self.syncs += 1
        await asyncio.sleep(0)
        return self

    def directory(self, path: str) -> str:
        return f"{self.label}:{path}"


@dataclass
class _SyncedBoundary:
    events: list[str]
    name: str

    async def sync(self) -> None:
        self.events.append(f"{self.name}:sync")


@dataclass
class _BoundaryDag:
    events: list[str]
    resolved_tree: object

    def foundation(self) -> _BoundaryDag:
        return self

    def guard(self, **_: object) -> _SyncedBoundary:
        self.events.append("guard")
        return _SyncedBoundary(self.events, "guard")

    def python_package(self) -> _BoundaryDag:
        return self

    def dependency_audit(self, **_: object) -> _SyncedBoundary:
        self.events.append("audit")
        return _SyncedBoundary(self.events, "audit")

    def git(self, *_: object, **__: object) -> _BoundaryDag:
        self.events.append("git")
        return self

    def commit(self, _: str) -> _BoundaryDag:
        self.events.append("commit")
        return self

    def tree(self, **_: object) -> object:
        self.events.append("tree")
        return self.resolved_tree


def _import_mutated_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> ModuleType:
    path = tmp_path / "mutated_main.py"
    path.write_text(source)
    dagger = ModuleType("dagger")
    dagger.Directory = object
    dagger.Secret = object
    dagger.Container = object
    dagger.function = lambda value: value
    dagger.check = lambda value: value
    dagger.object_type = lambda value: value
    dagger.dag = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "dagger", dagger)
    name = f"task_four_adapter_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _mutated_source(original: str, replacement: str) -> str:
    source = MODULE.read_text()
    assert source.count(original) == 1
    return source.replace(original, replacement)


def _assert_ci_uses_resolved_source(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    source, verified, artifacts, frontend = object(), object(), object(), object()

    async def resolve(*_: object) -> object:
        events.append("resolve")
        return verified

    async def shared(received: object) -> tuple[object, object]:
        assert received is verified, "CI must use authenticated exact source"
        events.append("shared")
        return artifacts, frontend

    async def matrix(received: object, built: object, proof: object) -> None:
        assert (received, built, proof) == (verified, artifacts, frontend)
        events.append("matrix")

    monkeypatch.setattr(module, "_release_source", resolve)
    monkeypatch.setattr(module, "_shared_outputs", shared)
    monkeypatch.setattr(module, "_runtime_matrix", matrix)
    result = asyncio.run(module.AgenticSaga().ci(source, "a" * 40, object()))
    assert result == "Agentic Saga canonical Dagger gate passed"
    assert events == ["resolve", "shared", "matrix"]


def _assert_shared_outputs_are_single_builds(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    artifacts = _AsyncOutput("artifacts")
    dependencies = _AsyncOutput("dependencies")
    frontend = _AsyncOutput("frontend")
    frontend_artifacts = object()

    def build_artifacts(_: object) -> _AsyncOutput:
        calls.append("artifacts")
        return artifacts

    def build_dependencies(_: object) -> _AsyncOutput:
        calls.append("dependencies")
        return dependencies

    def build_frontend(_: object, received: _AsyncOutput) -> _AsyncOutput:
        assert received is dependencies
        calls.append("frontend")
        return frontend

    def collect(received: _AsyncOutput, proof: _AsyncOutput) -> object:
        assert (received, proof) == (dependencies, frontend)
        return frontend_artifacts

    monkeypatch.setattr(module, "_artifact_builder", build_artifacts)
    monkeypatch.setattr(module, "_frontend_dependencies", build_dependencies)
    monkeypatch.setattr(module, "_frontend_builder", build_frontend)
    monkeypatch.setattr(module, "_frontend_artifacts", collect)
    result = asyncio.run(module._shared_outputs(object()))
    assert result == (f"artifacts:{main.RELEASE_ROOT}", frontend_artifacts)
    assert calls == ["artifacts", "dependencies", "frontend"]
    assert artifacts.syncs == frontend.syncs == 1


def _assert_runtime_failure_propagates(module: ModuleType) -> None:
    async def failure() -> None:
        raise RuntimeError("runtime lane failed")

    with pytest.raises(RuntimeError, match="runtime lane failed"):
        asyncio.run(module._bounded_gather(failure(), limit=2))


def test_should_resolve_exact_source_before_shared_and_runtime_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the real public adapter with isolated orchestration collaborators.
    # When CI runs through ordinary awaitables.
    # Then the resolved source becomes the sole input to both later phases.
    _assert_ci_uses_resolved_source(main, monkeypatch)


def test_should_reject_missing_exact_source_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given a copied adapter that bypasses authenticated source resolution.
    source = _mutated_source(
        "verified = await _release_source(source, commit_sha, git_auth_header)", "verified = source"
    )
    module = _import_mutated_adapter(monkeypatch, tmp_path, source)

    # When CI is exercised through real awaitables.
    # Then caller-provided source cannot silently replace authenticated exact source.
    with pytest.raises(AssertionError, match="authenticated exact source"):
        _assert_ci_uses_resolved_source(module, monkeypatch)


def test_should_build_first_party_artifacts_and_frontend_proof_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the real shared-output orchestration with async result probes.
    # When independent builds run.
    # Then each reusable build is created and synchronized once.
    _assert_shared_outputs_are_single_builds(main, monkeypatch)


def test_should_construct_the_real_lazy_dependency_and_release_graph() -> None:
    # Given an actual Dagger directory and its lazy graph API.
    source = dag.directory()

    # When every reusable graph builder composes without synchronizing a container.
    python = main._python(source, main.PYTHON_IMAGES[0])
    dependencies = main._frontend_dependencies(source)
    frontend = main._frontend_builder(source, dependencies)
    artifacts = main._frontend_artifacts(dependencies, frontend)
    release = main._release(source, main.PYTHON_IMAGES[1], artifacts)
    candidate = main._proved_candidate(source, main.PYTHON_IMAGES[1], dag.directory(), artifacts)

    # Then Dagger owns the real container and directory graph, without a test runtime emulator.
    assert isinstance(python, Container)
    assert isinstance(release, Container)
    assert isinstance(candidate, Container)
    assert isinstance(artifacts.coverage, Directory)
    assert main.QUALITY_PROOF in main._quality_proof_command()[-1]
    assert main._measurement_command()[-1] == main.QUALITY_PROOF


def test_should_guard_exact_source_and_complete_the_python_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given compact asynchronous boundaries for the two shared Dagger modules.
    tree = object()
    boundary = _BoundaryDag([], tree)
    monkeypatch.setattr(main, "dag", boundary)

    # When exact source resolution and the locked Python audit execute.
    resolved = asyncio.run(main._release_source(object(), "a" * 40, object()))
    asyncio.run(main._dependency_audit(object(), "a" * 40, object()))

    # Then the guard completes before checkout and the audit completes before return.
    assert resolved is tree
    assert boundary.events == [
        "guard",
        "guard:sync",
        "git",
        "commit",
        "tree",
        "audit",
        "audit:sync",
    ]


def test_should_start_the_two_runtime_lanes_with_real_awaitables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a runtime lane collaborator that records ordinary async calls.
    calls: list[tuple[str, str]] = []

    async def lane(_: object, image: str, wheelhouse: str, *__: object) -> None:
        calls.append((image, wheelhouse))
        await asyncio.sleep(0)

    monkeypatch.setattr(main, "_runtime_lane", lane)

    # When the real matrix fan-out runs.
    asyncio.run(main._runtime_matrix(object(), object(), object()))

    # Then both pinned runtimes receive their matching immutable wheelhouse.
    assert calls == list(zip(main.PYTHON_IMAGES, main.RUNTIME_WHEELHOUSES, strict=True))


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        (
            "artifact_builder = _artifact_builder(source)",
            "artifact_builder = _artifact_builder(source)\n    _artifact_builder(source)",
        ),
        (
            "frontend_builder = _frontend_builder(source, dependencies)",
            "frontend_builder = dependencies",
        ),
    ),
)
def test_should_reject_missing_or_duplicate_shared_builds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, original: str, replacement: str
) -> None:
    # Given a copied adapter with duplicate artifacts or no frontend proof.
    module = _import_mutated_adapter(monkeypatch, tmp_path, _mutated_source(original, replacement))

    # When its shared outputs execute.
    # Then each required shared build must occur exactly once.
    with pytest.raises(AssertionError):
        _assert_shared_outputs_are_single_builds(module, monkeypatch)


def test_should_propagate_a_runtime_lane_failure() -> None:
    # Given the real bounded fan-out.
    # When a runtime lane fails.
    # Then its exception remains visible to the caller.
    _assert_runtime_failure_propagates(main)


def test_should_reject_a_runtime_lane_failure_converted_to_a_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given a copied adapter that asks gather to return exceptions as successful results.
    source = _mutated_source(
        "await asyncio.gather(*(_run_bounded(operation, semaphore) for operation in operations))",
        "await asyncio.gather(\n"
        "        *(_run_bounded(operation, semaphore) for operation in operations),\n"
        "        return_exceptions=True,\n"
        "    )",
    )
    module = _import_mutated_adapter(monkeypatch, tmp_path, source)

    # When a real failing awaitable enters the bounded fan-out.
    # Then swallowed failure behavior is rejected.
    with pytest.raises(pytest.fail.Exception):
        _assert_runtime_failure_propagates(module)
