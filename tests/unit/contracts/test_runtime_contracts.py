from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentic_saga.contracts.runtime import (
    ExecutionBudget,
    ReadEvidence,
    SagaGoal,
    SagaStatus,
    TerminalRequirement,
)
from agentic_saga.temporal import SagaWorkflowInput, WorkflowState, WorkflowTool


def test_saga_status_exactly_matches_current_temporal_workflow_states() -> None:
    assert {status.value for status in SagaStatus} == {
        "running",
        "compensating",
        "human_required",
        "succeeded_verified",
        "compensated_verified",
        "aborted_clean",
    }


def test_saga_goal_is_strict_and_immutable() -> None:
    goal = SagaGoal(
        goal_id="trip-42",
        text="Book the trip or restore a safe state.",
        context={"trip_id": "trip-42"},
    )

    with pytest.raises(ValidationError):
        goal.text = "changed"


def test_execution_budget_rejects_unbounded_values() -> None:
    with pytest.raises(ValidationError):
        ExecutionBudget(
            turn_limit=10,
            tool_call_limit=10,
            elapsed_ms_limit=9,
            token_limit=10_000,
        )

    with pytest.raises(ValidationError, match="token_limit"):
        ExecutionBudget(
            turn_limit=10,
            tool_call_limit=10,
            elapsed_ms_limit=10,
            token_limit=9,
        )


def test_terminal_requirement_rejects_duplicate_rules() -> None:
    valid = TerminalRequirement(invariant_version="v1", required_rule_ids=("paid",))

    assert valid.required_rule_ids == ("paid",)
    with pytest.raises(ValidationError, match="unique"):
        TerminalRequirement(
            invariant_version="v1",
            required_rule_ids=("paid", "paid"),
        )


def test_goal_rejects_private_text_or_context() -> None:
    with pytest.raises(ValidationError, match="private material"):
        SagaGoal(goal_id="checkout", text="Bearer hidden-value", context={})
    with pytest.raises(ValidationError, match="private material"):
        SagaGoal(goal_id="checkout", text="Checkout safely.", context={"api_key": "hidden"})


def test_read_evidence_requires_exactly_one_outcome() -> None:
    evidence = ReadEvidence(
        tool_name="inspect_order",
        command={"order_id": "order-1"},
        observed_at_saga_seq=1,
        freshness="fresh",
        result={"paid": True},
    )

    assert evidence.result == {"paid": True}
    with pytest.raises(ValidationError, match="exactly one"):
        ReadEvidence(
            tool_name="inspect_order",
            command={"order_id": "order-1"},
            observed_at_saga_seq=1,
            freshness="fresh",
        )


def test_temporal_workflow_input_is_typed_and_frozen() -> None:
    tool = WorkflowTool(
        name="reserve_inventory",
        kind="effect",
        compensation_tool="release_inventory",
    )
    value = SagaWorkflowInput(
        saga_id="saga_0000000000000042",
        goal=SagaGoal(
            goal_id="order-42",
            text="Place the order or compensate completed work.",
            context={"order_id": "order-42"},
        ),
        tools=(tool,),
        max_agent_turns=10,
        max_tool_calls=10,
    )

    assert value.tools == (tool,)
    with pytest.raises(ValidationError):
        value.max_agent_turns = 9


def test_workflow_state_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        WorkflowState.model_validate({"saga_id": "saga_0000000000000042", "unknown": True})
