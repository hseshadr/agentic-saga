from __future__ import annotations

import asyncio
import gzip
import json
import os
import platform
import re
import resource
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from statistics import median
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast
from urllib.request import urlopen

from pydantic import SecretStr
from ruamel.yaml import YAML

import agentic_saga.manifest as manifest_module
from agentic_saga.agents import OpenRouterSettings
from agentic_saga.agents import deepagents as deepagents_module
from agentic_saga.contracts.trace import RunTrace
from agentic_saga.demo import assets as assets_module
from agentic_saga.demo import server as server_module
from agentic_saga.demo.assets import materialize_recorder_site
from agentic_saga.demo.reference import REFERENCE_SCENARIOS, load_reference_trace
from agentic_saga.demo.server import serve_recorder
from agentic_saga.evidence import run_trace as trace_module
from agentic_saga.storage import SQLiteKernelStore
from agentic_saga.storage import sqlite as sqlite_module
from examples.ecommerce.demo import run_scenario
from scripts.quality_proof import quality_results_from_proof
from scripts.release_contract import (
    BUDGETS,
    BudgetResult,
    EnvironmentIdentity,
    ReleaseReport,
    branch_percent,
    evaluate,
    frontend_branch_percent,
    release_environment_errors,
)

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

ROOT: Final[Path] = Path(__file__).parents[1]
WEB: Final[Path] = ROOT / "web" / "flight-recorder"
TRACES: Final[Path] = ROOT / "examples" / "ecommerce" / "flight-recorder" / "traces"
STATIC: Final[Path] = ROOT / "src" / "agentic_saga" / "demo" / "static"
_PROCESS_GRACE_SECONDS: Final[float] = 2.0
_MEMORY_WORKLOAD: Final[str] = """
import asyncio
import resource
import tempfile
from pathlib import Path
from agentic_saga.demo.assets import materialize_recorder_site
from agentic_saga.demo.reference import REFERENCE_SCENARIOS
from examples.ecommerce.demo import run_scenario

async def workload():
    traces = {}
    for scenario in REFERENCE_SCENARIOS:
        with tempfile.TemporaryDirectory(prefix="agentic-saga-memory-run-") as directory:
            traces[scenario] = (await run_scenario(scenario, Path(directory))).trace
    with tempfile.TemporaryDirectory(prefix="agentic-saga-memory-site-") as directory:
        materialize_recorder_site(Path(directory).resolve() / "site", traces)

asyncio.run(workload())
print(f"WORKLOAD_RSS={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}")
"""


class _Closeable(Protocol):
    def close(self) -> None: ...


class _Readable(Protocol):
    def fileno(self) -> int: ...
    def readline(self) -> str: ...


class ManagedProcess(Protocol):
    @property
    def stdout(self) -> _Closeable | None: ...

    @property
    def stderr(self) -> _Closeable | None: ...

    def poll(self) -> int | None: ...
    def send_signal(self, signal_number: int) -> None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float) -> int: ...
    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]: ...


def _environment() -> dict[str, str]:
    values = dict(os.environ)
    values.pop("OPENROUTER_API_KEY", None)
    values.pop("PYTHONPATH", None)
    values["npm_config_offline"] = "true"
    values["UV_OFFLINE"] = "1"
    values["UV_PYTHON_DOWNLOADS"] = "never"
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
        _checked(("git", "rev-parse", "HEAD"), root).strip(),
        "dirty" if dirty else "clean",
        platform.platform(),
        platform.processor().strip() or platform.machine(),
        f"Python {platform.python_version()}",
        _version(("node", "--version")),
        _version(_pnpm_command("--version"), WEB),
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
        evaluate("core_branch_coverage_percent", branch_percent(python_coverage, "/kernel/")),
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
        _frontend_coverage(),
    )
    executed = (
        evaluate("python_complexity_grade_a", 1),
        evaluate("browser_behavior_checks", 1),
    )
    return (*coverage, *executed)


def _frontend_coverage() -> Mapping[str, object]:
    return _read_json(WEB / "coverage" / "coverage-summary.json")


def _shape(value: object, depth: int = 1) -> tuple[int, int]:
    children = _children(value)
    if children is None:
        return depth, 1
    shapes = tuple(_shape(child, depth + 1) for child in children)
    return max((item[0] for item in shapes), default=depth), 1 + sum(item[1] for item in shapes)


def _children(value: object) -> tuple[object, ...] | None:
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, list):
        return tuple(value)
    return None


def _manifest_document() -> tuple[bytes, Mapping[str, object]]:
    raw = (ROOT / "examples" / "ecommerce" / "saga.yaml").read_bytes()
    parsed = YAML(typ="safe").load(raw)
    if not isinstance(parsed, Mapping):
        raise ValueError("reference manifest must be a mapping")
    return raw, parsed


def _manifest_results() -> tuple[BudgetResult, ...]:
    raw, parsed = _manifest_document()
    depth, nodes = _shape(parsed)
    budgets = parsed.get("budgets")
    if not isinstance(budgets, Mapping):
        raise ValueError("reference manifest has no budgets")
    return (
        *_manifest_shape_results(raw, depth, nodes),
        *_execution_results(budgets),
        effective_model_timeout_result(budgets),
    )


def _manifest_shape_results(raw: bytes, depth: int, nodes: int) -> tuple[BudgetResult, ...]:
    return (
        evaluate("manifest_bytes", len(raw)),
        evaluate("manifest_depth", depth),
        evaluate("manifest_nodes", nodes),
    )


def _execution_results(budgets: Mapping[str, object]) -> tuple[BudgetResult, ...]:
    names = (
        ("execution_turn_limit", "turn_limit"),
        ("execution_tool_call_limit", "tool_call_limit"),
        ("execution_elapsed_ms_limit", "elapsed_ms_limit"),
        ("execution_token_limit", "token_limit"),
    )
    return tuple(evaluate(result, _integer(budgets[field], field)) for result, field in names)


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return value


def _positive_integer(value: object, name: str) -> int:
    integer = _integer(value, name)
    if integer <= 0:
        raise ValueError(f"{name} must be positive")
    return integer


def effective_model_timeout_result(budgets: Mapping[str, object]) -> BudgetResult:
    turns = _positive_integer(budgets.get("turn_limit"), "turn_limit")
    elapsed = _positive_integer(budgets.get("elapsed_ms_limit"), "elapsed_ms_limit")
    per_turn = elapsed // turns
    configured = OpenRouterSettings(api_key=SecretStr("offline-measurement")).timeout_ms
    return evaluate("effective_model_timeout_ms", min(configured, per_turn))


def _field_bounds(model: type[object], field_name: str) -> tuple[int, int]:
    field = model.model_fields[field_name]  # type: ignore[attr-defined]
    lower = next((item.ge for item in field.metadata if hasattr(item, "ge")), None)
    upper = next((item.le for item in field.metadata if hasattr(item, "le")), None)
    return _integer_bounds(lower, upper, field_name)


def _integer_bounds(lower: object, upper: object, field_name: str) -> tuple[int, int]:
    if isinstance(lower, int) and isinstance(upper, int):
        return lower, upper
    raise RuntimeError(f"{field_name} is missing integer schema bounds")


def _schema_results(
    model: type[object], fields: Sequence[tuple[str, str]]
) -> tuple[BudgetResult, ...]:
    results: list[BudgetResult] = []
    for prefix, field in fields:
        lower, upper = _field_bounds(model, field)
        results.extend((evaluate(f"{prefix}_minimum", lower), evaluate(f"{prefix}_maximum", upper)))
    return tuple(results)


def _lease_check(value: timedelta, expected_error: bool) -> int:
    try:
        sqlite_module._require_lease_duration(value)
    except ValueError:
        return int(expected_error)
    return int(not expected_error)


def _lease_results() -> tuple[BudgetResult, ...]:
    return (
        evaluate("lease_max_duration_seconds", sqlite_module._MAX_LEASE_DURATION.total_seconds()),
        evaluate("lease_accepts_positive", _lease_check(timedelta(milliseconds=1), False)),
        evaluate("lease_rejects_nonpositive", _lease_check(timedelta(0), True)),
        evaluate("lease_rejects_over_max", _lease_check(timedelta(days=1, milliseconds=1), True)),
    )


def implementation_limit_results() -> tuple[BudgetResult, ...]:
    execution = (
        ("execution_turn_schema", "turn_limit"),
        ("execution_tool_call_schema", "tool_call_limit"),
        ("execution_elapsed_ms_schema", "elapsed_ms_limit"),
        ("execution_token_schema", "token_limit"),
    )
    return (
        *_fixed_limit_results(),
        *_schema_results(manifest_module._Budgets, execution),
        *_model_schema_results(),
        *_lease_results(),
    )


def _fixed_limit_results() -> tuple[BudgetResult, ...]:
    return (
        evaluate("manifest_parser_max_bytes", manifest_module._MAX_SOURCE_BYTES),
        evaluate("manifest_parser_max_depth", manifest_module._MAX_DOCUMENT_DEPTH),
        evaluate("manifest_parser_max_nodes", manifest_module._MAX_DOCUMENT_NODES),
        evaluate("materializer_max_static_files", assets_module._MAX_STATIC_FILES),
        evaluate("materializer_max_static_bytes", assets_module._MAX_STATIC_BYTES),
    )


def _model_schema_results() -> tuple[BudgetResult, ...]:
    lower, upper = _field_bounds(OpenRouterSettings, "timeout_ms")
    return (
        evaluate("model_timeout_schema_minimum_ms", lower),
        evaluate("model_timeout_schema_maximum_ms", upper),
    )


def _tree_size(root: Path) -> tuple[int, int]:
    files = tuple(path for path in root.rglob("*") if path.is_file())
    return len(files), sum(path.stat().st_size for path in files)


def _gzip_size(suffix: str) -> int:
    files = STATIC.rglob(f"*{suffix}")
    return sum(len(gzip.compress(path.read_bytes(), mtime=0)) for path in files)


def _asset_results() -> tuple[BudgetResult, ...]:
    catalog_files, catalog_bytes = _tree_size(TRACES)
    static_files, static_bytes = _tree_size(STATIC)
    return (
        evaluate("recorder_javascript_gzip_bytes", _gzip_size(".js")),
        evaluate("recorder_css_gzip_bytes", _gzip_size(".css")),
        evaluate("reference_trace_count", catalog_files - 1),
        evaluate("reference_catalog_bytes", catalog_bytes),
        evaluate("materializer_static_files", static_files),
        evaluate("materializer_static_bytes", static_bytes),
    )


def _boundary_results() -> tuple[BudgetResult, ...]:
    settings = OpenRouterSettings(api_key=SecretStr("offline-measurement"))
    return (
        evaluate("optional_model_calls", _configured_model_call_limit()),
        evaluate("agent_graph_steps", deepagents_module._RECURSION_LIMIT),
        evaluate("sdk_retries", settings.sdk_retries),
        evaluate("model_timeout_ms", settings.timeout_ms),
        evaluate("import_runs", assets_module._MAX_RUNS),
        evaluate("import_trace_bytes", trace_module._MAX_TRACE_BYTES),
        evaluate("import_index_bytes", assets_module._MAX_INDEX_BYTES),
        evaluate("import_json_depth", trace_module._MAX_DEPTH),
        evaluate("import_json_nodes", trace_module._MAX_NODES),
        *_server_boundary_results(),
    )


def _configured_model_call_limit() -> int:
    observed: list[int] = []
    model = cast("BaseChatModel", object())
    deepagents_module._create_graph(_probe_dependencies(observed), model, "measurement")
    if len(observed) != 1:
        raise RuntimeError("agent graph did not configure exactly one model-call limit")
    return observed[0]


def _probe_dependencies(observed: list[int]) -> deepagents_module._DeepAgentDependencies:
    def limit(*, run_limit: int, exit_behavior: Literal["error"]) -> object:
        del exit_behavior
        observed.append(run_limit)
        return object()

    value = SimpleNamespace(
        model_call_limit=limit,
        create_agent=lambda *arguments, **keywords: object(),
        tool_strategy=lambda schema: schema,
    )
    return cast(deepagents_module._DeepAgentDependencies, value)


def _server_boundary_results() -> tuple[BudgetResult, ...]:
    return (
        evaluate("request_path_chars", server_module._MAX_PATH),
        evaluate("request_headers", server_module._MAX_HEADERS),
        evaluate("request_header_bytes", server_module._MAX_HEADER_BYTES),
        evaluate("served_file_bytes", server_module._MAX_FILE_BYTES),
    )


def _sqlite_result() -> BudgetResult:
    with tempfile.TemporaryDirectory(prefix="agentic-saga-measure-sqlite-") as directory:
        store = SQLiteKernelStore.initialize(Path(directory) / "measure.db")
        value = store.connection_settings().busy_timeout_ms
    return evaluate("sqlite_busy_timeout_ms", value)


def measure_samples(count: int, operation: Callable[[], object]) -> tuple[float, ...]:
    samples: list[float] = []
    for _ in range(count):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1_000)
    return tuple(samples)


def _percentile(samples: Sequence[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999_999) - 1))
    return ordered[index]


def timing_result(name: str, samples: Sequence[float]) -> BudgetResult:
    p95 = _percentile(samples, 0.95)
    spec = BUDGETS[name]
    if len(samples) != spec.samples:
        raise ValueError(f"{name} requires exactly {spec.samples} samples")
    return evaluate(name, p95, statistics=(median(samples), max(samples)))


def _scenario_result(scenario: str) -> BudgetResult:
    name = f"offline_{scenario}_p95_ms"

    def operation() -> object:
        with tempfile.TemporaryDirectory(prefix="agentic-saga-measure-run-") as directory:
            return asyncio.run(run_scenario(scenario, Path(directory)))

    return timing_result(name, measure_samples(BUDGETS[name].samples, operation))


def _scenario_results() -> tuple[BudgetResult, ...]:
    return tuple(_scenario_result(scenario) for scenario in REFERENCE_SCENARIOS)


def _reference_traces() -> dict[str, RunTrace]:
    return {scenario: load_reference_trace(scenario) for scenario in REFERENCE_SCENARIOS}


def _materialize_once(traces: Mapping[str, RunTrace]) -> None:
    with tempfile.TemporaryDirectory(prefix="agentic-saga-measure-materialize-") as directory:
        materialize_recorder_site(Path(directory).resolve(strict=True) / "site", traces)


def _materialization_result() -> BudgetResult:
    traces = _reference_traces()
    samples = measure_samples(25, lambda: _materialize_once(traces))
    return timing_result("materialization_ms", samples)


def _server_samples() -> tuple[float, ...]:
    with tempfile.TemporaryDirectory(prefix="agentic-saga-measure-server-") as directory:
        destination = Path(directory).resolve(strict=True) / "site"
        materialize_recorder_site(destination, _reference_traces())
        with serve_recorder(destination) as server:
            urlopen(f"{server.url}/", timeout=2).read()  # noqa: S310 - loopback only
            return measure_samples(100, lambda: urlopen(f"{server.url}/", timeout=2).read())  # noqa: S310


def _read_ready(process: subprocess.Popen[str]) -> str:
    if process.stdout is None:
        raise RuntimeError("packaged recorder stdout is unavailable")
    line = _ready_line(process.stdout)
    match = re.fullmatch(r"Agentic Saga recorder: (http://127\.0\.0\.1:\d+)", line)
    if match is None:
        raise RuntimeError("packaged recorder did not emit a valid ready URL")
    return match.group(1)


def _ready_line(stream: _Readable) -> str:
    selector = selectors.DefaultSelector()
    try:
        selector.register(stream, selectors.EVENT_READ)
        ready = selector.select(10)
        return stream.readline().strip() if ready else ""
    finally:
        selector.close()


def _launch_demo(cli: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - wheel-installed executable and fixed argv
        (str(cli), "demo", "--scenario", "happy-path", "--port", "0"),
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_process(process: ManagedProcess, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _close_pipe(pipe: _Closeable | None) -> None:
    if pipe is not None:
        pipe.close()


def stop_process(process: ManagedProcess, grace_seconds: float = _PROCESS_GRACE_SECONDS) -> None:
    try:
        if process.poll() is None:
            _escalate_stop(process, grace_seconds)
    finally:
        _close_pipe(process.stdout)
        _close_pipe(process.stderr)


def _escalate_stop(process: ManagedProcess, grace_seconds: float) -> None:
    process.send_signal(signal.SIGINT)
    if _wait_process(process, grace_seconds):
        return
    process.terminate()
    if _wait_process(process, grace_seconds):
        return
    process.kill()
    if not _wait_process(process, grace_seconds):
        raise RuntimeError("child process could not be reaped")


def _demo_start_result(cli: Path) -> BudgetResult:
    samples: list[float] = []
    for _ in range(10):
        started = time.perf_counter()
        process = _launch_demo(cli)
        try:
            _read_ready(process)
            samples.append((time.perf_counter() - started) * 1_000)
        finally:
            stop_process(process)
    return timing_result("packaged_demo_start_ms", samples)


def _browser_result(cli: Path) -> BudgetResult:
    with tempfile.TemporaryDirectory(prefix="agentic-saga-browser-metrics-") as directory:
        output = Path(directory) / "measurements.json"
        result = _run_packaged_browser(cli, output)
        payload = _read_json(output) if output.is_file() else {}
    if result.returncode != 0:
        raise RuntimeError(f"packaged browser measurement failed\n{result.stderr[-4_000:]}")
    return timing_result("browser_fresh_navigation_ms", _browser_samples(payload))


def _browser_samples(payload: Mapping[str, object]) -> tuple[float, ...]:
    values = payload.get("samples_ms")
    if not isinstance(values, list) or not all(isinstance(item, int | float) for item in values):
        raise ValueError("packaged browser measurement has invalid samples")
    return tuple(float(item) for item in values)


def _run_packaged_browser(cli: Path, output: Path) -> subprocess.CompletedProcess[str]:
    environment = _environment() | {
        "AGENTIC_SAGA_BROWSER_METRICS": str(output),
        "AGENTIC_SAGA_CLI": str(cli),
    }
    return subprocess.run(  # noqa: S603 - pinned package script without a shell
        _pnpm_command("test:e2e:packaged"),
        cwd=WEB,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=300,
    )


def _close_capture(process: ManagedProcess) -> None:
    _close_pipe(process.stdout)
    _close_pipe(process.stderr)


def _communicate(process: ManagedProcess, timeout: float) -> tuple[str, str] | None:
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def bounded_output(process: ManagedProcess, grace_seconds: float = _PROCESS_GRACE_SECONDS) -> str:
    try:
        return _bounded_output(process, grace_seconds)
    finally:
        _close_capture(process)


def _bounded_output(process: ManagedProcess, grace_seconds: float) -> str:
    output = _communicate(process, grace_seconds)
    actions: tuple[Callable[[], None], ...] = (
        lambda: process.send_signal(signal.SIGINT),
        process.terminate,
        process.kill,
    )
    for action in actions:
        if output is not None:
            return output[0]
        action()
        output = _communicate(process, grace_seconds)
    if output is None:
        raise RuntimeError("measurement child could not be reaped")
    return output[0]


def _rss_bytes(raw: int, system: str) -> int:
    if system == "Darwin":
        return raw
    if system == "Linux":
        return raw * 1024
    raise ValueError("rejection child reported unsupported platform")


def rejection_results_from_output(output: str) -> tuple[BudgetResult, BudgetResult]:
    try:
        payload = json.loads(output)
        if not isinstance(payload, dict) or set(payload) != {"elapsed_ms", "rss_raw", "platform"}:
            raise ValueError
        elapsed = float(payload["elapsed_ms"])
        raw = _integer(payload["rss_raw"], "rss_raw")
        measured = _rss_bytes(raw, str(payload["platform"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("rejection child emitted invalid metrics") from error
    return (
        evaluate("maximum_input_rejection_ms", elapsed),
        evaluate("maximum_input_rejection_rss_bytes", measured),
    )


def _rejection_results() -> tuple[BudgetResult, BudgetResult]:
    process = subprocess.Popen(
        (sys.executable, "-m", "scripts.release_runner", "--rejection-child"),
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return rejection_results_from_output(bounded_output(process, 10))


def _rejection_child() -> int:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="agentic-saga-reject-child-") as directory:
        _reject_oversized_trace(Path(directory))
    payload = {
        "elapsed_ms": (time.perf_counter() - started) * 1_000,
        "rss_raw": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "platform": platform.system(),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _reject_oversized_trace(directory: Path) -> None:
    trace = directory / "oversized.json"
    with trace.open("wb") as stream:
        stream.truncate(assets_module._MAX_TRACE_BYTES + 1)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _read_expected_rejection(descriptor, trace.name)
    finally:
        os.close(descriptor)


def _read_expected_rejection(descriptor: int, name: str) -> None:
    try:
        assets_module._read_file(descriptor, name, assets_module._MAX_TRACE_BYTES)
    except assets_module.MaterializationError:
        return
    raise RuntimeError("over-limit recorder trace was accepted")


def _representative_memory_result() -> BudgetResult:
    output = _checked((sys.executable, "-c", _MEMORY_WORKLOAD))
    match = re.search(r"^WORKLOAD_RSS=(\d+)$", output, re.MULTILINE)
    if match is None:
        raise RuntimeError("representative workload did not report peak RSS")
    actual = _rss_bytes(int(match.group(1)), platform.system())
    return evaluate("representative_peak_rss_bytes", actual)


def _wheel_cli(workspace: Path) -> Path:
    configured = os.environ.get("AGENTIC_SAGA_CLI")
    if configured:
        return Path(configured).resolve(strict=True)
    shared = _shared_release_inputs()
    wheel, requirements = shared or _built_release_inputs(workspace)
    venv = workspace / "venv"
    _checked(("uv", "venv", "--offline", "--no-python-downloads", str(venv)))
    _install_wheel(venv, requirements, wheel, _release_wheelhouse())
    return venv / "bin" / "agentic-saga"


def _built_release_inputs(workspace: Path) -> tuple[Path, Path]:
    artifacts = workspace / "wheel"
    artifacts.mkdir()
    _checked(_wheel_build_command(artifacts))
    wheel = next(artifacts.glob("*.whl"))
    requirements = artifacts / "runtime-requirements.txt"
    _checked(_requirements_command(requirements))
    return wheel, requirements


def _shared_release_inputs() -> tuple[Path, Path] | None:
    configured = os.environ.get("AGENTIC_SAGA_RELEASE_ARTIFACTS")
    if configured is None:
        return None
    root = _shared_release_root(configured)
    return _shared_release_wheel(root), _shared_runtime_requirements(root)


def _shared_release_root(configured: str) -> Path:
    requested = Path(configured)
    if not configured or requested.is_symlink():
        raise ValueError("release artifacts must be a real directory")
    try:
        root = requested.resolve(strict=True)
    except OSError as error:
        raise ValueError("release artifacts must be a real directory") from error
    if not root.is_dir():
        raise ValueError("release artifacts must be a real directory")
    return root


def _shared_release_wheel(root: Path) -> Path:
    wheels = tuple(root.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].is_symlink() or not wheels[0].is_file():
        raise ValueError("release artifacts must contain exactly one wheel")
    return wheels[0].resolve(strict=True)


def _shared_runtime_requirements(root: Path) -> Path:
    requirements = root / "runtime-requirements.txt"
    if requirements.is_symlink() or not requirements.is_file():
        raise ValueError("release artifacts must contain runtime requirements")
    return requirements.resolve(strict=True)


def _wheel_build_command(artifacts: Path) -> tuple[str, ...]:
    return (
        "uv",
        "build",
        "--offline",
        "--no-python-downloads",
        "--no-build-isolation",
        "--wheel",
        "--out-dir",
        str(artifacts),
    )


def _requirements_command(output: Path) -> tuple[str, ...]:
    return (
        "uv",
        "export",
        "--frozen",
        "--offline",
        "--no-python-downloads",
        "--no-dev",
        "--no-emit-project",
        "--format",
        "requirements.txt",
        "-o",
        str(output),
    )


def _release_wheelhouse() -> Path | None:
    configured = os.environ.get("AGENTIC_SAGA_RELEASE_WHEELHOUSE")
    if configured is None:
        return None
    wheelhouse = Path(configured).resolve(strict=True)
    if not wheelhouse.is_dir():
        raise ValueError("release wheelhouse must be a directory")
    return wheelhouse


def _install_prefix(venv: Path, wheelhouse: Path | None) -> tuple[str, ...]:
    python = str(venv / "bin" / "python")
    common = ("uv", "pip", "install", "--offline", "--no-python-downloads", "--python", python)
    if wheelhouse is None:
        return common
    return (*common, "--no-index", "--find-links", str(wheelhouse))


def _install_wheel(venv: Path, requirements: Path, wheel: Path, wheelhouse: Path | None) -> None:
    common = _install_prefix(venv, wheelhouse)
    _checked((*common, "--require-hashes", "-r", str(requirements)))
    _checked((*common, "--no-deps", str(wheel)))


def _release_results(cli: Path, quality: tuple[BudgetResult, ...]) -> tuple[BudgetResult, ...]:
    return _static_results(quality) + _workload_results(cli)


def _static_results(quality: tuple[BudgetResult, ...]) -> tuple[BudgetResult, ...]:
    return (
        *quality,
        *_manifest_results(),
        *implementation_limit_results(),
        *_asset_results(),
        *_boundary_results(),
        _sqlite_result(),
    )


def _workload_results(cli: Path) -> tuple[BudgetResult, ...]:
    return (
        *_scenario_results(),
        _demo_start_result(cli),
        _browser_result(cli),
        timing_result("loopback_get_ms", _server_samples()),
        _materialization_result(),
        _representative_memory_result(),
        *_rejection_results(),
    )


def measure_release(quality_proof: Path | None = None) -> ReleaseReport:
    identity = collect_environment()
    quality = quality_results_from_proof(quality_proof) if quality_proof else _quality_results()
    with tempfile.TemporaryDirectory(prefix="agentic-saga-release-") as directory:
        results = _release_results(_wheel_cli(Path(directory)), quality)
    return ReleaseReport(identity, results)


def _render_result(result: BudgetResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    range_text = f"lower={result.lower:.3f} " if result.lower is not None else ""
    line = (
        f"{result.name}: {status} actual={result.actual:.3f} {result.unit} {range_text}"
        f"limit={result.limit:.3f} {result.unit} samples={result.samples} "
        f"temperature={result.temperature} comparison={result.comparison}"
    )
    if result.p50 is None or result.maximum is None:
        return line
    return f"{line} p50={result.p50:.3f} p95={result.actual:.3f} max={result.maximum:.3f}"


def render_report(report: ReleaseReport) -> str:
    errors = release_environment_errors(report.environment)
    lines = (*_identity_lines(report.environment), _environment_line(errors))
    passed = _report_passes(report, errors)
    result_lines = tuple(map(_render_result, report.results))
    return "\n".join((*lines, *result_lines, f"overall: {'PASS' if passed else 'FAIL'}")) + "\n"


def _environment_line(errors: Sequence[str]) -> str:
    status = "PASS" if not errors else f"FAIL ({'; '.join(errors)})"
    return f"release_environment: {status}"


def _report_passes(report: ReleaseReport, errors: Sequence[str]) -> bool:
    complete = len(report.results) == len(BUDGETS)
    return not errors and complete and all(item.passed for item in report.results)


def _identity_lines(identity: EnvironmentIdentity) -> tuple[str, ...]:
    return (
        "Agentic Saga release measurement",
        f"commit: {identity.commit}",
        f"tree_state: {identity.tree_state}",
        f"os: {identity.os}",
        f"cpu: {identity.cpu}",
        f"python: {identity.python}",
        f"node: {identity.node}",
        f"pnpm: {identity.pnpm}",
    )


def check_python_coverage(path: Path) -> int:
    actual = branch_percent(_read_json(path), "/kernel/")
    result = evaluate("core_branch_coverage_percent", actual)
    print(f"kernel branch coverage: {actual:.3f}%")
    return 0 if result.passed else 1


def check_release_coverage(path: Path) -> int:
    actual = branch_percent(_read_json(path), "scripts/")
    result = evaluate("release_scripts_branch_coverage_percent", actual)
    print(f"release-script branch coverage: {actual:.3f}%")
    return 0 if result.passed else 1


def _only_path(arguments: Sequence[str]) -> Path:
    if len(arguments) != 1:
        raise ValueError("coverage check requires exactly one JSON path")
    return Path(arguments[0])


def _rejection_command(arguments: Sequence[str]) -> int:
    if arguments:
        raise ValueError("rejection child takes no arguments")
    return _rejection_child()


def _python_coverage_command(arguments: Sequence[str]) -> int:
    return check_python_coverage(_only_path(arguments))


def _release_coverage_command(arguments: Sequence[str]) -> int:
    return check_release_coverage(_only_path(arguments))


_INTERNAL_COMMANDS: Final[Mapping[str, Callable[[Sequence[str]], int]]] = MappingProxyType(
    {
        "--rejection-child": _rejection_command,
        "--check-python-coverage": _python_coverage_command,
        "--check-release-coverage": _release_coverage_command,
    }
)


def internal_main(arguments: Sequence[str]) -> int:
    if not arguments:
        raise ValueError("unsupported internal release-runner arguments")
    command, *values = arguments
    handler = _INTERNAL_COMMANDS.get(command)
    if handler is None:
        raise ValueError("unsupported internal release-runner arguments")
    return handler(values)


if __name__ == "__main__":
    raise SystemExit(internal_main(tuple(sys.argv[1:])))
