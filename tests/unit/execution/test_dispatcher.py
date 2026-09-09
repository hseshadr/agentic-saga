from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentic_saga.contracts.common import Direction, Reversibility, sha256_json
from agentic_saga.contracts.events import (
    DispatchAbortedBeforeEntry,
    DispatchStarted,
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    SagaCreated,
    SagaStarted,
)
from agentic_saga.contracts.outcomes import (
    SAFE_NO_EFFECT_REASON,
    EffectConfirmed,
    EffectOutcome,
    NoEffectConfirmed,
    OutcomeUnknown,
    PartialEffectConfirmed,
    ReconcileEffectConfirmed,
    generated_outcome_correlation,
    normalize_effect_outcome,
    safe_outcome_json,
)
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ReconcileContext,
    ToolCapabilities,
)
from agentic_saga.execution.dispatcher import DispatchFailure, DispatchResult
from agentic_saga.kernel.ports import EffectCapabilityProof
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import OperationStatus, SagaSnapshot

SAGA_ID = "saga_0000000000008001"
OPERATION_ID = f"op_{'8' * 64}"
NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
COMMAND = {"amount_minor": 14900}
COMMAND_HASH = sha256_json(COMMAND)


class Command(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    amount_minor: int = Field(strict=True, gt=0)
    currency: Literal["USD"] = "USD"


class Adapter:
    async def execute(self, command: Command, context: EffectContext) -> EffectConfirmed:
        del command, context
        return EffectConfirmed(receipt={"provider_receipt": "receipt_1"})

    async def reconcile(
        self, command: Command, context: ReconcileContext
    ) -> ReconcileEffectConfirmed:
        del command, context
        return ReconcileEffectConfirmed(receipt={"provider_receipt": "receipt_1"})


def _capabilities() -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=True,
    )


def _definition() -> EffectToolDefinition[Command]:
    return EffectToolDefinition(
        name="charge_payment",
        definition_version="charge-payment-v7",
        command_schema_version="charge-command-v3",
        input_model=Command,
        adapter=Adapter(),
        capabilities=_capabilities(),
        compensate_with="refund_payment",
    )


def _base(seq: int) -> dict[str, object]:
    return {
        "event_id": f"evt_{seq:016d}",
        "saga_id": SAGA_ID,
        "saga_seq": seq,
        "definition_version": "checkout-saga-v1",
        "fence_token": 1,
        "actor": "dispatcher",
        "trace_id": "trace_0000000000008001",
        "recorded_at": NOW,
    }


def _effect(seq: int) -> dict[str, object]:
    return _base(seq) | {
        "operation_id": OPERATION_ID,
        "step_instance_id": "step_00008001",
        "direction": Direction.FORWARD,
        "semantic_generation": 0,
        "delivery_attempt": 1,
        "tool_name": "charge_payment",
        "redacted_command": COMMAND,
        "command_hash": COMMAND_HASH,
    }


def _running_snapshot() -> SagaSnapshot:
    created = SagaCreated.model_validate(
        _base(1)
        | {
            "fence_token": None,
            "definition_name": "checkout",
            "definition_fingerprint": "f" * 64,
            "redacted_goal": {"order_id": "order_8"},
        }
    )
    started = SagaStarted.model_validate(_base(2))
    return reduce_event(reduce_event(None, created), started)


def test_should_require_explicit_effect_definition_and_command_schema_versions() -> None:
    # Given / When
    definition = _definition()

    # Then
    assert definition.definition_version == "charge-payment-v7"
    assert definition.command_schema_version == "charge-command-v3"
    with pytest.raises(TypeError):
        EffectToolDefinition(  # type: ignore[call-arg]
            name="missing_versions",
            input_model=Command,
            capabilities=_capabilities(),
            compensate_with=None,
        )


def test_should_reject_capability_proof_with_mismatched_digest() -> None:
    # Given / When / Then
    with pytest.raises(ValidationError, match="digest does not match"):
        EffectCapabilityProof(
            capabilities=_capabilities(),
            capability_digest="f" * 64,
        )


@pytest.mark.parametrize(
    "outcome",
    [
        EffectConfirmed.model_validate(
            {"receipt": {"provider_receipt": "receipt_1", "nested": {"api_key": "raw-secret"}}}
        ),
        PartialEffectConfirmed(
            receipts=({"provider_receipt": "receipt_1", "authorization": "Bearer raw"},)
        ),
        NoEffectConfirmed(reason="private provider explanation"),
        OutcomeUnknown(correlation="exception text must not persist"),
    ],
)
def test_should_normalize_every_adapter_outcome_to_one_safe_representation(
    outcome: EffectOutcome,
) -> None:
    # Given
    fallback = generated_outcome_correlation(SAGA_ID, OPERATION_ID, 1, "adapter_result")

    # When
    safe = normalize_effect_outcome(outcome, fallback_correlation=fallback)
    representation = safe_outcome_json(safe)

    # Then
    assert "raw-secret" not in str(representation)
    assert "Bearer raw" not in str(representation)
    assert "private provider explanation" not in str(representation)
    assert "exception text must not persist" not in str(representation)
    if isinstance(safe, NoEffectConfirmed):
        assert safe.reason == SAFE_NO_EFFECT_REASON


def test_should_bind_outcome_event_to_safe_representation_and_hash() -> None:
    # Given
    outcome = EffectConfirmed(receipt={"provider_receipt": "receipt_1"})
    result = safe_outcome_json(outcome)
    payload = _effect(4) | {
        "outcome": outcome,
        "redacted_result": result,
        "result_hash": sha256_json(result),
    }

    # When / Then
    EffectOutcomeRecorded.model_validate(payload)
    with pytest.raises(ValidationError, match="safe outcome representation"):
        EffectOutcomeRecorded.model_validate(payload | {"redacted_result": {"kind": "wrong"}})
    with pytest.raises(ValidationError, match="canonical hash"):
        EffectOutcomeRecorded.model_validate(payload | {"result_hash": "f" * 64})


def test_should_reject_raw_sensitive_outcome_from_durable_event() -> None:
    # Given
    raw = EffectConfirmed(receipt={"api_key": "raw-secret"})
    result = safe_outcome_json(raw)

    # When / Then
    with pytest.raises(ValidationError, match="normalized"):
        EffectOutcomeRecorded.model_validate(
            _effect(4)
            | {"outcome": raw, "redacted_result": result, "result_hash": sha256_json(result)}
        )


def test_should_return_dispatched_operation_to_intent_after_proven_pre_entry_abort() -> None:
    # Given
    running = _running_snapshot()
    intent = EffectIntentRecorded.model_validate(_effect(3) | {"compensate_with": "refund_payment"})
    intended = reduce_event(running, intent)
    dispatched = reduce_event(intended, DispatchStarted.model_validate(_effect(4)))
    aborted = DispatchAbortedBeforeEntry.model_validate(_effect(5))

    # When
    restored = reduce_event(dispatched, aborted)

    # Then
    operation = restored.operations[OPERATION_ID]
    assert operation.status is OperationStatus.INTENT_DURABLE
    assert operation.delivery_attempt == 1


def test_should_keep_dispatch_result_failure_free_of_private_details() -> None:
    # Given / When
    result = DispatchResult(
        operation_id=OPERATION_ID,
        outcome=None,
        failure=DispatchFailure(code="authority_lost"),
    )

    # Then
    assert result.failure is not None
    assert result.failure.model_dump() == {"code": "authority_lost"}
