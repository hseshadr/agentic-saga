import ast
import importlib
import inspect
import json
import pkgutil
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from ruamel.yaml import YAML

import agentic_saga
from agentic_saga.agents import __all__ as agents_all
from agentic_saga.agents import build_openrouter_driver
from agentic_saga.contracts import __all__ as contracts_all
from agentic_saga.demo import RecorderServer
from agentic_saga.demo import __all__ as demo_all
from agentic_saga.evidence import __all__ as evidence_all
from agentic_saga.execution import ReconciliationResult
from agentic_saga.execution import __all__ as execution_all
from agentic_saga.kernel import __all__ as kernel_all
from agentic_saga.storage import __all__ as storage_all

ROOT = Path(__file__).parents[1]

_ROOT_FACADE = (
    "SagaContext",
    "SagaDefinition",
    "SagaGoal",
    "SagaManifest",
    "SagaRuntime",
    "__version__",
    "compose_runtime",
    "load_saga_context",
)
_AGENTS_FACADE = ("DeepAgentsDriver", "OpenRouterSettings", "build_openrouter_driver")
_DEMO_FACADE = ("RecorderServer", "materialize_recorder_site", "serve_recorder")

_EXECUTION_FACADE = (
    "DispatchResult",
    "Dispatcher",
    "EmergencyUnwinder",
    "LeaseService",
    "Reconciler",
    "ReconciliationResult",
    "SagaRuntime",
    "compose_runtime",
)
_STORAGE_FACADE = ("SQLiteKernelStore",)


def _agentic_saga_modules() -> Iterator[ModuleType]:
    yield agentic_saga
    for info in pkgutil.walk_packages(agentic_saga.__path__, "agentic_saga."):
        yield importlib.import_module(info.name)


def _supported_objects() -> Iterator[tuple[str, object]]:
    seen: set[int] = set()
    for module in _agentic_saga_modules():
        for exported_name in getattr(module, "__all__", ()):
            exported = getattr(module, exported_name)
            if id(exported) in seen or not (
                inspect.isclass(exported) or inspect.isfunction(exported)
            ):
                continue
            seen.add(id(exported))
            yield f"{exported.__module__}.{exported.__qualname__}", exported


def _long_functions(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) or not node.body:
            continue
        end_line = node.body[-1].end_lineno
        if end_line is None:
            continue
        body_lines = end_line - node.body[0].lineno + 1
        if body_lines > 15:
            yield f"{path.relative_to(ROOT)}:{node.lineno} {node.name} ({body_lines})"


def _direct_docstring(exported: object) -> str | None:
    if inspect.isclass(exported):
        value = vars(exported).get("__doc__")
        return value if isinstance(value, str) else None
    return exported.__doc__ if inspect.isfunction(exported) else None


def _without_inline_comments(text: str) -> list[str]:
    return [line.split("#", 1)[0].rstrip() for line in text.splitlines()]


def _contains_sequence(text: str, expected: tuple[str, ...]) -> bool:
    lines = _without_inline_comments(text)
    width = len(expected)
    for index in range(len(lines) - width + 1):
        if lines[index : index + width] == list(expected):
            return True
    return False


def _ecosystem_block(config: str, ecosystem: str) -> str:
    lines = config.splitlines()
    marker = f"  - package-ecosystem: {ecosystem}"
    start = lines.index(marker)
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("  - package-ecosystem:")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_required_oss_files_exist() -> None:
    required = {
        "LICENSE",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "PROVENANCE.md",
        "CHANGELOG.md",
        "CITATION.cff",
        "QUICKSTART.md",
        "kernel-safety-contract.md",
    }
    root_files = {path.name for path in ROOT.iterdir()}
    docs_files = {path.name for path in (ROOT / "docs").iterdir()}
    assert required <= root_files | docs_files


def test_primary_public_entrypoints_explain_their_contracts() -> None:
    entrypoints = (
        agentic_saga.SagaDefinition,
        agentic_saga.SagaGoal,
        build_openrouter_driver,
        RecorderServer,
        ReconciliationResult,
    )
    assert all(inspect.getdoc(entrypoint) for entrypoint in entrypoints)


def test_every_supported_callable_and_class_explains_its_contract() -> None:
    missing = tuple(
        name for name, exported in _supported_objects() if not _direct_docstring(exported)
    )
    assert missing == ()


def test_production_functions_honor_the_documented_lean_limit() -> None:
    roots = (ROOT / "src", ROOT / "examples")
    paths = (path for root in roots for path in root.rglob("*.py"))
    violations = tuple(violation for path in paths for violation in _long_functions(path))
    assert violations == ()


def test_kernel_safety_contract_freezes_claims_and_assumptions() -> None:
    contract = (ROOT / "docs" / "kernel-safety-contract.md").read_text()
    required = (
        "Provides: deterministic authorization",
        "Does not provide: universal exactly-once effects",
        "## SQLite and filesystem assumptions",
        "## Executable evidence",
        "trusted application code",
        "external process, container, or service",
        "fresh clean-current-commit report",
        "surviving, untested, suspicious, timed-out, interrupted, or crashing selected mutant",
        "whole-repository mutation score",
        "`uv run poe mutation`",
    )
    assert all(value in contract for value in required)


def test_reader_entry_points_link_the_safety_contract() -> None:
    readme = (ROOT / "README.md").read_text()
    quickstart = (ROOT / "QUICKSTART.md").read_text()

    assert "[Kernel safety contract](docs/kernel-safety-contract.md)" in readme
    assert "[kernel safety contract](docs/kernel-safety-contract.md)" in quickstart


def test_mutation_gate_is_locked_and_machine_readable() -> None:
    config = (ROOT / "pyproject.toml").read_text()
    runner = ROOT / "scripts" / "run_kernel_mutation_gate.py"
    required = (
        '"mutmut>=3.6,<4"',
        "[tool.mutmut]",
        'source_paths = ["src/agentic_saga"]',
        "mutate_only_covered_lines = true",
        "timeout_multiplier = 4.0",
        "timeout_constant = 1.0",
        "use_setproctitle = false",
    )
    assert all(value in config for value in required)
    assert 'mutation = "python scripts/run_kernel_mutation_gate.py"' in config
    assert runner.exists()
    assert "selected safety-critical mutation score" in runner.read_text()
    assert "_MINIMUM_SCORE = 0.85" in runner.read_text()
    assert "mutants/" in (ROOT / ".gitignore").read_text().splitlines()


def test_gate_enforces_the_core_branch_coverage_floor() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    tasks = config["tool"]["poe"]["tasks"]

    assert tasks["core-coverage"] == (
        "python -m scripts.release_runner --check-python-coverage .coverage.json"
    )
    assert tasks["release-script-coverage"] == (
        "python -m scripts.release_runner --check-release-coverage .coverage-release-scripts.json"
    )
    assert tasks["release-script-test"]["env"] == {"COVERAGE_FILE": ".coverage-release-scripts"}
    assert tasks["gate"] == [
        "lint-check",
        "format-check",
        "typecheck",
        "typecheck-release",
        "complexity",
        "test",
        "core-coverage",
        "release-script-test",
        "release-script-coverage",
    ]
    assert tasks["artifacts"] == "bash scripts/build_release_artifacts.sh dist/release"
    assert tasks["release-candidate"] == ("bash scripts/verify_release_candidate.sh dist/release")


def test_agent_dependencies_are_optional_and_bdd_is_development_only() -> None:
    config = (ROOT / "pyproject.toml").read_text()
    expected = 'agent = ["deepagents>=0.7.13,<0.8", "langchain-openrouter>=0.2.8,<0.3"]'
    assert expected in config
    parsed = tomllib.loads(config)
    assert set(parsed["project"]["optional-dependencies"]) == {"agent"}
    assert "pytest-bdd>=8.1,<9" in parsed["dependency-groups"]["dev"]
    assert "pytest-bdd" not in parsed["project"]["dependencies"]


def test_python_support_matches_the_verified_matrix() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["project"]["requires-python"] == ">=3.12,<3.14"


def test_oss_metadata_and_contributor_routes_are_complete() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert {"agents", "saga-pattern", "distributed-transactions"} <= set(config["keywords"])
    assert {"Homepage", "Documentation", "Repository", "Issues"} <= set(config["urls"])
    readme = (ROOT / "README.md").read_text()
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    assert "actions/workflows/dagger.yml/badge.svg" in readme
    for value in ("Python 3.12 and 3.13", "pnpm@11.5.0 gate", "SECURITY.md", "CODE_OF_CONDUCT.md"):
        assert value in contributing


def test_readme_scopes_historical_dagger_proof() -> None:
    readme = (ROOT / "README.md").read_text()
    current = ("635974d51f87aa802886914be6a46bfd28518c66", "34727077866", "34727111885")
    stale = (
        "matching hosted CI evidence still must be recorded",
        "matching hosted CI run have not yet been recorded",
        "verified the clean exact main commit, including both supported",
    )
    assert all(value in readme for value in current)
    assert "not full release-matrix evidence" in readme
    assert all(value not in readme for value in stale)


def test_current_dagger_docs_name_the_repository_auth_secret() -> None:
    docs = (
        ROOT / "docs/superpowers/specs/2026-09-12-dagger-portfolio-integration-design.md",
        ROOT / "docs/superpowers/plans/2026-09-12-dagger-portfolio-integration.md",
    )
    contents = tuple(path.read_text() for path in docs)
    assert all("DAGGER_GIT_HTTP_AUTH_HEADER" in text for text in contents)
    assert all("scripts/measure_release.py" in text for text in contents)
    assert "Under each pinned Python 3.12 and 3.13" in contents[0]
    assert "The release-candidate script continues to own" not in contents[0]
    assert all("derived from `${{ github.token }}`" not in text for text in contents)


def test_default_development_gate_installs_optional_agent_dependencies() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    agent = set(config["project"]["optional-dependencies"]["agent"])
    development = set(config["dependency-groups"]["dev"])
    assert agent <= development


def test_subpackage_facades_export_only_current_consumers() -> None:
    assert tuple(agentic_saga.__all__) == _ROOT_FACADE
    assert tuple(agents_all) == _AGENTS_FACADE
    assert tuple(contracts_all) == ()
    assert tuple(demo_all) == _DEMO_FACADE
    assert tuple(evidence_all) == ()
    assert tuple(kernel_all) == ()
    assert tuple(execution_all) == _EXECUTION_FACADE
    assert tuple(storage_all) == _STORAGE_FACADE


def test_docs_name_shipped_ecommerce_and_source_recorder() -> None:
    # Given the repository documentation.
    readme = (ROOT / "README.md").read_text()
    quickstart = (ROOT / "QUICKSTART.md").read_text()
    agent_guide = (ROOT / "docs" / "agent-adapter.md").read_text()

    # When the private-development status is inspected.
    assert "Under private development" in readme
    assert "uv run --no-dev python -m examples.ecommerce.run" in quickstart
    assert "providers and agent-driven workflows remain planned" not in quickstart
    assert "Flight Recorder" in quickstart
    assert "`ToolCall`, `Finish`, `BeginCompensation`, or `Escalate`" in agent_guide
    assert "`AgentPlanningError`" in agent_guide
    assert "Planned: executable ecommerce providers" not in agent_guide


def test_readme_source_map_names_every_public_package_role() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "`src/agentic_saga/contracts/`" in readme
    assert "`src/agentic_saga/cli/`" in readme


def test_reader_quickstarts_skip_the_development_toolchain() -> None:
    reader_guides = (
        ROOT / "README.md",
        ROOT / "QUICKSTART.md",
        ROOT / "docs" / "flight-recorder.md",
    )
    for guide in reader_guides:
        content = guide.read_text()
        assert "uv run --no-dev agentic-saga demo" in content
        assert "uv run agentic-saga demo" not in content


def test_quickstart_runs_the_generic_kernel_proof() -> None:
    quickstart = (ROOT / "QUICKSTART.md").read_text()
    proof = ROOT / "tests" / "integration" / "test_kernel_end_to_end.py"
    command = "uv run pytest tests/integration/test_kernel_end_to_end.py -q"
    assert proof.exists()
    assert command in quickstart
    assert "generic durable effect" in quickstart


def test_ecommerce_example_has_a_runnable_source_map() -> None:
    guide = ROOT / "examples" / "ecommerce" / "README.md"
    assert guide.exists()
    text = guide.read_text()
    required = (
        "uv run --no-dev python -m examples.ecommerce.run",
        "demo.py",
        "domain.py",
        "provider.py",
        "saga.yaml",
        "tests/bdd/features/ecommerce_saga.feature",
        "The kernel owns",
    )
    assert all(value in text for value in required)


def test_ecommerce_live_eval_walkthrough_is_copy_pasteable_and_honest() -> None:
    guide = (ROOT / "examples" / "ecommerce" / "README.md").read_text()
    required = (
        "uv run python -m examples.ecommerce.eval --live",
        "RUN_LIVE_MODEL_EVALS=1",
        "OPENROUTER_API_KEY",
        "costs money",
        ".artifacts/eval/traces",
        "provider failure",
        "no universal exactly-once",
        "alternate warehouse",
        "tamper-evident integrity",
        "not authenticity",
    )
    assert all(value in guide for value in required)


def test_live_eval_plan_names_the_shipped_example_local_boundary() -> None:
    plan = (ROOT / "docs/superpowers/plans/2026-09-06-ecommerce-agents-evals.md").read_text()
    quickstart = (ROOT / "QUICKSTART.md").read_text()
    design = (ROOT / "docs/superpowers/specs/2026-09-06-agentic-saga-design.md").read_text()
    command = "uv run python -m examples.ecommerce.eval --live"
    assert command in plan
    assert command in quickstart
    assert command in design
    assert "provider transport retries are zero" in plan
    assert "returned identity, usage, and cost remain `null`" in plan
    assert "live ecommerce path remains planned" not in design


def test_generic_kernel_proof_has_no_ecommerce_fixture_vocabulary() -> None:
    proof = (ROOT / "tests" / "integration" / "test_kernel_end_to_end.py").read_text().lower()
    harness = (ROOT / "tests" / "support" / "kernel_harness.py").read_text().lower()
    domain_words = (
        "ecommerce",
        "payment",
        "charge",
        "refund",
        "order_",
        "amount_minor",
        "currency",
    )

    assert all(word not in proof for word in domain_words)
    assert all(word not in harness for word in domain_words)


def test_design_and_changelog_separate_kernel_from_example_demo() -> None:
    design = (ROOT / "docs/superpowers/specs/2026-09-06-agentic-saga-design.md").read_text()
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert "The shipped offline ecommerce reference" in design
    assert "The shipped `pytest-bdd` feature" in design
    assert "Generic durable Saga kernel" in changelog
    assert "Executable offline ecommerce reference" in changelog
    assert "24-case evaluation corpus" in changelog
    assert "applications register their implementations outside" in design


def test_design_and_plan_match_current_domain_neutral_policy_surface() -> None:
    design = (ROOT / "docs/superpowers/specs/2026-09-06-agentic-saga-design.md").read_text()
    plan = (ROOT / "docs/superpowers/plans/2026-09-06-kernel-runtime.md").read_text()

    assert "- Resource and tenant selectors." not in design
    assert "tenant/resource/currency/amount selectors pass" not in plan
    assert "Task 18 supersedes the original domain callback slots" in plan


def test_safety_contract_names_the_supported_facade() -> None:
    contract = (ROOT / "docs" / "kernel-safety-contract.md").read_text()
    required = (
        "## Supported imports",
        "The root package exports eight named symbols",
        "The three `agentic_saga.demo` exports are supported",
        "defining module",
        "not compatibility promises",
    )
    assert all(value in contract for value in required)


def test_context_manifest_names_the_shipped_ecommerce_assembly() -> None:
    guide = (ROOT / "docs" / "context-manifest.md").read_text()
    assert "executable ecommerce assembly is shipped" in guide
    assert "integration is planned with the reference ecommerce application" not in guide
    assert "uv run --no-dev python -m examples.ecommerce.run" in guide


def test_security_policy_requires_private_reporting_and_acknowledgement() -> None:
    # Given the security policy.
    security = (ROOT / "SECURITY.md").read_text()

    # When the reporting boundary is inspected.
    required = ("0.1.x", "harish.seshadri@gmail.com", "public vulnerability issue", "72 hours")

    # Then private reporting and the response target are explicit.
    assert all(value in security for value in required)


def test_security_policy_states_v01_safety_exclusions() -> None:
    # Given the security policy.
    security = (ROOT / "SECURITY.md").read_text()

    # When the documented guarantees are inspected.
    exclusions = (
        "single-host reference backend",
        "universal exactly-once delivery",
        "arbitrary-tool safety",
    )

    # Then unsupported safety claims are explicitly excluded.
    assert all(value in security for value in exclusions)


def test_provenance_names_current_hosted_controls_and_evidence() -> None:
    # Given the provenance and release-status document.
    provenance = (ROOT / "PROVENANCE.md").read_text()

    # When local evidence and repository controls are inspected.
    current = (
        "Current evidence",
        "local `uv run poe gate`",
        "No registry artifacts",
        "635974d51f87aa802886914be6a46bfd28518c66",
    )
    controls = (
        "Repository controls",
        "`.github/workflows/dagger.yml`",
        "`.github/workflows/dagger-security.yml`",
        "34727077866",
        "34727111885",
        "not full release-matrix evidence",
    )

    # Then local and hosted evidence are bound to the immutable commit and controls.
    assert all(value in provenance for value in current)
    assert all(value in provenance for value in controls)
    assert ".github/workflows/ci.yml" not in provenance
    assert ".github/workflows/security-audit.yml" not in provenance


def test_provenance_requires_authorized_trusted_publication() -> None:
    # Given the provenance and release-status document.
    provenance = (ROOT / "PROVENANCE.md").read_text()

    # When publication controls are inspected.
    required = ("separate, fresh authorization", "trusted publishing", "does not publish")

    # Then publication remains explicitly gated and out of scope.
    assert all(value in provenance for value in required)


def test_citation_contains_project_metadata() -> None:
    # Given the citation metadata.
    citation = (ROOT / "CITATION.cff").read_text()

    # When required citation fields are inspected.
    required = (
        "cff-version: 1.2.0",
        "title: Agentic Saga",
        "type: software",
        "family-names: Seshadri",
        "given-names: Harish",
        "repository-code: https://github.com/hseshadr/agentic-saga",
        "license: Apache-2.0",
        "version: 0.1.0",
    )

    # Then the project is citeable with the approved identity and version.
    assert all(value in citation for value in required)


def test_contributor_guide_contains_required_workflow() -> None:
    # Given the contributor guide.
    contributing = (ROOT / "CONTRIBUTING.md").read_text()

    # When development and review entry points are inspected.
    required = (
        "## Development setup",
        "uv sync --group dev",
        "## Test-driven changes",
        "## Local quality gate",
        "uv run poe gate",
        "## Live-model tests",
        "## Pull requests",
    )

    # Then contributors can find the required local workflow.
    assert all(value in contributing for value in required)


def _workflow_paths() -> tuple[Path, ...]:
    workflows = ROOT / ".github" / "workflows"
    return tuple(sorted({*workflows.glob("*.yml"), *workflows.glob("*.yaml")}))


def _workflow_document(path: Path) -> dict[str, object]:
    assert path.is_file(), f"{path.name} must be a Dagger ingress workflow"
    parsed = YAML(typ="safe").load(path.read_text())
    assert isinstance(parsed, dict)
    return cast(dict[str, object], parsed)


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _steps(document: dict[str, object], job_name: str = "Dagger") -> tuple[dict[str, object], ...]:
    jobs = _mapping(document["jobs"])
    assert list(jobs) == ["dagger"]
    job = _mapping(jobs["dagger"])
    assert set(job) == {"name", "runs-on", "steps"}
    assert job["name"] == job_name and job["runs-on"] == "ubuntu-latest"
    steps = job["steps"]
    assert isinstance(steps, list)
    return tuple(_mapping(step) for step in steps)


def _assert_checkout(step: dict[str, object]) -> None:
    assert set(step) == {"uses", "with"}
    assert step["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert _mapping(step["with"]) == {
        "fetch-depth": 0,
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
    }


def _assert_dagger(step: dict[str, object], operation: str) -> None:
    assert set(step) == {"uses", "env", "with"}
    assert step["uses"] == "dagger/dagger-for-github@27b130bf0f79a7f6fbbbe0fbca6760dc9bb40a77"
    assert _mapping(step["env"]) == {
        "DAGGER_GIT_HTTP_AUTH_HEADER": "${{ secrets.DAGGER_GIT_HTTP_AUTH_HEADER }}"
    }
    expected = (
        f"{operation} --source=. --commit-sha="
        "${{ github.sha }} "
        "--git-auth-header=env:DAGGER_GIT_HTTP_AUTH_HEADER"
    )
    assert _mapping(step["with"]) == {"version": "0.21.8", "verb": "call", "args": expected}


def _assert_ingress(document: dict[str, object], operation: str, job_name: str = "Dagger") -> None:
    assert _mapping(document["permissions"]) == {"contents": "read"}
    steps = _steps(document, job_name)
    assert len(steps) == 2 and all("run" not in step for step in steps)
    _assert_checkout(steps[0])
    _assert_dagger(steps[1], operation)


def _assert_ci_triggers(triggers: Mapping[str, object]) -> None:
    assert triggers == {
        "pull_request": None,
        "push": {"branches": ["main"]},
        "workflow_dispatch": None,
    }


def _assert_security_triggers(triggers: Mapping[str, object]) -> None:
    assert triggers == {
        "schedule": [{"cron": "17 8 * * 1"}],
        "workflow_dispatch": None,
    }


def _active_line_count(path: Path) -> int:
    lines = path.read_text().splitlines()
    return sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in lines)


def _ingress_fixture(operation: str = "ci") -> dict[str, object]:
    return {
        "permissions": {"contents": "read"},
        "jobs": {
            "dagger": {
                "name": "Dagger",
                "runs-on": "ubuntu-latest",
                "steps": [
                    {
                        "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                        "with": {
                            "fetch-depth": 0,
                            "ref": "${{ github.sha }}",
                            "persist-credentials": False,
                        },
                    },
                    {
                        "uses": "dagger/dagger-for-github@27b130bf0f79a7f6fbbbe0fbca6760dc9bb40a77",
                        "env": {
                            "DAGGER_GIT_HTTP_AUTH_HEADER": (
                                "${{ secrets.DAGGER_GIT_HTTP_AUTH_HEADER }}"
                            )
                        },
                        "with": {
                            "version": "0.21.8",
                            "verb": "call",
                            "args": (
                                f"{operation} --source=. --commit-sha="
                                "${{ github.sha }} "
                                "--git-auth-header=env:DAGGER_GIT_HTTP_AUTH_HEADER"
                            ),
                        },
                    },
                ],
            }
        },
    }


def _fixture_job(document: dict[str, object]) -> dict[str, object]:
    return _mapping(_mapping(document["jobs"])["dagger"])


def _fixture_steps(document: dict[str, object]) -> list[dict[str, object]]:
    steps = _fixture_job(document)["steps"]
    assert isinstance(steps, list)
    return cast(list[dict[str, object]], steps)


def test_workflows_are_only_24_line_two_action_dagger_ingress() -> None:
    # Given repository-authored workflow documents in either supported YAML extension.
    paths = _workflow_paths()

    # When their transport surfaces are structurally inspected.
    actual = {path.name for path in paths}

    # Then only two compact Dagger ingress workflows remain.
    assert actual == {"dagger.yml", "dagger-security.yml"}
    assert all(_active_line_count(path) <= 24 for path in paths)


def test_dagger_ci_ingress_is_structurally_closed_and_exact() -> None:
    # Given the CI ingress document.
    document = _workflow_document(ROOT / ".github/workflows/dagger.yml")

    # When its event and execution boundaries are inspected.
    triggers = _mapping(document["on"])

    # Then it accepts PRs and main pushes through one closed Dagger job.
    _assert_ci_triggers(triggers)
    _assert_ingress(document, "ci")


def test_dagger_security_ingress_is_structurally_closed_and_exact() -> None:
    # Given the scheduled security ingress document.
    document = _workflow_document(ROOT / ".github/workflows/dagger-security.yml")

    # When its event and execution boundaries are inspected.
    triggers = _mapping(document["on"])

    # Then it exposes only scheduled/manual security through one closed Dagger job.
    _assert_security_triggers(triggers)
    _assert_ingress(document, "security", "Dagger security audit")


def test_dagger_ingress_rejects_extra_jobs_and_job_reuse() -> None:
    # Given bypasses through a second job and a reusable job call.
    extra_job = _ingress_fixture()
    _mapping(extra_job["jobs"])["escape"] = {}
    reusable_job = _ingress_fixture()
    _fixture_job(reusable_job)["uses"] = "hseshadr/ci/.github/workflows/python-gate.yml@main"

    # When the closed ingress policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_ingress(extra_job, "ci")
    with pytest.raises(AssertionError):
        _assert_ingress(reusable_job, "ci")

    # Then neither hosted execution bypass is accepted.


def test_dagger_ingress_rejects_shell_and_mutable_action_steps() -> None:
    # Given a shell execution bypass and a mutable Dagger action reference.
    shell_step = _ingress_fixture()
    _fixture_steps(shell_step)[0]["run"] = "echo bypass"
    mutable_action = _ingress_fixture()
    _fixture_steps(mutable_action)[1]["uses"] = "dagger/dagger-for-github@main"

    # When the closed ingress policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_ingress(shell_step, "ci")
    with pytest.raises(AssertionError):
        _assert_ingress(mutable_action, "ci")

    # Then neither execution bypass is accepted.


def test_dagger_ingress_rejects_untyped_or_unmasked_secret_forwarding() -> None:
    # Given direct, embedded Basic, and embedded Bearer credentials.
    direct = _ingress_fixture()
    _fixture_steps(direct)[1]["env"] = {"GITHUB_TOKEN": "${{ github.token }}"}
    basic = _ingress_fixture()
    _fixture_steps(basic)[1]["env"] = {"DAGGER_GIT_HTTP_AUTH_HEADER": "Basic encoded-token"}
    bearer = _ingress_fixture()
    _fixture_steps(bearer)[1]["env"] = {"DAGGER_GIT_HTTP_AUTH_HEADER": "Bearer ${{ github.token }}"}

    # When the closed ingress policy is evaluated.
    for document in (direct, basic, bearer):
        with pytest.raises(AssertionError):
            _assert_ingress(document, "ci")

    # Then only the named, precomputed repository secret can reach Dagger.


def test_should_reject_conditional_dagger_step_when_validating_ingress() -> None:
    # Given a Dagger step that can skip the required check.
    document = _ingress_fixture()
    _fixture_steps(document)[1]["if"] = "${{ false }}"

    # When / Then the closed ingress policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_ingress(document, "ci")


def test_should_reject_continue_on_error_when_validating_ingress() -> None:
    # Given a Dagger step that can hide a failed required check.
    document = _ingress_fixture()
    _fixture_steps(document)[1]["continue-on-error"] = True

    # When / Then the closed ingress policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_ingress(document, "ci")


def test_should_reject_job_permission_override_when_validating_ingress() -> None:
    # Given a job that escalates the workflow's read-only permission.
    document = _ingress_fixture()
    _fixture_job(document)["permissions"] = {"contents": "write"}

    # When / Then the closed ingress policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_ingress(document, "ci")


def test_should_reject_missing_manual_trigger_when_validating_ci_events() -> None:
    # Given CI events without the required manual entry point.
    triggers = {"pull_request": None, "push": {"branches": ["main"]}}

    # When / Then the exact CI event policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_ci_triggers(triggers)


def test_should_reject_extra_trigger_when_validating_ci_events() -> None:
    # Given CI events with an unapproved scheduled entry point.
    triggers = {
        "pull_request": None,
        "push": {"branches": ["main"]},
        "workflow_dispatch": None,
        "schedule": [{"cron": "0 0 * * *"}],
    }

    # When / Then the exact CI event policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_ci_triggers(triggers)


def test_should_reject_dispatch_inputs_when_validating_ci_events() -> None:
    # Given CI events whose manual entry point accepts arbitrary inputs.
    triggers = {
        "pull_request": None,
        "push": {"branches": ["main"]},
        "workflow_dispatch": {"inputs": {"command": {"required": False}}},
    }

    # When / Then the exact CI event policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_ci_triggers(triggers)


def test_should_reject_wrong_cron_when_validating_security_events() -> None:
    # Given security events with a drifted weekly schedule.
    triggers = {
        "schedule": [{"cron": "0 0 * * 0"}],
        "workflow_dispatch": None,
    }

    # When / Then the exact security event policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_security_triggers(triggers)


def test_should_reject_extra_trigger_when_validating_security_events() -> None:
    # Given security events with an unapproved issue entry point.
    triggers = {
        "schedule": [{"cron": "17 8 * * 1"}],
        "workflow_dispatch": None,
        "issues": None,
    }

    # When / Then the exact security event policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_security_triggers(triggers)


def test_should_reject_dispatch_inputs_when_validating_security_events() -> None:
    # Given security events whose manual entry point accepts arbitrary inputs.
    triggers = {
        "schedule": [{"cron": "17 8 * * 1"}],
        "workflow_dispatch": {"inputs": {"command": {"required": False}}},
    }

    # When / Then the exact security event policy is evaluated.
    with pytest.raises(AssertionError):
        _assert_security_triggers(triggers)


def test_python_tooling_targets_the_supported_312_floor() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert config["tool"]["ruff"]["target-version"] == "py312"
    assert config["tool"]["mypy"]["python_version"] == "3.12"
    release_typecheck = config["tool"]["poe"]["tasks"]["typecheck-release"]
    assert release_typecheck == {
        "cmd": (
            "mypy --strict --explicit-package-bases scripts/measure_release.py "
            "scripts/release_contract.py scripts/release_runner.py scripts/quality_proof.py"
        ),
        "env": {"MYPYPATH": "src"},
    }


def test_flight_recorder_pins_its_package_manager_and_lockfile() -> None:
    package = json.loads((ROOT / "web" / "flight-recorder" / "package.json").read_text())
    assert package["packageManager"] == "pnpm@11.5.0"
    assert (ROOT / "web" / "flight-recorder" / "pnpm-lock.yaml").is_file()


def test_frontend_gate_runs_the_native_asset_safety_suite() -> None:
    recorder = ROOT / "web" / "flight-recorder"
    package = json.loads((recorder / "package.json").read_text())

    assert package["scripts"]["test:assets"] == "node --test scripts/sync-package-assets.test.mjs"
    assert "pnpm test:assets" in package["scripts"]["gate"]
    assert (recorder / "scripts" / "sync-package-assets.test.mjs").is_file()


def test_flight_recorder_defines_a_separate_packaged_browser_gate() -> None:
    recorder = ROOT / "web" / "flight-recorder"
    package = json.loads((recorder / "package.json").read_text())

    assert package["scripts"]["test:e2e:packaged"] == (
        "playwright test --config playwright.packaged.config.ts"
    )
    assert (recorder / "playwright.packaged.config.ts").is_file()
    assert (recorder / "e2e-packaged" / "packaged-recorder.spec.ts").is_file()
    suite = (recorder / "e2e-packaged" / "packaged-recorder.spec.ts").read_text()
    assert 'page.on("websocket"' in suite


def test_packaged_recorder_cleanup_is_bounded_and_escalates() -> None:
    suite = (
        ROOT / "web" / "flight-recorder" / "e2e-packaged" / "packaged-recorder.spec.ts"
    ).read_text()

    required = (
        'const STOP_SIGNALS = ["SIGINT", "SIGTERM", "SIGKILL"] as const;',
        "await waitForExit(process, STOP_GRACE_MS)",
        'process.off("error", onError)',
        'process.off("exit", onExit)',
        'lines.off("line", onLine)',
        "process.stdin.destroy()",
        "process.stdout.destroy()",
        "process.stderr.destroy()",
    )
    assert all(value in suite for value in required)
    assert suite.index('"SIGINT"') < suite.index('"SIGTERM"') < suite.index('"SIGKILL"')


def test_packaged_recorder_labels_the_fresh_navigation_measurement_exactly() -> None:
    suite = (
        ROOT / "web" / "flight-recorder" / "e2e-packaged" / "packaged-recorder.spec.ts"
    ).read_text()

    assert "warm-browser fresh-page navigation budget" in suite
    assert "BROWSER_FRESH_NAVIGATION_P95_MS" in suite
    assert "BROWSER_FIRST_USABLE_P95_MS" not in suite


def test_flight_recorder_dev_server_is_loopback_only() -> None:
    package = json.loads((ROOT / "web" / "flight-recorder" / "package.json").read_text())
    wildcard_host = ".".join(("0", "0", "0", "0"))
    sources = [
        ROOT / "docs" / "flight-recorder.md",
    ]

    assert package["scripts"]["dev"] == "vite --host 127.0.0.1"
    assert all(wildcard_host not in source.read_text() for source in sources)


def test_predictable_claim_ids_are_fixture_export_only() -> None:
    demo = (ROOT / "examples" / "ecommerce" / "demo.py").read_text()
    exporter = (ROOT / "examples" / "ecommerce" / "export_flight_recorder.py").read_text()

    assert "def _deterministic_claim_ids" not in demo
    assert "claim_id_factory=_deterministic_claim_ids()" in exporter


def test_flight_recorder_docs_label_current_and_historical_rules_truthfully() -> None:
    guide = (ROOT / "docs" / "flight-recorder.md").read_text()
    plan = (ROOT / "docs" / "superpowers" / "plans" / "2026-09-06-flight-recorder.md").read_text()

    assert "Amber dashed signals are proposals" not in guide
    assert "agent-originated" in guide
    assert "## Historical Constraints" in plan
    assert "## Historical Integration Sketch" in plan


def test_frontend_generated_outputs_are_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text().splitlines()

    assert "web/flight-recorder/coverage/" in ignored
    assert "*.tsbuildinfo" in ignored


def test_dependabot_configures_weekly_ecosystem_updates() -> None:
    config = (ROOT / ".github/dependabot.yml").read_text()
    for ecosystem in ("pip", "github-actions"):
        block = _ecosystem_block(config, ecosystem)
        schedule = ("    schedule:", "      interval: weekly", "      day: monday")
        assert _contains_sequence(block, schedule)
        assert "    open-pull-requests-limit: 5" in block
        assert "      - dependencies" in block
        assert f"      - {ecosystem}" in block
    assert "auto-merge" not in config
