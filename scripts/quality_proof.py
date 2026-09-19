from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)

from scripts.release_contract import BudgetResult, branch_percent, evaluate, frontend_branch_percent

ROOT: Final[Path] = Path(__file__).parents[1]
_SCHEMA_VERSION: Final[int] = 1
_COMPLETED_CHECKS: Final[tuple[str, str]] = ("python-gate", "frontend-gate")
_RESULT_NAMES: Final[tuple[str, ...]] = (
    "core_branch_coverage_percent",
    "release_scripts_branch_coverage_percent",
    "frontend_branch_coverage_percent",
    "python_complexity_grade_a",
    "browser_behavior_checks",
)
_SCHEMA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "source_commit",
        "python_version",
        "input_digests",
        "evidence_digests",
        "completed_checks",
        "results",
    }
)
_INPUT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "pyproject_sha256",
        "uv_lock_sha256",
        "frontend_package_json_sha256",
        "pnpm_lock_sha256",
    }
)
_EVIDENCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "python_coverage_sha256",
        "release_coverage_sha256",
        "frontend_coverage_sha256",
    }
)
_RESULT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "actual",
        "limit",
        "unit",
        "samples",
        "temperature",
        "passed",
        "p50",
        "maximum",
        "comparison",
        "lower",
    }
)
_COMMIT: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}")


class ProofResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: StrictStr
    actual: StrictFloat
    limit: StrictFloat
    unit: StrictStr
    samples: StrictInt
    temperature: StrictStr
    passed: StrictBool
    p50: StrictFloat | None
    maximum: StrictFloat | None
    comparison: Literal["<=", ">=", "==", "range"]
    lower: StrictFloat | None


class QualityProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: StrictInt
    source_commit: StrictStr
    python_version: StrictStr
    input_digests: Mapping[StrictStr, StrictStr]
    evidence_digests: Mapping[StrictStr, StrictStr]
    completed_checks: tuple[StrictStr, ...]
    results: tuple[ProofResult, ...]


def write_quality_proof(path: Path) -> None:
    atomic_write_json(path, quality_proof_payload())


def quality_proof_payload() -> Mapping[str, object]:
    return _payload(_current_proof())


def quality_results_from_proof(path: Path) -> tuple[BudgetResult, ...]:
    proof = parse_quality_proof(read_json_object(path))
    validate_quality_proof(proof)
    return _proof_results(proof)


def parse_quality_proof(payload: Mapping[str, object]) -> QualityProof:
    _require_schema_keys(payload)
    try:
        return QualityProof.model_validate(_model_payload(payload))
    except ValidationError as error:
        raise ValueError("invalid quality proof schema") from error


def _model_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    checks = payload["completed_checks"]
    results = payload["results"]
    if not isinstance(checks, list) or not isinstance(results, list):
        raise ValueError("quality proof schema keys differ")
    return {**payload, "completed_checks": tuple(checks), "results": tuple(results)}


def validate_quality_proof(proof: QualityProof) -> None:
    _validate_identity(proof)
    _validate_evidence(proof)
    _validate_completed_checks(proof)
    _validate_results(proof)


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(path)
    try:
        temporary.write_bytes(_canonical_json(payload))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json_object(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid quality proof JSON: {path}") from error
    if not isinstance(value, Mapping):
        raise ValueError(f"expected quality proof object: {path}")
    return value


def _current_proof() -> QualityProof:
    commit = _source_commit()
    return QualityProof(
        schema_version=_SCHEMA_VERSION,
        source_commit=commit,
        python_version=_python_version(),
        input_digests=_input_digests(),
        evidence_digests=_evidence_digests(),
        completed_checks=_COMPLETED_CHECKS,
        results=tuple(_proof_result(result) for result in _expected_results()),
    )


def _payload(proof: QualityProof) -> Mapping[str, object]:
    return {
        "schema_version": proof.schema_version,
        "source_commit": proof.source_commit,
        "python_version": proof.python_version,
        "input_digests": dict(proof.input_digests),
        "evidence_digests": dict(proof.evidence_digests),
        "completed_checks": proof.completed_checks,
        "results": tuple(_result_payload(result) for result in proof.results),
    }


def _result_payload(result: ProofResult) -> Mapping[str, object]:
    return {
        "name": result.name,
        "actual": result.actual,
        "limit": result.limit,
        "unit": result.unit,
        "samples": result.samples,
        "temperature": result.temperature,
        "passed": result.passed,
        "p50": result.p50,
        "maximum": result.maximum,
        "comparison": result.comparison,
        "lower": result.lower,
    }


def _require_schema_keys(payload: Mapping[str, object]) -> None:
    _require_exact_keys(payload, _SCHEMA_KEYS)
    _require_nested_keys(payload, "input_digests", _INPUT_KEYS)
    _require_nested_keys(payload, "evidence_digests", _EVIDENCE_KEYS)
    _require_result_keys(payload)


def _require_result_keys(payload: Mapping[str, object]) -> None:
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("quality proof schema keys differ")
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError("quality proof schema keys differ")
        _require_exact_keys(result, _RESULT_KEYS)


def _require_nested_keys(
    payload: Mapping[str, object], name: str, expected: frozenset[str]
) -> None:
    value = payload.get(name)
    if not isinstance(value, Mapping):
        raise ValueError("quality proof schema keys differ")
    _require_exact_keys(value, expected)


def _require_exact_keys(payload: Mapping[str, object], expected: frozenset[str]) -> None:
    if frozenset(payload) != expected:
        raise ValueError("quality proof schema keys differ")


def _validate_identity(proof: QualityProof) -> None:
    values = (proof.source_commit, _source_commit(), proof.python_version, _python_version())
    if proof.schema_version != _SCHEMA_VERSION or values[0] != values[1] or values[2] != values[3]:
        raise ValueError("quality proof identity mismatch")
    if dict(proof.input_digests) != _input_digests():
        raise ValueError("quality proof identity mismatch")


def _validate_evidence(proof: QualityProof) -> None:
    if dict(proof.evidence_digests) != _evidence_digests():
        raise ValueError("quality proof evidence mismatch")


def _validate_completed_checks(proof: QualityProof) -> None:
    if proof.completed_checks != _COMPLETED_CHECKS:
        raise ValueError("quality proof completed checks mismatch")


def _validate_results(proof: QualityProof) -> None:
    if _proof_results(proof) != _expected_results():
        raise ValueError("quality proof evidence mismatch")


def _proof_results(proof: QualityProof) -> tuple[BudgetResult, ...]:
    _require_result_names(proof.results)
    return tuple(_budget_result(result) for result in proof.results)


def _require_result_names(results: tuple[ProofResult, ...]) -> None:
    if tuple(result.name for result in results) != _RESULT_NAMES:
        raise ValueError("quality proof results differ")


def _budget_result(result: ProofResult) -> BudgetResult:
    if not math.isfinite(result.actual):
        raise ValueError("quality proof has non-finite result")
    budget = BudgetResult(**result.model_dump())
    if budget != evaluate(budget.name, budget.actual):
        raise ValueError("quality proof result metadata mismatch")
    return budget


def _expected_results() -> tuple[BudgetResult, ...]:
    return (
        evaluate("core_branch_coverage_percent", _python_coverage()),
        evaluate("release_scripts_branch_coverage_percent", _release_coverage()),
        evaluate("frontend_branch_coverage_percent", _frontend_coverage()),
        evaluate("python_complexity_grade_a", 1),
        evaluate("browser_behavior_checks", 1),
    )


def _python_coverage() -> float:
    return branch_percent(_evidence_json(".coverage.json"), "/agentic_saga/")


def _release_coverage() -> float:
    return branch_percent(_evidence_json(".coverage-release-scripts.json"), "scripts/")


def _frontend_coverage() -> float:
    evidence = _evidence_json("web/flight-recorder/coverage/coverage-summary.json")
    return frontend_branch_percent(evidence)


def _evidence_json(relative: str) -> Mapping[str, object]:
    return read_json_object(ROOT / relative)


def _proof_result(result: BudgetResult) -> ProofResult:
    return ProofResult(
        name=result.name,
        actual=float(result.actual),
        limit=float(result.limit),
        unit=result.unit,
        samples=result.samples,
        temperature=result.temperature,
        passed=result.passed,
        p50=result.p50,
        maximum=result.maximum,
        comparison=result.comparison,
        lower=result.lower,
    )


def _input_digests() -> dict[str, str]:
    return _digests(
        (
            ("pyproject_sha256", "pyproject.toml"),
            ("uv_lock_sha256", "uv.lock"),
            ("frontend_package_json_sha256", "web/flight-recorder/package.json"),
            ("pnpm_lock_sha256", "web/flight-recorder/pnpm-lock.yaml"),
        )
    )


def _evidence_digests() -> dict[str, str]:
    return _digests(
        (
            ("python_coverage_sha256", ".coverage.json"),
            ("release_coverage_sha256", ".coverage-release-scripts.json"),
            ("frontend_coverage_sha256", "web/flight-recorder/coverage/coverage-summary.json"),
        )
    )


def _digests(named_paths: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return {name: _sha256(ROOT / relative) for name, relative in named_paths}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_commit() -> str:
    commit = _git_output(("rev-parse", "HEAD"))
    if _COMMIT.fullmatch(commit) is None or _git_output(("status", "--porcelain=v1")):
        raise ValueError("quality proof identity mismatch")
    return commit


def _git_output(arguments: tuple[str, ...]) -> str:
    result = subprocess.run(  # noqa: S603 - fixed git subcommands
        (_git_executable(), *arguments), cwd=ROOT, capture_output=True, check=False, text=True
    )
    if result.returncode != 0:
        raise ValueError("quality proof identity mismatch")
    return result.stdout.strip()


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("quality proof identity mismatch")
    return executable


def _python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    rendered = json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return f"{rendered}\n".encode()


def _temporary_path(path: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    return Path(name)
