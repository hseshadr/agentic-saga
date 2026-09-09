from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agentic_saga.contracts.common import Direction, sha256_json
from agentic_saga.contracts.events import (
    DispatchStarted,
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    ReconciliationRecorded,
    SagaCreated,
    SagaStarted,
)
from agentic_saga.contracts.outcomes import (
    OutcomeUnknown,
    ReconcileConflict,
    ReconcileEffectConfirmed,
    ReconcileNoEffectConfirmed,
    ReconcilePending,
    ReconcileUnsupported,
    ReconciliationOutcome,
    safe_outcome_json,
    safe_reconciliation_json,
)
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.tools import ReconcileContext
from agentic_saga.execution.reconciliation import (
    ReconciliationPlanner,
    RecoveryHorizon,
    retention_covers_horizon,
)
from agentic_saga.kernel.reducer import InvalidTransition, reduce_event
from agentic_saga.kernel.state import OperationStatus, SagaSnapshot

SAGA_ID = "saga_0000000000009001"
OPERATION_ID = f"op_{'9' * 64}"
NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
COMMAND = {"amount_minor": 14900}
COMMAND_HASH = sha256_json(COMMAND)
POLICY_HASH = "a" * 64


def _base(seq: int) -> dict[str, object]:
    return {
        "event_id": f"evt_{seq:016d}",
        "saga_id": SAGA_ID,
        "saga_seq": seq,
        "definition_version": "checkout-v1",
        "fence_token": 1,
        "actor": "reconciler",
        "trace_id": "trace_0000000000009001",
        "recorded_at": NOW + timedelta(seconds=seq),
    }


def _effect(seq: int) -> dict[str, object]:
    return _base(seq) | {
        "operation_id": OPERATION_ID,
        "step_instance_id": "step_00009001",
        "direction": Direction.FORWARD,
        "semantic_generation": 0,
        "delivery_attempt": 1,
        "tool_name": "charge_payment",
        "redacted_command": COMMAND,
        "command_hash": COMMAND_HASH,
    }


def _unknown_snapshot() -> SagaSnapshot:
    created = SagaCreated.model_validate(
        _base(1)
        | {
            "fence_token": None,
            "definition_name": "checkout",
            "definition_fingerprint": "f" * 64,
            "redacted_goal": {},
        }
    )
    started = SagaStarted.model_validate(_base(2))
    intent = EffectIntentRecorded.model_validate(_effect(3) | {"compensate_with": "refund"})
    dispatch = DispatchStarted.model_validate(_effect(4))
    unknown = _unknown_event()
    snapshot = reduce_event(None, created)
    for event in (started, intent, dispatch, unknown):
        snapshot = reduce_event(snapshot, event)
    return snapshot


def _unknown_event() -> EffectOutcomeRecorded:
    outcome = OutcomeUnknown(correlation="opaque://provider/request00000001/v1")
    result = safe_outcome_json(outcome)
    proof = {"outcome": outcome, "redacted_result": result, "result_hash": sha256_json(result)}
    return EffectOutcomeRecorded.model_validate(_effect(5) | proof)


def _recorded(
    snapshot: SagaSnapshot, outcome: ReconciliationOutcome, action: str
) -> ReconciliationRecorded:
    safe = safe_reconciliation_json(outcome)
    return ReconciliationRecorded.model_validate(
        _effect(snapshot.seq + 1)
        | {
            "reconciliation_attempt": 1,
            "outcome": outcome,
            "action": action,
            "redacted_result": safe,
            "result_hash": sha256_json(safe),
            "recovery_policy_digest": POLICY_HASH,
        }
    )


@pytest.mark.parametrize(
    ("outcome", "covers", "action"),
    [
        (ReconcileEffectConfirmed(receipt={"payment_id": "pay_1"}), False, "confirm"),
        (ReconcileNoEffectConfirmed(reason="not found"), False, "retry_same_id"),
        (
            ReconcilePending(correlation="opaque://provider/request00000001/v1", check_after=NOW),
            True,
            "wait",
        ),
        (ReconcileConflict(reason="two records"), True, "human"),
        (ReconcileUnsupported(reason="no lookup"), False, "human"),
        (ReconcileUnsupported(reason="no lookup"), True, "retry_same_id"),
    ],
)
def test_should_follow_fail_closed_reconciliation_table(
    outcome: ReconciliationOutcome, covers: bool, action: str
) -> None:
    # Given / When
    decision = ReconciliationPlanner().decide(outcome, covers)

    # Then
    assert decision.action == action


def test_should_reject_non_opaque_reconcile_context_correlation() -> None:
    # Given
    payload = {
        "saga_id": SAGA_ID,
        "step_instance_id": "step_00009001",
        "operation_id": OPERATION_ID,
        "fence_token": 1,
        "delivery_attempt": 1,
        "correlation": "secret provider text",
    }

    # When / Then
    with pytest.raises(ValidationError, match="opaque"):
        ReconcileContext.model_validate(payload)


def test_should_cover_exact_recovery_horizon_boundary() -> None:
    # Given
    horizon = RecoveryHorizon(
        maximum_retry_delay=timedelta(minutes=5),
        operator_response_window=timedelta(minutes=10),
        clock_skew_allowance=timedelta(seconds=30),
    )
    first_dispatch = NOW
    boundary_now = NOW + timedelta(hours=1) - horizon.total

    # When / Then
    assert retention_covers_horizon(boundary_now, first_dispatch, 3600, horizon)
    assert not retention_covers_horizon(
        boundary_now + timedelta(microseconds=1), first_dispatch, 3600, horizon
    )
    assert not retention_covers_horizon(NOW, first_dispatch, None, horizon)


def test_should_encode_recovery_horizon_without_microsecond_truncation() -> None:
    # Given
    horizon = RecoveryHorizon(
        maximum_retry_delay=timedelta(microseconds=1),
        operator_response_window=timedelta(microseconds=2),
        clock_skew_allowance=timedelta(microseconds=3),
    )

    # When
    policy = horizon.public_policy()

    # Then
    assert policy.maximum_retry_delay_microseconds == 1
    assert policy.operator_response_window_microseconds == 2
    assert policy.clock_skew_allowance_microseconds == 3


@pytest.mark.parametrize(
    ("outcome", "action", "status", "saga_status"),
    [
        (
            ReconcileEffectConfirmed(receipt={"payment_id": "pay_1"}),
            "confirm",
            OperationStatus.EFFECT_CONFIRMED,
            SagaStatus.RUNNING,
        ),
        (
            ReconcileNoEffectConfirmed(reason="provider_confirmed_no_effect"),
            "retry_same_id",
            OperationStatus.INTENT_DURABLE,
            SagaStatus.RUNNING,
        ),
        (
            ReconcilePending(
                correlation="opaque://provider/request00000002/v1",
                check_after=NOW + timedelta(minutes=1),
            ),
            "wait",
            OperationStatus.OUTCOME_UNKNOWN,
            SagaStatus.RECONCILING_UNKNOWN,
        ),
        (
            ReconcileConflict(reason="provider_evidence_conflict"),
            "human",
            OperationStatus.OUTCOME_UNKNOWN,
            SagaStatus.RECONCILING_UNKNOWN,
        ),
    ],
)
def test_should_reduce_dedicated_reconciliation_evidence(
    outcome: ReconciliationOutcome,
    action: str,
    status: OperationStatus,
    saga_status: SagaStatus,
) -> None:
    # Given
    snapshot = _unknown_snapshot()

    # When
    projected = reduce_event(snapshot, _recorded(snapshot, outcome, action))

    # Then
    assert projected.operations[OPERATION_ID].status is status
    assert projected.status is saga_status


def test_should_reject_effect_outcome_as_reconciliation_evidence() -> None:
    # Given
    snapshot = _unknown_snapshot()
    receipt = ReconcileEffectConfirmed(receipt={"payment_id": "pay_1"})
    safe = {"kind": "effect_confirmed", "receipt": receipt.receipt}
    forged = EffectOutcomeRecorded.model_validate(
        _effect(snapshot.seq + 1)
        | {
            "outcome": safe,
            "redacted_result": safe,
            "result_hash": sha256_json(safe),
        }
    )

    # When / Then
    with pytest.raises(InvalidTransition, match="reconciliation"):
        reduce_event(snapshot, forged)


@pytest.mark.parametrize(
    ("outcome", "action"),
    [
        (ReconcileEffectConfirmed(receipt={"payment_id": "pay_1"}), "human"),
        (ReconcileNoEffectConfirmed(reason="provider_confirmed_no_effect"), "confirm"),
        (
            ReconcilePending(
                correlation="opaque://provider/request00000002/v1",
                check_after=NOW + timedelta(minutes=1),
            ),
            "retry_same_id",
        ),
        (ReconcileConflict(reason="provider_evidence_conflict"), "wait"),
    ],
)
def test_should_reject_action_that_contradicts_reconciliation_evidence(
    outcome: ReconciliationOutcome, action: str
) -> None:
    # Given
    snapshot = _unknown_snapshot()

    # When / Then
    with pytest.raises(ValidationError, match="action"):
        _recorded(snapshot, outcome, action)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("maximum_retry_delay", timedelta(0)),
        ("operator_response_window", timedelta(0)),
        ("clock_skew_allowance", timedelta(microseconds=-1)),
    ],
)
def test_should_reject_invalid_recovery_horizon(field: str, value: timedelta) -> None:
    # Given
    raw = {
        "maximum_retry_delay": timedelta(seconds=1),
        "operator_response_window": timedelta(seconds=1),
        "clock_skew_allowance": timedelta(0),
    }

    # When / Then
    with pytest.raises(ValidationError):
        RecoveryHorizon.model_validate(raw | {field: value})
