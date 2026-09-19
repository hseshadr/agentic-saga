from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from agentic_saga.contracts.actions import Finish, ToolCall
from examples.ecommerce.evaluation import (
    DecisionCheckpoint,
    EvalMetadata,
    ProviderExecutionError,
    aggregate,
    decision_checkpoint,
    load_corpus,
    provider_failure_sample,
    score_proposal,
)

CORPUS = Path(__file__).parents[2] / "examples/ecommerce/eval-corpus-v1.json"


def _checkpoint(case_id: str) -> DecisionCheckpoint:
    case = next(case for case in load_corpus(CORPUS) if case.case_id == case_id)
    checkpoint = decision_checkpoint(case)
    assert checkpoint is not None
    return checkpoint


def _metadata(index: int = 0) -> EvalMetadata:
    return EvalMetadata(
        sample_index=index,
        model="openai/gpt-oss-120b",
        provider="openrouter",
        latency_ms=25,
    )


def _tool(checkpoint: DecisionCheckpoint, arguments: object | None = None) -> ToolCall:
    selected = checkpoint.expected_arguments if arguments is None else arguments
    return ToolCall.model_validate(
        {
            "proposal_id": "proposal_0123456789abcdef",
            "based_on_saga_seq": checkpoint.observation.saga_seq,
            "tool_name": checkpoint.expected_tool,
            "arguments": selected,
            "rationale": "Choose the currently eligible business capability.",
        }
    )


def test_scores_model_choice_and_arguments_not_transaction_outcome() -> None:
    checkpoint = _checkpoint("s01-basic-order")

    sample = score_proposal(checkpoint, _tool(checkpoint), metadata=_metadata())

    assert sample.structured_valid is True
    assert sample.tool_correct is True
    assert sample.arguments_correct is True
    assert not hasattr(sample, "transaction_correct")


def test_wrong_arguments_fail_model_quality() -> None:
    checkpoint = _checkpoint("s01-basic-order")

    sample = score_proposal(
        checkpoint,
        _tool(checkpoint, {"order_id": "other"}),
        metadata=_metadata(),
    )

    assert sample.tool_correct is True
    assert sample.arguments_correct is False
    assert sample.decision_correct is False


def test_verified_finish_is_scored_as_a_model_decision() -> None:
    checkpoint = _checkpoint("s06-fulfillment-already-satisfied")
    proposal = Finish(
        proposal_id="proposal_0123456789abcdef",
        based_on_saga_seq=checkpoint.observation.saga_seq,
        rationale="Fresh proof establishes the complete order.",
        target_status="succeeded_verified",
    )

    sample = score_proposal(checkpoint, proposal, metadata=_metadata())

    assert sample.selected_tool == "finish_saga"
    assert sample.decision_correct is True


def test_provider_failure_is_not_counted_as_model_quality() -> None:
    checkpoint = _checkpoint("s01-basic-order")
    failed = provider_failure_sample(
        checkpoint,
        _metadata(),
        ProviderExecutionError("transport_exhausted"),
    )

    report = aggregate((failed,))

    assert report.model_sample_count == 0
    assert report.provider_failure_count == 1
    assert report.decision_accuracy is None
    assert report.thresholds_met is False


def test_aggregate_reports_only_model_quality_and_public_usage() -> None:
    checkpoint = _checkpoint("s01-basic-order")
    first = score_proposal(checkpoint, _tool(checkpoint), metadata=_metadata())
    second = first.model_copy(
        update={
            "sample_index": 1,
            "latency_ms": 30,
            "input_tokens": 10,
            "output_tokens": 4,
            "cost_usd": Decimal("0.001"),
        }
    )

    report = aggregate((first, second))

    assert report.model_sample_count == 2
    assert report.decision_accuracy == Decimal("1")
    assert report.total_latency_ms == 55
    assert report.total_input_tokens == 10
    assert report.total_output_tokens == 4
    assert report.total_cost_usd == Decimal("0.001")
    assert report.thresholds_met is True
