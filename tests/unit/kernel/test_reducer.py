from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from agentic_saga.contracts.common import Direction, JsonObject, sha256_json
from agentic_saga.contracts.events import (
    ApprovalConsumed,
    CompensationIntentRecorded,
    CompensationStarted,
    DispatchAbortedBeforeEntry,
    DispatchStarted,
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    HumanRequired,
    HumanResolutionRecorded,
    InvariantEvaluated,
    LedgerEvent,
    ReconciliationRecorded,
    RecoveryPlanAccepted,
    RecoveryPlanRejected,
    RecoveryPlanRequired,
    SagaCreated,
    SagaStarted,
    TerminalAssigned,
)
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    NoEffectConfirmed,
    OutcomeUnknown,
    PartialEffectConfirmed,
    ReconcileEffectConfirmed,
    ReconcileNoEffectConfirmed,
    ReconciliationOutcome,
    normalize_effect_outcome,
    safe_outcome_json,
    safe_reconciliation_json,
)
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.kernel.reducer import InvalidTransition, SequenceGap, reduce_event
from agentic_saga.kernel.state import (
    CompensationObligation,
    ObligationStatus,
    OperationStatus,
    SagaSnapshot,
)

SAGA_ID = "saga_0000000000000001"
OTHER_SAGA_ID = "saga_0000000000000002"
FORWARD_OP = f"op_{'a' * 64}"
COMPENSATION_OP = f"op_{'b' * 64}"
NEXT_COMPENSATION_OP = f"op_{'e' * 64}"
SECOND_OP = f"op_{'f' * 64}"
THIRD_OP = f"op_{'9' * 64}"
STEP_ID = "step_00000001"
SECOND_STEP_ID = "step_00000002"
THIRD_STEP_ID = "step_00000003"
TRACE_ID = "trace_0000000000000001"
RECORDED_AT = datetime(2026, 9, 6, 12, tzinfo=UTC)
HASH = "c" * 64
OTHER_HASH = "d" * 64


def _metadata(seq: int) -> dict[str, object]:
    return {
        "event_id": f"evt_{seq:016d}",
        "saga_id": SAGA_ID,
        "saga_seq": seq,
        "schema_version": "1.0",
        "definition_version": "checkout-v1",
        "fence_token": None,
        "actor": "kernel",
        "trace_id": TRACE_ID,
        "recorded_at": RECORDED_AT + timedelta(seconds=seq),
    }


def _effect_metadata(
    operation_id: str = FORWARD_OP,
    direction: Direction = Direction.FORWARD,
    tool_name: str = "charge_payment",
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "step_instance_id": STEP_ID,
        "direction": direction,
        "semantic_generation": 0,
        "delivery_attempt": 1,
        "tool_name": tool_name,
        "redacted_command": {"amount_minor": 14900},
        "command_hash": HASH,
    }


def saga_created(seq: int = 1) -> SagaCreated:
    payload = _metadata(seq) | {
        "event_type": "saga_created",
        "definition_name": "checkout",
        "definition_fingerprint": "f" * 64,
        "redacted_goal": {"order_id": "order-1"},
    }
    return SagaCreated.model_validate(payload)


def saga_started(seq: int = 2) -> SagaStarted:
    return SagaStarted.model_validate(_metadata(seq) | {"event_type": "saga_started"})


def effect_intent(
    seq: int = 3,
    compensate_with: str | None = "refund_payment",
) -> EffectIntentRecorded:
    payload = _metadata(seq) | _effect_metadata()
    payload |= {"event_type": "effect_intent_recorded", "compensate_with": compensate_with}
    return EffectIntentRecorded.model_validate(payload)


def dispatch_started(
    seq: int = 4,
    operation_id: str = FORWARD_OP,
    direction: Direction = Direction.FORWARD,
    tool_name: str = "charge_payment",
) -> DispatchStarted:
    payload = _metadata(seq) | _effect_metadata(operation_id, direction, tool_name)
    return DispatchStarted.model_validate(payload | {"event_type": "dispatch_started"})


def effect_outcome(
    seq: int,
    outcome: EffectOutcome,
    operation_id: str = FORWARD_OP,
    direction: Direction = Direction.FORWARD,
    tool_name: str = "charge_payment",
) -> EffectOutcomeRecorded:
    payload = _metadata(seq) | _effect_metadata(operation_id, direction, tool_name)
    safe_outcome = normalize_effect_outcome(
        outcome,
        fallback_correlation="opaque://agentic-saga/reducerfixture1/v1",
    )
    result = safe_outcome_json(safe_outcome)
    payload |= {"event_type": "effect_outcome_recorded", "outcome": safe_outcome}
    payload |= {"redacted_result": result, "result_hash": sha256_json(result)}
    return EffectOutcomeRecorded.model_validate(payload)


def reconciliation_outcome(
    seq: int,
    outcome: ReconciliationOutcome,
    operation_id: str = FORWARD_OP,
    direction: Direction = Direction.FORWARD,
    tool_name: str = "charge_payment",
) -> ReconciliationRecorded:
    payload = _metadata(seq) | _effect_metadata(operation_id, direction, tool_name)
    result = safe_reconciliation_json(outcome)
    payload |= {
        "reconciliation_attempt": 1,
        "outcome": outcome,
        "action": "confirm",
        "redacted_result": result,
        "result_hash": sha256_json(result),
        "recovery_policy_digest": HASH,
    }
    return ReconciliationRecorded.model_validate(payload)


def reconciliation_retry(seq: int, operation_id: str = FORWARD_OP) -> ReconciliationRecorded:
    outcome = ReconcileNoEffectConfirmed(reason="provider_confirmed_no_effect")
    result = safe_reconciliation_json(outcome)
    payload = _metadata(seq) | _effect_metadata(operation_id)
    payload |= {
        "reconciliation_attempt": 1,
        "outcome": outcome,
        "action": "retry_same_id",
        "redacted_result": result,
        "result_hash": sha256_json(result),
        "recovery_policy_digest": HASH,
    }
    return ReconciliationRecorded.model_validate(payload)


def dispatch_aborted(seq: int, *, delivery_attempt: int = 1) -> DispatchAbortedBeforeEntry:
    effect = _effect_metadata()
    effect["delivery_attempt"] = delivery_attempt
    payload = _metadata(seq) | effect
    return DispatchAbortedBeforeEntry.model_validate(payload)


def approval_consumed(seq: int, decision_id: str = "decision_01") -> ApprovalConsumed:
    payload = _metadata(seq) | {
        "decision_id": decision_id,
        "proposal_hash": HASH,
        "verification_result": True,
    }
    return ApprovalConsumed.model_validate(payload)


def recovery_required(seq: int = 3) -> RecoveryPlanRequired:
    payload = _metadata(seq) | {"event_type": "recovery_plan_required", "reason_code": "blocked"}
    return RecoveryPlanRequired.model_validate(payload)


def recovery_accepted(seq: int = 4) -> RecoveryPlanAccepted:
    payload = _metadata(seq) | {"event_type": "recovery_plan_accepted", "plan_hash": HASH}
    return RecoveryPlanAccepted.model_validate(payload)


def recovery_rejected(seq: int = 4) -> RecoveryPlanRejected:
    payload = _metadata(seq) | {
        "event_type": "recovery_plan_rejected",
        "reason_code": "unsafe_plan",
    }
    return RecoveryPlanRejected.model_validate(payload)


def compensation_started(seq: int = 6) -> CompensationStarted:
    return CompensationStarted.model_validate(
        _metadata(seq) | {"event_type": "compensation_started"}
    )


def compensation_intent(
    seq: int = 7,
    operation_id: str = COMPENSATION_OP,
    semantic_generation: int = 0,
) -> CompensationIntentRecorded:
    effect = _effect_metadata(operation_id, Direction.COMPENSATION, "refund_payment")
    effect["semantic_generation"] = semantic_generation
    payload = _metadata(seq) | effect
    payload |= {
        "event_type": "compensation_intent_recorded",
        "compensates_operation_id": FORWARD_OP,
        "forward_receipts": ({"payment_id": "payment-1"},),
    }
    return CompensationIntentRecorded.model_validate(payload)


def invariant_evaluated(
    seq: int,
    evaluated_at_seq: int,
    target_status: SagaStatus = SagaStatus.SUCCEEDED_VERIFIED,
) -> InvariantEvaluated:
    payload = _metadata(seq) | {
        "event_type": "invariant_evaluated",
        "evaluated_at_seq": evaluated_at_seq,
        "target_status": target_status,
        "invariant_version": "checkout-invariants-v1",
        "evidence_digest": HASH,
        "results": {"order_fulfilled": True},
        "all_passed": True,
    }
    return InvariantEvaluated.model_validate(payload)


def human_required(seq: int = 3) -> HumanRequired:
    payload = _metadata(seq) | {"event_type": "human_required", "reason_code": "approval"}
    return HumanRequired.model_validate(payload)


def human_resolution(seq: int = 4, verified: bool = True) -> HumanResolutionRecorded:
    payload = _metadata(seq) | {
        "event_type": "human_resolution_recorded",
        "decision_id": "decision_01",
        "proposal_hash": HASH,
        "verification_result": verified,
    }
    return HumanResolutionRecorded.model_validate(payload)


def terminal_assigned(seq: int, status: SagaStatus) -> TerminalAssigned:
    payload = _metadata(seq) | {"event_type": "terminal_assigned", "status": status}
    return TerminalAssigned.model_validate(payload)


def running_snapshot() -> SagaSnapshot:
    created = reduce_event(None, saga_created())
    return reduce_event(created, saga_started())


def dispatched_snapshot() -> SagaSnapshot:
    intended = reduce_event(running_snapshot(), effect_intent())
    return reduce_event(intended, dispatch_started())


def confirmed_snapshot() -> SagaSnapshot:
    outcome = EffectConfirmed(receipt={"payment_id": "payment-1"})
    return reduce_event(dispatched_snapshot(), effect_outcome(5, outcome))


def alternate_intent(seq: int, operation_id: str = SECOND_OP) -> EffectIntentRecorded:
    step_id = THIRD_STEP_ID if operation_id == THIRD_OP else SECOND_STEP_ID
    return effect_intent(seq).model_copy(
        update={"operation_id": operation_id, "step_instance_id": step_id}
    )


def alternate_dispatch(seq: int, operation_id: str = SECOND_OP) -> DispatchStarted:
    step_id = THIRD_STEP_ID if operation_id == THIRD_OP else SECOND_STEP_ID
    return dispatch_started(seq, operation_id).model_copy(update={"step_instance_id": step_id})


def alternate_outcome(seq: int, outcome: EffectOutcome) -> EffectOutcomeRecorded:
    return effect_outcome(seq, outcome, SECOND_OP).model_copy(
        update={"step_instance_id": SECOND_STEP_ID}
    )


def two_unknown_operations_snapshot() -> SagaSnapshot:
    unknown = OutcomeUnknown(correlation="provider-request-7")
    first = reduce_event(dispatched_snapshot(), effect_outcome(5, unknown))
    second = first.operations[FORWARD_OP].model_copy(
        update={"operation_id": SECOND_OP, "step_instance_id": SECOND_STEP_ID}
    )
    second_obligation = first.obligations[FORWARD_OP].model_copy(
        update={"forward_operation_id": SECOND_OP}
    )
    return SagaSnapshot(
        saga_id=SAGA_ID,
        seq=8,
        status=SagaStatus.RECONCILING_UNKNOWN,
        definition_version="checkout-v1",
        operations={FORWARD_OP: first.operations[FORWARD_OP], SECOND_OP: second},
        obligations={FORWARD_OP: first.obligations[FORWARD_OP], SECOND_OP: second_obligation},
    )


def partially_compensated_snapshot() -> SagaSnapshot:
    compensating = reduce_event(confirmed_snapshot(), compensation_started())
    intended = reduce_event(compensating, compensation_intent())
    dispatched = reduce_event(
        intended,
        dispatch_started(8, COMPENSATION_OP, Direction.COMPENSATION, "refund_payment"),
    )
    partial = PartialEffectConfirmed(receipts=({"refund_id": "refund-partial"},))
    return reduce_event(
        dispatched,
        effect_outcome(9, partial, COMPENSATION_OP, Direction.COMPENSATION, "refund_payment"),
    )


def test_should_create_saga_only_at_sequence_one() -> None:
    # Given / When / Then
    with pytest.raises(SequenceGap, match="sequence 1"):
        reduce_event(None, saga_created(seq=2))


def test_should_reject_duplicate_consumed_approval_ids_in_snapshot() -> None:
    # Given
    current = running_snapshot()
    payload = current.model_dump(mode="python") | {
        "consumed_approval_ids": ("decision_01", "decision_01")
    }

    # When / Then
    with pytest.raises(ValidationError, match="approval IDs must be unique"):
        SagaSnapshot.model_validate(payload)


def test_should_require_saga_created_as_first_event() -> None:
    # Given / When / Then
    with pytest.raises(InvalidTransition, match="SagaCreated"):
        reduce_event(None, saga_started(seq=1))


def test_should_require_exact_contiguous_sequence() -> None:
    # Given
    snapshot = reduce_event(None, saga_created())

    # When / Then
    with pytest.raises(SequenceGap, match="expected 2"):
        reduce_event(snapshot, saga_started(seq=3))


def test_should_reject_changed_saga_identity() -> None:
    # Given
    snapshot = reduce_event(None, saga_created())
    event = saga_started().model_copy(update={"saga_id": OTHER_SAGA_ID})

    # When / Then
    with pytest.raises(InvalidTransition, match="Saga ID"):
        reduce_event(snapshot, event)


def test_should_reject_changed_definition_identity() -> None:
    # Given
    snapshot = reduce_event(None, saga_created())
    event = saga_started().model_copy(update={"definition_version": "checkout-v2"})

    # When / Then
    with pytest.raises(InvalidTransition, match="definition"):
        reduce_event(snapshot, event)


def test_should_arm_compensation_before_dispatch() -> None:
    # Given
    snapshot = running_snapshot()

    # When
    intended = reduce_event(snapshot, effect_intent())

    # Then
    assert intended.operations[FORWARD_OP].status is OperationStatus.INTENT_DURABLE
    assert intended.obligations[FORWARD_OP].status is ObligationStatus.ARMED


def test_should_not_create_obligation_when_intent_has_no_compensation() -> None:
    # Given / When
    intended = reduce_event(running_snapshot(), effect_intent(compensate_with=None))

    # Then
    assert intended.obligations == {}


def test_should_reject_compensation_direction_in_forward_intent() -> None:
    # Given
    event = effect_intent().model_copy(update={"direction": Direction.COMPENSATION})

    # When / Then
    with pytest.raises(InvalidTransition, match="forward direction"):
        reduce_event(running_snapshot(), event)


def test_should_reject_duplicate_operation_intent() -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent())

    # When / Then
    with pytest.raises(InvalidTransition, match="already exists"):
        reduce_event(intended, effect_intent(seq=4))


def test_should_require_resolution_before_human_approved_effect_intent() -> None:
    # Given
    paused = reduce_event(running_snapshot(), human_required())

    # When / Then
    with pytest.raises(InvalidTransition, match="verified human resolution"):
        reduce_event(paused, effect_intent(seq=4))


def test_should_reject_dispatch_without_durable_intent() -> None:
    # Given
    snapshot = running_snapshot()

    # When / Then
    with pytest.raises(InvalidTransition, match="durable intent"):
        reduce_event(snapshot, dispatch_started(seq=3))


def test_should_reject_dispatch_while_human_approval_is_pending() -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent())
    paused = reduce_event(intended, human_required(seq=4))

    # When / Then
    with pytest.raises(InvalidTransition, match="quiescent"):
        reduce_event(paused, dispatch_started(seq=5))


def test_should_reject_dispatch_after_effect_has_been_confirmed() -> None:
    # Given
    snapshot = confirmed_snapshot()

    # When / Then
    with pytest.raises(InvalidTransition, match="durable intent"):
        reduce_event(snapshot, dispatch_started(seq=6))


def test_should_require_next_attempt_when_redispatching_confirmed_absence() -> None:
    # Given
    absent = NoEffectConfirmed(reason="provider proved absence")
    snapshot = reduce_event(dispatched_snapshot(), effect_outcome(5, absent))
    retry = dispatch_started(seq=6).model_copy(update={"delivery_attempt": 2})

    # When
    dispatched = reduce_event(snapshot, retry)

    # Then
    assert dispatched.operations[FORWARD_OP].status is OperationStatus.DISPATCHED
    assert dispatched.operations[FORWARD_OP].delivery_attempt == 2


def test_should_reject_nonmonotonic_redispatch_attempt() -> None:
    # Given
    absent = NoEffectConfirmed(reason="provider proved absence")
    snapshot = reduce_event(dispatched_snapshot(), effect_outcome(5, absent))

    # When / Then
    with pytest.raises(InvalidTransition, match="attempt"):
        reduce_event(snapshot, dispatch_started(seq=6))


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("step_instance_id", "step_00000002"),
        ("direction", Direction.COMPENSATION),
        ("semantic_generation", 1),
        ("tool_name", "capture_payment"),
        ("command_hash", OTHER_HASH),
    ],
)
def test_should_keep_operation_identity_stable(
    changed_field: str,
    changed_value: object,
) -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent())
    event = dispatch_started().model_copy(update={changed_field: changed_value})

    # When / Then
    with pytest.raises(InvalidTransition, match="metadata"):
        reduce_event(intended, event)


def test_should_reject_outcome_before_dispatch() -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent())
    outcome = EffectConfirmed(receipt={"payment_id": "payment-1"})

    # When / Then
    with pytest.raises(InvalidTransition, match="dispatch"):
        reduce_event(intended, effect_outcome(4, outcome))


def test_should_reject_changed_operation_metadata_in_outcome() -> None:
    # Given
    outcome = EffectConfirmed(receipt={"payment_id": "payment-1"})
    event = effect_outcome(5, outcome).model_copy(update={"command_hash": OTHER_HASH})

    # When / Then
    with pytest.raises(InvalidTransition, match="metadata"):
        reduce_event(dispatched_snapshot(), event)


def test_should_require_outcome_attempt_to_match_dispatch() -> None:
    # Given
    outcome = EffectConfirmed(receipt={"payment_id": "payment-1"})
    event = effect_outcome(5, outcome).model_copy(update={"delivery_attempt": 2})

    # When / Then
    with pytest.raises(InvalidTransition, match="attempt"):
        reduce_event(dispatched_snapshot(), event)


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_obligation"),
    [
        (
            EffectConfirmed(receipt={"payment_id": "payment-1"}),
            OperationStatus.EFFECT_CONFIRMED,
            ObligationStatus.ELIGIBLE,
        ),
        (
            PartialEffectConfirmed(receipts=({"payment_id": "payment-1"},)),
            OperationStatus.PARTIAL_EFFECT_CONFIRMED,
            ObligationStatus.ELIGIBLE,
        ),
        (
            NoEffectConfirmed(reason="provider proved absence"),
            OperationStatus.NO_EFFECT_CONFIRMED,
            ObligationStatus.NOT_REQUIRED,
        ),
        (
            OutcomeUnknown(correlation="provider-request-7"),
            OperationStatus.OUTCOME_UNKNOWN,
            ObligationStatus.ARMED,
        ),
    ],
)
def test_should_project_typed_effect_outcomes(
    outcome: EffectOutcome,
    expected_status: OperationStatus,
    expected_obligation: ObligationStatus,
) -> None:
    # Given
    snapshot = dispatched_snapshot()

    # When
    updated = reduce_event(snapshot, effect_outcome(5, outcome))

    # Then
    assert updated.operations[FORWARD_OP].status is expected_status
    assert updated.obligations[FORWARD_OP].status is expected_obligation


def test_should_treat_timeout_or_exception_as_unknown() -> None:
    # Given
    snapshot = dispatched_snapshot()
    timeout = OutcomeUnknown(correlation="TimeoutError:request-7")

    # When
    unknown = reduce_event(snapshot, effect_outcome(5, timeout))

    # Then
    assert unknown.operations[FORWARD_OP].status is OperationStatus.OUTCOME_UNKNOWN
    assert unknown.status is SagaStatus.RECONCILING_UNKNOWN


def test_should_reconcile_unknown_outcome_without_new_dispatch() -> None:
    # Given
    unknown = OutcomeUnknown(correlation="provider-request-7")
    snapshot = reduce_event(dispatched_snapshot(), effect_outcome(5, unknown))

    # When
    receipt = ReconcileEffectConfirmed(receipt={"payment_id": "payment-1"})
    reconciled = reduce_event(snapshot, reconciliation_outcome(6, receipt))

    # Then
    assert reconciled.status is SagaStatus.RUNNING
    assert reconciled.operations[FORWARD_OP].status is OperationStatus.EFFECT_CONFIRMED
    assert reconciled.obligations[FORWARD_OP].status is ObligationStatus.ELIGIBLE


def test_should_reject_reconciliation_for_an_already_known_outcome() -> None:
    # Given
    known = confirmed_snapshot()
    receipt = ReconcileEffectConfirmed(receipt={"payment_id": "payment-1"})

    # When / Then
    with pytest.raises(InvalidTransition, match="unknown operation"):
        reduce_event(known, reconciliation_outcome(6, receipt))


def test_should_keep_reconciling_when_one_of_multiple_unknowns_retries() -> None:
    # Given
    unknowns = two_unknown_operations_snapshot()

    # When
    retrying = reduce_event(unknowns, reconciliation_retry(9))

    # Then
    assert retrying.status is SagaStatus.RECONCILING_UNKNOWN
    assert retrying.operations[FORWARD_OP].status is OperationStatus.INTENT_DURABLE
    assert retrying.operations[SECOND_OP].status is OperationStatus.OUTCOME_UNKNOWN


def test_should_reject_pre_entry_abort_without_matching_dispatch() -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent())

    # When / Then
    with pytest.raises(InvalidTransition, match="requires prior dispatch"):
        reduce_event(intended, dispatch_aborted(4))


def test_should_reject_pre_entry_abort_for_a_different_delivery_attempt() -> None:
    # Given / When / Then
    with pytest.raises(InvalidTransition, match="attempt does not match"):
        reduce_event(dispatched_snapshot(), dispatch_aborted(5, delivery_attempt=2))


def test_should_remain_quiescent_until_every_unknown_operation_is_reconciled() -> None:
    # Given
    snapshot = two_unknown_operations_snapshot()
    receipt = ReconcileEffectConfirmed(receipt={"payment_id": "payment-1"})

    # When
    reconciled_one = reduce_event(snapshot, reconciliation_outcome(9, receipt))

    # Then
    assert reconciled_one.status is SagaStatus.RECONCILING_UNKNOWN
    with pytest.raises(InvalidTransition, match="quiescent"):
        reduce_event(reconciled_one, compensation_started(seq=10))


def test_should_block_mutation_when_another_operation_is_dispatched() -> None:
    # Given
    first = reduce_event(running_snapshot(), effect_intent())
    second = reduce_event(first, alternate_intent(4))
    dispatched = reduce_event(second, dispatch_started(seq=5))

    # When / Then
    with pytest.raises(InvalidTransition, match="quiescent"):
        reduce_event(dispatched, alternate_dispatch(6))
    with pytest.raises(InvalidTransition, match="quiescent"):
        reduce_event(dispatched, alternate_intent(6, THIRD_OP))


def test_should_block_compensation_when_any_operation_remains_unknown() -> None:
    # Given
    snapshot = two_unknown_operations_snapshot()
    receipt = ReconcileEffectConfirmed(receipt={"payment_id": "payment-1"})
    reconciled_one = reduce_event(snapshot, reconciliation_outcome(9, receipt))
    inconsistent = reconciled_one.model_copy(update={"status": SagaStatus.RUNNING})

    # When / Then
    with pytest.raises(InvalidTransition, match="quiescent"):
        reduce_event(inconsistent, compensation_started(seq=10))


def test_should_project_outcome_without_inventing_an_obligation() -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent(compensate_with=None))
    dispatched = reduce_event(intended, dispatch_started())

    # When
    receipt = EffectConfirmed(receipt={"payment_id": "payment-1"})
    confirmed = reduce_event(dispatched, effect_outcome(5, receipt))

    # Then
    assert confirmed.obligations == {}


def test_should_not_start_compensation_for_absent_effect() -> None:
    # Given
    no_effect = NoEffectConfirmed(reason="provider proved absence")
    snapshot = reduce_event(dispatched_snapshot(), effect_outcome(5, no_effect))

    # When / Then
    with pytest.raises(InvalidTransition):
        reduce_event(snapshot, compensation_started())


def test_should_not_start_compensation_for_unknown_effect() -> None:
    # Given
    unknown = OutcomeUnknown(correlation="provider-request-7")
    snapshot = reduce_event(dispatched_snapshot(), effect_outcome(5, unknown))

    # When / Then
    with pytest.raises(InvalidTransition):
        reduce_event(snapshot, compensation_started())


def test_should_abort_cleanly_after_authoritative_absence() -> None:
    # Given
    absent = NoEffectConfirmed(reason="provider proved absence")
    completed = reduce_event(dispatched_snapshot(), effect_outcome(5, absent))
    proved = reduce_event(
        completed,
        invariant_evaluated(6, evaluated_at_seq=5, target_status=SagaStatus.ABORTED_CLEAN),
    )

    # When
    terminal = reduce_event(proved, terminal_assigned(7, SagaStatus.ABORTED_CLEAN))

    # Then
    assert terminal.status is SagaStatus.ABORTED_CLEAN


def test_should_start_compensation_only_for_confirmed_effect() -> None:
    # Given
    snapshot = confirmed_snapshot()

    # When
    updated = reduce_event(snapshot, compensation_started())

    # Then
    assert updated.status is SagaStatus.COMPENSATING


def test_should_require_eligible_forward_operation_for_compensation_intent() -> None:
    # Given
    snapshot = confirmed_snapshot()
    compensating = reduce_event(snapshot, compensation_started())
    event = compensation_intent().model_copy(update={"compensates_operation_id": f"op_{'e' * 64}"})

    # When / Then
    with pytest.raises(InvalidTransition, match="eligible"):
        reduce_event(compensating, event)


def test_should_require_compensation_direction_for_compensation_intent() -> None:
    # Given
    compensating = reduce_event(confirmed_snapshot(), compensation_started())
    event = compensation_intent().model_copy(update={"direction": Direction.FORWARD})

    # When / Then
    with pytest.raises(InvalidTransition, match="compensation direction"):
        reduce_event(compensating, event)


def test_should_require_registered_compensation_tool_for_obligation() -> None:
    # Given
    compensating = reduce_event(confirmed_snapshot(), compensation_started())
    event = compensation_intent().model_copy(update={"tool_name": "cancel_order"})

    # When / Then
    with pytest.raises(InvalidTransition, match="compensation tool"):
        reduce_event(compensating, event)


def test_should_bind_compensation_intent_to_forward_generation() -> None:
    # Given
    compensating = reduce_event(confirmed_snapshot(), compensation_started())
    payload = compensation_intent().model_dump(mode="python") | {"semantic_generation": 1}
    event = CompensationIntentRecorded.model_validate(payload)

    # When / Then
    with pytest.raises(InvalidTransition, match="generation must match"):
        reduce_event(compensating, event)


def test_should_bind_compensation_intent_to_exact_forward_receipts() -> None:
    # Given
    compensating = reduce_event(confirmed_snapshot(), compensation_started())
    payload = compensation_intent().model_dump(mode="python") | {
        "forward_receipts": ({"payment_id": "different-payment"},)
    }
    event = CompensationIntentRecorded.model_validate(payload)

    # When / Then
    with pytest.raises(InvalidTransition, match="receipts do not match"):
        reduce_event(compensating, event)


def test_should_reject_new_compensation_identity_for_obligation_with_durable_identity() -> None:
    # Given
    compensating = reduce_event(confirmed_snapshot(), compensation_started())
    obligation = compensating.obligations[FORWARD_OP]
    persisted = CompensationObligation.model_validate(
        obligation.model_dump(mode="python") | {"compensation_operation_id": NEXT_COMPENSATION_OP}
    )
    current = SagaSnapshot.model_validate(
        compensating.model_dump(mode="python") | {"obligations": {FORWARD_OP: persisted}}
    )

    # When / Then
    with pytest.raises(InvalidTransition, match="reuse its existing operation identity"):
        reduce_event(current, compensation_intent())


def test_should_satisfy_obligation_only_after_confirmed_compensation() -> None:
    # Given
    compensating = reduce_event(confirmed_snapshot(), compensation_started())
    intended = reduce_event(compensating, compensation_intent())
    dispatched = reduce_event(
        intended,
        dispatch_started(8, COMPENSATION_OP, Direction.COMPENSATION, "refund_payment"),
    )

    # When
    receipt = EffectConfirmed(receipt={"refund_id": "refund-1"})
    completed = reduce_event(
        dispatched,
        effect_outcome(9, receipt, COMPENSATION_OP, Direction.COMPENSATION, "refund_payment"),
    )

    # Then
    assert completed.obligations[FORWARD_OP].status is ObligationStatus.SATISFIED


def test_should_reconcile_unknown_compensation_back_to_compensating() -> None:
    # Given
    compensating = reduce_event(confirmed_snapshot(), compensation_started())
    intended = reduce_event(compensating, compensation_intent())
    dispatched = reduce_event(
        intended,
        dispatch_started(8, COMPENSATION_OP, Direction.COMPENSATION, "refund_payment"),
    )
    unknown = OutcomeUnknown(correlation="refund-request-1")
    unresolved = reduce_event(
        dispatched,
        effect_outcome(9, unknown, COMPENSATION_OP, Direction.COMPENSATION, "refund_payment"),
    )

    # When
    receipt = ReconcileEffectConfirmed(receipt={"refund_id": "refund-1"})
    reconciled = reduce_event(
        unresolved,
        reconciliation_outcome(
            10, receipt, COMPENSATION_OP, Direction.COMPENSATION, "refund_payment"
        ),
    )

    # Then
    assert reconciled.status is SagaStatus.COMPENSATING
    assert reconciled.obligations[FORWARD_OP].status is ObligationStatus.SATISFIED


def test_should_not_create_new_identity_after_partial_compensation() -> None:
    # Given
    partial = partially_compensated_snapshot()

    # When
    retry = compensation_intent(10, NEXT_COMPENSATION_OP)

    # Then
    assert partial.status is SagaStatus.HUMAN_REQUIRED
    assert partial.obligations[FORWARD_OP].status is ObligationStatus.IN_PROGRESS
    with pytest.raises(InvalidTransition, match="human_required"):
        reduce_event(partial, retry)


@pytest.mark.parametrize("semantic_generation", [1, 2])
def test_should_reject_changed_generation_after_partial_compensation(
    semantic_generation: int,
) -> None:
    # Given
    partial = partially_compensated_snapshot()
    retry = compensation_intent(10, NEXT_COMPENSATION_OP, semantic_generation)

    # When / Then
    with pytest.raises(InvalidTransition):
        reduce_event(partial, retry)


def test_should_not_terminally_compensate_a_partial_repair() -> None:
    # Given
    partial = partially_compensated_snapshot()
    proof = invariant_evaluated(
        10,
        evaluated_at_seq=9,
        target_status=SagaStatus.COMPENSATED_VERIFIED,
    )
    proved = reduce_event(partial, proof)

    # When / Then
    with pytest.raises(InvalidTransition, match="not legal"):
        reduce_event(proved, terminal_assigned(11, SagaStatus.COMPENSATED_VERIFIED))


def test_should_enter_and_accept_recovery_plan_state() -> None:
    # Given
    required = reduce_event(running_snapshot(), recovery_required())

    # When
    accepted = reduce_event(required, recovery_accepted())

    # Then
    assert required.status is SagaStatus.RECOVERY_PLAN_REQUIRED
    assert accepted.status is SagaStatus.RUNNING


def test_should_remain_recovery_required_when_plan_is_rejected() -> None:
    # Given
    required = reduce_event(running_snapshot(), recovery_required())

    # When
    rejected = reduce_event(required, recovery_rejected())

    # Then
    assert rejected.status is SagaStatus.RECOVERY_PLAN_REQUIRED


def test_should_require_a_new_recovery_plan_after_running_proposal_rejection() -> None:
    # Given / When
    rejected = reduce_event(running_snapshot(), recovery_rejected(seq=3))

    # Then
    assert rejected.status is SagaStatus.RECOVERY_PLAN_REQUIRED


def test_should_reject_recovery_decision_without_required_plan() -> None:
    # Given / When / Then
    with pytest.raises(InvalidTransition, match="recovery plan"):
        reduce_event(running_snapshot(), recovery_accepted(seq=3))


def test_should_make_human_required_durable_and_quiescent() -> None:
    # Given / When
    required = reduce_event(running_snapshot(), human_required())

    # Then
    assert required.status is SagaStatus.HUMAN_REQUIRED
    assert required.pending_approval is True


def test_should_clear_pending_approval_only_for_verified_resolution() -> None:
    # Given
    required = reduce_event(running_snapshot(), human_required())

    # When
    rejected = reduce_event(required, human_resolution(verified=False))

    # Then
    assert rejected.status is SagaStatus.HUMAN_REQUIRED
    assert rejected.pending_approval is True


def test_should_record_verified_human_resolution_without_auth_proof() -> None:
    # Given
    required = reduce_event(running_snapshot(), human_required())

    # When
    resolved = reduce_event(required, human_resolution())

    # Then
    assert resolved.pending_approval is False
    assert "auth_proof" not in human_resolution().model_dump()


def test_should_preserve_human_required_precedence_over_late_outcomes() -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent())
    dispatched = reduce_event(intended, dispatch_started())
    paused = reduce_event(dispatched, human_required(seq=5))

    # When
    unknown = OutcomeUnknown(correlation="provider-request-7")
    unresolved = reduce_event(paused, effect_outcome(6, unknown))
    receipt = ReconcileEffectConfirmed(receipt={"payment_id": "payment-1"})
    reconciled = reduce_event(unresolved, reconciliation_outcome(7, receipt))

    # Then
    assert unresolved.status is SagaStatus.HUMAN_REQUIRED
    assert reconciled.status is SagaStatus.HUMAN_REQUIRED
    assert reconciled.pending_approval is True
    with pytest.raises(InvalidTransition, match="verified human resolution"):
        reduce_event(reconciled, alternate_intent(8))


def test_should_resume_approved_durable_intent_after_human_resolution() -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent())
    paused = reduce_event(intended, human_required(seq=4))

    # When
    resolved = reduce_event(paused, human_resolution(seq=5))
    dispatched = reduce_event(resolved, dispatch_started(seq=6))

    # Then
    assert resolved.status is SagaStatus.RUNNING
    assert dispatched.operations[FORWARD_OP].status is OperationStatus.DISPATCHED


def test_should_block_mutation_if_pending_approval_and_status_diverge() -> None:
    # Given
    paused = reduce_event(running_snapshot(), human_required())
    inconsistent = paused.model_copy(update={"status": SagaStatus.RUNNING})

    # When / Then
    with pytest.raises(InvalidTransition, match="pending human approval"):
        reduce_event(inconsistent, effect_intent(seq=4))


def test_should_converge_queued_forward_intent_before_compensation() -> None:
    # Given
    snapshot = confirmed_snapshot()
    queued = reduce_event(snapshot, alternate_intent(6))

    # When
    with pytest.raises(InvalidTransition, match="forward durable intent"):
        reduce_event(queued, compensation_started(seq=7))
    dispatched = reduce_event(queued, alternate_dispatch(7))
    absent = NoEffectConfirmed(reason="provider proved absence")
    converged = reduce_event(dispatched, alternate_outcome(8, absent))
    compensating = reduce_event(converged, compensation_started(seq=9))

    # Then
    assert converged.operations[SECOND_OP].status is OperationStatus.NO_EFFECT_CONFIRMED
    assert compensating.status is SagaStatus.COMPENSATING


def test_should_resume_latest_phase_after_human_directed_forward_work() -> None:
    # Given
    compensating = reduce_event(confirmed_snapshot(), compensation_started())
    paused = reduce_event(compensating, human_required(seq=7))
    resolved = reduce_event(paused, human_resolution(seq=8))
    intended = reduce_event(resolved, alternate_intent(9))
    dispatched = reduce_event(intended, alternate_dispatch(10))
    unknown = OutcomeUnknown(correlation="provider-request-8")
    unknown_event = alternate_outcome(11, unknown)
    unresolved = reduce_event(dispatched, unknown_event)

    # When
    receipt = ReconcileEffectConfirmed(receipt={"payment_id": "payment-2"})
    confirmed = reconciliation_outcome(12, receipt, SECOND_OP).model_copy(
        update={"step_instance_id": SECOND_STEP_ID}
    )
    reconciled = reduce_event(unresolved, confirmed)

    # Then
    assert reconciled.status is SagaStatus.RUNNING


def test_should_allow_verified_human_resolution_to_enter_compensation() -> None:
    # Given
    paused = reduce_event(confirmed_snapshot(), human_required(seq=6))
    resolved = reduce_event(paused, human_resolution(seq=7))

    # When
    compensating = reduce_event(resolved, compensation_started(seq=8))

    # Then
    assert compensating.status is SagaStatus.COMPENSATING


def test_should_allow_human_escalation_from_each_active_phase() -> None:
    # Given
    unknown = OutcomeUnknown(correlation="provider-request-7")
    reconciling = reduce_event(dispatched_snapshot(), effect_outcome(5, unknown))
    recovery = reduce_event(running_snapshot(), recovery_required())
    compensating = reduce_event(confirmed_snapshot(), compensation_started())
    active = (running_snapshot(), reconciling, recovery, compensating)

    # When
    escalated = tuple(reduce_event(item, human_required(item.seq + 1)) for item in active)

    # Then
    assert all(item.status is SagaStatus.HUMAN_REQUIRED for item in escalated)


def test_should_reject_human_escalation_before_saga_start() -> None:
    # Given
    created = reduce_event(None, saga_created())

    # When / Then
    with pytest.raises(InvalidTransition, match="active Saga phase"):
        reduce_event(created, human_required(seq=2))


def test_should_reject_duplicate_human_escalation() -> None:
    # Given
    paused = reduce_event(running_snapshot(), human_required())

    # When / Then
    with pytest.raises(InvalidTransition, match="active Saga phase"):
        reduce_event(paused, human_required(seq=4))


def test_should_reject_compensation_while_human_approval_is_pending() -> None:
    # Given
    paused = reduce_event(confirmed_snapshot(), human_required(seq=6))

    # When / Then
    with pytest.raises(InvalidTransition, match="verified human resolution"):
        reduce_event(paused, compensation_started(seq=7))


def test_should_reject_reusable_auth_proof_in_human_resolution_event() -> None:
    # Given
    payload = human_resolution().model_dump() | {"auth_proof": "bearer-secret"}

    # When / Then
    with pytest.raises(ValidationError, match="auth_proof"):
        HumanResolutionRecorded.model_validate(payload)


def test_should_reject_human_resolution_outside_human_required() -> None:
    # Given / When / Then
    with pytest.raises(InvalidTransition, match="human"):
        reduce_event(running_snapshot(), human_resolution(seq=3))


def test_should_reject_reused_approval_consumption_during_replay() -> None:
    # Given
    consumed = reduce_event(running_snapshot(), approval_consumed(3))

    # When / Then
    with pytest.raises(InvalidTransition, match="already consumed"):
        reduce_event(consumed, approval_consumed(4))


def test_should_reject_human_resolution_that_reuses_consumed_decision() -> None:
    # Given
    consumed = reduce_event(running_snapshot(), approval_consumed(3))
    paused = reduce_event(consumed, human_required(seq=4))

    # When / Then
    with pytest.raises(InvalidTransition, match="human decision was already consumed"):
        reduce_event(paused, human_resolution(seq=5))


def test_should_keep_rejected_human_request_pending() -> None:
    # Given
    paused = reduce_event(running_snapshot(), human_required())
    payload = human_resolution().model_dump(mode="python") | {"action": "reject"}
    rejected = HumanResolutionRecorded.model_validate(payload)

    # When
    current = reduce_event(paused, rejected)

    # Then
    assert current.status is SagaStatus.HUMAN_REQUIRED
    assert current.pending_approval is True
    assert current.consumed_approval_ids == ("decision_01",)


def test_should_require_invariant_to_evaluate_immediately_prior_state() -> None:
    # Given
    snapshot = running_snapshot()

    # When / Then
    with pytest.raises(InvalidTransition, match="invariant"):
        reduce_event(snapshot, invariant_evaluated(3, evaluated_at_seq=1))


def test_should_persist_exact_invariant_proof_markers() -> None:
    # Given
    event = invariant_evaluated(3, evaluated_at_seq=2)

    # When
    proved = reduce_event(running_snapshot(), event)

    # Then
    assert proved.last_invariant_target is SagaStatus.SUCCEEDED_VERIFIED
    assert proved.last_invariant_version == "checkout-invariants-v1"
    assert proved.last_invariant_evidence_digest == HASH


def test_should_serialize_exact_invariant_proof_fields() -> None:
    # Given
    event = invariant_evaluated(3, evaluated_at_seq=2)

    # When
    dumped = event.model_dump(mode="json")
    adapter: TypeAdapter[LedgerEvent] = TypeAdapter(LedgerEvent)
    restored = adapter.validate_json(event.model_dump_json())

    # Then
    assert dumped["target_status"] == SagaStatus.SUCCEEDED_VERIFIED
    assert dumped["evidence_digest"] == HASH
    assert restored == event


def test_should_reject_invalid_invariant_evidence_digest() -> None:
    # Given
    payload = invariant_evaluated(3, evaluated_at_seq=2).model_dump()

    # When / Then
    with pytest.raises(ValidationError, match="evidence_digest"):
        InvariantEvaluated.model_validate(payload | {"evidence_digest": "not-a-digest"})


def test_should_reject_terminal_target_substitution_at_reducer_boundary() -> None:
    # Given
    proved = reduce_event(running_snapshot(), invariant_evaluated(3, evaluated_at_seq=2))

    # When / Then
    with pytest.raises(InvalidTransition, match="proof target"):
        reduce_event(proved, terminal_assigned(4, SagaStatus.ABORTED_CLEAN))


def test_should_not_assign_terminal_status_without_current_invariant_evidence() -> None:
    # Given
    snapshot = running_snapshot()

    # When / Then
    with pytest.raises(InvalidTransition, match="invariant"):
        reduce_event(snapshot, terminal_assigned(3, SagaStatus.SUCCEEDED_VERIFIED))


def test_should_not_assign_terminal_status_while_human_approval_is_pending() -> None:
    # Given
    paused = reduce_event(running_snapshot(), human_required())
    proof = invariant_evaluated(
        4,
        evaluated_at_seq=3,
        target_status=SagaStatus.RESOLVED_WITH_EXCEPTION,
    )
    proved = reduce_event(paused, proof)

    # When / Then
    with pytest.raises(InvalidTransition, match="pending human approval"):
        reduce_event(proved, terminal_assigned(5, SagaStatus.RESOLVED_WITH_EXCEPTION))


def test_should_not_assign_terminal_status_while_operation_is_unknown() -> None:
    # Given
    unknown = OutcomeUnknown(correlation="provider-request-7")
    snapshot = reduce_event(dispatched_snapshot(), effect_outcome(5, unknown))
    proof = invariant_evaluated(
        6,
        evaluated_at_seq=5,
        target_status=SagaStatus.RESOLVED_WITH_EXCEPTION,
    )
    proved = reduce_event(snapshot, proof)

    # When / Then
    with pytest.raises(InvalidTransition, match="unknown operations"):
        reduce_event(proved, terminal_assigned(7, SagaStatus.RESOLVED_WITH_EXCEPTION))


def terminal_ready_snapshot(status: SagaStatus, target: SagaStatus) -> SagaSnapshot:
    snapshot = SagaSnapshot(
        saga_id=SAGA_ID,
        seq=10,
        status=status,
        definition_version="checkout-v1",
        operations={},
        obligations={},
        last_invariant_seq=10,
        last_invariant_target=target,
        last_invariant_version="checkout-invariants-v1",
        last_invariant_evidence_digest=HASH,
        pending_approval=False,
    )
    return snapshot.model_copy(update={"last_invariant_passed": True})


@pytest.mark.parametrize(
    ("source", "allowed_targets"),
    [
        (SagaStatus.CREATED, frozenset()),
        (
            SagaStatus.RUNNING,
            frozenset({SagaStatus.SUCCEEDED_VERIFIED, SagaStatus.ABORTED_CLEAN}),
        ),
        (SagaStatus.RECOVERY_PLAN_REQUIRED, frozenset()),
        (SagaStatus.RETRY_WAIT, frozenset()),
        (SagaStatus.RECONCILING_UNKNOWN, frozenset()),
        (SagaStatus.COMPENSATING, frozenset({SagaStatus.COMPENSATED_VERIFIED})),
        (SagaStatus.HUMAN_REQUIRED, frozenset({SagaStatus.RESOLVED_WITH_EXCEPTION})),
    ],
)
def test_should_enforce_exhaustive_source_to_terminal_matrix(
    source: SagaStatus,
    allowed_targets: frozenset[SagaStatus],
) -> None:
    targets = (
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    )

    # When / Then
    for target in targets:
        snapshot = terminal_ready_snapshot(source, target)
        event = terminal_assigned(11, target)
        if target in allowed_targets:
            assert reduce_event(snapshot, event).status is target
        else:
            with pytest.raises(InvalidTransition, match="terminal target"):
                reduce_event(snapshot, event)


def test_should_require_passing_invariant_evidence_for_terminal_assignment() -> None:
    # Given
    failed = invariant_evaluated(3, evaluated_at_seq=2).model_copy(update={"all_passed": False})
    evaluated = reduce_event(running_snapshot(), failed)

    # When / Then
    with pytest.raises(InvalidTransition, match="passing invariant"):
        reduce_event(evaluated, terminal_assigned(4, SagaStatus.SUCCEEDED_VERIFIED))


def test_should_not_assign_terminal_status_with_runnable_intent() -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent())
    proved = reduce_event(intended, invariant_evaluated(4, evaluated_at_seq=3))

    # When / Then
    with pytest.raises(InvalidTransition, match="runnable operations"):
        reduce_event(proved, terminal_assigned(5, SagaStatus.SUCCEEDED_VERIFIED))


def test_should_not_assign_terminal_status_with_dispatched_operation() -> None:
    # Given
    proved = reduce_event(dispatched_snapshot(), invariant_evaluated(5, evaluated_at_seq=4))

    # When / Then
    with pytest.raises(InvalidTransition, match="runnable operations"):
        reduce_event(proved, terminal_assigned(6, SagaStatus.SUCCEEDED_VERIFIED))


def test_should_assign_terminal_status_only_through_terminal_event() -> None:
    # Given
    snapshot = running_snapshot()

    # When
    proved = reduce_event(snapshot, invariant_evaluated(3, evaluated_at_seq=2))
    terminal = reduce_event(proved, terminal_assigned(4, SagaStatus.SUCCEEDED_VERIFIED))

    # Then
    assert proved.status is SagaStatus.RUNNING
    assert terminal.status is SagaStatus.SUCCEEDED_VERIFIED
    assert terminal.last_invariant_seq == terminal.seq - 1


def test_should_reject_nonterminal_target_in_terminal_event() -> None:
    # Given
    payload = terminal_assigned(4, SagaStatus.SUCCEEDED_VERIFIED).model_dump()

    # When / Then
    with pytest.raises(ValidationError, match="status"):
        TerminalAssigned.model_validate(payload | {"status": SagaStatus.RUNNING})


def test_should_reject_every_transition_after_terminal() -> None:
    # Given
    proved = reduce_event(running_snapshot(), invariant_evaluated(3, evaluated_at_seq=2))
    terminal = reduce_event(proved, terminal_assigned(4, SagaStatus.SUCCEEDED_VERIFIED))

    # When / Then
    with pytest.raises(InvalidTransition, match="terminal"):
        reduce_event(terminal, recovery_required(seq=5))


def test_should_reject_second_saga_created_event() -> None:
    # Given
    created = reduce_event(None, saga_created())

    # When / Then
    with pytest.raises(InvalidTransition, match="first event"):
        reduce_event(created, saga_created(seq=2))


def test_should_copy_snapshot_mappings_on_write() -> None:
    # Given
    running = running_snapshot()

    # When
    intended = reduce_event(running, effect_intent())

    # Then
    assert running.operations == {}
    assert running.obligations == {}
    assert FORWARD_OP in intended.operations


def test_should_expose_genuinely_immutable_snapshot_mappings() -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent())

    # When / Then
    with pytest.raises(TypeError):
        intended.operations[COMPENSATION_OP] = intended.operations[FORWARD_OP]  # type: ignore[index]
    with pytest.raises(TypeError):
        del intended.obligations[FORWARD_OP]  # type: ignore[attr-defined]


def test_should_deeply_freeze_event_and_every_snapshot_generation() -> None:
    # Given
    command: dict[str, object] = {"order": [{"marker": "original"}]}
    payload = effect_intent().model_dump() | {"redacted_command": command}
    event = EffectIntentRecorded.model_validate(payload)
    intended = reduce_event(running_snapshot(), event)
    intended_json = intended.model_dump_json()

    # When
    order = cast(list[object], command["order"])
    cast(dict[str, object], order[0])["marker"] = "changed"
    frozen_order = cast(tuple[JsonObject, ...], event.redacted_command["order"])

    # Then
    assert frozen_order[0]["marker"] == "original"
    assert intended.model_dump_json() == intended_json
    with pytest.raises(TypeError):
        frozen_order[0]["marker"] = "changed"  # type: ignore[index]


def test_should_keep_nested_result_evidence_immutable_across_snapshots() -> None:
    # Given
    outcome = EffectConfirmed.model_validate({"receipt": {"provider": {"ids": ["payment-1"]}}})
    event = effect_outcome(5, outcome)
    before = dispatched_snapshot()

    # When
    after = reduce_event(before, event)
    after_json = after.model_dump_json()
    result = after.operations[FORWARD_OP].redacted_result
    assert result is not None
    result_receipt = cast(JsonObject, result["receipt"])
    result_provider = cast(JsonObject, result_receipt["provider"])
    receipt = after.operations[FORWARD_OP].receipts[0]
    provider = cast(JsonObject, receipt["provider"])

    # Then
    with pytest.raises(TypeError):
        result_provider["ids"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        provider["ids"] = ()  # type: ignore[index]
    assert before.operations[FORWARD_OP].redacted_result is None
    assert after.model_dump_json() == after_json


def test_should_copy_caller_owned_mapping_during_snapshot_validation() -> None:
    # Given
    intended = reduce_event(running_snapshot(), effect_intent())
    operations = dict(intended.operations)
    snapshot = SagaSnapshot(
        saga_id=SAGA_ID,
        seq=3,
        status=SagaStatus.RUNNING,
        definition_version="checkout-v1",
        operations=operations,
        obligations={},
    )

    # When
    operations.clear()

    # Then
    assert FORWARD_OP in snapshot.operations


@pytest.mark.parametrize(
    "event_model",
    [
        SagaCreated,
        SagaStarted,
        EffectIntentRecorded,
        DispatchStarted,
        EffectOutcomeRecorded,
        RecoveryPlanRequired,
        RecoveryPlanAccepted,
        RecoveryPlanRejected,
        CompensationStarted,
        CompensationIntentRecorded,
        InvariantEvaluated,
        HumanRequired,
        HumanResolutionRecorded,
        TerminalAssigned,
    ],
)
def test_should_keep_every_event_strict_frozen_and_closed(event_model: type[BaseModel]) -> None:
    # Given / When / Then
    assert event_model.model_config["strict"] is True
    assert event_model.model_config["extra"] == "forbid"
    assert event_model.model_config["frozen"] is True


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("event_id", "event-1"),
        ("saga_id", "not-a-saga"),
        ("saga_seq", True),
        ("schema_version", "2.0"),
        ("definition_version", ""),
        ("actor", ""),
        ("trace_id", "trace-short"),
        ("recorded_at", datetime(2026, 9, 6, 12)),
        ("recorded_at", datetime(2026, 9, 6, 12, tzinfo=timezone(timedelta(hours=1)))),
    ],
)
def test_should_reject_invalid_common_event_field(field: str, invalid: object) -> None:
    # Given
    payload = saga_created().model_dump() | {field: invalid}

    # When / Then
    with pytest.raises(ValidationError):
        SagaCreated.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("operation_id", "op_short"),
        ("step_instance_id", "step_short"),
        ("semantic_generation", -1),
        ("delivery_attempt", 0),
        ("tool_name", ""),
        ("command_hash", "not-a-hash"),
        ("redacted_command", {"amount_minor": float("inf")}),
    ],
)
def test_should_reject_invalid_effect_event_field(field: str, invalid: object) -> None:
    # Given
    payload = effect_intent().model_dump() | {field: invalid}

    # When / Then
    with pytest.raises(ValidationError):
        EffectIntentRecorded.model_validate(payload)


def test_should_reject_unknown_event_discriminator() -> None:
    # Given
    payload = saga_created().model_dump() | {"event_type": "future_event"}

    # When / Then
    with pytest.raises(ValidationError):
        TypeAdapter(LedgerEvent).validate_python(payload)


def test_should_reject_unknown_runtime_event() -> None:
    # Given
    snapshot = running_snapshot()

    # When / Then
    with pytest.raises(InvalidTransition, match="unknown event"):
        reduce_event(snapshot, cast(LedgerEvent, object()))
