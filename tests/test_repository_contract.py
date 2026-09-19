from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import agentic_saga

ROOT = Path(__file__).parents[1]
_LEGACY = ("execution", "kernel", "storage")


def test_project_has_one_temporal_runtime() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]

    assert any(item.startswith("temporalio") for item in dependencies)
    assert not any(tuple((ROOT / "src" / "agentic_saga" / name).glob("*.py")) for name in _LEGACY)
    assert not {"SagaDefinition", "SagaRuntime", "compose_runtime"} & set(agentic_saga.__all__)


def test_gate_runs_temporal_tests_and_release_proof() -> None:
    poe = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["poe"]
    tasks = poe["tasks"]

    assert poe["env"]["COVERAGE_FILE"] == ".coverage-core"
    assert tasks["test-temporal"] == (
        "pytest -m temporal --force-enable-socket --cov=src/agentic_saga --cov-branch --cov-append"
    )
    assert "test-temporal" in tasks["gate"]
    assert "release-script-test" in tasks["gate"]
    assert "release-script-coverage" in tasks["gate"]


def test_release_build_is_locked_hashed_and_nonpublishing() -> None:
    scripts = tuple(
        (ROOT / "scripts" / name).read_text()
        for name in (
            "build_release_artifacts.sh",
            "build_runtime_wheelhouse.sh",
            "verify_release_candidate.sh",
        )
    )
    joined = "\n".join(scripts)

    assert "uv export --locked" in scripts[0]
    assert "--require-hashes" in joined
    assert "SHA256SUMS" in joined
    assert "SOURCE_COMMIT" in joined
    assert not re.search(r"\b(?:twine|uv)\s+publish\b", joined)


def test_installed_release_verifies_temporal_public_surface() -> None:
    source = (ROOT / "scripts" / "verify_release_candidate.sh").read_text()

    assert "import agentic_saga, temporalio" in source
    assert "agentic_saga.WorkflowState" in source
    assert "agentic_saga.start_saga" in source
    assert "temporal.build_worker" in source
    assert "temporal.connect_client" in source
    assert "temporal.project_run_trace" in source
    assert 'not hasattr(agentic_saga, "SagaRuntime")' in source


def test_dagger_keeps_pinned_industry_strength_ci_contract() -> None:
    path = ROOT / ".dagger" / "src" / "agentic_saga_ci" / "main.py"
    source = path.read_text()
    tree = ast.parse(source)
    methods = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert {"ci", "security"} <= methods
    assert "dag.foundation().guard" in source
    assert "commit(commit_sha)" in source
    assert source.count("@sha256:") >= 4
    assert ":latest" not in source


def test_release_measurement_has_no_legacy_runtime_claims() -> None:
    paths = (
        ROOT / "scripts" / "release_runner.py",
        ROOT / "scripts" / "release_contract.py",
        ROOT / "scripts" / "run_mutation_gate.py",
    )
    source = "\n".join(path.read_text().lower() for path in paths)

    assert "sqlite" not in source
    assert "agentic_saga.kernel" not in source
    assert "agentic_saga.storage" not in source
    assert "temporal_runtime_dependency" in source


def test_mutation_gate_uses_workflow_neutral_name() -> None:
    tasks = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["poe"]["tasks"]
    stale_name = "_".join(("run", "kernel", "mutation", "gate.py"))

    assert tasks["mutation"] == "python scripts/run_mutation_gate.py"
    assert (ROOT / "scripts" / "run_mutation_gate.py").is_file()
    assert not (ROOT / "scripts" / stale_name).exists()
