from __future__ import annotations

import json
import os
import platform
import runpy
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import agentic_saga.manifest as manifest_module
import scripts.measure_release as orchestrator
import scripts.quality_proof as proof_module
import scripts.release_runner as runner
from agentic_saga.agents import deepagents as deepagents_module
from agentic_saga.demo import assets as assets_module
from scripts.release_contract import (
    BUDGETS,
    BudgetResult,
    EnvironmentIdentity,
    ReleaseReport,
    evaluate,
)

_QUALITY_RESULT_NAMES = (
    "core_branch_coverage_percent",
    "release_scripts_branch_coverage_percent",
    "frontend_branch_coverage_percent",
    "python_complexity_grade_a",
    "browser_behavior_checks",
)


def _proof_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proof-repository"
    (root / "web" / "flight-recorder" / "coverage").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'proof-fixture'\n")
    (root / "uv.lock").write_text("version = 1\n")
    (root / "web" / "flight-recorder" / "package.json").write_text('{"name":"fixture"}\n')
    (root / "web" / "flight-recorder" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (root / ".gitignore").write_text(".coverage*.json\nweb/flight-recorder/coverage/\n")
    _commit_proof_repository(root)
    _write_proof_evidence(root)
    monkeypatch.setattr(proof_module, "ROOT", root)
    return root


def _write_proof_evidence(root: Path) -> None:
    _write_evidence(root / ".coverage.json", _python_evidence())
    _write_evidence(root / ".coverage-release-scripts.json", _release_evidence())
    _write_evidence(_frontend_coverage_path(root), _frontend_evidence())


def _python_evidence() -> dict[str, object]:
    summary = {"covered_branches": 9, "num_branches": 10}
    return {"files": {"src/agentic_saga/kernel/runtime.py": {"summary": summary}}}


def _release_evidence() -> dict[str, object]:
    summary = {"covered_branches": 19, "num_branches": 20}
    return {"files": {"scripts/release_runner.py": {"summary": summary}}}


def _frontend_evidence() -> dict[str, object]:
    return {"total": {"branches": {"covered": 9, "total": 10}}}


def _frontend_coverage_path(root: Path) -> Path:
    return root / "web" / "flight-recorder" / "coverage" / "coverage-summary.json"


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.write_text(json.dumps(evidence))


def _commit_proof_repository(root: Path) -> None:
    _git(root, "init")
    _git(root, "add", ".")
    _commit_fixture(root)


def _commit_fixture(root: Path) -> None:
    _git(
        root,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        "fixture",
    )


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(("git", *arguments), cwd=root, check=True, capture_output=True, text=True)


def _valid_quality_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _proof_repository(tmp_path, monkeypatch)
    path = tmp_path / "quality-proof.json"
    proof_module.write_quality_proof(path)
    return path


def _tampered_quality_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> Path:
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    if field in {"source_commit", "python_version"}:
        payload[field] = "tampered"
    else:
        _mutable_mapping(payload["input_digests"])[field] = "0" * 64
    path.write_text(json.dumps(payload))
    return path


def _proof_payload(path: Path) -> dict[str, object]:
    return dict(proof_module.read_json_object(path))


def _write_proof_payload(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, allow_nan=True))


def _mutable_mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _mutable_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def _first_result(payload: dict[str, object]) -> dict[str, object]:
    return _mutable_mapping(_mutable_list(payload["results"])[0])


class _Stream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StubbornProcess:
    def __init__(self) -> None:
        self.stdout = _Stream()
        self.stderr = _Stream()
        self.actions: list[object] = []
        self.waits = 0

    def poll(self) -> int | None:
        return None

    def send_signal(self, value: int) -> None:
        self.actions.append(value)

    def terminate(self) -> None:
        self.actions.append("terminate")

    def kill(self) -> None:
        self.actions.append("kill")

    def wait(self, timeout: float) -> int:
        self.actions.append(("wait", timeout))
        self.waits += 1
        if self.waits < 3:
            raise subprocess.TimeoutExpired("fake", timeout)
        return -9

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        del input, timeout
        raise AssertionError("unexpected capture")


class _StubbornCapture(_StubbornProcess):
    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        del input
        if timeout is None:
            raise AssertionError("timeout is required")
        self.actions.append(("communicate", timeout))
        self.waits += 1
        if self.waits < 4:
            raise subprocess.TimeoutExpired("fake", timeout)
        return '{"elapsed_ms": 7.5, "rss_raw": 64, "platform": "Linux"}\n', ""


class _NeverCaptured(_StubbornProcess):
    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        del input
        if timeout is None:
            raise AssertionError("timeout is required")
        raise subprocess.TimeoutExpired("fake", timeout)


class _EventuallyStopped(_StubbornProcess):
    def __init__(self, successful_wait: int) -> None:
        super().__init__()
        self.successful_wait = successful_wait

    def wait(self, timeout: float) -> int:
        self.actions.append(("wait", timeout))
        self.waits += 1
        if self.waits < self.successful_wait:
            raise subprocess.TimeoutExpired("fake", timeout)
        return -signal.SIGINT


def _passing_report() -> ReleaseReport:
    results = tuple(
        evaluate(
            name,
            spec.limit,
            statistics=(spec.limit, spec.limit) if spec.samples > 1 else None,
        )
        for name, spec in BUDGETS.items()
    )
    identity = EnvironmentIdentity(
        "a" * 40, "clean", "TestOS", "TestCPU", "Python 3.12.9", "v24.0.0", "11.5.0"
    )
    return ReleaseReport(identity, results)


def _recording_measurement(
    received: list[Path | None],
) -> Callable[[Path | None], ReleaseReport]:
    def measure(path: Path | None = None) -> ReleaseReport:
        received.append(path)
        return _passing_report()

    return measure


def test_quality_evidence_enforces_python_and_frontend_branches_separately() -> None:
    python = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 95, "num_branches": 100}
            }
        }
    }
    release = {
        "files": {
            "scripts/release_runner.py": {"summary": {"covered_branches": 92, "num_branches": 100}}
        }
    }
    frontend = {"total": {"branches": {"covered": 89, "total": 100, "pct": 99}}}

    results = runner.coverage_results(python, release, frontend)

    assert results[0].actual == 95
    assert results[0].passed
    assert results[1].actual == 92
    assert results[1].passed
    assert results[2].actual == 89
    assert not results[2].passed


def test_valid_quality_proof_reuses_results_without_running_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    proof = _valid_quality_proof(tmp_path, monkeypatch)
    # When
    results = proof_module.quality_results_from_proof(proof)
    # Then
    assert tuple(result.name for result in results) == _QUALITY_RESULT_NAMES


@pytest.mark.parametrize("field", ["p50", "maximum", "comparison", "lower"])
def test_quality_proof_rejects_missing_required_result_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    del _first_result(payload)[field]
    _write_proof_payload(path, payload)

    # When / Then
    with pytest.raises(ValueError, match="quality proof schema"):
        proof_module.quality_results_from_proof(path)


def test_quality_proof_rejects_coerced_numeric_result_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    _first_result(payload)["actual"] = "90.0"
    _write_proof_payload(path, payload)

    # When / Then
    with pytest.raises(ValueError, match="invalid quality proof schema"):
        proof_module.quality_results_from_proof(path)


def test_quality_proof_rejects_unknown_result_name_before_budget_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    _first_result(payload)["name"] = "not-a-budget"
    _write_proof_payload(path, payload)

    # When / Then
    with pytest.raises(ValueError, match="quality proof results differ"):
        proof_module.quality_results_from_proof(path)


def test_release_cli_returns_usage_error_for_unknown_proof_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    _first_result(payload)["name"] = "not-a-budget"
    _write_proof_payload(path, payload)
    monkeypatch.setattr(runner, "collect_environment", lambda: _passing_report().environment)

    # When / Then
    assert orchestrator.main(("--quality-proof", str(path))) == 2


@pytest.mark.parametrize("contents", ["[", "[]"])
def test_quality_proof_reader_rejects_invalid_or_nonobject_documents(
    tmp_path: Path, contents: str
) -> None:
    # Given
    path = tmp_path / "proof.json"
    path.write_text(contents)

    # When / Then
    with pytest.raises(ValueError, match="quality proof"):
        proof_module.read_json_object(path)


def test_quality_proof_rejects_nonlist_completed_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    payload["completed_checks"] = {"python-gate": True}
    _write_proof_payload(path, payload)

    # When / Then
    with pytest.raises(ValueError, match="quality proof schema"):
        proof_module.quality_results_from_proof(path)


def test_quality_proof_rejects_nonlist_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    payload["results"] = {"name": "not-a-list"}
    _write_proof_payload(path, payload)

    # When / Then
    with pytest.raises(ValueError, match="quality proof schema"):
        proof_module.quality_results_from_proof(path)


def test_quality_proof_rejects_nonobject_result_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    _mutable_list(payload["results"])[0] = []
    _write_proof_payload(path, payload)

    # When / Then
    with pytest.raises(ValueError, match="quality proof schema"):
        proof_module.quality_results_from_proof(path)


def test_quality_proof_rejects_nonobject_input_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    payload["input_digests"] = []
    _write_proof_payload(path, payload)

    # When / Then
    with pytest.raises(ValueError, match="quality proof schema"):
        proof_module.quality_results_from_proof(path)


def test_quality_proof_rejects_result_that_disagrees_with_coverage_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    _first_result(payload)["actual"] = 91.0
    _write_proof_payload(path, payload)

    # When / Then
    with pytest.raises(ValueError, match="quality proof evidence mismatch"):
        proof_module.quality_results_from_proof(path)


def test_quality_proof_rejects_tampered_result_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    _first_result(payload)["unit"] = "tampered"
    _write_proof_payload(path, payload)

    # When / Then
    with pytest.raises(ValueError, match="result metadata mismatch"):
        proof_module.quality_results_from_proof(path)


@pytest.mark.parametrize("field", ["source_commit", "python_version", "uv_lock_sha256"])
def test_quality_proof_rejects_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    proof = _tampered_quality_proof(tmp_path, monkeypatch, field)

    with pytest.raises(ValueError, match="quality proof identity mismatch"):
        proof_module.quality_results_from_proof(proof)


@pytest.mark.parametrize("change", ["unknown", "missing", "extra"])
def test_quality_proof_rejects_nonexact_schema_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    # Given
    path = _valid_quality_proof(tmp_path, monkeypatch)
    _tamper_schema(path, _proof_payload(path), change)

    # When / Then
    with pytest.raises(ValueError, match="quality proof schema keys"):
        proof_module.quality_results_from_proof(path)


def _tamper_schema(path: Path, payload: dict[str, object], change: str) -> None:
    if change == "unknown":
        payload["unrecognized"] = True
    elif change == "missing":
        del payload["results"]
    else:
        _mutable_mapping(payload["input_digests"])["extra"] = "0" * 64
    _write_proof_payload(path, payload)


@pytest.mark.parametrize(
    "checks",
    [("python-gate",), ("python-gate", "python-gate"), ("frontend-gate", "python-gate")],
)
def test_quality_proof_rejects_missing_or_duplicate_completed_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checks: tuple[str, ...]
) -> None:
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    payload["completed_checks"] = checks
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="completed checks"):
        proof_module.quality_results_from_proof(path)


def test_quality_proof_rejects_nonfinite_coverage_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _valid_quality_proof(tmp_path, monkeypatch)
    payload = _proof_payload(path)
    _first_result(payload)["actual"] = float("nan")
    path.write_text(json.dumps(payload, allow_nan=True))

    with pytest.raises(ValueError, match="non-finite"):
        proof_module.quality_results_from_proof(path)


def test_quality_proof_rejects_modified_coverage_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _valid_quality_proof(tmp_path, monkeypatch)
    root = proof_module.ROOT
    (root / ".coverage.json").write_text("{}")

    with pytest.raises(ValueError, match="quality proof evidence mismatch"):
        proof_module.quality_results_from_proof(path)


@pytest.mark.parametrize("change", ["abbreviated", "dirty"])
def test_quality_proof_rejects_invalid_repository_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = _valid_quality_proof(tmp_path, monkeypatch)
    if change == "abbreviated":
        payload = _proof_payload(path)
        source_commit = payload["source_commit"]
        assert isinstance(source_commit, str)
        payload["source_commit"] = source_commit[:7]
        path.write_text(json.dumps(payload))
    else:
        (proof_module.ROOT / "pyproject.toml").write_text("dirty\n")

    with pytest.raises(ValueError, match="quality proof identity mismatch"):
        proof_module.quality_results_from_proof(path)


def test_quality_proof_atomic_write_cleans_up_after_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    _proof_repository(tmp_path, monkeypatch)
    destination = tmp_path / "quality-proof.json"
    monkeypatch.setattr(os, "replace", _replace_failure)
    # When / Then
    with pytest.raises(OSError, match="replace failed"):
        proof_module.write_quality_proof(destination)
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".quality-proof-*.tmp"))


def _replace_failure(_source: object, _target: object) -> None:
    raise OSError("replace failed")


def test_measure_release_reuses_quality_proof_without_running_quality_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    proof = _valid_quality_proof(tmp_path, monkeypatch)
    identity = _passing_report().environment
    expected = proof_module.quality_results_from_proof(proof)
    _configure_proof_reuse(monkeypatch, identity, tmp_path)
    # When
    report = runner.measure_release(proof)
    # Then
    assert report.environment == identity
    assert report.results == expected


def _configure_proof_reuse(
    monkeypatch: pytest.MonkeyPatch, identity: EnvironmentIdentity, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "collect_environment", lambda: identity)
    monkeypatch.setattr(runner, "_quality_results", _unexpected_quality_gate)
    monkeypatch.setattr(runner, "_wheel_cli", lambda _workspace: tmp_path / "cli")
    monkeypatch.setattr(runner, "_release_results", lambda _cli, quality: quality)


def _unexpected_quality_gate() -> tuple[BudgetResult, ...]:
    pytest.fail("quality gates must not run")


def test_repository_coverage_command_fails_below_true_branch_floor(tmp_path: Path) -> None:
    coverage = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 89, "num_branches": 100}
            }
        }
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(coverage))

    assert runner.check_python_coverage(path) == 1


def test_release_script_coverage_command_fails_below_true_branch_floor(tmp_path: Path) -> None:
    coverage = {
        "files": {
            "scripts/release_runner.py": {"summary": {"covered_branches": 89, "num_branches": 100}}
        }
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(coverage))

    assert runner.check_release_coverage(path) == 1


def test_release_coverage_commands_accept_true_branch_floor(tmp_path: Path) -> None:
    coverage = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 9, "num_branches": 10}
            },
            "scripts/release_runner.py": {"summary": {"covered_branches": 19, "num_branches": 20}},
        }
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(coverage))

    assert runner.check_python_coverage(path) == 0
    assert runner.check_release_coverage(path) == 0


def test_internal_cli_dispatches_both_coverage_checks(tmp_path: Path) -> None:
    coverage = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 9, "num_branches": 10}
            },
            "scripts/release_runner.py": {"summary": {"covered_branches": 9, "num_branches": 10}},
        }
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(coverage))

    assert runner.internal_main(("--check-python-coverage", str(path))) == 0
    assert runner.internal_main(("--check-release-coverage", str(path))) == 0


def test_internal_cli_dispatches_rejection_child_and_rejects_unknown_args(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert runner.internal_main(("--rejection-child",)) == 0
    assert "elapsed_ms" in capsys.readouterr().out

    with pytest.raises(ValueError, match="unsupported internal release-runner arguments"):
        runner.internal_main(("unknown",))


def test_command_runner_is_offline_and_reports_failure_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", "must-not-leak")
    success = runner._checked((sys.executable, "-c", "print('ok')"))

    assert success == "ok\n"
    assert "OPENROUTER_API_KEY" not in runner._environment()
    assert "PYTHONPATH" not in runner._environment()
    assert runner._environment()["UV_OFFLINE"] == "1"
    with pytest.raises(RuntimeError, match="release measurement command failed"):
        runner._checked(
            (sys.executable, "-c", "import sys; sys.stderr.write('broken'); sys.exit(4)")
        )


def test_version_and_pnpm_resolution_use_exact_tool_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "pnpm"
    executable.write_text("#!/bin/sh\nprintf '11.5.0\\n'\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    assert runner._version((sys.executable, "-c", "print('v1.2.3')")) == "v1.2.3"
    assert runner._pnpm_command("gate") == ("pnpm", "gate")

    executable.write_text("#!/bin/sh\nprintf '11.4.0\\n'\n")
    assert runner._pnpm_command("gate") == ("npx", "--yes", "pnpm@11.5.0", "gate")


def test_json_reader_and_shape_fail_closed_for_invalid_documents(tmp_path: Path) -> None:
    mapping = tmp_path / "mapping.json"
    sequence = tmp_path / "sequence.json"
    mapping.write_text('{"nested": [1, 2]}')
    sequence.write_text("[]")

    assert runner._read_json(mapping) == {"nested": [1, 2]}
    assert runner._shape({"nested": [1, 2]}) == (3, 4)
    assert runner._shape("leaf") == (1, 1)
    with pytest.raises(ValueError, match="expected JSON object"):
        runner._read_json(sequence)


@pytest.mark.parametrize("value", [True, "1", 1.5, None])
def test_integer_boundary_rejects_non_integer_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        runner._integer(value, "field")


@pytest.mark.parametrize("value", [0, -1])
def test_positive_integer_boundary_rejects_nonpositive_values(value: int) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        runner._positive_integer(value, "field")


def test_schema_bound_reader_rejects_missing_integer_bounds() -> None:
    assert runner._integer_bounds(1, 2, "field") == (1, 2)
    with pytest.raises(RuntimeError, match="missing integer schema bounds"):
        runner._integer_bounds(None, 2, "field")


@pytest.mark.parametrize(
    ("module", "name", "value", "result_name"),
    [
        (manifest_module, "_MAX_SOURCE_BYTES", 65_537, "manifest_parser_max_bytes"),
        (manifest_module, "_MAX_DOCUMENT_DEPTH", 17, "manifest_parser_max_depth"),
        (manifest_module, "_MAX_DOCUMENT_NODES", 4_097, "manifest_parser_max_nodes"),
        (assets_module, "_MAX_STATIC_FILES", 201, "materializer_max_static_files"),
        (assets_module, "_MAX_STATIC_BYTES", 32 * 1024 * 1024 + 1, "materializer_max_static_bytes"),
    ],
)
def test_implementation_constant_weakening_fails_fixed_budget(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    value: int,
    result_name: str,
) -> None:
    monkeypatch.setattr(module, name, value)

    results = runner.implementation_limit_results()

    assert not next(item for item in results if item.name == result_name).passed


def test_implementation_limits_cover_schema_bounds_and_lease_positivity() -> None:
    results = {result.name: result for result in runner.implementation_limit_results()}

    assert results["execution_turn_schema_minimum"].actual == 1
    assert not any("cost_microusd" in name for name in results)
    assert results["model_timeout_schema_minimum_ms"].actual == 100
    assert results["lease_accepts_positive"].passed
    assert results["lease_rejects_nonpositive"].passed
    assert results["lease_rejects_over_max"].passed


def test_reference_execution_budget_must_be_positive_and_schema_bounded() -> None:
    budgets = {
        "turn_limit": 0,
        "tool_call_limit": 10_001,
        "elapsed_ms_limit": 1,
        "token_limit": 1,
    }

    results = {result.name: result for result in runner._execution_results(budgets)}

    assert not results["execution_turn_limit"].passed
    assert not results["execution_tool_call_limit"].passed
    assert set(results) == {
        "execution_turn_limit",
        "execution_tool_call_limit",
        "execution_elapsed_ms_limit",
        "execution_token_limit",
    }


def test_effective_model_timeout_is_capped_by_the_saga_budget() -> None:
    budgets = {
        "turn_limit": 20,
        "elapsed_ms_limit": 120_000,
    }

    result = runner.effective_model_timeout_result(budgets)

    assert result.name == "effective_model_timeout_ms"
    assert result.actual == 6_000
    assert result.passed


def test_effective_model_timeout_uses_fixed_floor_reservation() -> None:
    # Given
    budgets = {"turn_limit": 3, "elapsed_ms_limit": 10}

    # When
    result = runner.effective_model_timeout_result(budgets)

    # Then
    assert result.actual == 3


def test_effective_timeout_rejects_invalid_saga_divisor() -> None:
    with pytest.raises(ValueError, match="turn_limit must be positive"):
        runner.effective_model_timeout_result({"turn_limit": 0, "elapsed_ms_limit": 120_000})


def test_manifest_measurement_reads_shape_budgets_and_effective_timeout() -> None:
    results = {result.name: result for result in runner._manifest_results()}

    assert results["manifest_bytes"].passed
    assert results["manifest_depth"].passed
    assert results["effective_model_timeout_ms"].actual == 6_000


def test_manifest_measurement_rejects_non_mapping_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "examples" / "ecommerce"
    manifest.mkdir(parents=True)
    (manifest / "saga.yaml").write_text("- not-a-mapping\n")
    monkeypatch.setattr(runner, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="reference manifest must be a mapping"):
        runner._manifest_results()


def test_manifest_measurement_requires_budget_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "examples" / "ecommerce"
    manifest.mkdir(parents=True)
    (manifest / "saga.yaml").write_text("budgets: []\n")
    monkeypatch.setattr(runner, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="reference manifest has no budgets"):
        runner._manifest_results()


def test_static_boundary_measurements_use_real_package_limits() -> None:
    assets = {result.name: result for result in runner._asset_results()}
    boundaries = {result.name: result for result in runner._boundary_results()}
    sqlite = runner._sqlite_result()

    assert assets["reference_trace_count"].actual == 4
    assert boundaries["optional_model_calls"].actual == 1
    assert boundaries["model_timeout_ms"].actual == 10_000
    assert sqlite.actual == 5_000


def test_model_call_probe_requires_exactly_one_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deepagents_module, "_create_graph", lambda *args: object())

    with pytest.raises(RuntimeError, match="exactly one model-call limit"):
        runner._configured_model_call_limit()


def test_demo_shutdown_escalates_and_closes_pipes() -> None:
    process = _StubbornProcess()

    runner.stop_process(process, grace_seconds=0.01)

    assert process.actions == [
        signal.SIGINT,
        ("wait", 0.01),
        "terminate",
        ("wait", 0.01),
        "kill",
        ("wait", 0.01),
    ]
    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize(
    ("successful_wait", "expected_actions"),
    [
        (1, [signal.SIGINT, ("wait", 0.01)]),
        (2, [signal.SIGINT, ("wait", 0.01), "terminate", ("wait", 0.01)]),
    ],
)
def test_demo_shutdown_stops_at_the_first_successful_escalation(
    successful_wait: int, expected_actions: list[object]
) -> None:
    process = _EventuallyStopped(successful_wait)

    runner.stop_process(process, grace_seconds=0.01)

    assert process.actions == expected_actions
    assert process.stdout.closed
    assert process.stderr.closed


def test_demo_shutdown_closes_pipes_when_child_cannot_be_reaped() -> None:
    process = _EventuallyStopped(4)

    with pytest.raises(RuntimeError, match="could not be reaped"):
        runner.stop_process(process, grace_seconds=0.01)

    assert process.stdout.closed
    assert process.stderr.closed


def test_demo_shutdown_does_not_signal_an_exited_child() -> None:
    process = _StubbornProcess()
    process.poll = lambda: 0  # type: ignore[method-assign]

    runner.stop_process(process, grace_seconds=0.01)

    assert process.actions == []
    assert process.stdout.closed
    assert process.stderr.closed


def test_rejection_child_timeout_escalates_and_parses_child_rss() -> None:
    process = _StubbornCapture()

    output = runner.bounded_output(process, grace_seconds=0.01)
    results = runner.rejection_results_from_output(output)

    assert signal.SIGINT in process.actions
    assert "terminate" in process.actions
    assert "kill" in process.actions
    assert results[0].actual == 7.5
    assert results[1].actual == 64 * 1024
    assert process.stdout.closed
    assert process.stderr.closed


def test_capture_returns_immediately_and_closes_optional_pipes() -> None:
    process = _StubbornCapture()
    process.waits = 3
    process.stderr = None  # type: ignore[assignment]

    output = runner.bounded_output(process, grace_seconds=0.01)

    assert '"elapsed_ms": 7.5' in output
    assert process.stdout.closed


def test_capture_raises_after_bounded_kill_and_closes_pipes() -> None:
    process = _NeverCaptured()

    with pytest.raises(RuntimeError, match="could not be reaped"):
        runner.bounded_output(process, grace_seconds=0.01)

    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "{}",
        '{"elapsed_ms": 1, "rss_raw": 2}',
        '{"elapsed_ms": 1, "rss_raw": 2, "platform": "Plan9"}',
        "not-json",
    ],
)
def test_rejection_child_metrics_fail_closed(payload: str) -> None:
    with pytest.raises(ValueError, match="rejection child"):
        runner.rejection_results_from_output(payload)


def test_rejection_rss_conversion_uses_child_platform() -> None:
    darwin = json.dumps({"elapsed_ms": 1, "rss_raw": 64, "platform": "Darwin"})
    linux = json.dumps({"elapsed_ms": 1, "rss_raw": 64, "platform": "Linux"})

    assert runner.rejection_results_from_output(darwin)[1].actual == 64
    assert runner.rejection_results_from_output(linux)[1].actual == 64 * 1024


def test_real_oversized_trace_is_rejected_in_the_measurement_child(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert runner._rejection_child() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["elapsed_ms"] >= 0
    assert payload["rss_raw"] > 0
    assert payload["platform"] in {"Darwin", "Linux"}


def test_expected_rejection_fails_if_reader_accepts_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assets_module, "_read_file", lambda *arguments: b"accepted")

    with pytest.raises(RuntimeError, match="over-limit recorder trace was accepted"):
        runner._read_expected_rejection(0, "trace.json")


def test_ready_reader_accepts_only_loopback_cli_banner() -> None:
    valid = subprocess.Popen(
        (sys.executable, "-c", "print('Agentic Saga recorder: http://127.0.0.1:4321')"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    invalid = subprocess.Popen(
        (sys.executable, "-c", "print('ready on an external host')"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert runner._read_ready(valid) == "http://127.0.0.1:4321"
        with pytest.raises(RuntimeError, match="valid ready URL"):
            runner._read_ready(invalid)
    finally:
        runner.stop_process(valid, grace_seconds=0.1)
        runner.stop_process(invalid, grace_seconds=0.1)


def test_ready_reader_rejects_missing_stdout() -> None:
    process = _StubbornProcess()
    process.stdout = None  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="stdout is unavailable"):
        runner._read_ready(process)  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [{}, {"samples_ms": "bad"}, {"samples_ms": [1, "bad"]}])
def test_browser_samples_reject_invalid_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="invalid samples"):
        runner._browser_samples(payload)


def test_browser_samples_accept_numbers() -> None:
    assert runner._browser_samples({"samples_ms": [1, 2.5]}) == (1.0, 2.5)


def test_browser_result_parses_written_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def packaged(_cli: Path, output: Path) -> subprocess.CompletedProcess[str]:
        output.write_text(json.dumps({"samples_ms": list(range(1, 11))}))
        return subprocess.CompletedProcess(("packaged",), 0, "", "")

    monkeypatch.setattr(runner, "_run_packaged_browser", packaged)

    result = runner._browser_result(Path("/unused"))

    assert result.actual == 10
    assert result.p50 == 5.5


def test_browser_result_reports_failed_packaged_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = subprocess.CompletedProcess(("packaged",), 2, "", "browser failed")
    monkeypatch.setattr(runner, "_run_packaged_browser", lambda *_arguments: failed)

    with pytest.raises(RuntimeError, match="browser failed"):
        runner._browser_result(Path("/unused"))


def test_memory_result_parses_rss_and_rejects_missing_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_checked", lambda *_arguments: "WORKLOAD_RSS=64\n")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert runner._representative_memory_result().actual == 64 * 1024

    monkeypatch.setattr(runner, "_checked", lambda *_arguments: "no measurement\n")
    with pytest.raises(RuntimeError, match="did not report peak RSS"):
        runner._representative_memory_result()


def test_wheel_cli_uses_explicit_installed_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = tmp_path / "agentic-saga"
    cli.write_text("executable")
    monkeypatch.setenv("AGENTIC_SAGA_CLI", str(cli))
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", str(tmp_path / "missing"))

    assert runner._wheel_cli(tmp_path / "workspace") == cli.resolve()


def _shared_release_artifacts(root: Path, wheel_count: int = 1) -> Path:
    root.mkdir()
    (root / "runtime-requirements.txt").write_text("locked")
    for index in range(wheel_count):
        (root / f"agentic_saga-{index}-py3-none-any.whl").touch()
    return root


def _configure_shared_artifacts(
    monkeypatch: pytest.MonkeyPatch, artifacts: Path, wheelhouse: Path
) -> None:
    monkeypatch.delenv("AGENTIC_SAGA_CLI", raising=False)
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", str(artifacts))
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_WHEELHOUSE", str(wheelhouse))


def _command_recorder(commands: list[tuple[str, ...]]) -> Callable[[tuple[str, ...], Path], str]:
    def checked(command: tuple[str, ...], cwd: Path = runner.ROOT) -> str:
        del cwd
        commands.append(command)
        return ""

    return checked


def _assert_shared_installs(
    commands: list[tuple[str, ...]], artifacts: Path, wheelhouse: Path
) -> None:
    installs = tuple(command for command in commands if command[:3] == ("uv", "pip", "install"))
    assert len(installs) == 2
    assert all(
        "--no-index" in command and str(wheelhouse.resolve()) in command for command in installs
    )
    assert str((artifacts / "runtime-requirements.txt").resolve()) in installs[0]
    assert str(next(artifacts.glob("*.whl")).resolve()) in installs[1]


def test_wheel_cli_installs_the_shared_release_wheel_without_building(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _shared_release_artifacts(tmp_path / "release")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    commands: list[tuple[str, ...]] = []
    _configure_shared_artifacts(monkeypatch, artifacts, wheelhouse)
    monkeypatch.setattr(runner, "_checked", _command_recorder(commands))

    runner._wheel_cli(workspace)

    assert not any(command[:2] in {("uv", "build"), ("uv", "export")} for command in commands)
    _assert_shared_installs(commands, artifacts, wheelhouse)


@pytest.mark.parametrize("wheel_count", [0, 2])
def test_wheel_cli_rejects_nonexact_shared_wheel_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wheel_count: int
) -> None:
    artifacts = _shared_release_artifacts(tmp_path / "release", wheel_count)
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", str(artifacts))

    with pytest.raises(ValueError, match="exactly one wheel"):
        runner._wheel_cli(tmp_path / "workspace")


def test_wheel_cli_rejects_shared_artifacts_without_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _shared_release_artifacts(tmp_path / "release")
    (artifacts / "runtime-requirements.txt").unlink()
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", str(artifacts))

    with pytest.raises(ValueError, match="runtime requirements"):
        runner._wheel_cli(tmp_path / "workspace")


def test_wheel_cli_rejects_missing_shared_artifact_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", str(tmp_path / "missing"))

    with pytest.raises(ValueError, match="release artifacts"):
        runner._wheel_cli(tmp_path / "workspace")


def test_wheel_cli_rejects_nondirectory_shared_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_file = tmp_path / "release"
    artifact_file.touch()
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", str(artifact_file))

    with pytest.raises(ValueError, match="release artifacts"):
        runner._wheel_cli(tmp_path / "workspace")


def test_wheel_cli_rejects_symlinked_shared_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _shared_release_artifacts(tmp_path / "target")
    link = tmp_path / "release"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", str(link))

    with pytest.raises(ValueError, match="release artifacts"):
        runner._wheel_cli(tmp_path / "workspace")


def test_wheel_cli_rejects_shared_artifacts_through_symlinked_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    _shared_release_artifacts(target / "release")
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_ARTIFACTS", str(alias / "release"))

    with pytest.raises(ValueError, match="release artifacts"):
        runner._wheel_cli(tmp_path / "workspace")


def test_wheel_cli_installs_only_from_explicit_release_wheelhouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a verified release wheelhouse and an otherwise empty measurement workspace.
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    commands: list[tuple[str, ...]] = []
    monkeypatch.delenv("AGENTIC_SAGA_CLI", raising=False)
    monkeypatch.setenv("AGENTIC_SAGA_RELEASE_WHEELHOUSE", str(wheelhouse))

    def checked(command: tuple[str, ...], cwd: Path = runner.ROOT) -> str:
        del cwd
        commands.append(command)
        if command[:2] == ("uv", "build"):
            (workspace / "wheel" / "agentic_saga-0.1.0-py3-none-any.whl").touch()
        return ""

    monkeypatch.setattr(runner, "_checked", checked)

    # When the release measurement prepares its isolated wheel CLI.
    runner._wheel_cli(workspace)

    # Then every install is index-free and resolves dependencies only from that wheelhouse.
    installs = tuple(command for command in commands if command[:3] == ("uv", "pip", "install"))
    assert len(installs) == len(("dependencies", "project"))
    assert all("--no-index" in command for command in installs)
    assert all(
        command[command.index("--find-links") + 1] == str(wheelhouse.resolve())
        for command in installs
    )


def test_rendered_dirty_node_26_report_cannot_say_pass() -> None:
    identity = EnvironmentIdentity(
        "d" * 40, "dirty", "TestOS", "TestCPU", "Python 3.12.9", "v26.0.0", "11.5.0"
    )
    report = ReleaseReport(identity, (evaluate("sdk_retries", 0),))

    rendered = runner.render_report(report)

    assert f"commit: {'d' * 40}" in rendered
    assert "python: Python 3.12.9" in rendered
    assert "node: v26.0.0" in rendered
    assert "pnpm: 11.5.0" in rendered
    assert "release_environment: FAIL" in rendered
    assert "overall: FAIL" in rendered
    assert "node must be 24.x" in rendered


def test_rendered_complete_report_includes_ranges_and_timing() -> None:
    rendered = runner.render_report(_passing_report())

    assert "execution_turn_limit: PASS" in rendered
    assert "materialization_ms: PASS" in rendered
    assert "p50=500.000 p95=500.000 max=500.000" in rendered
    assert "release_environment: PASS" in rendered
    assert rendered.endswith("overall: PASS\n")


def test_quality_rows_are_emitted_only_after_executed_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    def checked(command: tuple[str, ...], cwd: Path = runner.ROOT) -> str:
        del cwd
        commands.append(command)
        return "passed"

    python = {
        "files": {
            "src/agentic_saga/kernel/runtime.py": {
                "summary": {"covered_branches": 9, "num_branches": 10}
            }
        }
    }
    release = {
        "files": {
            "scripts/release_runner.py": {"summary": {"covered_branches": 19, "num_branches": 20}}
        }
    }
    frontend = {"total": {"branches": {"covered": 9, "total": 10}}}
    monkeypatch.setattr(runner, "_checked", checked)
    monkeypatch.setattr(runner, "_pnpm_command", lambda *arguments: ("pnpm", *arguments))
    monkeypatch.setattr(runner, "_frontend_coverage", lambda: frontend)
    monkeypatch.setattr(
        runner, "_read_json", lambda path: release if "release-scripts" in path.name else python
    )

    results = {result.name: result for result in runner._quality_results()}

    assert commands == [("uv", "run", "poe", "gate"), ("pnpm", "gate")]
    assert results["python_complexity_grade_a"].passed
    assert results["browser_behavior_checks"].passed


def test_failed_quality_gate_emits_no_quality_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(_command: tuple[str, ...], _cwd: Path = runner.ROOT) -> str:
        raise RuntimeError("quality gate failed")

    monkeypatch.setattr(runner, "_checked", failed)

    with pytest.raises(RuntimeError, match="quality gate failed"):
        runner._quality_results()


def test_offline_scenario_p95_tolerates_only_one_outlier() -> None:
    name = "offline_lost-response_p95_ms"
    one_outlier = (*([100.0] * 19), 2_100.0)
    two_outliers = (*([100.0] * 18), 2_100.0, 2_200.0)

    result = runner.timing_result(name, one_outlier)

    assert BUDGETS[name].samples == 20
    assert (result.actual, result.maximum, result.passed) == (100.0, 2_100.0, True)
    assert not runner.timing_result(name, two_outliers).passed


def test_offline_scenario_measurement_uses_fresh_in_process_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directories: list[Path] = []

    async def run_scenario(name: str, directory: Path) -> object:
        directories.append(directory)
        assert name == "happy-path"
        return object()

    def reject_subprocess(_command: tuple[str, ...], _cwd: Path = runner.ROOT) -> str:
        pytest.fail("offline runtime measurement must not launch a CLI subprocess")

    monkeypatch.setattr(runner, "run_scenario", run_scenario, raising=False)
    monkeypatch.setattr(runner, "_checked", reject_subprocess)

    result = runner._scenario_result("happy-path")

    assert result.samples == 20
    assert len(set(directories)) == 20
    assert all(not directory.exists() for directory in directories)


def test_materialization_uses_exact_sample_contract(tmp_path: Path) -> None:
    samples = runner.measure_samples(25, lambda: None)

    result = runner.timing_result("materialization_ms", samples)

    assert result.samples == 25
    assert result.temperature == "fresh-destination"

    with pytest.raises(ValueError, match="exactly 25 samples"):
        runner.timing_result("materialization_ms", samples[:-1])


def test_release_orchestrator_rejects_arguments_before_measuring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        orchestrator, "measure_release", lambda: pytest.fail("measurement must not start")
    )

    assert orchestrator.main(("unexpected",)) == 2


def test_release_orchestrator_accepts_only_quality_proof_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = tmp_path / "proof.json"
    received: list[Path | None] = []
    monkeypatch.setattr(
        orchestrator,
        "measure_release",
        _recording_measurement(received),
    )

    assert orchestrator.main(("--quality-proof", str(proof))) == 0
    assert received == [proof]
    assert orchestrator.main(("--quality-proof",)) == 2


def test_release_orchestrator_renders_complete_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orchestrator, "measure_release", _passing_report)

    assert orchestrator.main(()) == 0
    assert capsys.readouterr().out.endswith("overall: PASS\n")


def test_release_orchestrator_returns_failure_for_failed_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _passing_report()
    failed = evaluate("release_scripts_branch_coverage_percent", 89)
    index = next(index for index, item in enumerate(report.results) if item.name == failed.name)
    results = (*report.results[:index], failed, *report.results[index + 1 :])
    monkeypatch.setattr(
        orchestrator,
        "measure_release",
        lambda: ReleaseReport(report.environment, results),
    )

    assert orchestrator.main(()) == 1


def test_release_orchestrator_reports_measurement_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failed() -> ReleaseReport:
        raise RuntimeError("evidence unavailable")

    monkeypatch.setattr(orchestrator, "measure_release", failed)

    assert orchestrator.main(()) == 2
    assert "release measurement failed: evidence unavailable" in capsys.readouterr().err


def test_release_orchestrator_direct_script_path_rejects_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", [str(Path(orchestrator.__file__)), "unexpected"])

    with pytest.raises(SystemExit, match="2"):
        runpy.run_path(str(Path(orchestrator.__file__)), run_name="__main__")
