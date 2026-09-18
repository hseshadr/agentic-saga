from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from examples.ecommerce.evaluation import (
    CorpusValidationError,
    EvalCategory,
    ReleaseProof,
    load_corpus,
)

CORPUS_PATH = Path(__file__).parents[2] / "examples" / "ecommerce" / "eval-corpus-v1.json"
EXPECTED_IDS = frozenset(
    {
        "s01-basic-order",
        "s02-two-items",
        "s03-existing-order",
        "s04-charge-already-satisfied",
        "s05-reservation-already-satisfied",
        "s06-fulfillment-already-satisfied",
        "r01-alternate-warehouse",
        "r02-restock-within-policy",
        "r03-primary-version-conflict",
        "r04-transient-inventory-read",
        "r05-refund-and-cancel",
        "r06-partial-reservation-repair",
        "a01-output-injection",
        "a02-cross-customer",
        "a03-second-charge",
        "a04-inflated-amount",
        "a05-agent-key",
        "a06-false-finish",
        "e01-irreversible-action",
        "e02-unknown-charge",
        "e03-unknown-refund",
        "e04-conflicting-evidence",
        "e05-budget-exhausted",
        "e06-short-dedup-horizon",
    }
)


def _case(case_id: str, category: str = "straightforward") -> dict[str, object]:
    return {
        "case_id": case_id,
        "category": category,
        "goal": "Complete one authorized order.",
        "fixture": {},
        "allowed_states": ["succeeded_verified"],
        "required_semantic_events": ["invariant_evaluated", "terminal_assigned"],
        "required_proof_rules": ["order_fulfilled"],
        "forbidden_effects": [],
        "escalation_required": False,
        "kernel_rejection_required": False,
        "max_agent_turns": 8,
    }


def _document(cases: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "corpus_id": "ecommerce-live-eval-v1",
        "prompt_version": "ecommerce-agent-v1",
        "cases": cases,
    }


def _full_cases() -> list[dict[str, object]]:
    cases = [_case(f"case-{index:02d}") for index in range(24)]
    for case, proof in zip(cases, ReleaseProof, strict=False):
        case["release_proof"] = proof.value
    return cases


def _write(path: Path, value: dict[str, object]) -> Path:
    path.write_text(json.dumps(value))
    return path


def test_should_load_exact_versioned_24_case_corpus() -> None:
    # Given the checked-in evaluation corpus.
    # When it is loaded through the strict boundary.
    cases = load_corpus(CORPUS_PATH)

    # Then every required stable case ID appears exactly once.
    assert frozenset(case.case_id for case in cases) == EXPECTED_IDS
    assert len(cases) == 24


def test_should_keep_six_cases_in_each_evaluation_category() -> None:
    # Given the checked-in evaluation corpus.
    # When its categories are counted.
    counts = Counter(case.category for case in load_corpus(CORPUS_PATH))

    # Then the benchmark is balanced across the four difficulty classes.
    assert counts == {category: 6 for category in EvalCategory}


def test_should_define_exactly_four_canonical_release_proofs() -> None:
    cases = load_corpus(CORPUS_PATH)
    release = {case.release_proof: case.case_id for case in cases if case.release_proof}

    assert release == {
        ReleaseProof.HAPPY_PATH: "s01-basic-order",
        ReleaseProof.COMPENSATION: "r05-refund-and-cancel",
        ReleaseProof.UNKNOWN_RECONCILIATION: "r06-partial-reservation-repair",
        ReleaseProof.HUMAN_ESCALATION: "e01-irreversible-action",
    }


def test_should_require_a_deterministic_oracle_for_every_case() -> None:
    # Given every versioned evaluation case.
    cases = load_corpus(CORPUS_PATH)

    # When its explicit oracle is inspected.
    # Then it has an allowed outcome or requires a safe human pause.
    assert all(case.allowed_states or case.escalation_required for case in cases)
    assert all(case.required_semantic_events for case in cases)


def test_unknown_refund_goal_should_not_steer_the_model_to_the_fixture_outcome() -> None:
    cases = {case.case_id: case for case in load_corpus(CORPUS_PATH)}
    recovery_goal = cases["r05-refund-and-cancel"].goal
    unknown_goal = cases["e03-unknown-refund"].goal

    assert unknown_goal == recovery_goal
    assert "unresolved" not in unknown_goal.lower()
    assert "human" not in unknown_goal.lower()


def test_already_reserved_goal_should_require_current_inventory_evidence() -> None:
    cases = {case.case_id: case for case in load_corpus(CORPUS_PATH)}
    goal = cases["s05-reservation-already-satisfied"].goal.lower()

    assert "authoritative" not in goal
    assert "evidence" not in goal
    assert "already confirms" not in goal
    assert "inspect" in goal


@pytest.mark.parametrize("schema_version", ["0.9", "2.0"])
def test_should_reject_unknown_corpus_schema_version(tmp_path: Path, schema_version: str) -> None:
    # Given an otherwise valid document with an unsupported version.
    document = _document(_full_cases())
    document["schema_version"] = schema_version

    # When the untrusted corpus is loaded, then it fails closed.
    with pytest.raises(CorpusValidationError, match="invalid evaluation corpus"):
        load_corpus(_write(tmp_path / "corpus.json", document))


def test_should_reject_duplicate_case_ids(tmp_path: Path) -> None:
    # Given a corpus that reuses an identity.
    cases = _full_cases()
    cases[-1] = _case("case-00")
    path = _write(tmp_path / "corpus.json", _document(cases))

    # When the corpus is loaded, then ambiguous result attribution is rejected.
    with pytest.raises(CorpusValidationError, match="invalid evaluation corpus"):
        load_corpus(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "unknown"),
        ("allowed_states", []),
        ("required_semantic_events", []),
    ],
)
def test_should_reject_unknown_category_or_incomplete_oracle(
    tmp_path: Path, field: str, value: object
) -> None:
    # Given a case with an unknown enum or incomplete deterministic oracle.
    cases = _full_cases()
    cases[0][field] = value

    # When loaded, then the strict boundary rejects the shape.
    with pytest.raises(CorpusValidationError, match="invalid evaluation corpus"):
        load_corpus(_write(tmp_path / "corpus.json", _document(cases)))


def test_should_reject_incomplete_corpus(tmp_path: Path) -> None:
    # Given the versioned corpus envelope with only 23 cases.
    path = _write(tmp_path / "corpus.json", _document(_full_cases()[:-1]))

    # When loaded, then the fixed benchmark denominator is rejected.
    with pytest.raises(CorpusValidationError, match="invalid evaluation corpus"):
        load_corpus(path)


def test_should_reject_oversized_corpus_before_json_parsing(tmp_path: Path) -> None:
    # Given an input larger than the documented corpus bound.
    path = tmp_path / "large.json"
    path.write_bytes(b" " * 524_289)

    # When loaded, then it is rejected before model validation.
    with pytest.raises(CorpusValidationError, match="size limit"):
        load_corpus(path)
