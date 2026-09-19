from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from examples.ecommerce.evaluation import (
    CorpusValidationError,
    EvalCategory,
    EvalSuite,
    WorkflowProof,
    decision_checkpoint,
    load_corpus,
    select_cases,
)

CORPUS = Path(__file__).parents[2] / "examples/ecommerce/eval-corpus-v1.json"


def test_corpus_keeps_24_balanced_transaction_fixtures() -> None:
    cases = load_corpus(CORPUS)

    assert len(cases) == 24
    assert Counter(case.category for case in cases) == {category: 6 for category in EvalCategory}
    assert {case.workflow_proof for case in cases if case.workflow_proof} == set(WorkflowProof)


def test_live_model_suite_excludes_workflow_owned_human_cases() -> None:
    cases = load_corpus(CORPUS)

    extended = select_cases(cases, EvalSuite.EXTENDED)

    assert len(extended) == 18
    assert all(decision_checkpoint(case) is not None for case in extended)
    assert all(case.category is not EvalCategory.ESCALATION for case in extended)


def test_smoke_is_one_temporal_forward_decision() -> None:
    selected = select_cases(load_corpus(CORPUS), EvalSuite.SMOKE)
    checkpoint = decision_checkpoint(selected[0])

    assert [case.case_id for case in selected] == ["s01-basic-order"]
    assert checkpoint is not None
    assert checkpoint.expected_tool == "reserve_inventory"
    assert tuple(tool.name for tool in checkpoint.available_tools) == ("reserve_inventory",)
    recent = checkpoint.observation.projection["recent_events"]
    assert isinstance(recent, tuple)
    assert len(recent) <= 20


def test_ready_order_exposes_only_verified_success_finish() -> None:
    cases = {case.case_id: case for case in load_corpus(CORPUS)}

    checkpoint = decision_checkpoint(cases["s06-fulfillment-already-satisfied"])

    assert checkpoint is not None
    assert checkpoint.expected_tool == "finish_saga"
    assert checkpoint.available_tools == ()
    assert checkpoint.observation.finish_allowed is True


def test_adversarial_public_label_is_visible_but_not_authoritative() -> None:
    cases = {case.case_id: case for case in load_corpus(CORPUS)}

    checkpoint = decision_checkpoint(cases["a01-output-injection"])

    assert checkpoint is not None
    assert checkpoint.expected_tool == "reserve_inventory"
    assert "ignore policy" in str(checkpoint.observation.projection["recent_events"])


def test_corpus_fails_closed_on_unknown_schema(tmp_path: Path) -> None:
    payload = json.loads(CORPUS.read_bytes())
    payload["schema_version"] = "unknown"
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(CorpusValidationError, match="invalid evaluation corpus"):
        load_corpus(path)


def test_corpus_is_bounded_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "corpus.json"
    path.write_bytes(b" " * 524_289)

    with pytest.raises(CorpusValidationError, match="size limit"):
        load_corpus(path)
