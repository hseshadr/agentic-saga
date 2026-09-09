from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from agentic_saga.contracts.common import Direction, JsonObject, Reversibility
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    ReconcileEffectConfirmed,
    ReconciliationOutcome,
)
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.execution.unwind import EmergencyUnwinder, UnwindAction, UnwindTrigger
from agentic_saga.kernel.state import (
    CompensationObligation,
    ObligationStatus,
    OperationRecord,
    OperationStatus,
    SagaSnapshot,
)

SAGA_ID = "saga_0000000000011001"
OPERATION_ID = f"op_{'1' * 64}"
OTHER_OPERATION_ID = f"op_{'2' * 64}"
HASH = "a" * 64


class Command(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    kind: Literal["forward", "repair"]


class Adapter:
    async def execute(self, command: Command, context: EffectContext) -> EffectOutcome:
        del command
        return EffectConfirmed(receipt={"operation_id": context.operation_id})

    async def reconcile(self, command: Command, context: ReconcileContext) -> ReconciliationOutcome:
        del command
        return ReconcileEffectConfirmed(receipt={"correlation": context.correlation})


def _capabilities(reversibility: Reversibility) -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=reversibility,
        partial_effects_possible=True,
    )


def _definition(
    name: str,
    reversibility: Reversibility,
    compensate_with: str | None = None,
) -> EffectToolDefinition[Command]:
    return EffectToolDefinition(
        name=name,
        definition_version=f"{name}-v1",
        command_schema_version="command-v1",
        input_model=Command,
        adapter=Adapter(),
        capabilities=_capabilities(reversibility),
        compensate_with=compensate_with,
    )


def _registry(reversibility: Reversibility = Reversibility.EXACT) -> ToolRegistry:
    return ToolRegistry(
        (
            _definition("charge", reversibility, "refund"),
            _definition("refund", Reversibility.IRREVERSIBLE),
        )
    )


def _operation(status: OperationStatus, operation_id: str = OPERATION_ID) -> OperationRecord:
    receipts: tuple[JsonObject, ...] = ()
    correlation = None
    if status in {OperationStatus.EFFECT_CONFIRMED, OperationStatus.PARTIAL_EFFECT_CONFIRMED}:
        receipts = ({"receipt_id": operation_id[-8:]},)
    if status is OperationStatus.OUTCOME_UNKNOWN:
        correlation = "opaque://provider/lookup-11/v1"
    return OperationRecord(
        operation_id=operation_id,
        step_instance_id=f"step_{operation_id[-8:]}",
        direction=Direction.FORWARD,
        semantic_generation=0,
        delivery_attempt=1,
        tool_name="charge",
        status=status,
        redacted_command={"resource_id": "order-11", "kind": "forward"},
        command_hash=HASH,
        receipts=receipts,
        correlation=correlation,
    )


def _snapshot(
    *operations: OperationRecord,
    obligation_status: ObligationStatus | None = ObligationStatus.ELIGIBLE,
) -> SagaSnapshot:
    obligations: dict[str, CompensationObligation] = {}
    if operations and obligation_status is not None:
        forward = operations[0]
        obligations[forward.operation_id] = CompensationObligation(
            forward_operation_id=forward.operation_id,
            compensation_tool_name="refund",
            status=obligation_status,
            receipts=forward.receipts,
        )
    return SagaSnapshot(
        saga_id=SAGA_ID,
        seq=4,
        status=SagaStatus.RUNNING,
        definition_version="workflow-v1",
        operations={item.operation_id: item for item in operations},
        obligations=obligations,
    )


@pytest.mark.parametrize("trigger", tuple(UnwindTrigger))
def test_safe_confirmed_effects_choose_compensation(trigger: UnwindTrigger) -> None:
    decision = EmergencyUnwinder().plan(
        _snapshot(_operation(OperationStatus.EFFECT_CONFIRMED)), _registry(), trigger
    )

    assert decision.action is UnwindAction.COMPENSATE
    assert decision.blocker_operation_ids == ()
    assert decision.frontier is not None
    assert tuple(item.forward_operation_id for item in decision.frontier.ordered) == (OPERATION_ID,)


def test_stalled_compensation_requires_human_instead_of_restarting_phase() -> None:
    running = _snapshot(_operation(OperationStatus.EFFECT_CONFIRMED))
    compensating = running.model_copy(update={"status": SagaStatus.COMPENSATING})

    decision = EmergencyUnwinder().plan(compensating, _registry(), _trigger())

    assert decision.action is UnwindAction.HUMAN_REQUIRED
    assert decision.reason_codes == ("compensation_progress_stalled",)
    assert decision.blocker_operation_ids == (OPERATION_ID,)


@pytest.mark.parametrize(
    "status",
    (
        OperationStatus.INTENT_DURABLE,
        OperationStatus.DISPATCHED,
        OperationStatus.OUTCOME_UNKNOWN,
    ),
)
def test_potentially_live_forward_work_chooses_reconciliation(
    status: OperationStatus,
) -> None:
    decision = EmergencyUnwinder().plan(_snapshot(_operation(status)), _registry(), _trigger())

    assert decision.action is UnwindAction.RECONCILE
    assert decision.blocker_operation_ids == (OPERATION_ID,)
    assert decision.reason_codes == ("forward_work_unresolved",)


def test_exact_partial_forward_receipts_remain_compensatable() -> None:
    decision = EmergencyUnwinder().plan(
        _snapshot(_operation(OperationStatus.PARTIAL_EFFECT_CONFIRMED)),
        _registry(),
        _trigger(),
    )

    assert decision.action is UnwindAction.COMPENSATE
    assert decision.frontier is not None
    assert decision.frontier.ordered[0].receipts == ({"receipt_id": "11111111"},)


def test_irreversible_effect_requires_human() -> None:
    decision = EmergencyUnwinder().plan(
        _snapshot(_operation(OperationStatus.EFFECT_CONFIRMED)),
        _registry(Reversibility.IRREVERSIBLE),
        _trigger(),
    )

    assert decision.action is UnwindAction.HUMAN_REQUIRED
    assert decision.reason_codes == ("irreversible_effect",)
    assert decision.blocker_operation_ids == (OPERATION_ID,)


def test_opaque_receipt_mismatch_requires_human() -> None:
    snapshot = _snapshot(_operation(OperationStatus.EFFECT_CONFIRMED))
    obligation = snapshot.obligations[OPERATION_ID].model_copy(
        update={"receipts": ({"receipt_id": "different"},)}
    )

    decision = EmergencyUnwinder().plan(
        snapshot.model_copy(update={"obligations": {OPERATION_ID: obligation}}),
        _registry(),
        _trigger(),
    )

    assert decision.action is UnwindAction.HUMAN_REQUIRED
    assert decision.reason_codes == ("compensation_evidence_invalid",)


def test_effect_free_state_is_only_an_abort_clean_candidate() -> None:
    no_effect = _operation(OperationStatus.NO_EFFECT_CONFIRMED)
    decision = EmergencyUnwinder().plan(
        _snapshot(no_effect, obligation_status=ObligationStatus.NOT_REQUIRED),
        _registry(),
        _trigger(),
    )

    assert decision.action is UnwindAction.ABORT_CLEAN
    assert decision.frontier is None


def test_missing_historical_definition_requires_human_with_public_blocker() -> None:
    decision = EmergencyUnwinder().plan(
        _snapshot(
            _operation(OperationStatus.EFFECT_CONFIRMED),
            obligation_status=None,
        ),
        ToolRegistry(),
        _trigger(),
    )

    assert decision.action is UnwindAction.HUMAN_REQUIRED
    assert decision.reason_codes == ("historical_tool_evidence_unavailable",)
    assert decision.blocker_operation_ids == (OPERATION_ID,)


def test_blockers_are_sorted_and_repeatable() -> None:
    left = _operation(OperationStatus.OUTCOME_UNKNOWN, OTHER_OPERATION_ID)
    right = _operation(OperationStatus.DISPATCHED, OPERATION_ID)
    snapshot = _snapshot(left, right, obligation_status=ObligationStatus.ARMED)

    first = EmergencyUnwinder().plan(snapshot, _registry(), _trigger())
    second = EmergencyUnwinder().plan(snapshot, _registry(), _trigger())

    assert first == second
    assert first.blocker_operation_ids == (OPERATION_ID, OTHER_OPERATION_ID)


def _trigger() -> UnwindTrigger:
    return UnwindTrigger.BUDGET_EXHAUSTED
