from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from agentic_saga.contracts.common import Direction, JsonObject, SagaId, sha256_json
from agentic_saga.contracts.events import (
    DispatchStarted,
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    SagaCreated,
    SagaStarted,
)
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    OutcomeUnknown,
    normalize_effect_outcome,
    safe_outcome_json,
)
from agentic_saga.kernel.ports import OutboxCommand, TransitionBatch
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import SagaSnapshot

SAGA_ID = "saga_0000000000000001"
OTHER_SAGA_ID = "saga_0000000000000002"
OPERATION_ID = f"op_{'a' * 64}"
OTHER_OPERATION_ID = f"op_{'b' * 64}"
RECORDED_AT = datetime(2026, 9, 6, 12, tzinfo=UTC)


@dataclass(frozen=True)
class PreparedTransition:
    created: SagaCreated
    started: SagaStarted
    intent: EffectIntentRecorded
    projection: SagaSnapshot
    command: OutboxCommand
    batch: TransitionBatch


def canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return sha256(encoded).hexdigest()


def saga_created(saga_id: SagaId = SAGA_ID, event_id: str = "evt_0000000000000001") -> SagaCreated:
    return SagaCreated(
        event_id=event_id,
        saga_id=saga_id,
        saga_seq=1,
        definition_version="checkout-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000000001",
        recorded_at=RECORDED_AT,
        definition_name="checkout",
        definition_fingerprint="f" * 64,
        redacted_goal={"order_id": "order-1"},
    )


def saga_started(saga_id: SagaId = SAGA_ID, seq: int = 2) -> SagaStarted:
    return SagaStarted(
        event_id=f"evt_{seq:016d}",
        saga_id=saga_id,
        saga_seq=seq,
        definition_version="checkout-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000000001",
        recorded_at=RECORDED_AT + timedelta(seconds=seq),
    )


def effect_intent(
    saga_id: SagaId = SAGA_ID,
    operation_id: str = OPERATION_ID,
    seq: int = 3,
) -> EffectIntentRecorded:
    command_hash = canonical_hash(
        {"amount_minor": 14900, "credential_ref": "vault://payments/demo"}
    )
    return EffectIntentRecorded(
        event_id=f"evt_{seq:016d}",
        saga_id=saga_id,
        saga_seq=seq,
        definition_version="checkout-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000000001",
        recorded_at=RECORDED_AT + timedelta(seconds=seq),
        operation_id=operation_id,
        step_instance_id="step_00000001",
        direction=Direction.FORWARD,
        semantic_generation=0,
        delivery_attempt=1,
        tool_name="charge_payment",
        redacted_command={"amount_minor": 14900, "credential_ref": "vault://payments/demo"},
        command_hash=command_hash,
        compensate_with="refund_payment",
    )


def outbox_command(
    saga_id: SagaId = SAGA_ID,
    operation_id: str = OPERATION_ID,
    command_id: str = "cmd_0000000000000001",
) -> OutboxCommand:
    command_hash = canonical_hash(
        {"amount_minor": 14900, "credential_ref": "vault://payments/demo"}
    )
    return OutboxCommand(
        command_id=command_id,
        saga_id=saga_id,
        operation_id=operation_id,
        tool_name="charge_payment",
        definition_version="checkout-v1",
        command_schema_version="1.0",
        step_instance_id="step_00000001",
        direction=Direction.FORWARD,
        semantic_generation=0,
        command={"amount_minor": 14900, "credential_ref": "vault://payments/demo"},
        command_hash=command_hash,
        available_at=RECORDED_AT,
    )


def dispatch_started(seq: int = 4, operation_id: str = OPERATION_ID) -> DispatchStarted:
    command_hash = canonical_hash(
        {"amount_minor": 14900, "credential_ref": "vault://payments/demo"}
    )
    return DispatchStarted(
        event_id=f"evt_{seq:016d}",
        saga_id=SAGA_ID,
        saga_seq=seq,
        definition_version="checkout-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000000001",
        recorded_at=RECORDED_AT + timedelta(seconds=seq),
        operation_id=operation_id,
        step_instance_id="step_00000001",
        direction=Direction.FORWARD,
        semantic_generation=0,
        delivery_attempt=1,
        tool_name="charge_payment",
        redacted_command={"amount_minor": 14900, "credential_ref": "vault://payments/demo"},
        command_hash=command_hash,
    )


def dispatch_batch(
    snapshot: SagaSnapshot,
    operation_id: str = OPERATION_ID,
    transition_id: str = "txn_0000000000000002",
) -> TransitionBatch:
    event = dispatch_started(snapshot.seq + 1, operation_id)
    return TransitionBatch(
        transition_id=transition_id,
        saga_id=SAGA_ID,
        expected_seq=snapshot.seq,
        expected_fence_token=0,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )


def effect_outcome(
    seq: int = 5,
    operation_id: str = OPERATION_ID,
    *,
    unknown: bool = False,
) -> EffectOutcomeRecorded:
    command: JsonObject = {"amount_minor": 14900, "credential_ref": "vault://payments/demo"}
    raw_outcome = (
        OutcomeUnknown(correlation="provider response unavailable")
        if unknown
        else EffectConfirmed(receipt={"payment_id": "pay_1"})
    )
    outcome = normalize_effect_outcome(
        raw_outcome,
        fallback_correlation="opaque://agentic-saga/storagefixture1/v1",
    )
    result = safe_outcome_json(outcome)
    return EffectOutcomeRecorded(
        event_id=f"evt_{seq:016d}",
        saga_id=SAGA_ID,
        saga_seq=seq,
        definition_version="checkout-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000000001",
        recorded_at=RECORDED_AT + timedelta(seconds=seq),
        operation_id=operation_id,
        step_instance_id="step_00000001",
        direction=Direction.FORWARD,
        semantic_generation=0,
        delivery_attempt=1,
        tool_name="charge_payment",
        redacted_command=command,
        command_hash=canonical_hash(command),
        outcome=outcome,
        redacted_result=result,
        result_hash=sha256_json(result),
    )


def outcome_batch(
    snapshot: SagaSnapshot,
    *,
    operation_id: str = OPERATION_ID,
    unknown: bool = False,
    transition_id: str = "txn_0000000000000003",
) -> TransitionBatch:
    event = effect_outcome(snapshot.seq + 1, operation_id, unknown=unknown)
    return TransitionBatch(
        transition_id=transition_id,
        saga_id=SAGA_ID,
        expected_seq=snapshot.seq,
        expected_fence_token=0,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )


def prepared_transition(
    saga_id: SagaId = SAGA_ID,
    operation_id: str = OPERATION_ID,
    transition_id: str = "txn_0000000000000001",
    command_id: str = "cmd_0000000000000001",
) -> PreparedTransition:
    created, started = saga_created(saga_id), saga_started(saga_id)
    intent = effect_intent(saga_id, operation_id)
    projection = reduce_event(reduce_event(reduce_event(None, created), started), intent)
    command = outbox_command(saga_id, operation_id, command_id)
    batch = TransitionBatch(
        transition_id=transition_id,
        saga_id=saga_id,
        expected_seq=1,
        expected_fence_token=0,
        events=(started, intent),
        projection=projection,
        outbox_commands=(command,),
    )
    return PreparedTransition(created, started, intent, projection, command, batch)
