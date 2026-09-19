from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import scripts.quality_proof as proof
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

_EXPECTED = {
    "core_branch_coverage_percent",
    "release_scripts_branch_coverage_percent",
    "frontend_branch_coverage_percent",
    "python_complexity_grade_a",
    "browser_behavior_checks",
    "temporal_runtime_dependency",
    "legacy_runtime_absent",
    "public_api_imports",
    "temporal_tests_passed",
    "package_build_install",
    "recorder_cli_ready",
}


def _identity(**changes: str) -> EnvironmentIdentity:
    values = {
        "commit": "a" * 40,
        "tree_state": "clean",
        "os": "test",
        "cpu": "test",
        "python": "Python 3.13.5",
        "node": "v24.6.0",
        "pnpm": "11.5.0",
    }
    return EnvironmentIdentity(**(values | changes))


def _complete_report() -> ReleaseReport:
    return ReleaseReport(
        _identity(), tuple(evaluate(name, spec.limit) for name, spec in BUDGETS.items())
    )


def test_contract_contains_only_current_release_proofs() -> None:
    assert set(BUDGETS) == _EXPECTED
    assert not any(name.startswith(("sqlite", "lease", "outbox")) for name in BUDGETS)


def test_evaluate_fails_closed_for_non_finite_or_below_floor() -> None:
    assert evaluate("core_branch_coverage_percent", 90).passed
    assert not evaluate("core_branch_coverage_percent", 89.99).passed
    assert not evaluate("core_branch_coverage_percent", float("nan")).passed


def test_branch_coverage_aggregates_matching_files() -> None:
    payload = {
        "files": {
            "src/agentic_saga/a.py": {"summary": {"covered_branches": 8, "num_branches": 10}},
            "src/agentic_saga/b.py": {"summary": {"covered_branches": 10, "num_branches": 10}},
            "examples/demo.py": {"summary": {"covered_branches": 0, "num_branches": 10}},
        }
    }

    assert branch_percent(payload, "/agentic_saga/") == 90


def test_branch_coverage_ignores_valid_branchless_modules() -> None:
    payload = {
        "files": {
            "src/agentic_saga/contracts.py": {
                "summary": {"covered_branches": 9, "num_branches": 10}
            },
            "src/agentic_saga/__init__.py": {"summary": {"covered_branches": 0, "num_branches": 0}},
        }
    }

    assert branch_percent(payload, "/agentic_saga/") == 90


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"files": {}},
        {"files": {"a.py": {"summary": {}}}},
        {"files": {"a.py": {"summary": []}}},
        {"files": {"a.py": {"summary": {"covered_branches": 1, "num_branches": 0}}}},
    ],
)
def test_branch_coverage_rejects_missing_evidence(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        branch_percent(payload, "a.py")


def test_frontend_coverage_is_strict() -> None:
    payload = {"total": {"branches": {"covered": 9, "total": 10}}}

    assert frontend_branch_percent(payload) == 90
    with pytest.raises(ValueError):
        frontend_branch_percent({})


def test_environment_requires_exact_release_toolchain() -> None:
    assert release_environment_errors(_identity()) == ()
    errors = release_environment_errors(_identity(tree_state="dirty", pnpm="11.4.0"))
    assert "tree must be clean" in errors
    assert "pnpm must be 11.5.0" in errors


def test_complete_report_accepts_exact_results() -> None:
    require_complete_report(_complete_report())


def test_complete_report_rejects_missing_duplicate_or_tampered_results() -> None:
    report = _complete_report()
    with pytest.raises(ValueError, match="names differ"):
        require_complete_report(replace(report, results=report.results[:-1]))
    with pytest.raises(ValueError, match="duplicate"):
        require_complete_report(replace(report, results=(report.results[0],) * len(report.results)))
    tampered = replace(report.results[0], limit=0)
    with pytest.raises(ValueError, match="published budget"):
        require_complete_report(replace(report, results=(tampered, *report.results[1:])))


def _proof_results() -> tuple[BudgetResult, ...]:
    names = (
        "core_branch_coverage_percent",
        "release_scripts_branch_coverage_percent",
        "frontend_branch_coverage_percent",
        "python_complexity_grade_a",
        "browser_behavior_checks",
    )
    return tuple(evaluate(name, BUDGETS[name].limit) for name in names)


def _patch_proof_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proof, "_source_commit", lambda: "a" * 40)
    monkeypatch.setattr(proof, "_python_version", lambda: "3.13")
    monkeypatch.setattr(
        proof,
        "_input_digests",
        lambda: {name: "a" * 64 for name in proof._INPUT_KEYS},
    )
    monkeypatch.setattr(
        proof,
        "_evidence_digests",
        lambda: {name: "b" * 64 for name in proof._EVIDENCE_KEYS},
    )
    monkeypatch.setattr(proof, "_expected_results", _proof_results)


def _json_proof_payload() -> dict[str, object]:
    value = json.loads(json.dumps(proof.quality_proof_payload()))
    assert isinstance(value, dict)
    return value


def test_quality_proof_round_trip_is_identity_and_evidence_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_proof_identity(monkeypatch)
    path = tmp_path / "quality-proof.json"

    proof.write_quality_proof(path)
    results = proof.quality_results_from_proof(path)

    assert results == _proof_results()
    assert path.read_bytes().endswith(b"\n")
    assert not tuple(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "identity mismatch"),
        ("source_commit", "b" * 40, "identity mismatch"),
        ("python_version", "3.12", "identity mismatch"),
        (
            "input_digests",
            {name: "0" * 64 for name in proof._INPUT_KEYS},
            "identity mismatch",
        ),
        (
            "evidence_digests",
            {name: "0" * 64 for name in proof._EVIDENCE_KEYS},
            "evidence mismatch",
        ),
        ("completed_checks", ["python-gate"], "completed checks mismatch"),
    ],
)
def test_quality_proof_rejects_identity_or_evidence_tampering(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    _patch_proof_identity(monkeypatch)
    payload = _json_proof_payload()
    payload[field] = value

    parsed = proof.parse_quality_proof(payload)
    with pytest.raises(ValueError, match=message):
        proof.validate_quality_proof(parsed)


def test_quality_proof_rejects_result_tampering(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_proof_identity(monkeypatch)
    payload = _json_proof_payload()
    raw_results = payload["results"]
    assert isinstance(raw_results, list)
    results = [dict(item) for item in raw_results]
    results[0]["actual"] = 100.0
    payload["results"] = results

    with pytest.raises(ValueError, match="evidence mismatch"):
        proof.validate_quality_proof(proof.parse_quality_proof(payload))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": 1},
    ],
)
def test_quality_proof_requires_exact_schema(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="schema keys differ"):
        proof.parse_quality_proof(payload)


def test_quality_proof_rejects_invalid_types_after_shape_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_proof_identity(monkeypatch)
    payload = _json_proof_payload()
    payload["schema_version"] = "1"
    with pytest.raises(ValueError, match="invalid quality proof schema"):
        proof.parse_quality_proof(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("completed_checks", "python-gate"),
        ("results", "not-a-list"),
        ("results", [None]),
        ("input_digests", []),
    ],
)
def test_quality_proof_requires_list_checks_results_and_digest_maps(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _patch_proof_identity(monkeypatch)
    payload = _json_proof_payload()
    payload[field] = value
    with pytest.raises(ValueError, match="schema keys differ"):
        proof.parse_quality_proof(payload)


def test_quality_proof_rejects_unknown_or_reordered_result_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_proof_identity(monkeypatch)
    payload = _json_proof_payload()
    results = payload["results"]
    assert isinstance(results, list)
    changed = [dict(result) for result in results]
    changed[0]["name"] = "unexpected-result"
    payload["results"] = changed

    parsed = proof.parse_quality_proof(payload)
    with pytest.raises(ValueError, match="results differ"):
        proof.validate_quality_proof(parsed)


def test_quality_proof_rejects_non_object_and_invalid_json(tmp_path: Path) -> None:
    array = tmp_path / "array.json"
    array.write_text("[]")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{")

    with pytest.raises(ValueError, match="expected quality proof object"):
        proof.read_json_object(array)
    with pytest.raises(ValueError, match="invalid quality proof JSON"):
        proof.read_json_object(invalid)


def test_quality_proof_rejects_non_finite_or_wrong_result_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = proof._proof_result(_proof_results()[0])
    with pytest.raises(ValueError, match="non-finite"):
        proof._budget_result(result.model_copy(update={"actual": float("nan")}))
    with pytest.raises(ValueError, match="metadata mismatch"):
        proof._budget_result(result.model_copy(update={"limit": 0.0}))


def test_quality_proof_digest_and_evidence_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence = tmp_path / ".coverage.json"
    evidence.write_text(
        json.dumps(
            {
                "files": {
                    "src/agentic_saga/a.py": {
                        "summary": {"covered_branches": 9, "num_branches": 10}
                    }
                }
            }
        )
    )
    monkeypatch.setattr(proof, "ROOT", tmp_path)

    assert proof._python_coverage() == 90
    assert proof._digests((("coverage", ".coverage.json"),))["coverage"] == proof._sha256(evidence)


def test_quality_proof_git_identity_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="identity mismatch"):
        proof._git_executable()


@pytest.mark.parametrize(
    "outputs",
    [("not-a-commit", ""), ("a" * 40, "uncommitted change")],
)
def test_quality_proof_rejects_invalid_or_dirty_git_identity(
    monkeypatch: pytest.MonkeyPatch,
    outputs: tuple[str, str],
) -> None:
    values = iter(outputs)
    monkeypatch.setattr(proof, "_git_output", lambda _: next(values))

    with pytest.raises(ValueError, match="identity mismatch"):
        proof._source_commit()


def test_quality_proof_accepts_clean_full_git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter(("a" * 40, ""))
    monkeypatch.setattr(proof, "_git_output", lambda _: next(values))

    assert proof._source_commit() == "a" * 40


def test_quality_proof_git_command_fails_closed_and_strips_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proof, "_git_executable", lambda: "git")
    failed = subprocess.CompletedProcess(("git",), 1, "", "failure")
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: failed)
    with pytest.raises(ValueError, match="identity mismatch"):
        proof._git_output(("rev-parse", "HEAD"))

    passed = subprocess.CompletedProcess(("git",), 0, "a" * 40 + "\n", "")
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: passed)
    assert proof._git_output(("rev-parse", "HEAD")) == "a" * 40


def test_quality_proof_resolves_git_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/git")

    assert proof._git_executable() == "/usr/bin/git"
