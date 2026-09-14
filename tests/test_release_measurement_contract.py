from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.release_contract import (
    BUDGETS,
    BudgetResult,
    EnvironmentIdentity,
    ReleaseReport,
    branch_percent,
    evaluate,
    frontend_branch_percent,
    release_environment_errors,
    require_complete_report,
)

ROOT = Path(__file__).parents[1]


def _complete_results() -> tuple[BudgetResult, ...]:
    return tuple(
        evaluate(
            name,
            spec.limit,
            statistics=(spec.limit, spec.limit) if spec.samples > 1 else None,
        )
        for name, spec in BUDGETS.items()
    )


def test_true_branch_coverage_does_not_accept_high_combined_coverage() -> None:
    coverage = {
        "totals": {"percent_covered": 98.0, "covered_branches": 8, "num_branches": 10},
        "files": {
            "src/agentic_saga/kernel/__init__.py": {
                "summary": {"covered_branches": 0, "num_branches": 0}
            },
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 8, "num_branches": 10}
            },
        },
    }

    result = evaluate("core_branch_coverage_percent", branch_percent(coverage, "kernel"))

    assert result.actual == 80.0
    assert not result.passed


def test_release_script_branch_budget_is_independent_and_fail_closed() -> None:
    coverage = {
        "files": {
            "scripts/release_contract.py": {
                "summary": {"covered_branches": 89, "num_branches": 100}
            }
        }
    }

    result = evaluate(
        "release_scripts_branch_coverage_percent", branch_percent(coverage, "scripts/")
    )

    assert result.actual == 89.0
    assert result.limit == 90
    assert not result.passed


def test_branch_coverage_rejects_an_all_zero_denominator() -> None:
    coverage = {
        "files": {
            "src/agentic_saga/kernel/__init__.py": {
                "summary": {"covered_branches": 0, "num_branches": 0}
            }
        }
    }

    with pytest.raises(ValueError, match="measurable branch counts"):
        branch_percent(coverage, "kernel")


@pytest.mark.parametrize(
    ("coverage", "message"),
    [
        ({}, "no file details"),
        ({"files": {"other.py": {"summary": {}}}}, "no files matching"),
        ({"files": {"kernel.py": []}}, "invalid file details"),
        (
            {"files": {"kernel.py": {"summary": {"num_branches": 1}}}},
            "no measurable branch counts",
        ),
        (
            {"files": {"kernel.py": {"summary": {"covered_branches": 0, "num_branches": -1}}}},
            "no measurable branch counts",
        ),
    ],
)
def test_branch_coverage_rejects_malformed_file_evidence(
    coverage: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        branch_percent(coverage, "kernel")


def test_frontend_branch_coverage_uses_covered_and_total_counts() -> None:
    coverage = {
        "total": {
            "branches": {"covered": 8, "total": 10, "pct": 98.0},
            "lines": {"covered": 99, "total": 100, "pct": 99.0},
        }
    }

    result = evaluate("frontend_branch_coverage_percent", frontend_branch_percent(coverage))

    assert result.actual == 80.0
    assert not result.passed


@pytest.mark.parametrize(
    "coverage",
    [
        {},
        {"total": []},
        {"total": {"branches": []}},
        {"total": {"branches": {"covered": 0, "total": 0}}},
        {"total": {"branches": {"covered": 0, "total": "1"}}},
    ],
)
def test_frontend_branch_coverage_rejects_missing_or_empty_counts(
    coverage: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="branch"):
        frontend_branch_percent(coverage)


@pytest.mark.parametrize("covered", [-1, 11])
def test_branch_coverage_rejects_impossible_counts(covered: int) -> None:
    python = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": covered, "num_branches": 10}
            }
        }
    }
    frontend = {"total": {"branches": {"covered": covered, "total": 10}}}

    with pytest.raises(ValueError, match="branch counts"):
        branch_percent(python, "kernel")
    with pytest.raises(ValueError, match="branch counts"):
        frontend_branch_percent(frontend)


def test_budget_table_is_immutable_and_uses_published_limits() -> None:
    assert BUDGETS["manifest_parser_max_bytes"].limit == 64 * 1024
    assert BUDGETS["manifest_parser_max_depth"].limit == 16
    assert BUDGETS["manifest_parser_max_nodes"].limit == 4_096
    assert BUDGETS["materializer_max_static_files"].limit == 200
    assert BUDGETS["materializer_max_static_bytes"].limit == 32 * 1024 * 1024
    with pytest.raises(TypeError):
        BUDGETS["manifest_parser_max_bytes"] = BUDGETS["manifest_parser_max_depth"]  # type: ignore[index]


def test_complete_report_rejects_weakened_comparison_or_limit() -> None:
    results = _complete_results()
    environment = EnvironmentIdentity(
        "a" * 40, "clean", "test", "test", "Python 3.12.9", "v24.1.0", "11.5.0"
    )
    valid = ReleaseReport(environment, results)
    require_complete_report(valid)

    weakened = replace(results[0], comparison="<=", limit=results[0].limit + 1)
    with pytest.raises(ValueError, match="published budget"):
        require_complete_report(ReleaseReport(environment, (weakened, *results[1:])))


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "other"},
        {"samples": 2},
        {"temperature": "other"},
        {"passed": False},
        {"lower": 0},
    ],
)
def test_complete_report_rejects_tampered_result_metadata(change: dict[str, object]) -> None:
    results = _complete_results()
    environment = EnvironmentIdentity(
        "a" * 40, "clean", "test", "test", "Python 3.12.9", "v24.1.0", "11.5.0"
    )
    tampered = replace(results[0], **change)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="published budget"):
        require_complete_report(ReleaseReport(environment, (tampered, *results[1:])))


def test_complete_report_requires_timing_percentiles_and_maximum() -> None:
    results = _complete_results()
    environment = EnvironmentIdentity(
        "a" * 40, "clean", "test", "test", "Python 3.12.9", "v24.1.0", "11.5.0"
    )
    index = next(
        index for index, result in enumerate(results) if result.name == "materialization_ms"
    )
    tampered = replace(results[index], p50=None, maximum=None)
    changed = (*results[:index], tampered, *results[index + 1 :])

    with pytest.raises(ValueError, match="published budget"):
        require_complete_report(ReleaseReport(environment, changed))


@pytest.mark.parametrize(
    ("p50", "maximum"),
    [(1.0, None), (math.nan, 2.0), (2.0, 1.0)],
)
def test_complete_report_rejects_invalid_timing_statistics(
    p50: float, maximum: float | None
) -> None:
    results = _complete_results()
    index = next(index for index, item in enumerate(results) if item.name == "materialization_ms")
    tampered = replace(results[index], actual=1.5, p50=p50, maximum=maximum)
    changed = (*results[:index], tampered, *results[index + 1 :])
    environment = EnvironmentIdentity(
        "a" * 40, "clean", "test", "test", "Python 3.12.9", "v24.1.0", "11.5.0"
    )

    with pytest.raises(ValueError, match="published budget"):
        require_complete_report(ReleaseReport(environment, changed))


def test_complete_report_rejects_duplicate_missing_and_extra_results() -> None:
    results = _complete_results()
    environment = EnvironmentIdentity(
        "a" * 40, "clean", "test", "test", "Python 3.12.9", "v24.1.0", "11.5.0"
    )
    extra = replace(results[0], name="unpublished")

    with pytest.raises(ValueError, match="duplicate"):
        require_complete_report(ReleaseReport(environment, (*results, results[0])))
    with pytest.raises(ValueError, match="names differ"):
        require_complete_report(ReleaseReport(environment, results[:-1]))
    with pytest.raises(ValueError, match="names differ"):
        require_complete_report(ReleaseReport(environment, (*results[1:], extra)))


def test_complete_report_recomputes_effective_timeout_result() -> None:
    results = _complete_results()
    index = next(
        index for index, item in enumerate(results) if item.name == "effective_model_timeout_ms"
    )
    tampered = replace(results[index], actual=10_000, passed=True)
    changed = (*results[:index], tampered, *results[index + 1 :])
    environment = EnvironmentIdentity(
        "a" * 40, "clean", "test", "test", "Python 3.12.9", "v24.1.0", "11.5.0"
    )

    with pytest.raises(ValueError, match="published budget"):
        require_complete_report(ReleaseReport(environment, changed))


@pytest.mark.parametrize(
    ("python", "node", "pnpm", "expected"),
    [
        ("Python 3.12.9", "v24.7.0", "11.5.0", ()),
        ("Python 3.13.2", "v24.0.0", "11.5.0", ()),
        ("Python 3.11.9", "v24.0.0", "11.5.0", ("python",)),
        ("Python 3.12.9", "v26.0.0", "11.5.0", ("node",)),
        ("Python 3.12.9-extra", "v24.0.0", "11.5.0", ("python",)),
        ("Python 3.12.9", "v24.0.0-extra", "11.5.0", ("node",)),
        ("Python 3.12.9", "v24.0.0", "11.4.0", ("pnpm",)),
    ],
)
def test_release_environment_requires_exact_toolchain(
    python: str, node: str, pnpm: str, expected: tuple[str, ...]
) -> None:
    identity = EnvironmentIdentity("b" * 40, "clean", "test", "test", python, node, pnpm)

    errors = release_environment_errors(identity)

    assert tuple(error.split()[0] for error in errors) == expected


def test_dirty_checkout_is_not_release_valid() -> None:
    identity = EnvironmentIdentity(
        "c" * 40, "dirty", "test", "test", "Python 3.12.9", "v24.0.0", "11.5.0"
    )

    assert release_environment_errors(identity) == ("tree must be clean",)


def test_abbreviated_commit_is_not_release_valid() -> None:
    identity = EnvironmentIdentity(
        "abc123", "clean", "test", "test", "Python 3.13.9", "v24.0.0", "11.5.0"
    )

    assert release_environment_errors(identity) == ("commit must be a full SHA",)


def test_range_budget_enforces_both_bounds() -> None:
    assert not evaluate("model_timeout_ms", 99).passed
    assert evaluate("model_timeout_ms", 100).passed
    assert evaluate("model_timeout_ms", 60_000).passed
    assert not evaluate("model_timeout_ms", 60_001).passed


def test_removed_cost_budget_is_not_a_published_release_claim() -> None:
    with pytest.raises(KeyError, match="execution_cost_microusd_limit"):
        evaluate("execution_cost_microusd_limit", 0)


def test_nonfinite_measurement_never_passes() -> None:
    assert not evaluate("sdk_retries", math.nan).passed


def test_budget_result_is_not_an_untyped_mapping() -> None:
    result = evaluate("sdk_retries", 0)

    assert isinstance(result, BudgetResult)


def test_python_gate_generates_json_then_enforces_true_kernel_branches() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert "--cov-report=json:.coverage.json" in pyproject
    assert "--cov-fail-under=90" in pyproject
    assert "scripts.release_runner --check-python-coverage .coverage.json" in pyproject
    assert ".coverage-release-scripts.json" in pyproject
    assert "--check-release-coverage .coverage-release-scripts.json" in pyproject
    assert "--cov-fail-under=90" in pyproject
    scripts = (
        "scripts/measure_release.py scripts/release_contract.py "
        "scripts/release_runner.py scripts/quality_proof.py"
    )
    assert scripts in pyproject
    assert 'gate = ["lint-check", "format-check", "typecheck", "typecheck-release",' in pyproject
