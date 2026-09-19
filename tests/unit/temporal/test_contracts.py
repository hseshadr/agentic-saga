from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agentic_saga.contracts.actions import ToolCall
from agentic_saga.contracts.common import Direction
from agentic_saga.contracts.runtime import SagaGoal, SagaStatus
from agentic_saga.temporal.contracts import (
    ActivityIdentity,
    AgentDecisionObservation,
    AgentDecisionResult,
    CompensationActivityRequest,
    CompensationActivityResult,
    CompensationRecord,
    ForwardActivityRequest,
    ForwardActivityResult,
    HumanCompensationResolution,
    HumanResolutionVerificationResult,
    ReconciliationActivityRequest,
    ReconciliationActivityResult,
    SagaWorkflowInput,
    ToolActivityRequest,
    ToolActivityResult,
    WorkflowEvent,
    WorkflowResult,
    WorkflowState,
    WorkflowTool,
)

SAGA_ID = "saga_1234567890abcdef"
STEP_ID = "step_12345678"


def _event(seq: int, *, kind: str = "started") -> WorkflowEvent:
    return WorkflowEvent(
        seq=seq,
        kind=kind,
        details={"goal_id": "checkout"},
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        before_status=SagaStatus.RUNNING,
        after_status=SagaStatus.RUNNING,
    )


def test_activity_identity_is_stable_and_direction_scoped() -> None:
    forward = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    repeated = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    compensation = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.COMPENSATION, 0)

    assert forward == repeated
    assert forward.operation_id != compensation.operation_id
    assert forward.idempotency_key == forward.operation_id


def test_activity_identity_rejects_a_mismatched_operation_id() -> None:
    identity = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)

    with pytest.raises(ValidationError, match="operation identity does not match"):
        ActivityIdentity(
            saga_id=identity.saga_id,
            step_instance_id=identity.step_instance_id,
            direction=identity.direction,
            semantic_generation=identity.semantic_generation,
            operation_id="op_" + "0" * 64,
            idempotency_key="op_" + "0" * 64,
        )


def test_activity_contracts_round_trip_as_strict_json() -> None:
    request = ForwardActivityRequest(
        identity=ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0),
        tool_name="reserve_inventory",
        arguments={"sku": "bag", "quantity": 1},
        declared_kind="effect",
        declared_compensation_tool="release_inventory",
    )
    result = ForwardActivityResult.succeeded({"reservation_id": "res_1"})

    encoded_request = request.model_dump_json()
    encoded_result = result.model_dump_json()

    assert ForwardActivityRequest.model_validate_json(encoded_request) == request
    assert ForwardActivityResult.model_validate_json(encoded_result) == result
    assert (
        json.loads(encoded_request)["identity"]["idempotency_key"] == request.identity.operation_id
    )


def test_forward_request_rejects_compensation_identity() -> None:
    identity = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.COMPENSATION, 0)

    with pytest.raises(ValidationError, match="forward request requires forward identity"):
        ForwardActivityRequest(
            identity=identity,
            tool_name="reserve",
            arguments={},
            declared_kind="effect",
            declared_compensation_tool="release",
        )


@pytest.mark.parametrize(
    "declaration",
    [
        {"declared_kind": "read", "declared_compensation_tool": "undo_lookup"},
        {"declared_kind": "effect", "declared_compensation_tool": None},
    ],
)
def test_forward_request_rejects_kind_and_compensation_mismatch(
    declaration: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="requires matching compensation"):
        ForwardActivityRequest.model_validate(
            {
                "identity": ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0),
                "tool_name": "reserve",
                "arguments": {},
                **declaration,
            }
        )


def test_compensation_contract_requires_forward_receipt() -> None:
    identity = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.COMPENSATION, 0)
    forward = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)

    with pytest.raises(ValidationError):
        CompensationActivityRequest(
            identity=identity,
            tool_name="release",
            arguments={},
            compensates_operation_id=forward.operation_id,
            forward_receipt={},
        )


def test_result_contracts_fail_closed_on_ambiguous_shapes() -> None:
    with pytest.raises(ValidationError):
        ForwardActivityResult(outcome="succeeded", receipt=None, reason_code=None)
    with pytest.raises(ValidationError):
        CompensationActivityResult(outcome="unresolved", receipt={}, reason_code="timeout")
    with pytest.raises(ValidationError):
        CompensationActivityResult(outcome="unresolved", receipt=None, reason_code=None)


def test_compensation_request_rejects_wrong_direction_and_target() -> None:
    forward = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    compensation = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.COMPENSATION, 0)
    values = {"tool_name": "release", "arguments": {}, "forward_receipt": {"id": "r1"}}

    with pytest.raises(ValidationError, match="requires compensation identity"):
        CompensationActivityRequest.model_validate(
            {"identity": forward, "compensates_operation_id": forward.operation_id, **values}
        )
    with pytest.raises(ValidationError, match="target does not match"):
        CompensationActivityRequest.model_validate(
            {
                "identity": compensation,
                "compensates_operation_id": "op_" + "1" * 64,
                **values,
            }
        )


def test_compensation_record_rejects_invalid_identity_shapes() -> None:
    forward = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    compensation = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.COMPENSATION, 0)
    values = _record_values()

    with pytest.raises(ValidationError, match="forward identity"):
        CompensationRecord.model_validate(
            {
                "forward_identity": compensation,
                "compensation_identity": compensation,
                **values,
            }
        )
    with pytest.raises(ValidationError, match="compensation identity"):
        CompensationRecord.model_validate(
            {"forward_identity": forward, "compensation_identity": forward, **values}
        )


def _record_values() -> dict[str, object]:
    return {
        "compensation_tool": "release",
        "compensation_arguments": {},
        "forward_receipt": {"id": "r1"},
    }


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"forward_receipt": {}}, "forward receipt"),
        ({"state": "succeeded"}, "succeeded compensation"),
        ({"state": "unresolved"}, "unresolved compensation"),
    ],
)
def test_compensation_record_requires_state_evidence(
    updates: dict[str, object], message: str
) -> None:
    forward = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    compensation = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.COMPENSATION, 0)
    values = {
        "forward_identity": forward,
        "compensation_identity": compensation,
        **_record_values(),
        **updates,
    }
    with pytest.raises(ValidationError, match=message):
        CompensationRecord.model_validate(values)


def test_workflow_state_and_result_are_serializable() -> None:
    event = _event(1)
    state = WorkflowState(
        saga_id=SAGA_ID,
        status=SagaStatus.RUNNING,
        events=(event,),
        compensations=(),
    )
    result = WorkflowResult.from_state(state)

    assert WorkflowState.model_validate_json(state.model_dump_json()) == state
    assert WorkflowResult.model_validate_json(result.model_dump_json()) == result
    assert result.event_count == 1


def test_workflow_event_rejects_non_utc_timestamp() -> None:
    non_utc = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))

    with pytest.raises(ValidationError, match="timestamp must use UTC"):
        WorkflowEvent(
            seq=1,
            kind="started",
            details={},
            recorded_at=non_utc,
            before_status=SagaStatus.RUNNING,
            after_status=SagaStatus.RUNNING,
        )


def test_human_required_result_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="human-required result needs a reason"):
        WorkflowResult(
            saga_id=SAGA_ID,
            status=SagaStatus.HUMAN_REQUIRED,
            event_count=0,
            human_required_reason=None,
        )


def test_workflow_state_rejects_gaps_and_invalid_human_reason() -> None:
    gap = _event(2)
    base: dict[str, object] = {"saga_id": SAGA_ID, "compensations": ()}

    with pytest.raises(ValidationError, match="events must be contiguous"):
        WorkflowState.model_validate({"status": SagaStatus.RUNNING, "events": (gap,), **base})
    with pytest.raises(ValidationError, match="state needs exactly one reason"):
        WorkflowState.model_validate({"status": SagaStatus.HUMAN_REQUIRED, "events": (), **base})
    with pytest.raises(ValidationError, match="state needs exactly one reason"):
        WorkflowState.model_validate(
            {
                "status": SagaStatus.RUNNING,
                "events": (),
                "human_required_reason": "unexpected_reason",
                **base,
            }
        )


@pytest.mark.parametrize(
    "tool",
    [
        {"name": "lookup", "kind": "read", "compensation_tool": "undo_lookup"},
        {"name": "charge", "kind": "effect", "compensation_tool": None},
    ],
)
def test_workflow_tool_requires_kind_consistent_compensation(
    tool: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="tool kind"):
        WorkflowTool.model_validate(tool)


def test_reconciliation_request_accepts_only_forward_safe_correlation() -> None:
    forward = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    request = ReconciliationActivityRequest(
        forward_identity=forward,
        tool_name="charge",
        arguments={"order_id": "order_1"},
        correlation_id=forward.operation_id,
        declared_kind="effect",
        declared_compensation_tool="refund",
    )
    invalid = request.model_dump()
    invalid["correlation_id"] = "op_" + "1" * 64

    assert request.correlation_id == forward.operation_id
    with pytest.raises(ValidationError, match="safe correlation"):
        ReconciliationActivityRequest.model_validate(invalid)


def test_reconciliation_request_requires_a_forward_identity() -> None:
    identity = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.COMPENSATION, 0)

    with pytest.raises(ValidationError, match="requires a forward identity"):
        ReconciliationActivityRequest(
            forward_identity=identity,
            tool_name="charge",
            arguments={},
            correlation_id=identity.operation_id,
            declared_kind="effect",
            declared_compensation_tool="refund",
        )


def test_compensation_reconciliation_requires_its_causal_forward_operation() -> None:
    forward = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    other_step = "step_87654321"
    other_forward = ActivityIdentity.create(SAGA_ID, other_step, Direction.FORWARD, 0)
    compensation = CompensationActivityRequest(
        identity=ActivityIdentity.create(SAGA_ID, other_step, Direction.COMPENSATION, 0),
        tool_name="refund",
        arguments={},
        compensates_operation_id=other_forward.operation_id,
        forward_receipt={"charge_id": "charge_1"},
    )

    with pytest.raises(ValidationError, match="causal forward operation"):
        ReconciliationActivityRequest(
            forward_identity=forward,
            tool_name="refund",
            arguments={},
            correlation_id=compensation.identity.operation_id,
            declared_kind="effect",
            declared_compensation_tool="refund",
            compensation=compensation,
        )


def test_forward_reconciliation_requires_a_reversible_effect_declaration() -> None:
    forward = ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)

    with pytest.raises(ValidationError, match="declared reversible effect"):
        ReconciliationActivityRequest(
            forward_identity=forward,
            tool_name="lookup",
            arguments={},
            correlation_id=forward.operation_id,
            declared_kind="read",
        )


@pytest.mark.parametrize("outcome", ["pending", "conflict", "unsupported"])
def test_reconciliation_result_fails_closed_without_a_reason(outcome: str) -> None:
    with pytest.raises(ValidationError, match="requires exactly one reason"):
        ReconciliationActivityResult.model_validate({"outcome": outcome})


@pytest.mark.parametrize(
    "result",
    [
        {
            "outcome": "confirmed_effect",
            "receipt": {"charge": "confirmed"},
            "reason_code": "unexpected_reason",
        },
        {
            "outcome": "confirmed_no_effect",
            "receipt": {"charge": "absent"},
            "reason_code": "not_found",
        },
    ],
)
def test_reconciliation_result_rejects_ambiguous_receipt_and_reason(
    result: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ReconciliationActivityResult.model_validate(result)


def test_reconciliation_confirmation_requires_public_receipt() -> None:
    confirmed = ReconciliationActivityResult.confirmed_effect({"charge": "confirmed"})

    assert confirmed.receipt is not None
    with pytest.raises(ValidationError, match="public and redacted"):
        ReconciliationActivityResult.confirmed_effect({"api_key": "secret"})


def test_workflow_rejects_unknown_and_cyclic_prerequisites() -> None:
    goal = SagaGoal(goal_id="checkout", text="Checkout safely.", context={})
    unknown = (WorkflowTool(name="ship", kind="read", prerequisites=("charge",)),)
    cyclic = (
        WorkflowTool(name="reserve", kind="read", prerequisites=("charge",)),
        WorkflowTool(name="charge", kind="read", prerequisites=("reserve",)),
    )

    with pytest.raises(ValidationError, match="known tools"):
        SagaWorkflowInput(
            saga_id=SAGA_ID, goal=goal, tools=unknown, max_agent_turns=2, max_tool_calls=2
        )
    with pytest.raises(ValidationError, match="acyclic"):
        SagaWorkflowInput(
            saga_id=SAGA_ID, goal=goal, tools=cyclic, max_agent_turns=2, max_tool_calls=2
        )


@pytest.mark.parametrize(
    "prerequisites",
    [("reserve",), ("lookup", "lookup")],
    ids=["self", "duplicate"],
)
def test_workflow_tool_rejects_self_and_duplicate_prerequisites(
    prerequisites: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError, match="unique and exclude the tool"):
        WorkflowTool(name="reserve", kind="read", prerequisites=prerequisites)


def test_workflow_rejects_duplicate_tool_names() -> None:
    goal = SagaGoal(goal_id="checkout", text="Checkout safely.", context={})
    tools = (
        WorkflowTool(name="lookup", kind="read"),
        WorkflowTool(name="lookup", kind="read"),
    )

    with pytest.raises(ValidationError, match="workflow tools must be unique"):
        SagaWorkflowInput(
            saga_id=SAGA_ID,
            goal=goal,
            tools=tools,
            max_agent_turns=2,
            max_tool_calls=2,
        )


def test_proof_capability_is_restricted_to_reads() -> None:
    with pytest.raises(ValidationError, match="proof"):
        WorkflowTool(
            name="charge",
            kind="effect",
            compensation_tool="refund",
            proof_for_success=True,
        )


def test_agent_decision_observation_is_bounded() -> None:
    events = tuple(_event(index, kind="read") for index in range(1, 22))

    with pytest.raises(ValidationError):
        AgentDecisionObservation(
            recent_events=events,
            completed_tools=(),
            tool_call_counts=(),
            compensation_count=0,
            total_event_count=21,
            remaining_turns=1,
            remaining_tool_calls=1,
        )


def test_sensitive_agent_arguments_are_rejected_before_dispatch() -> None:
    proposal = ToolCall(
        proposal_id="proposal_12345678",
        tool_name="charge",
        arguments={"api_key": "must_not_persist"},
        based_on_saga_seq=1,
        rationale="Attempt an unsafe argument.",
    )

    with pytest.raises(ValidationError, match="public and redacted"):
        AgentDecisionResult(proposal=proposal)


def test_tool_activity_request_requires_exactly_one_request() -> None:
    forward = ForwardActivityRequest(
        identity=ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0),
        tool_name="reserve",
        arguments={},
        declared_kind="effect",
        declared_compensation_tool="release",
    )
    compensation = CompensationActivityRequest(
        identity=ActivityIdentity.create(SAGA_ID, STEP_ID, Direction.COMPENSATION, 0),
        tool_name="release",
        arguments={},
        compensates_operation_id=forward.identity.operation_id,
        forward_receipt={"reservation_id": "reservation_1"},
    )

    with pytest.raises(ValidationError, match="exactly one request"):
        ToolActivityRequest()
    with pytest.raises(ValidationError, match="exactly one request"):
        ToolActivityRequest(forward=forward, compensation=compensation)


def test_constructed_absent_tool_request_denies_identity_and_tool_access() -> None:
    absent = ToolActivityRequest.model_construct()

    with pytest.raises(ValueError, match="request is absent"):
        absent.identity()
    with pytest.raises(ValueError, match="request is absent"):
        absent.tool_name()


def test_tool_activity_result_rejects_private_receipt() -> None:
    with pytest.raises(ValidationError, match="public and redacted"):
        ToolActivityResult.succeeded({"api_key": "must_not_persist"})


@pytest.mark.parametrize(
    "result",
    [
        {
            "outcome": "succeeded",
            "receipt": {"reservation_id": "reservation_1"},
            "reason_code": "unexpected_reason",
        },
        {
            "outcome": "failed",
            "receipt": {"reservation_id": "reservation_1"},
            "reason_code": "provider_rejected",
        },
    ],
)
def test_tool_activity_result_rejects_ambiguous_success_and_failure_shapes(
    result: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ToolActivityResult.model_validate(result)


def test_human_resolution_requires_opaque_authorization_and_public_receipt() -> None:
    values = {
        "operation_id": "op_" + "1" * 64,
        "based_on_event_seq": 3,
        "authorization_reference": "authorization_secret",
        "receipt": {"confirmed": True},
    }

    with pytest.raises(ValidationError):
        HumanCompensationResolution.model_validate(values)
    values["authorization_reference"] = "authz_1234567890abcdef"
    values["receipt"] = {"api_key": "must_not_persist"}
    with pytest.raises(ValidationError, match="public and redacted"):
        HumanCompensationResolution.model_validate(values)


def test_human_resolution_requires_a_non_empty_receipt() -> None:
    with pytest.raises(ValidationError, match="requires a receipt"):
        HumanCompensationResolution(
            operation_id="op_" + "1" * 64,
            based_on_event_seq=3,
            authorization_reference="authz_1234567890abcdef",
            receipt={},
        )


def test_human_verification_result_has_exact_trusted_shape() -> None:
    verified = HumanResolutionVerificationResult.accepted(
        trusted_actor="operator_1", authorization_id="authz_1234567890abcdef"
    )

    assert verified.trusted_actor == "operator_1"
    with pytest.raises(ValidationError, match="verified result"):
        HumanResolutionVerificationResult.model_validate({"verified": True})
