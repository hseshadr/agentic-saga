from __future__ import annotations

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
    "docs/kernel-safety-contract.md",
    "docs/operations.md",
)
COMPOSITION_ARGUMENTS = (
    "store=store",
    "definition=definition",
    "policy_context_provider=policy_context_provider",
    "terminal_gate=terminal_gate",
    "invariant_evidence_provider=invariant_evidence_provider",
    "clock=clock",
    'worker_id="orders-worker"',
    'id_namespace=b"acme-orders-v1"',
)
PLANNING_BUDGET_CLAUSE = (
    "`turn_limit`, `tool_call_limit`, `token_limit`, and `elapsed_ms_limit` are positive"
)
PLANNING_CAPACITY_CLAUSE = "token and elapsed limits are each at least the turn limit"
NO_METERING_CLAUSE = (
    "Token/time limits are not input-token, actual-usage, end-to-end-time, or monetary meters"
)
OPTIONAL_MODEL_CLAUSE = "One model call, eight graph steps, zero SDK retries"


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_public_docs_do_not_expose_internal_work_item_status() -> None:
    for path in PUBLIC_RELEASE_DOCS:
        assert "Task " not in _read(path), path


def test_reader_paths_name_the_supported_runtime_factory() -> None:
    for path in ("README.md", "QUICKSTART.md"):
        text = _read(path)
        assert "from agentic_saga import" in text
        assert "compose_runtime" in text
        assert all(argument in text for argument in COMPOSITION_ARGUMENTS)


def test_reader_paths_pass_the_goal_to_the_runtime_separately() -> None:
    for path in ("README.md", "QUICKSTART.md"):
        text = _read(path)
        assert "runtime.start(" in text
        assert "goal=goal" in text


def test_safety_contract_reports_the_exact_public_surface() -> None:
    contract = _read("docs/kernel-safety-contract.md")
    assert "eight named symbols" in contract


def test_safety_contract_maps_restart_read_and_lease_guarantees_to_tests() -> None:
    contract = _read("docs/kernel-safety-contract.md")
    required = (
        "Definition fingerprint and restart binding",
        "test_fresh_process_resume_rejects_same_version_with_changed_definition",
        "Bounded read failure evidence",
        "test_stalled_read_records_safe_unavailable_outcome_within_turn_budget",
        "Awaited-work lease lifecycle",
        "test_runtime_cannot_write_or_release_after_awaited_work_loses_authority",
        "Schema v1 is deliberately rejected",
    )
    assert all(value in contract for value in required)


def test_release_workload_excludes_live_paid_model_calls() -> None:
    operations = _read("docs/operations.md")
    assert "24-case deterministic offline evaluation corpus" in operations
    assert "offline adapter construction and consent guards" in operations
    assert "excludes live paid model calls unless separately authorized" in operations


def test_budget_contract_freezes_exact_semantics() -> None:
    operations = _read("docs/operations.md")
    clauses = (
        PLANNING_BUDGET_CLAUSE,
        PLANNING_CAPACITY_CLAUSE,
        NO_METERING_CLAUSE,
        OPTIONAL_MODEL_CLAUSE,
    )
    for clause in clauses:
        assert clause in operations
