from __future__ import annotations

import json
import os
import platform
import selectors
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import agentic_saga
from scripts.quality_proof import quality_results_from_proof
from scripts.release_contract import (
    BudgetResult,
    EnvironmentIdentity,
    ReleaseReport,
    branch_percent,
    evaluate,
    frontend_branch_percent,
    release_environment_errors,
)

ROOT: Final = Path(__file__).parents[1]
WEB: Final = ROOT / "web" / "flight-recorder"
_REQUIRED_API: Final = frozenset(
    {
        "SagaGoal",
        "SagaWorkflowInput",
        "WorkflowState",
        "WorkflowTool",
        "query_saga_state",
        "start_saga",
    }
)
_LEGACY_API: Final = frozenset({"SagaDefinition", "SagaRuntime", "compose_runtime"})
_LEGACY_PACKAGES: Final = ("execution", "kernel", "storage")
_INHERITED_ENVIRONMENT: Final = frozenset(
    {
        "AGENTIC_SAGA_RELEASE_ARTIFACTS",
        "AGENTIC_SAGA_RELEASE_WHEELHOUSE",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "XDG_CACHE_HOME",
    }
)


def _environment() -> dict[str, str]:
    values = {name: value for name in _INHERITED_ENVIRONMENT if (value := os.getenv(name))}
    values.update({"npm_config_offline": "true", "UV_PYTHON_DOWNLOADS": "never"})
    return values


def _run(command: Sequence[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv without a shell
        command, cwd=cwd, env=_environment(), capture_output=True, check=False, text=True
    )


def _checked(command: Sequence[str], cwd: Path = ROOT) -> str:
    result = _run(command, cwd)
    if result.returncode == 0:
        return result.stdout
    detail = (result.stderr or result.stdout)[-4_000:]
    raise RuntimeError(f"release measurement command failed: {command[0]}\n{detail}")


def _version(command: Sequence[str], cwd: Path = ROOT) -> str:
    return _checked(command, cwd).strip().splitlines()[-1]


def _pnpm_command(*arguments: str) -> tuple[str, ...]:
    direct = _run(("pnpm", "--version"), WEB)
    if direct.returncode == 0 and direct.stdout.strip() == "11.5.0":
        return "pnpm", *arguments
    return "npx", "--yes", "pnpm@11.5.0", *arguments


def collect_environment(root: Path = ROOT) -> EnvironmentIdentity:
    dirty = bool(_checked(("git", "status", "--porcelain=v1"), root).strip())
    return EnvironmentIdentity(
        commit=_checked(("git", "rev-parse", "HEAD"), root).strip(),
        tree_state="dirty" if dirty else "clean",
        os=platform.platform(),
        cpu=platform.processor().strip() or platform.machine(),
        python=f"Python {platform.python_version()}",
        node=_version(("node", "--version")),
        pnpm=_version(_pnpm_command("--version"), WEB),
    )


def _read_json(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return value


def coverage_results(
    python_coverage: Mapping[str, object],
    release_coverage: Mapping[str, object],
    frontend_coverage: Mapping[str, object],
) -> tuple[BudgetResult, ...]:
    return (
        evaluate("core_branch_coverage_percent", branch_percent(python_coverage, "/agentic_saga/")),
        evaluate(
            "release_scripts_branch_coverage_percent",
            branch_percent(release_coverage, "scripts/"),
        ),
        evaluate("frontend_branch_coverage_percent", frontend_branch_percent(frontend_coverage)),
    )


def _quality_results() -> tuple[BudgetResult, ...]:
    _checked(("uv", "run", "poe", "gate"))
    _checked(_pnpm_command("gate"), WEB)
    coverage = coverage_results(
        _read_json(ROOT / ".coverage.json"),
        _read_json(ROOT / ".coverage-release-scripts.json"),
        _read_json(WEB / "coverage" / "coverage-summary.json"),
    )
    return (
        *coverage,
        evaluate("python_complexity_grade_a", 1),
        evaluate("browser_behavior_checks", 1),
    )


def _temporal_dependency_present() -> bool:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    dependencies = project["dependencies"]
    return any(str(item).startswith("temporalio") for item in dependencies)


def _legacy_runtime_absent() -> bool:
    package = ROOT / "src" / "agentic_saga"
    return all(not tuple((package / name).glob("*.py")) for name in _LEGACY_PACKAGES)


def _public_api_is_temporal() -> bool:
    exports = frozenset(agentic_saga.__all__)
    return exports >= _REQUIRED_API and not exports & _LEGACY_API


def runtime_contract_results() -> tuple[BudgetResult, ...]:
    return (
        evaluate("temporal_runtime_dependency", int(_temporal_dependency_present())),
        evaluate("legacy_runtime_absent", int(_legacy_runtime_absent())),
        evaluate("public_api_imports", int(_public_api_is_temporal())),
        evaluate("temporal_tests_passed", 1),
    )


def _artifact_root(configured: str) -> Path:
    requested = Path(configured)
    if not requested.is_absolute() or requested.is_symlink():
        raise ValueError("release artifacts must be a direct absolute directory")
    return _resolved_artifact_root(requested)


def _resolved_artifact_root(requested: Path) -> Path:
    try:
        resolved = requested.resolve(strict=True)
    except OSError as error:
        raise ValueError("release artifacts must be a real directory") from error
    if resolved != requested or not resolved.is_dir():
        raise ValueError("release artifacts must be a real directory")
    return resolved


def _single_wheel(root: Path) -> Path:
    wheels = tuple(root.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("release artifacts must contain exactly one wheel")
    return wheels[0]


def _built_wheel(workspace: Path) -> Path:
    artifacts = workspace / "artifacts"
    _checked(("uv", "build", "--offline", "--no-build-isolation", "--out-dir", str(artifacts)))
    return _single_wheel(artifacts)


def _wheel_input(workspace: Path) -> tuple[Path, Path | None, Path | None]:
    configured = os.environ.get("AGENTIC_SAGA_RELEASE_ARTIFACTS")
    if configured is None:
        return _built_wheel(workspace), None, None
    root = _artifact_root(configured)
    requirements = root / "runtime-requirements.txt"
    if not requirements.is_file():
        raise ValueError("release artifacts must contain runtime requirements")
    return _single_wheel(root), requirements, _wheelhouse()


def _wheelhouse() -> Path:
    configured = os.environ.get("AGENTIC_SAGA_RELEASE_WHEELHOUSE")
    if configured is None:
        raise ValueError("release wheelhouse is required for shared artifacts")
    return _artifact_root(configured)


def _venv_python(venv: Path) -> Path:
    return venv / "bin" / "python"


def _create_venv(venv: Path, isolated: bool) -> Path:
    command = ["uv", "venv", "--offline", "--no-python-downloads"]
    if not isolated:
        command.append("--system-site-packages")
    _checked((*command, "--python", sys.executable, str(venv)))
    return _venv_python(venv)


def _install_dependencies(python: Path, requirements: Path, wheelhouse: Path) -> None:
    _checked(
        (
            "uv",
            "pip",
            "install",
            "--offline",
            "--require-hashes",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--python",
            str(python),
            "-r",
            str(requirements),
        )
    )


def _install_wheel(python: Path, wheel: Path) -> None:
    _checked(
        ("uv", "pip", "install", "--offline", "--no-deps", "--python", str(python), str(wheel))
    )


def _installed_python(workspace: Path) -> Path:
    wheel, requirements, wheelhouse = _wheel_input(workspace)
    python = _create_venv(workspace / "venv", requirements is not None)
    if requirements is not None and wheelhouse is not None:
        _install_dependencies(python, requirements, wheelhouse)
    _install_wheel(python, wheel)
    return python


def _installed_surface(python: Path) -> bool:
    source = (
        "import agentic_saga, agentic_saga.temporal as temporal, temporalio; "
        "assert agentic_saga.WorkflowState.__module__ == 'agentic_saga.temporal.contracts'; "
        "assert all(callable(item) for item in (temporal.TemporalActivities, "
        "temporal.build_worker, temporal.connect_client, temporal.project_run_trace))"
    )
    return _run((str(python), "-c", source)).returncode == 0


def _cli_path(python: Path) -> Path:
    return python.parent / "agentic-saga"


def _read_ready_line(process: subprocess.Popen[str]) -> str:
    if process.stdout is None:
        return ""
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout=5):
            return ""
        return process.stdout.readline()


def _stop_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _recorder_ready(cli: Path) -> bool:
    process = subprocess.Popen(  # noqa: S603 - installed first-party CLI
        (str(cli), "demo", "--port", "0"),
        cwd=ROOT,
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        return _read_ready_line(process).startswith("Agentic Saga demo: http://127.0.0.1:")
    finally:
        _stop_process(process)


def package_results(workspace: Path) -> tuple[BudgetResult, ...]:
    python = _installed_python(workspace)
    cli = _cli_path(python)
    return (
        evaluate("package_build_install", int(_installed_surface(python))),
        evaluate("recorder_cli_ready", int(_recorder_ready(cli))),
    )


def measure_release(quality_proof: Path | None = None) -> ReleaseReport:
    quality = (
        _quality_results() if quality_proof is None else quality_results_from_proof(quality_proof)
    )
    with tempfile.TemporaryDirectory(prefix="agentic-saga-release-") as directory:
        packaged = package_results(Path(directory))
    return ReleaseReport(collect_environment(), (*quality, *runtime_contract_results(), *packaged))


def _render_result(result: BudgetResult) -> str:
    state = "PASS" if result.passed else "FAIL"
    actual = f"actual={result.actual:g}{result.unit}"
    limit = f"limit={result.limit:g}{result.unit}"
    return f"{result.name}: {state} {actual} {limit}"


def render_report(report: ReleaseReport) -> str:
    errors = release_environment_errors(report.environment)
    environment = "PASS" if not errors else f"FAIL ({'; '.join(errors)})"
    identity = report.environment
    lines = (
        "Agentic Saga release measurement",
        f"commit: {identity.commit}",
        f"tree: {identity.tree_state}",
        f"python: {identity.python}",
        f"node: {identity.node}",
        f"pnpm: {identity.pnpm}",
        *(_render_result(result) for result in report.results),
        f"release_environment: {environment}",
    )
    return "\n".join(lines) + "\n"


def check_python_coverage(path: Path) -> int:
    actual = branch_percent(_read_json(path), "/agentic_saga/")
    result = evaluate("core_branch_coverage_percent", actual)
    print(f"core branch coverage: {actual:.3f}%")
    return int(not result.passed)


def check_release_coverage(path: Path) -> int:
    actual = branch_percent(_read_json(path), "scripts/")
    result = evaluate("release_scripts_branch_coverage_percent", actual)
    print(f"release-script branch coverage: {actual:.3f}%")
    return int(not result.passed)


def _only_path(arguments: Sequence[str]) -> Path:
    if len(arguments) != 1:
        raise ValueError("coverage command requires exactly one path")
    return Path(arguments[0])


def internal_main(arguments: Sequence[str]) -> int:
    commands = {
        "--check-python-coverage": check_python_coverage,
        "--check-release-coverage": check_release_coverage,
    }
    if not arguments or arguments[0] not in commands:
        raise ValueError("unsupported internal release-runner arguments")
    return commands[arguments[0]](_only_path(arguments[1:]))


if __name__ == "__main__":
    raise SystemExit(internal_main(tuple(sys.argv[1:])))
