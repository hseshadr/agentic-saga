from __future__ import annotations

import math
import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

Comparison = Literal["<=", ">=", "==", "range"]


@dataclass(frozen=True)
class BudgetSpec:
    name: str
    comparison: Comparison
    limit: float
    unit: str = "count"
    samples: int = 1
    temperature: str = "n/a"
    lower: float | None = None


@dataclass(frozen=True)
class BudgetResult:
    name: str
    actual: float
    limit: float
    unit: str
    samples: int
    temperature: str
    passed: bool
    p50: float | None = None
    maximum: float | None = None
    comparison: Comparison = "<="
    lower: float | None = None


@dataclass(frozen=True)
class EnvironmentIdentity:
    commit: str
    tree_state: str
    os: str
    cpu: str
    python: str
    node: str
    pnpm: str


@dataclass(frozen=True)
class ReleaseReport:
    environment: EnvironmentIdentity
    results: tuple[BudgetResult, ...]


def _spec(name: str, comparison: Comparison, limit: float, unit: str = "count") -> BudgetSpec:
    return BudgetSpec(name, comparison, limit, unit)


def _range(name: str, lower: float, limit: float, unit: str) -> BudgetSpec:
    return BudgetSpec(name, "range", limit, unit, lower=lower)


def _timed(name: str, limit: float, samples: int, temperature: str) -> BudgetSpec:
    return BudgetSpec(name, "<=", limit, "ms", samples, temperature)


MIB: Final[int] = 1024 * 1024
_SPECS: Final[tuple[BudgetSpec, ...]] = (
    _spec("core_branch_coverage_percent", ">=", 90, "%"),
    _spec("release_scripts_branch_coverage_percent", ">=", 90, "%"),
    _spec("frontend_branch_coverage_percent", ">=", 90, "%"),
    _spec("python_complexity_grade_a", "==", 1, "gate"),
    _spec("browser_behavior_checks", "==", 1, "gate"),
    _spec("manifest_bytes", "<=", 64 * 1024, "bytes"),
    _spec("manifest_depth", "<=", 16, "levels"),
    _spec("manifest_nodes", "<=", 4_096, "nodes"),
    _spec("manifest_parser_max_bytes", "==", 64 * 1024, "bytes"),
    _spec("manifest_parser_max_depth", "==", 16, "levels"),
    _spec("manifest_parser_max_nodes", "==", 4_096, "nodes"),
    _range("execution_turn_limit", 1, 10_000, "turns"),
    _range("execution_tool_call_limit", 1, 10_000, "calls"),
    _range("execution_elapsed_ms_limit", 1, 86_400_000, "ms"),
    _range("execution_token_limit", 1, 100_000_000, "tokens"),
    _spec("execution_turn_schema_minimum", "==", 1, "turns"),
    _spec("execution_turn_schema_maximum", "==", 10_000, "turns"),
    _spec("execution_tool_call_schema_minimum", "==", 1, "calls"),
    _spec("execution_tool_call_schema_maximum", "==", 10_000, "calls"),
    _spec("execution_elapsed_ms_schema_minimum", "==", 1, "ms"),
    _spec("execution_elapsed_ms_schema_maximum", "==", 86_400_000, "ms"),
    _spec("execution_token_schema_minimum", "==", 1, "tokens"),
    _spec("execution_token_schema_maximum", "==", 100_000_000, "tokens"),
    _spec("recorder_javascript_gzip_bytes", "<=", 110 * 1024, "bytes"),
    _spec("recorder_css_gzip_bytes", "<=", 5 * 1024, "bytes"),
    _spec("reference_trace_count", "==", 4, "traces"),
    _spec("reference_catalog_bytes", "<=", MIB, "bytes"),
    _spec("materializer_static_files", "<=", 200, "files"),
    _spec("materializer_static_bytes", "<=", 32 * MIB, "bytes"),
    _spec("materializer_max_static_files", "==", 200, "files"),
    _spec("materializer_max_static_bytes", "==", 32 * MIB, "bytes"),
    _spec("native_deferred_calls", "==", 1, "calls"),
    _spec("builtin_agent_capabilities", "==", 0, "capabilities"),
    _spec("sdk_retries", "==", 0, "retries"),
    _range("model_timeout_ms", 100, 60_000, "ms"),
    _spec("effective_model_timeout_ms", "==", 30_000, "ms"),
    _spec("model_timeout_schema_minimum_ms", "==", 100, "ms"),
    _spec("model_timeout_schema_maximum_ms", "==", 60_000, "ms"),
    _spec("import_runs", "<=", 100, "runs"),
    _spec("import_trace_bytes", "<=", 8 * MIB, "bytes"),
    _spec("import_index_bytes", "<=", 512 * 1024, "bytes"),
    _spec("import_json_depth", "<=", 16, "levels"),
    _spec("import_json_nodes", "<=", 100_000, "nodes"),
    _spec("request_path_chars", "<=", 240, "chars"),
    _spec("request_headers", "<=", 40, "headers"),
    _spec("request_header_bytes", "<=", 16 * 1024, "bytes"),
    _spec("served_file_bytes", "<=", 8 * MIB, "bytes"),
    _spec("lease_max_duration_seconds", "==", 86_400, "s"),
    _spec("lease_accepts_positive", "==", 1, "gate"),
    _spec("lease_rejects_nonpositive", "==", 1, "gate"),
    _spec("lease_rejects_over_max", "==", 1, "gate"),
    _spec("sqlite_busy_timeout_ms", "==", 5_000, "ms"),
    *(
        _timed(
            f"offline_{scenario}_p95_ms",
            2_000,
            20,
            "fresh-store",
        )
        for scenario in (
            "happy-path",
            "lost-response",
            "business-failure",
            "compensation-failure",
        )
    ),
    _timed("packaged_demo_start_ms", 3_000, 10, "cold-process"),
    _timed(
        "browser_fresh_navigation_ms",
        2_000,
        10,
        "warm-browser/fresh-page",
    ),
    _timed("loopback_get_ms", 100, 100, "warm-server"),
    _timed("materialization_ms", 500, 25, "fresh-destination"),
    _spec("representative_peak_rss_bytes", "<=", 256 * MIB, "bytes"),
    BudgetSpec("maximum_input_rejection_ms", "<=", 1_000, "ms", 1, "fresh-child"),
    BudgetSpec("maximum_input_rejection_rss_bytes", "<=", 256 * MIB, "bytes", 1, "fresh-child"),
)
BUDGETS: Final[Mapping[str, BudgetSpec]] = MappingProxyType({spec.name: spec for spec in _SPECS})
_COMPARISONS: Final[Mapping[Comparison, Callable[[float, float], bool]]] = MappingProxyType(
    {"<=": operator.le, ">=": operator.ge, "==": operator.eq}
)


def _passes(spec: BudgetSpec, actual: float) -> bool:
    if not math.isfinite(actual):
        return False
    if spec.comparison == "range":
        return _in_range(spec, actual)
    return _COMPARISONS[spec.comparison](actual, spec.limit)


def _in_range(spec: BudgetSpec, actual: float) -> bool:
    return spec.lower is not None and spec.lower <= actual <= spec.limit


def evaluate(
    name: str,
    actual: float,
    *,
    statistics: tuple[float, float] | None = None,
) -> BudgetResult:
    spec = BUDGETS[name]
    p50, maximum = statistics or (None, None)
    return _result(spec, actual, p50, maximum)


def _result(
    spec: BudgetSpec, actual: float, p50: float | None, maximum: float | None
) -> BudgetResult:
    identity = (spec.name, actual, spec.limit, spec.unit, spec.samples, spec.temperature)
    statistics = (_passes(spec, actual), p50, maximum, spec.comparison, spec.lower)
    return BudgetResult(*identity, *statistics)


def _counts(summary: Mapping[str, object]) -> tuple[int, int]:
    return _checked_counts(
        summary.get("covered_branches"),
        summary.get("num_branches"),
        "coverage JSON has no measurable branch counts",
    )


def _checked_counts(covered: object, total: object, error: str) -> tuple[int, int]:
    if not isinstance(covered, int):
        raise ValueError(error)
    if not isinstance(total, int):
        raise ValueError(error)
    if total < 0:
        raise ValueError(error)
    if covered not in range(total + 1):
        raise ValueError(error)
    return covered, total


def _coverage_files(payload: Mapping[str, object]) -> Mapping[object, object]:
    files = payload.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("coverage JSON has no file details")
    return files


def _file_counts(value: object) -> tuple[int, int]:
    summary = value.get("summary") if isinstance(value, Mapping) else None
    if not isinstance(summary, Mapping):
        raise ValueError("coverage JSON has invalid file details")
    return _counts(summary)


def _matching_counts(
    files: Mapping[object, object], path_fragment: str
) -> tuple[tuple[int, int], ...]:
    return tuple(
        _file_counts(value)
        for path, value in files.items()
        if isinstance(path, str) and path_fragment in path
    )


def branch_percent(payload: Mapping[str, object], path_fragment: str) -> float:
    selected = _matching_counts(_coverage_files(payload), path_fragment)
    if not selected:
        raise ValueError(f"coverage JSON has no files matching {path_fragment}")
    covered, total = map(sum, zip(*selected, strict=True))
    if total == 0:
        raise ValueError("coverage JSON has no measurable branch counts")
    return covered * 100.0 / total


def frontend_branch_percent(payload: Mapping[str, object]) -> float:
    total = payload.get("total")
    branches = total.get("branches") if isinstance(total, Mapping) else None
    if not isinstance(branches, Mapping):
        raise ValueError("frontend coverage JSON has no branch counts")
    error = "frontend coverage has invalid branch counts"
    covered, count = _checked_counts(branches.get("covered"), branches.get("total"), error)
    if count == 0:
        raise ValueError(error)
    return covered * 100.0 / count


def release_environment_errors(identity: EnvironmentIdentity) -> tuple[str, ...]:
    checks = (
        ("commit must be a full SHA", re.fullmatch(r"[0-9a-f]{40}", identity.commit)),
        ("tree must be clean", identity.tree_state == "clean"),
        ("python must be 3.12 or 3.13", _release_python(identity.python)),
        ("node must be 24.x", re.fullmatch(r"v24\.\d+\.\d+", identity.node)),
        ("pnpm must be 11.5.0", identity.pnpm == "11.5.0"),
    )
    return tuple(message for message, valid in checks if not valid)


def _release_python(value: str) -> re.Match[str] | None:
    return re.fullmatch(r"Python 3\.(?:12|13)\.\d+", value)


def _valid_metadata(result: BudgetResult, spec: BudgetSpec) -> bool:
    exact = _result_metadata(result) == _spec_metadata(result, spec)
    return exact and _valid_statistics(result, spec)


def _valid_statistics(result: BudgetResult, spec: BudgetSpec) -> bool:
    if spec.samples == 1:
        return (result.p50, result.maximum) == (None, None)
    if result.p50 is None:
        return False
    if result.maximum is None:
        return False
    return _finite_order(result.p50, result.actual, result.maximum)


def _finite_order(p50: float, p95: float, maximum: float) -> bool:
    if not all(math.isfinite(value) for value in (p50, p95, maximum)):
        return False
    return p50 <= p95 <= maximum


def _result_metadata(result: BudgetResult) -> tuple[object, ...]:
    return (
        result.limit,
        result.comparison,
        result.unit,
        result.samples,
        result.temperature,
        result.lower,
        result.passed,
    )


def _spec_metadata(result: BudgetResult, spec: BudgetSpec) -> tuple[object, ...]:
    return (
        spec.limit,
        spec.comparison,
        spec.unit,
        spec.samples,
        spec.temperature,
        spec.lower,
        _passes(spec, result.actual),
    )


def require_complete_report(report: ReleaseReport) -> None:
    results = {result.name: result for result in report.results}
    _require_result_names(results, len(report.results))
    if _has_invalid_metadata(results):
        raise ValueError("release measurement differs from the published budget contract")


def _require_result_names(results: Mapping[str, BudgetResult], count: int) -> None:
    if len(results) != count:
        raise ValueError("release measurements contain duplicate names")
    if results.keys() == BUDGETS.keys():
        return
    missing = sorted(BUDGETS.keys() - results.keys())
    extra = sorted(results.keys() - BUDGETS.keys())
    raise ValueError(f"release measurement names differ: missing={missing}, extra={extra}")


def _has_invalid_metadata(results: Mapping[str, BudgetResult]) -> bool:
    validity = (_valid_metadata(result, BUDGETS[name]) for name, result in results.items())
    return not all(validity)
