from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
PUBLIC_RELEASE_DOCS = (
    "README.md",
    "QUICKSTART.md",
    "CHANGELOG.md",
    "PROVENANCE.md",
    "SECURITY.md",
    "docs/agent-adapter.md",
    "docs/flight-recorder.md",
    "docs/operations.md",
    "docs/temporal-safety-contract.md",
)
_RETIRED = (
    "compose_runtime",
    "SagaRuntime",
    "SQLiteKernelStore",
    "agentic_saga.kernel",
    "agentic_saga.storage",
)


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def _normalized(path: str) -> str:
    return " ".join(_read(path).split())


def test_public_release_docs_exist_and_hide_internal_work_status() -> None:
    for path in PUBLIC_RELEASE_DOCS:
        assert "Task " not in _read(path), path


def test_reader_paths_describe_only_the_temporal_runtime() -> None:
    for path in ("README.md", "QUICKSTART.md"):
        text = _read(path)
        assert "Temporal" in text, path
        assert not any(value in text for value in _RETIRED), path


def test_quickstart_runs_the_canonical_temporal_bdd_in_one_command() -> None:
    quickstart = _read("QUICKSTART.md")

    assert (
        "uv run pytest -q -m temporal --force-enable-socket tests/bdd/steps/test_ecommerce_saga.py"
    ) in quickstart
    assert "WorkflowEnvironment.start_time_skipping()" in quickstart
    assert "temporal server start-dev" in quickstart
    assert "Temporal Cloud" in quickstart
    assert "production-ready self-hosted Temporal Service" in quickstart


def test_quickstart_explains_agent_compensation_and_human_boundary() -> None:
    quickstart = _read("QUICKSTART.md")

    required = (
        "agent is the planner, not the transaction engine",
        "cancel fulfillment -> refund payment -> release inventory",
        "reconciles the same operation",
        "stale and unauthorized Updates are rejected",
    )
    assert all(value in quickstart for value in required)


def test_temporal_safety_contract_names_public_integration_legos() -> None:
    contract = _read("docs/temporal-safety-contract.md")
    required = (
        "TemporalActivities",
        "build_worker",
        "connect_client",
        "project_run_trace",
        "query_saga_state",
        "resolve_human_compensation",
    )
    assert all(value in contract for value in required)


def test_temporal_safety_contract_maps_claims_to_existing_evidence() -> None:
    contract = _read("docs/temporal-safety-contract.md")
    paths = re.findall(r"`((?:tests/)[^`]+\.py)`", contract)

    assert paths
    assert all((ROOT / path).is_file() for path in paths)


def test_operations_freezes_delivery_and_human_resolution_boundaries() -> None:
    operations = _normalized("docs/operations.md")
    required = (
        "Activities execute at least once",
        "stable idempotency key",
        "reconcile before retrying or compensating",
        "automatically compensates confirmed effects in reverse dependency order",
        "A Query returns a read-only",
        "A validated Update binds the decision",
    )
    assert all(value in operations for value in required)


def test_security_requires_encryption_kms_and_namespace_access_for_private_data() -> None:
    security = _read("SECURITY.md")
    required = (
        "public-safe payloads",
        "Payload Codec",
        "external KMS",
        "least-privilege roles",
        "namespace-scoped API keys or mTLS identities",
    )
    assert all(value in security for value in required)


def test_openrouter_setup_uses_only_the_secret_free_template() -> None:
    for path in ("QUICKSTART.md", "SECURITY.md", "docs/operations.md"):
        text = _read(path)
        assert "cp .env.example .env" in text, path
        assert "OPENROUTER_API_KEY to your own key" in text, path
        assert not re.search(r"OPENROUTER_API_KEY=[A-Za-z0-9_-]{12,}", text), path


def test_release_docs_separate_deterministic_proof_from_live_model_quality() -> None:
    provenance = _normalized("PROVENANCE.md")
    operations = _normalized("docs/operations.md")

    assert "Model-quality evidence is not release-correctness evidence" in provenance
    assert "Ordinary tests" in operations
    assert "make no paid model call" in operations


def test_owned_document_links_resolve() -> None:
    paths = (
        "QUICKSTART.md",
        "SECURITY.md",
        "PROVENANCE.md",
        "docs/operations.md",
        "docs/temporal-safety-contract.md",
    )
    for path in paths:
        _assert_relative_links_resolve(ROOT / path)


def _assert_relative_links_resolve(path: Path) -> None:
    links = re.findall(r"\[[^]]*\]\(([^)]+)\)", path.read_text())
    relative = (link.split("#", 1)[0] for link in links if not _external(link))
    assert all((path.parent / link).exists() for link in relative), path


def _external(link: str) -> bool:
    return link.startswith(("http://", "https://", "mailto:", "#"))
