from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from agentic_saga.contracts.common import Direction, JsonObject
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.temporal.contracts import ActivityIdentity, WorkflowEvent, WorkflowState
from agentic_saga.temporal.trace import project_run_trace

_START = datetime(2026, 9, 19, 12, tzinfo=UTC)
_SAGA_ID = "saga_1234567890abcdef"


def _event(
    seq: int,
    kind: str,
    details: dict[str, object],
    before: SagaStatus,
    after: SagaStatus,
) -> WorkflowEvent:
    return WorkflowEvent(
        seq=seq,
        kind=kind,
        details=cast(JsonObject, details),
        recorded_at=_START + timedelta(seconds=seq - 1),
        before_status=before,
        after_status=after,
    )


def test_projector_builds_strict_trace_and_terminal_proof() -> None:
    identity = ActivityIdentity.create(_SAGA_ID, "step_00000001", Direction.FORWARD, 0)
    state = WorkflowState(
        saga_id=_SAGA_ID,
        status=SagaStatus.SUCCEEDED_VERIFIED,
        events=(
            _event(
                1,
                "started",
                {"goal_id": "checkout"},
                SagaStatus.RUNNING,
                SagaStatus.RUNNING,
            ),
            _event(
                2,
                "forward_result",
                {
                    "arguments": {"order_id": "order_demo_001"},
                    "operation_id": identity.operation_id,
                    "outcome": "succeeded",
                    "proof_for_success": True,
                    "public_receipt": {"verified": True},
                    "reason_code": None,
                    "tool_name": "verify_order",
                    "verified": True,
                },
                SagaStatus.RUNNING,
                SagaStatus.RUNNING,
            ),
            _event(
                3,
                "status_changed",
                {"reason_code": None, "status": "succeeded_verified"},
                SagaStatus.RUNNING,
                SagaStatus.SUCCEEDED_VERIFIED,
            ),
        ),
        compensations=(),
    )

    trace = project_run_trace(state, definition_version="checkout-v1")

    assert trace.outcome is SagaStatus.SUCCEEDED_VERIFIED
    assert trace.finished_at == state.events[-1].recorded_at
    assert trace.events[0].before_status is None
    assert trace.events[1].event_type == "invariant_evaluated"
    assert trace.events[1].redacted_input == {"order_id": "order_demo_001"}
    assert trace.events[1].redacted_output == {
        "kind": "read_observed",
        "verified": True,
    }
    assert trace.events[1].input_hash is not None
    assert trace.events[1].output_hash is not None
    assert trace.proofs[0].rule_id == "verify_order"
    assert trace.proofs[0].result == "valid"


def test_projector_keeps_human_required_trace_open() -> None:
    state = WorkflowState(
        saga_id=_SAGA_ID,
        status=SagaStatus.HUMAN_REQUIRED,
        events=(
            _event(1, "started", {"goal_id": "checkout"}, SagaStatus.RUNNING, SagaStatus.RUNNING),
            _event(
                2,
                "status_changed",
                {"reason_code": "provider_pending", "status": "human_required"},
                SagaStatus.RUNNING,
                SagaStatus.HUMAN_REQUIRED,
            ),
        ),
        compensations=(),
        human_required_reason="provider_pending",
    )

    trace = project_run_trace(state, definition_version="checkout-v1")

    assert trace.outcome is SagaStatus.HUMAN_REQUIRED
    assert trace.finished_at is None
    assert trace.events[-1].event_type == "human_required"


def test_projector_redacts_sensitive_workflow_details() -> None:
    state = WorkflowState(
        saga_id=_SAGA_ID,
        status=SagaStatus.COMPENSATING,
        events=(
            _event(
                1,
                "started",
                {"goal_id": "checkout"},
                SagaStatus.RUNNING,
                SagaStatus.HUMAN_REQUIRED,
            ),
            _event(
                2,
                "human_resolved",
                {
                    "actor": "checkout_operator",
                    "authorization_id": "Bearer synthetic-secret",
                    "operation_id": "op_" + "a" * 64,
                },
                SagaStatus.HUMAN_REQUIRED,
                SagaStatus.COMPENSATING,
            ),
        ),
        compensations=(),
    )

    trace = project_run_trace(state, definition_version="checkout-v1")

    assert trace.events[-1].rationale["authorization_id"] == "[REDACTED]"
    assert "synthetic-secret" not in trace.model_dump_json()
