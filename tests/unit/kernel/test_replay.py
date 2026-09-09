from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter

from agentic_saga.contracts.common import Direction, sha256_json
from agentic_saga.contracts.events import (
    DispatchStarted,
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    InvariantEvaluated,
    LedgerEvent,
    SagaCreated,
    SagaStarted,
    TerminalAssigned,
)
from agentic_saga.contracts.outcomes import EffectConfirmed, safe_outcome_json
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.kernel.reducer import SequenceGap, rebuild_projection
from agentic_saga.kernel.state import OperationStatus

SAGA_ID = "saga_0000000000000001"
OPERATION_ID = f"op_{'a' * 64}"
STEP_ID = "step_00000001"
TRACE_ID = "trace_0000000000000001"
RECORDED_AT = datetime(2026, 9, 6, 12, tzinfo=UTC)
COMMAND_HASH = "c" * 64


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


def _effect_metadata() -> dict[str, object]:
    return {
        "operation_id": OPERATION_ID,
        "step_instance_id": STEP_ID,
        "direction": Direction.FORWARD,
        "semantic_generation": 0,
        "delivery_attempt": 1,
        "tool_name": "charge_payment",
        "redacted_command": {
            "amount_minor": 14900,
            "items": [{"sku": "sku_1", "quantity": 1}],
        },
        "command_hash": COMMAND_HASH,
    }


def complete_event_stream() -> tuple[LedgerEvent, ...]:
    outcome = EffectConfirmed(receipt={"payment_id": "payment-1"})
    safe_result = safe_outcome_json(outcome)
    events: tuple[LedgerEvent, ...] = (
        SagaCreated.model_validate(
            _metadata(1)
            | {
                "event_type": "saga_created",
                "definition_name": "checkout",
                "definition_fingerprint": "f" * 64,
                "redacted_goal": {"order_id": "order-1"},
            }
        ),
        SagaStarted.model_validate(_metadata(2) | {"event_type": "saga_started"}),
        EffectIntentRecorded.model_validate(
            _metadata(3)
            | _effect_metadata()
            | {"event_type": "effect_intent_recorded", "compensate_with": "refund_payment"}
        ),
        DispatchStarted.model_validate(
            _metadata(4) | _effect_metadata() | {"event_type": "dispatch_started"}
        ),
        EffectOutcomeRecorded.model_validate(
            _metadata(5)
            | _effect_metadata()
            | {
                "event_type": "effect_outcome_recorded",
                "outcome": outcome,
                "redacted_result": safe_result,
                "result_hash": sha256_json(safe_result),
            }
        ),
        InvariantEvaluated.model_validate(
            _metadata(6)
            | {
                "event_type": "invariant_evaluated",
                "evaluated_at_seq": 5,
                "target_status": SagaStatus.SUCCEEDED_VERIFIED,
                "invariant_version": "checkout-invariants-v1",
                "evidence_digest": COMMAND_HASH,
                "results": {"order_fulfilled": True},
                "all_passed": True,
            }
        ),
        TerminalAssigned.model_validate(
            _metadata(7)
            | {"event_type": "terminal_assigned", "status": SagaStatus.SUCCEEDED_VERIFIED}
        ),
    )
    return events


def test_should_rebuild_the_same_material_projection_deterministically() -> None:
    # Given
    events = complete_event_stream()

    # When
    first = rebuild_projection(events)
    second = rebuild_projection(iter(events))

    # Then
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert first.operations[OPERATION_ID].status is OperationStatus.EFFECT_CONFIRMED
    assert first.last_invariant_target is SagaStatus.SUCCEEDED_VERIFIED
    assert first.last_invariant_version == "checkout-invariants-v1"
    assert first.last_invariant_evidence_digest == COMMAND_HASH


def test_should_replay_equally_after_json_serialization_round_trip() -> None:
    # Given
    events = complete_event_stream()
    adapter: TypeAdapter[tuple[LedgerEvent, ...]] = TypeAdapter(tuple[LedgerEvent, ...])

    # When
    restored = adapter.validate_json(adapter.dump_json(events))
    original = rebuild_projection(events)
    replayed = rebuild_projection(restored)

    # Then
    assert replayed == original
    assert replayed.model_dump_json() == original.model_dump_json()


def test_should_reject_empty_event_stream() -> None:
    # Given / When / Then
    with pytest.raises(SequenceGap, match="SagaCreated at sequence 1"):
        rebuild_projection(())


def test_should_reject_reordered_event_stream() -> None:
    # Given
    events = complete_event_stream()
    reordered = (events[0], events[2], events[1], *events[3:])

    # When / Then
    with pytest.raises(SequenceGap, match="expected 2"):
        rebuild_projection(reordered)


def test_should_reject_duplicate_event_sequence() -> None:
    # Given
    events = complete_event_stream()
    duplicated = (events[0], events[1], events[1])

    # When / Then
    with pytest.raises(SequenceGap, match="expected 3"):
        rebuild_projection(duplicated)


def test_should_leave_prior_replayed_snapshot_material_unchanged() -> None:
    # Given
    events = complete_event_stream()
    before = rebuild_projection(events[:2])

    # When
    after = rebuild_projection(events[:3])

    # Then
    assert before.seq == 2
    assert before.operations == {}
    assert after.seq == 3
    assert OPERATION_ID in after.operations
