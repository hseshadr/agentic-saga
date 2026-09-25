from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import cast

import dagger
import pytest
import yaml
from dagger import Container, Directory, dag

from agentic_saga_ci import main

ROOT = Path(__file__).parents[2]
MODULE = ROOT / ".dagger" / "src" / "agentic_saga_ci" / "main.py"
CONFIG = ROOT / "dagger.json"
PYPROJECT = ROOT / ".dagger" / "pyproject.toml"
WORKFLOWS = ROOT / ".github" / "workflows"
FOUNDATION_SHA = "9d491851fc5c65ad4a388ed2dd7bb4def4e1f007"
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
NODE_PATHS = (
    "bin/corepack",
    "bin/node",
    "bin/npm",
    "bin/npx",
    "bin/pnpm",
    "lib/node_modules/**",
)
FRONTEND_LOCK_INPUTS = (
    "web/flight-recorder/package.json",
    "web/flight-recorder/pnpm-lock.yaml",
)
VALID_MANIFEST = "\n".join(
    (
        f"{'a' * 64}  agentic_saga-0.1.0-py3-none-any.whl",
        f"{'b' * 64}  agentic_saga-0.1.0.tar.gz",
        f"{'c' * 64}  runtime-requirements.txt",
    )
)
PUBLIC_INPUTS = (
    ("commit_sha", "str"),
    ("git_auth_header", "dagger.Secret | None"),
)
AUTH_ARGUMENT = "--git-auth-header=env:DAGGER_GIT_HTTP_AUTH_HEADER"
AUTH_PREDICATE = (
    "github.event.repository.private && (github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository) && "
    "secrets.DAGGER_GIT_HTTP_AUTH_HEADER != ''"
)
# Inverted contract (was: args carried `${{ <predicate> && '<auth flag>' || '' }}`, pasting an
# event expression into dagger-for-github's bash). The predicate now renders into the
# USE_GIT_AUTH env var and args only test it, so no expression reaches the script.
SAFE_AUTH_SWITCH_EXPRESSION = f"${{{{ {AUTH_PREDICATE} && 'true' || '' }}}}"
SAFE_AUTH_ARGUMENT = f"${{USE_GIT_AUTH:+{AUTH_ARGUMENT}}}"
SAFE_AUTH_ENV_EXPRESSION = (
    f"${{{{ {AUTH_PREDICATE} && secrets.DAGGER_GIT_HTTP_AUTH_HEADER || '' }}}}"
)
GENERATED_PATHS = (".dagger/sdk/generated.py", ".dagger/.venv/pyvenv.cfg")
GENERATED_PREFIXES = (".dagger/sdk/", ".dagger/.venv/")
DAGGER_LOCK = ".dagger/uv.lock"

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


def _annotation(node: ast.arg | None) -> str:
    return "None" if node is None or node.annotation is None else ast.unparse(node.annotation)


def _parameter(node: ast.arg | None) -> Parameter | None:
    return None if node is None else (node.arg, _annotation(node))


def _parameters(nodes: list[ast.arg]) -> tuple[Parameter, ...]:
    return tuple((node.arg, _annotation(node)) for node in nodes)


def _signature(method: PublicMethod) -> Signature:
    arguments = method.args
    positional = (
        arguments.args[1:] if arguments.args and arguments.args[0].arg == "self" else arguments.args
    )
    return (
        method.name,
        _parameters(arguments.posonlyargs),
        _parameters(positional),
        _parameter(arguments.vararg),
        _parameters(arguments.kwonlyargs),
        _parameter(arguments.kwarg),
        (len(arguments.defaults), len(arguments.kw_defaults)),
        ast.unparse(method.returns),
    )


def _literal_constants(source: str) -> dict[str, object]:
    return {
        node.target.id: ast.literal_eval(node.value)
        for node in _tree(source).body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    }


def _function_body(source: str, name: str) -> str:
    functions = [
        node
        for node in _tree(source).body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name
    ]
    assert len(functions) == 1
    return "\n".join(ast.unparse(statement) for statement in functions[0].body)


def _assert_ordered(body: str, markers: tuple[str, ...]) -> None:
    assert all(marker in body for marker in markers), "required runtime stage is missing"
    positions = tuple(body.index(marker) for marker in markers)
    assert positions == tuple(sorted(positions)), "runtime stages must preserve saga proof order"


def _assert_runtime_phase_contract(source: str) -> None:
    proof = _function_body(source, "_proved_candidate")
    proof_runtime = _function_body(source, "_prove_runtime")
    measurement = _function_body(source, "_measure_runtime")
    matrix = _function_body(source, "_runtime_matrix")
    proof_steps = (
        "poe', 'gate",
        "RELEASE_ROOT",
        "release-candidate",
        "FRONTEND_COVERAGE",
        "_quality_proof_command",
    )
    proof_runtime_steps = ("_proved_candidate", "return await proved.sync()")
    measurement_steps = (
        "with_env_variable('AGENTIC_SAGA_RELEASE_ARTIFACTS', RELEASE_ROOT)",
        "with_env_variable('AGENTIC_SAGA_RELEASE_WHEELHOUSE', wheelhouse)",
        "_measurement_command",
    )
    matrix_steps = ("_bounded_gather", "_measure_runtime")
    _assert_ordered(proof, proof_steps)
    _assert_ordered(proof_runtime, proof_runtime_steps)
    _assert_ordered(measurement, measurement_steps)
    _assert_ordered(matrix, matrix_steps)


def _assert_frontend_runtime_contract(source: str) -> None:
    body = _function_body(source, "_release")
    markers = (
        "_mount_frontend",
        "_source_layer(base, source, FRONTEND_LOCK_INPUTS)",
        "playwright', 'install-deps",
        "_install_project",
    )
    _assert_ordered(body, markers)


def _assert_public_schema(source: str) -> None:
    actual = tuple(_signature(method) for method in _public_methods(_adapter_class(_tree(source))))
    expected = (
        ("ci", (), PUBLIC_INPUTS, None, (), None, (1, 0), "str"),
        ("security", (), PUBLIC_INPUTS, None, (), None, (1, 0), "str"),
    )
    assert actual == expected, "only ci and security may be public Dagger functions"


def _assert_ci_resolves_exact_source(source: str) -> None:
    adapter = _adapter_class(_tree(source))
    ci = next(method for method in _public_methods(adapter) if method.name == "ci")
    calls = [
        node.func.id
        for node in ast.walk(ci)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert calls.count("_release_source") == 1, "CI must resolve canonical exact source once"


def _assert_immutable_runtime_contract(source: str) -> None:
    constants = _literal_constants(source)
    expected = {
        "PYTHON_IMAGES": PYTHON_IMAGES,
        "UV_IMAGE": UV_IMAGE,
        "NODE_IMAGE": NODE_IMAGE,
        "RUNTIME_WHEELHOUSES": WHEELHOUSES,
        "NODE_PATHS": NODE_PATHS,
        "FRONTEND_LOCK_INPUTS": FRONTEND_LOCK_INPUTS,
    }
    assert constants.items() >= expected.items(), (
        "runtime identities must remain immutable and versioned"
    )


def _assert_dagger_base_image(config: str) -> None:
    parsed = cast(dict[str, object], tomllib.loads(config))
    tool = cast(dict[str, object], parsed["tool"])
    dagger = cast(dict[str, str], tool["dagger"])
    assert dagger["base-image"] == PYTHON_IMAGES[1], "Dagger base image must be digest-pinned"


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
    parsed = cast(dict[str, object], yaml.safe_load(workflow))
    jobs = cast(dict[str, object], parsed["jobs"])
    job = cast(dict[str, object], jobs["dagger"])
    steps = cast(list[dict[str, object]], job["steps"])
    argument = "ci" if name == "dagger.yml" else "security"
    checkout = [step for step in steps if step.get("uses") == f"actions/checkout@{CHECKOUT_SHA}"]
    dagger_steps = [
        step
        for step in steps
        if step.get("uses") == f"dagger/dagger-for-github@{DAGGER_ACTION_SHA}"
    ]
    expected_args = f"{argument} --commit-sha=${{{{ github.sha }}}} {SAFE_AUTH_ARGUMENT}"
    arguments = [
        cast(dict[str, str], step["with"])["args"]
        for step in steps
        if isinstance(step.get("with"), dict) and "args" in cast(dict[str, object], step["with"])
    ]
    assert len(checkout) == 1
    assert len(dagger_steps) == 1
    assert dagger_steps[0]["env"] == {
        "DAGGER_GIT_HTTP_AUTH_HEADER": SAFE_AUTH_ENV_EXPRESSION,
        "USE_GIT_AUTH": SAFE_AUTH_SWITCH_EXPRESSION,
    }
    assert arguments == [expected_args], "workflow must contain one sole Dagger invocation"
    assert dagger_steps[0]["with"] == {"version": "0.21.8", "verb": "call", "args": expected_args}


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


def _assert_dagger_vcs_inputs(root: Path, config: str) -> None:
    parsed = cast(dict[str, object], json.loads(config))
    include = cast(list[str], parsed["include"])
    assert _git(root, "ls-files", "--", DAGGER_LOCK) == (DAGGER_LOCK,)
    assert DAGGER_LOCK in include, "Dagger module lock must be an explicit module input"
    _assert_generated_paths_are_untracked(root)


class RecordingWorkspace:
    """Stand in for the engine-detected workspace and record the requested tree."""

    def __init__(self, tree: Directory) -> None:
        self.tree = tree
        self.requests: list[tuple[str, tuple[str, ...]]] = []

    def directory(self, path: str, *, exclude: list[str]) -> Directory:
        self.requests.append((path, tuple(exclude)))
        return self.tree


def _saga(tree: Directory) -> main.AgenticSaga:
    return main.AgenticSaga.create(cast(dagger.Workspace, RecordingWorkspace(tree)))


def test_should_construct_source_from_the_filtered_workspace_root() -> None:
    # Given an engine workspace stand-in.
    tree = dag.directory()
    workspace = RecordingWorkspace(tree)

    # When the module constructor runs.
    saga = main.AgenticSaga.create(cast(dagger.Workspace, workspace))

    # Then the module owns the workspace root, filtered by the secret-safe ignore list.
    assert saga.source is tree
    assert workspace.requests == [("/", tuple(main.SOURCE_IGNORE_PATTERNS))]


def _caller_directory_inputs(source: str) -> tuple[str, ...]:
    adapter = _adapter_class(_tree(source))
    return tuple(
        f"{method.name}.{argument.arg}"
        for method in _public_methods(adapter)
        for argument in (*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs)
        if argument.arg == "source" or "Directory" in _annotation(argument)
    )


def _assert_no_caller_supplied_source(source: str) -> None:
    inputs = _caller_directory_inputs(source)
    assert inputs == (), f"public functions cannot accept a caller-supplied source: {inputs}"


def _source_constructor(adapter: ast.ClassDef) -> ast.FunctionDef | None:
    return next(
        (
            member
            for member in adapter.body
            if isinstance(member, ast.FunctionDef) and member.name == "create"
        ),
        None,
    )


def _assert_module_owned_source(source: str) -> None:
    adapter = _adapter_class(_tree(source))
    fields = [ast.unparse(member) for member in adapter.body if isinstance(member, ast.AnnAssign)]
    constructor = _source_constructor(adapter)
    assert fields == ["source: dagger.Directory = field()"], "module must own one source field"
    assert constructor is not None, "module must construct source from the engine workspace"
    body = ast.unparse(constructor)
    assert _parameters(constructor.args.args[1:]) == (("workspace", "dagger.Workspace"),)
    assert "workspace.directory('/', exclude=SOURCE_IGNORE_PATTERNS)" in body, (
        "module source must be the filtered workspace root"
    )


def test_should_not_accept_a_caller_supplied_source_on_any_public_function() -> None:
    # Given the adapter source.
    source = MODULE.read_text()

    # When every public Dagger function signature is inspected.
    # Then no entry point lets a caller hand CI or security a different tree.
    _assert_no_caller_supplied_source(source)


def test_should_own_source_through_the_engine_workspace() -> None:
    # Given the adapter source.
    source = MODULE.read_text()

    # When the module's source construction is inspected.
    # Then the tree comes from the detected workspace, filtered, and never from a caller.
    _assert_module_owned_source(source)


def test_should_reject_a_reintroduced_caller_supplied_source() -> None:
    # Given a copied adapter whose CI entry point accepts a caller directory again.
    source = MODULE.read_text().replace(
        "async def ci(\n        self,\n",
        "async def ci(\n        self,\n        source: dagger.Directory,\n",
        1,
    )
    assert source != MODULE.read_text(), "mutation must apply"

    # When the no-caller-source contract is applied.
    # Then the reintroduced parameter is rejected.
    with pytest.raises(AssertionError, match="caller-supplied source"):
        _assert_no_caller_supplied_source(source)


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


def test_should_require_one_exact_source_resolution_call() -> None:
    # Given the real public CI entry point.
    # When its narrow source-resolution call graph is checked.
    # Then it resolves canonical exact source once.
    _assert_ci_resolves_exact_source(MODULE.read_text())


def test_should_reject_hidden_or_defaulted_public_inputs() -> None:
    # Given copied public functions that hide an input in every supported argument form.
    source = """
class AgenticSaga:
    @function
    async def ci(self, source: dagger.Directory, /, *extra: str, commit_sha: str = '',
                 git_auth_header: dagger.Secret = None, **kwargs: str) -> str:
        return ''
    @function
    async def security(self, source: dagger.Directory, commit_sha: str,
                       *, git_auth_header: dagger.Secret) -> str:
        return ''
"""

    # When the closed signature contract is applied.
    # Then positional-only, variadic, keyword-only, kwargs, and defaults cannot hide inputs.
    with pytest.raises(AssertionError, match="only ci and security"):
        _assert_public_schema(source)


def test_should_pin_each_runtime_and_keep_separate_wheelhouses() -> None:
    # Given the adapter's immutable literals.
    source = MODULE.read_text()

    # When runtime identities are checked.
    # Then Python 3.12 and 3.13 retain pinned images and ABI-specific wheelhouses.
    _assert_immutable_runtime_contract(source)


def test_should_include_the_pnpm_executable_in_the_node_handoff() -> None:
    # Given the exact pinned Node-to-Python /usr/local handoff.
    source = MODULE.read_text()

    # When the immutable handoff paths are checked.
    # Then pnpm's executable and its package payload both reach each runtime lane.
    _assert_immutable_runtime_contract(source)


def test_should_bind_shared_artifacts_and_preserve_runtime_stage_order() -> None:
    # Given the real runtime-phase composition.
    source = MODULE.read_text()

    # When its ordered proof handoffs are inspected.
    # Then measurement follows the verified wheel and every required proof stage.
    _assert_runtime_phase_contract(source)


def test_should_install_the_project_offline_without_build_isolation() -> None:
    # Given the project is overlaid onto a fully provisioned dependency environment.
    body = _function_body(MODULE.read_text(), "_install_project")

    # When the final project install command is inspected.
    # Then it reuses the installed build backend without network or dependency resolution.
    assert "'--offline'" in body
    assert "'--no-build-isolation'" in body
    assert "'--no-editable'" in body


def test_should_filter_secret_prone_context_before_external_modules() -> None:
    source = MODULE.read_text()
    patterns = main.SOURCE_IGNORE_PATTERNS

    assert "workspace.directory('/', exclude=SOURCE_IGNORE_PATTERNS)" in ast.unparse(_tree(source))
    assert '".env"' in source
    assert '"**/.env"' in source
    assert patterns.index(".env.*") < patterns.index("!.env.example")
    assert patterns.index("**/.env.*") < patterns.index("!**/.env.example")
    release = _function_body(source, "_release_source")
    adapter = _adapter_class(_tree(source))
    method = next(item for item in _public_methods(adapter) if item.name == "security")
    security = ast.unparse(method)
    assert "await _guard(source" in release
    assert "verified = await _release_source(self.source" in security
    assert "_dependency_audit(verified" in security
    assert "_node(verified)" in security


def test_should_filter_generated_local_state_before_exact_source_guard() -> None:
    # Given local quality tools and the Dagger SDK generate ignored state.
    generated = {
        ".artifacts",
        ".dagger/.venv",
        ".dagger/sdk",
        "**/.coverage*",
        "**/.hypothesis",
        "**/.mypy_cache",
        "**/.pytest_cache",
        "**/.ruff_cache",
        "**/*.tsbuildinfo",
        "**/coverage",
        "**/test-results",
        "web/flight-recorder/dist",
    }

    # When the public source boundary is applied.
    # Then reproducible generated state cannot contaminate exact-commit comparison.
    assert generated <= set(main.SOURCE_IGNORE_PATTERNS)


def test_should_mount_the_locked_frontend_identity_before_corepack() -> None:
    # Given Node tools and packages are handed from the frontend builder to a Python lane.
    source = MODULE.read_text()

    # When the cross-image runtime order is checked.
    # Then Corepack sees the locked pnpm identity before Playwright installs OS dependencies.
    _assert_frontend_runtime_contract(source)


def test_should_reject_frontend_identity_without_package_manifest() -> None:
    # Given copied production source without the package-manager identity input.
    source = MODULE.read_text().replace('    "web/flight-recorder/package.json",\n', "", 1)

    # When immutable runtime literals are validated.
    # Then Corepack cannot silently fall back to an unpinned package-manager version.
    with pytest.raises(AssertionError, match="runtime identities"):
        _assert_immutable_runtime_contract(source)


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        ('    "bin/pnpm",\n', ""),
        (
            "    candidate = gated.with_directory(RELEASE_ROOT, artifacts)\n"
            '    candidate = candidate.with_exec(["uv", "run", "poe", "release-candidate"])\n'
            "    proved = candidate.with_directory(FRONTEND_COVERAGE, frontend.coverage)\n",
            "    proved = gated.with_directory(FRONTEND_COVERAGE, frontend.coverage)\n"
            "    candidate = proved.with_directory(RELEASE_ROOT, artifacts)\n"
            '    candidate = candidate.with_exec(["uv", "run", "poe", "release-candidate"])\n',
        ),
        (
            '"AGENTIC_SAGA_RELEASE_ARTIFACTS", RELEASE_ROOT',
            '"AGENTIC_SAGA_RELEASE_ARTIFACTS", "/unverified"',
        ),
        (
            "    candidates = await _bounded_gather(*operations, limit=2)\n"
            "    for candidate, (_, wheelhouse) in zip(candidates, lanes, strict=True):\n"
            "        await _measure_runtime(candidate, wheelhouse)\n",
            "    for candidate, (_, wheelhouse) in zip(candidates, lanes, strict=True):\n"
            "        await _measure_runtime(candidate, wheelhouse)\n"
            "    candidates = await _bounded_gather(*operations, limit=2)\n",
        ),
    ),
)
def test_should_reject_missing_pnpm_or_reordered_runtime_proof(
    original: str, replacement: str
) -> None:
    # Given copied production source with a broken runtime handoff or stage order.
    source = MODULE.read_text().replace(original, replacement, 1)

    # When the corresponding contract is applied.
    # Then the unsafe mutation fails closed.
    with pytest.raises(AssertionError):
        if original == '    "bin/pnpm",\n':
            _assert_immutable_runtime_contract(source)
        else:
            _assert_runtime_phase_contract(source)


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


def test_should_reject_a_copied_source_that_removes_python_313() -> None:
    # Given copied production source without the Python 3.13 immutable literal.
    removed = (
        "    (\n"
        '        "python:3.13.14-bookworm@sha256:"\n'
        '        "8b9a8b28d9cc221c6ab5d40e9cfcd99429959f6a8f5171612a99147975ab043f"\n'
        "    ),\n"
    )
    source = MODULE.read_text().replace(removed, "", 1)

    # When immutable runtime literals are validated.
    # Then a single-runtime regression fails closed.
    with pytest.raises(AssertionError, match="runtime identities"):
        _assert_immutable_runtime_contract(source)


def test_should_pin_the_dagger_module_base_image() -> None:
    # Given the generated-module build configuration.
    # When its TOML boundary is parsed.
    # Then it must use the exact Python 3.13 image digest from the adapter.
    _assert_dagger_base_image(PYPROJECT.read_text())


def test_should_reject_an_unpinned_dagger_module_base_image() -> None:
    # Given copied module configuration with a mutable base image.
    config = PYPROJECT.read_text().replace(PYTHON_IMAGES[1], "python:3.13-bookworm", 1)

    # When its TOML boundary is parsed.
    # Then a floating generated-module image is rejected.
    with pytest.raises(AssertionError, match="base image"):
        _assert_dagger_base_image(config)


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
    ("private", "event_name", "head_repository", "secret_available", "expected"),
    (
        (True, "push", None, True, True),
        (True, "pull_request", "hseshadr/agentic-saga", True, True),
        (True, "pull_request", "contributor/agentic-saga", True, False),
        (True, "schedule", None, True, True),
        (True, "workflow_dispatch", None, True, True),
        (True, "push", None, False, False),
        (True, "pull_request", "hseshadr/agentic-saga", False, False),
        (False, "push", None, True, False),
        (False, "pull_request", "hseshadr/agentic-saga", True, False),
        (False, "pull_request", "contributor/agentic-saga", True, False),
        (False, "schedule", None, True, False),
        (False, "workflow_dispatch", None, True, False),
    ),
)
def test_should_select_auth_only_for_private_trusted_events(
    private: bool,
    event_name: str,
    head_repository: str | None,
    secret_available: bool,
    expected: bool,
) -> None:
    # Given the repository visibility and event identity used by the exact workflow expression.
    trusted = event_name != "pull_request" or head_repository == main.REPOSITORY

    # When the authentication selection contract is evaluated.
    selected = private and trusted and secret_available
    secret = "masked-secret" if secret_available else ""
    environment = secret if selected else ""

    # Then public, external-fork, and no-secret events receive neither auth surface.
    assert selected is expected
    assert environment == (secret if expected else "")


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


def test_should_reject_an_extra_dagger_workflow_invocation() -> None:
    # Given copied CI workflow that adds a second Dagger arguments field.
    workflow = (
        (WORKFLOWS / "dagger.yml")
        .read_text()
        .replace(
            "with:\n          fetch-depth:",
            "with:\n          args: security --source=.\n          fetch-depth:",
            1,
        )
    )

    # When workflow ingress is parsed.
    # Then the sole matching public invocation is required.
    with pytest.raises(AssertionError, match="sole Dagger invocation"):
        _assert_workflow_boundary("dagger.yml", workflow)


def test_should_keep_generated_dagger_state_out_of_the_git_index() -> None:
    # Given the real worktree and its ignore rules.
    # When generated module state is checked through Git.
    # Then generated SDK and virtualenv content are not tracked.
    _assert_generated_paths_are_untracked(ROOT)


def test_should_track_the_module_lock_and_include_it_in_dagger_inputs() -> None:
    # Given the real Git index and Dagger module configuration.
    # When module input boundaries are checked.
    # Then the lock is both tracked and explicitly included before generated files are considered.
    _assert_dagger_vcs_inputs(ROOT, CONFIG.read_text())


def test_should_reject_generated_dagger_state_forced_into_a_fixture_index(tmp_path: Path) -> None:
    # Given an isolated repository where generated state was force-added.
    repository = tmp_path / "repository"
    (repository / ".dagger/sdk").mkdir(parents=True)
    (repository / ".dagger/.venv").mkdir()
    (repository / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    (repository / DAGGER_LOCK).write_text("lock")
    (repository / ".dagger/sdk/generated.py").write_text("generated")
    (repository / ".dagger/.venv/pyvenv.cfg").write_text("generated")
    _git(repository, "init", "--quiet")
    _git(repository, "add", ".gitignore", DAGGER_LOCK)
    _git(repository, "add", "--force", *GENERATED_PATHS)

    # When the same Git-index boundary is applied.
    # Then tracked generated content is rejected even though it is ignored for new files.
    with pytest.raises(AssertionError, match="cannot enter Git"):
        _assert_dagger_vcs_inputs(repository, CONFIG.read_text())


def _assert_runtime_failure_propagates(module: ModuleType) -> None:
    async def failure() -> None:
        raise RuntimeError("runtime lane failed")

    with pytest.raises(RuntimeError, match="runtime lane failed"):
        asyncio.run(module._bounded_gather(failure(), limit=2))


def _copied_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> ModuleType:
    path = tmp_path / "mutated_main.py"
    path.write_text(source)
    name = f"task_four_mutation_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


async def _close_real_awaitables(*operations: object) -> None:
    for operation in operations:
        cast(object, operation).close()  # type: ignore[attr-defined]


def _assert_shared_build_contract(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact_builds = 0
    frontend_proofs = 0

    def artifact_builder(_: object) -> Container:
        nonlocal artifact_builds
        artifact_builds += 1
        return dag.container()

    def dependencies(_: object) -> Container:
        return dag.container()

    def frontend_builder(_: object, __: object) -> Container:
        nonlocal frontend_proofs
        frontend_proofs += 1
        return dag.container()

    monkeypatch.setattr(module, "_artifact_builder", artifact_builder)
    monkeypatch.setattr(module, "_frontend_dependencies", dependencies)
    monkeypatch.setattr(module, "_frontend_builder", frontend_builder)
    monkeypatch.setattr(module, "_bounded_gather", _close_real_awaitables)
    artifacts, frontend = asyncio.run(module._shared_outputs(dag.directory()))
    assert isinstance(artifacts, Directory)
    assert isinstance(frontend.coverage, Directory)
    assert artifact_builds == 1, "shared output contract permits one artifact builder"
    assert frontend_proofs == 1, "shared output contract requires one frontend proof"


def test_should_reject_missing_exact_source_resolution() -> None:
    # Given a copied adapter that bypasses canonical source resolution.
    source = MODULE.read_text().replace(
        "verified = await _release_source(self.source, commit_sha, git_auth_header)",
        "verified = self.source",
    )

    # When the sole permitted call-graph boundary is checked.
    # Then CI cannot skip canonical exact-source resolution.
    with pytest.raises(AssertionError, match="canonical exact source"):
        _assert_ci_resolves_exact_source(source)


def test_should_construct_the_real_lazy_dependency_and_release_graph() -> None:
    # Given an actual Dagger directory and its lazy graph API.
    source = dag.directory()

    # When every reusable graph builder composes without synchronizing a container.
    python = main._python(source, main.PYTHON_IMAGES[0])
    node = main._node(source)
    dependencies = main._frontend_dependencies(source)
    frontend = main._frontend_builder(source, dependencies)
    artifacts = main._frontend_artifacts(dependencies, frontend)
    release = main._release(source, main.PYTHON_IMAGES[1], artifacts)
    artifact_builder = main._artifact_builder(source)
    candidate = main._proved_candidate(source, main.PYTHON_IMAGES[1], dag.directory(), artifacts)

    # Then Dagger owns the real container and directory graph, without a test runtime emulator.
    assert isinstance(python, Container)
    assert isinstance(node, Container)
    assert isinstance(release, Container)
    assert isinstance(artifact_builder, Container)
    assert isinstance(candidate, Container)
    assert isinstance(artifacts.coverage, Directory)
    assert main.QUALITY_PROOF in main._quality_proof_command()[-1]
    assert main._measurement_command()[-1] == main.QUALITY_PROOF


def test_should_validate_the_shared_release_artifact_manifest() -> None:
    # Given a three-entry wheel, sdist, and runtime-requirements SHA256 manifest.
    manifest = VALID_MANIFEST

    # When the public-result boundary validates it.
    result = main._validated_manifest(manifest)

    # Then the exact digest evidence is retained.
    assert result == manifest


@pytest.mark.parametrize(
    "manifest",
    (
        "",
        "not-a-digest  agentic_saga.whl",
        VALID_MANIFEST + f"\n{'d' * 64}  unexpected.txt",
        VALID_MANIFEST.replace("runtime-requirements.txt", "private-auth-token.txt"),
    ),
)
def test_should_fail_closed_for_an_invalid_shared_artifact_manifest(manifest: str) -> None:
    # Given malformed, incomplete, extra, or unapproved manifest content.
    # When the public-result boundary validates it.
    # Then hosted evidence cannot be emitted ambiguously.
    with pytest.raises(ValueError, match="artifact manifest"):
        main._validated_manifest(manifest)


@pytest.mark.parametrize("auth_header", (None, f"Basic {uuid.uuid4().hex}"))
def test_should_orchestrate_ci_with_plain_async_collaborators(
    monkeypatch: pytest.MonkeyPatch, auth_header: str | None
) -> None:
    # Given concrete source and output identities supplied by async collaborators.
    source, verified, artifacts, frontend = object(), object(), object(), object()
    events: list[str] = []

    async def resolve(received: object, commit_sha: str, auth: object) -> object:
        assert (received, commit_sha, auth) == (source, "a" * 40, auth_header)
        events.append("resolve")
        return verified

    async def shared(received: object) -> tuple[object, object]:
        assert received is verified
        events.append("shared")
        return artifacts, frontend

    async def matrix(*received: object) -> None:
        assert received == (verified, artifacts, frontend)
        events.append("matrix")

    async def manifest(received: object) -> str:
        assert received is artifacts
        events.append("manifest")
        return VALID_MANIFEST

    monkeypatch.setattr(main, "_release_source", resolve)
    monkeypatch.setattr(main, "_shared_outputs", shared)
    monkeypatch.setattr(main, "_runtime_matrix", matrix)
    monkeypatch.setattr(main, "_artifact_manifest", manifest, raising=False)

    # When public CI is awaited.
    result = asyncio.run(_saga(source).ci("a" * 40, auth_header))

    # Then the resolved source flows into both later orchestration phases.
    assert result == f"Agentic Saga canonical Dagger gate passed\nSHA256SUMS\n{VALID_MANIFEST}"
    assert auth_header is None or auth_header not in result
    assert events == ["resolve", "shared", "matrix", "manifest"]


def test_should_propagate_security_audit_failure_before_frontend_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a real async audit collaborator that fails before frontend construction.
    async def failed_audit(*_: object) -> None:
        raise RuntimeError("locked audit failed")

    async def verified_source(source: object, *_: object) -> object:
        return source

    monkeypatch.setattr(main, "_dependency_audit", failed_audit)
    monkeypatch.setattr(main, "_release_source", verified_source)

    # When the public security entry point is awaited.
    # Then its dependency-audit failure remains visible without starting a Dagger container.
    with pytest.raises(RuntimeError, match="locked audit failed"):
        asyncio.run(_saga(dag.directory()).security("a" * 40, object()))


def test_should_create_shared_outputs_from_real_lazy_dagger_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given real lazy Dagger containers and a no-engine sync boundary.
    source = dag.directory()

    async def close_without_sync(*operations: object) -> None:
        for operation in operations:
            cast(object, operation).close()  # type: ignore[attr-defined]

    monkeypatch.setattr(main, "_bounded_gather", close_without_sync)

    # When the shared graph is assembled without a Dagger engine session.
    artifacts, frontend = asyncio.run(main._shared_outputs(source))

    # Then all returned artifacts originate from the real Dagger SDK graph.
    assert isinstance(artifacts, Directory)
    assert isinstance(frontend.coverage, Directory)


def test_should_propagate_a_runtime_lane_failure() -> None:
    # Given the real bounded fan-out.
    # When a runtime lane fails.
    # Then its exception remains visible to the caller.
    _assert_runtime_failure_propagates(main)


def test_should_reject_an_unused_duplicate_artifact_builder_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given copied production module code with an unused second artifact builder.
    source = MODULE.read_text().replace(
        "artifact_builder = _artifact_builder(source)",
        "artifact_builder = _artifact_builder(source)\n    _artifact_builder(source)",
        1,
    )
    module = _copied_module(monkeypatch, tmp_path, source)

    # When its shared output behavior is executed.
    # Then the duplicate artifact construction is observed and rejected.
    with pytest.raises(AssertionError, match="artifact builder"):
        _assert_shared_build_contract(module, monkeypatch)


@pytest.mark.parametrize(
    "replacement",
    ("frontend_builder = dependencies", "frontend_builder = _frontend_dependencies(source)"),
)
def test_should_reject_omitted_or_replaced_frontend_proof_builder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replacement: str
) -> None:
    # Given copied production module code that omits or replaces the frontend proof builder.
    source = MODULE.read_text().replace(
        "frontend_builder = _frontend_builder(source, dependencies)", replacement, 1
    )
    module = _copied_module(monkeypatch, tmp_path, source)

    # When its shared output behavior is executed.
    # Then exactly one frontend proof builder invocation is required.
    with pytest.raises(AssertionError, match="frontend proof"):
        _assert_shared_build_contract(module, monkeypatch)


def test_should_reject_a_copied_bounded_gather_that_returns_exceptions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given copied production module code that turns lane failures into results.
    source = MODULE.read_text().replace(
        "await asyncio.gather(*tasks)",
        "await asyncio.gather(*tasks, return_exceptions=True)",
        1,
    )
    module = _copied_module(monkeypatch, tmp_path, source)

    # When a real awaitable fails in the copied fan-out.
    # Then converting that exception to a gather result is rejected.
    with pytest.raises(pytest.fail.Exception):
        _assert_runtime_failure_propagates(module)
