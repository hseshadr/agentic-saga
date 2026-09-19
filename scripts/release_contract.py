from __future__ import annotations

import math
import operator
import re
from collections.abc import Callable, Iterator, Mapping
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


def _spec(name: str, comparison: Comparison, limit: float, unit: str = "gate") -> BudgetSpec:
    return BudgetSpec(name, comparison, limit, unit)


_SPECS: Final = (
    _spec("core_branch_coverage_percent", ">=", 90, "%"),
    _spec("release_scripts_branch_coverage_percent", ">=", 90, "%"),
    _spec("frontend_branch_coverage_percent", ">=", 90, "%"),
    _spec("python_complexity_grade_a", "==", 1),
    _spec("browser_behavior_checks", "==", 1),
    _spec("temporal_runtime_dependency", "==", 1),
    _spec("legacy_runtime_absent", "==", 1),
    _spec("public_api_imports", "==", 1),
    _spec("temporal_tests_passed", "==", 1),
    _spec("package_build_install", "==", 1),
    _spec("recorder_cli_ready", "==", 1),
)
BUDGETS: Final[Mapping[str, BudgetSpec]] = MappingProxyType({spec.name: spec for spec in _SPECS})
_COMPARISONS: Final[Mapping[Comparison, Callable[[float, float], bool]]] = MappingProxyType(
    {"<=": operator.le, ">=": operator.ge, "==": operator.eq}
)


def evaluate(name: str, actual: float) -> BudgetResult:
    spec = BUDGETS[name]
    passed = math.isfinite(actual) and _COMPARISONS[spec.comparison](actual, spec.limit)
    return BudgetResult(
        name=spec.name,
        actual=actual,
        limit=spec.limit,
        unit=spec.unit,
        samples=spec.samples,
        temperature=spec.temperature,
        passed=passed,
        comparison=spec.comparison,
        lower=spec.lower,
    )


def _checked_counts(covered: object, total: object, error: str) -> tuple[int, int]:
    if not isinstance(covered, int) or not isinstance(total, int):
        raise ValueError(error)
    if total <= 0 or covered not in range(total + 1):
        raise ValueError(error)
    return covered, total


def _coverage_files(payload: Mapping[str, object]) -> Mapping[object, object]:
    files = payload.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("coverage JSON has no file details")
    return files


def _file_counts(value: object) -> tuple[int, int] | None:
    summary = value.get("summary") if isinstance(value, Mapping) else None
    if not isinstance(summary, Mapping):
        raise ValueError("coverage JSON has invalid file details")
    covered = summary.get("covered_branches")
    total = summary.get("num_branches")
    if covered == 0 and total == 0:
        return None
    return _checked_counts(
        covered,
        total,
        "coverage JSON has no measurable branch counts",
    )


def _matches_path(path: object, fragment: str) -> bool:
    return isinstance(path, str) and fragment in path


def _matching_values(files: Mapping[object, object], fragment: str) -> Iterator[object]:
    for path, value in files.items():
        if _matches_path(path, fragment):
            yield value


def _measurable_counts(values: Iterator[object]) -> Iterator[tuple[int, int]]:
    for value in values:
        count = _file_counts(value)
        if count is not None:
            yield count


def branch_percent(payload: Mapping[str, object], path_fragment: str) -> float:
    files = _coverage_files(payload)
    counts = tuple(_measurable_counts(_matching_values(files, path_fragment)))
    if not counts:
        raise ValueError(f"coverage JSON has no measurable files matching {path_fragment}")
    covered, total = map(sum, zip(*counts, strict=True))
    return covered * 100.0 / total


def frontend_branch_percent(payload: Mapping[str, object]) -> float:
    total = payload.get("total")
    branches = total.get("branches") if isinstance(total, Mapping) else None
    if not isinstance(branches, Mapping):
        raise ValueError("frontend coverage JSON has no branch counts")
    covered, count = _checked_counts(
        branches.get("covered"),
        branches.get("total"),
        "frontend coverage has invalid branch counts",
    )
    return covered * 100.0 / count


def release_environment_errors(identity: EnvironmentIdentity) -> tuple[str, ...]:
    checks = (
        ("commit must be a full SHA", re.fullmatch(r"[0-9a-f]{40}", identity.commit)),
        ("tree must be clean", identity.tree_state == "clean"),
        ("python must be 3.12 or 3.13", re.fullmatch(r"Python 3\.(?:12|13)\.\d+", identity.python)),
        ("node must be 24.x", re.fullmatch(r"v24\.\d+\.\d+", identity.node)),
        ("pnpm must be 11.5.0", identity.pnpm == "11.5.0"),
    )
    return tuple(message for message, valid in checks if not valid)


def require_complete_report(report: ReleaseReport) -> None:
    results = {result.name: result for result in report.results}
    _require_names(results, len(report.results))
    if any(result != evaluate(name, result.actual) for name, result in results.items()):
        raise ValueError("release measurement differs from the published budget contract")


def _require_names(results: Mapping[str, BudgetResult], count: int) -> None:
    if len(results) != count:
        raise ValueError("release measurements contain duplicate names")
    if results.keys() == BUDGETS.keys():
        return
    missing = sorted(BUDGETS.keys() - results.keys())
    extra = sorted(results.keys() - BUDGETS.keys())
    raise ValueError(f"release measurement names differ: missing={missing}, extra={extra}")
