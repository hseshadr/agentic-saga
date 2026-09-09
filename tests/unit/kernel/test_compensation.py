from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, cast

import pytest
from pydantic import BaseModel, ConfigDict

from agentic_saga.contracts.common import Direction, JsonObject, Reversibility
from agentic_saga.contracts.events import CompensationIntentRecorded
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
from agentic_saga.kernel.compensation import (
    CompensationBlocked,
    CompensationPlanner,
    DependencyCycle,
)
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import (
    CompensationObligation,
    ObligationStatus,
    OperationRecord,
    OperationStatus,
    SagaSnapshot,
)

SAGA_ID = "saga_0000000000009001"
ORDER_OP = f"op_{'1' * 64}"
PAYMENT_OP = f"op_{'2' * 64}"
INVENTORY_OP = f"op_{'3' * 64}"
UNKNOWN_OP = f"op_{'4' * 64}"
REPAIR_OP = f"op_{'5' * 64}"
HASH = "a" * 64


class Command(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    kind: Literal["repair"] = "repair"


class Adapter:
    async def execute(self, command: Command, context: EffectContext) -> EffectOutcome:
        del command
        return EffectConfirmed(receipt={"operation_id": context.operation_id})

    async def reconcile(self, command: Command, context: ReconcileContext) -> ReconciliationOutcome:
        del command
        return ReconcileEffectConfirmed(receipt={"correlation": context.correlation})


def capabilities() -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=True,
    )


def definition(
    name: str,
    *,
    compensate_with: str | None = None,
    dependencies: tuple[str, ...] = (),
    independent_with: tuple[str, ...] = (),
) -> EffectToolDefinition[Command]:
    return EffectToolDefinition(
        name=name,
        definition_version=f"{name}-v1",
        command_schema_version="command-v1",
        input_model=Command,
        adapter=Adapter(),
        capabilities=capabilities(),
        compensate_with=compensate_with,
        compensation_dependencies=dependencies,
        compensation_independent_with=independent_with,
        compensation_resource_selector=("resource_id",),
    )


def registry(
    *,
    payment_dependencies: tuple[str, ...] = ("create_order",),
    inventory_dependencies: tuple[str, ...] = ("charge_payment",),
    order_independent: tuple[str, ...] = (),
    payment_independent: tuple[str, ...] = (),
) -> ToolRegistry:
    definitions = (
        definition(
            "create_order",
            compensate_with="cancel_order",
            independent_with=order_independent,
        ),
        definition(
            "charge_payment",
            compensate_with="refund_payment",
            dependencies=payment_dependencies,
            independent_with=payment_independent,
        ),
        definition(
            "reserve_inventory",
            compensate_with="release_inventory",
            dependencies=inventory_dependencies,
        ),
        definition("cancel_order"),
        definition("refund_payment"),
        definition("release_inventory"),
    )
    return ToolRegistry(definitions)


def operation(  # noqa: PLR0913
    operation_id: str,
    tool_name: str,
    step_number: int,
    *,
    status: OperationStatus = OperationStatus.EFFECT_CONFIRMED,
    generation: int = 0,
    resource_id: str | None = None,
    receipts: tuple[JsonObject, ...] = (),
) -> OperationRecord:
    resource = resource_id or f"resource-{step_number}"
    stored_receipts = receipts or ({"receipt_id": operation_id[-8:]},)
    if status is OperationStatus.OUTCOME_UNKNOWN:
        stored_receipts = ()
    return OperationRecord(
        operation_id=operation_id,
        step_instance_id=f"step_{step_number:08d}",
        direction=Direction.FORWARD,
        semantic_generation=generation,
        delivery_attempt=1,
        tool_name=tool_name,
        status=status,
        redacted_command={"resource_id": resource, "kind": "repair"},
        command_hash=HASH,
        redacted_result={"outcome": status.value},
        result_hash=HASH,
        receipts=stored_receipts,
        correlation="opaque://provider/lookup-4/v1"
        if status is OperationStatus.OUTCOME_UNKNOWN
        else None,
    )


def obligation(
    operation_id: str,
    compensation: str,
    *,
    status: ObligationStatus = ObligationStatus.ELIGIBLE,
    receipts: tuple[JsonObject, ...] = (),
) -> CompensationObligation:
    stored_receipts = receipts or ({"receipt_id": operation_id[-8:]},)
    if status is ObligationStatus.ARMED:
        stored_receipts = ()
    return CompensationObligation(
        forward_operation_id=operation_id,
        compensation_tool_name=compensation,
        status=status,
        receipts=stored_receipts,
    )


def snapshot(
    operations: Iterable[OperationRecord],
    obligations: Iterable[CompensationObligation],
) -> SagaSnapshot:
    operation_map = {item.operation_id: item for item in operations}
    obligation_map = {item.forward_operation_id: item for item in obligations}
    return SagaSnapshot(
        saga_id=SAGA_ID,
        seq=9,
        status=SagaStatus.COMPENSATING,
        definition_version="checkout-v1",
        operations=operation_map,
        obligations=obligation_map,
    )


def planner() -> CompensationPlanner:
    return CompensationPlanner(OperationIdentityFactory(b"compensation-tests"))


def test_only_confirmed_or_exact_partial_effects_are_eligible() -> None:
    confirmed = operation(ORDER_OP, "create_order", 1, receipts=({"order_id": "o1"},))
    unknown = operation(
        UNKNOWN_OP,
        "charge_payment",
        4,
        status=OperationStatus.OUTCOME_UNKNOWN,
    )
    current = snapshot(
        (unknown, confirmed),
        (
            obligation(ORDER_OP, "cancel_order", receipts=confirmed.receipts),
            obligation(UNKNOWN_OP, "refund_payment", status=ObligationStatus.ARMED),
        ),
    )

    frontier = planner().plan(current, registry())

    assert frontier.blocked_by_unknown == (UNKNOWN_OP,)
    assert frontier.runnable == ()
    assert tuple(item.forward_operation_id for item in frontier.ordered) == (ORDER_OP,)


def test_frontier_uses_reverse_topological_order_with_stable_ties() -> None:
    order = operation(ORDER_OP, "create_order", 1)
    payment = operation(PAYMENT_OP, "charge_payment", 2)
    inventory = operation(INVENTORY_OP, "reserve_inventory", 3)
    current = snapshot(
        (inventory, order, payment),
        (
            obligation(INVENTORY_OP, "release_inventory"),
            obligation(ORDER_OP, "cancel_order"),
            obligation(PAYMENT_OP, "refund_payment"),
        ),
    )

    first = planner().plan(current, registry())
    second = planner().plan(current.model_copy(), registry())

    assert tuple(item.tool_name for item in first.ordered) == (
        "release_inventory",
        "refund_payment",
        "cancel_order",
    )
    assert first == second
    assert first.runnable == (first.ordered[0],)
    assert first.parallel_groups == ()


def test_partial_effect_compensates_exact_normalized_receipts() -> None:
    receipts = ({"reservation_id": "r1"}, {"reservation_id": "r2"})
    partial = operation(
        INVENTORY_OP,
        "reserve_inventory",
        3,
        status=OperationStatus.PARTIAL_EFFECT_CONFIRMED,
        receipts=receipts,
    )
    current = snapshot(
        (partial,),
        (obligation(INVENTORY_OP, "release_inventory", receipts=receipts),),
    )

    item = planner().plan(current, registry()).ordered[0]

    assert item.receipts == receipts


def test_compensation_identity_uses_forward_step_and_semantic_generation() -> None:
    forward = operation(
        PAYMENT_OP,
        "charge_payment",
        2,
        generation=7,
        receipts=({"payment_id": "p1"},),
    )
    current = snapshot(
        (forward,),
        (obligation(PAYMENT_OP, "refund_payment", receipts=forward.receipts),),
    )

    item = planner().plan(current, registry()).ordered[0]

    expected = OperationIdentityFactory(b"compensation-tests").create(
        SAGA_ID,
        forward.step_instance_id,
        Direction.COMPENSATION,
        forward.semantic_generation,
    )
    assert item.operation_id == expected
    assert item.semantic_generation == 7


def test_registry_rejects_dependency_cycle_and_planner_rejects_bad_replay() -> None:
    cyclic = (
        definition(
            "create_order",
            compensate_with="cancel_order",
            dependencies=("charge_payment",),
        ),
        definition(
            "charge_payment",
            compensate_with="refund_payment",
            dependencies=("create_order",),
        ),
        definition("cancel_order"),
        definition("refund_payment"),
    )

    with pytest.raises(DependencyCycle):
        ToolRegistry(cyclic)

    safe = registry(payment_dependencies=(), inventory_dependencies=())
    safe._definitions["create_order"] = cast(EffectToolDefinition[BaseModel], cyclic[0])
    safe._definitions["charge_payment"] = cast(EffectToolDefinition[BaseModel], cyclic[1])
    current = snapshot(
        (
            operation(ORDER_OP, "create_order", 1),
            operation(PAYMENT_OP, "charge_payment", 2),
        ),
        (
            obligation(ORDER_OP, "cancel_order"),
            obligation(PAYMENT_OP, "refund_payment"),
        ),
    )

    with pytest.raises(DependencyCycle):
        planner().plan(current, safe)


def test_parallel_frontier_requires_symmetric_independence_and_distinct_resources() -> None:
    order = operation(ORDER_OP, "create_order", 1, resource_id="order-1")
    payment = operation(PAYMENT_OP, "charge_payment", 2, resource_id="account-7")
    current = snapshot(
        (payment, order),
        (
            obligation(PAYMENT_OP, "refund_payment"),
            obligation(ORDER_OP, "cancel_order"),
        ),
    )
    parallel_registry = registry(
        payment_dependencies=(),
        inventory_dependencies=(),
        order_independent=("charge_payment",),
        payment_independent=("create_order",),
    )

    frontier = planner().plan(current, parallel_registry)

    assert frontier.runnable == frontier.ordered
    assert frontier.parallel_groups == (frontier.ordered,)

    missing = order.model_copy(update={"redacted_command": {}})
    one_missing = snapshot(
        (payment, missing),
        (
            obligation(PAYMENT_OP, "refund_payment"),
            obligation(ORDER_OP, "cancel_order"),
        ),
    )
    assert len(planner().plan(one_missing, parallel_registry).runnable) == 1


def test_receipt_mismatch_fails_closed_without_guessing() -> None:
    forward = operation(
        PAYMENT_OP,
        "charge_payment",
        2,
        receipts=({"payment_id": "p1"},),
    )
    current = snapshot(
        (forward,),
        (obligation(PAYMENT_OP, "refund_payment", receipts=({"payment_id": "p2"},)),),
    )

    with pytest.raises(CompensationBlocked, match="receipt"):
        planner().plan(current, registry())


def test_obligation_targeting_compensation_record_fails_closed() -> None:
    # Given
    base = operation(PAYMENT_OP, "charge_payment", 2)
    repair = OperationRecord.model_validate(
        base.model_dump(mode="python")
        | {
            "direction": Direction.COMPENSATION,
            "tool_name": "refund_payment",
            "compensates_operation_id": ORDER_OP,
        }
    )
    current = snapshot(
        (repair,),
        (obligation(PAYMENT_OP, "refund_payment", receipts=repair.receipts),),
    )

    # When / Then
    with pytest.raises(CompensationBlocked, match="does not target a forward effect"):
        planner().plan(current, registry())


def test_obligation_with_misbound_forward_record_fails_closed() -> None:
    # Given
    record = OperationRecord.model_validate(
        operation(PAYMENT_OP, "charge_payment", 2).model_dump(mode="python")
        | {"operation_id": ORDER_OP}
    )
    persisted = SagaSnapshot(
        saga_id=SAGA_ID,
        seq=9,
        status=SagaStatus.COMPENSATING,
        definition_version="checkout-v1",
        operations={PAYMENT_OP: record},
        obligations={PAYMENT_OP: obligation(PAYMENT_OP, "refund_payment")},
    )

    # When / Then
    with pytest.raises(CompensationBlocked, match="obligation identity changed"):
        planner().plan(persisted, registry())


def test_corrupt_resolved_obligation_without_compensation_evidence_fails_closed() -> None:
    forward = operation(PAYMENT_OP, "charge_payment", 2)
    resolved = obligation(PAYMENT_OP, "refund_payment").model_copy(
        update={"status": ObligationStatus.SATISFIED}
    )

    with pytest.raises(CompensationBlocked, match="satisfied"):
        planner().plan(snapshot((forward,), (resolved,)), registry())


def test_armed_obligation_after_proven_absence_fails_closed() -> None:
    absent = operation(
        PAYMENT_OP,
        "charge_payment",
        2,
        status=OperationStatus.NO_EFFECT_CONFIRMED,
    )
    armed = obligation(PAYMENT_OP, "refund_payment", status=ObligationStatus.ARMED)

    with pytest.raises(CompensationBlocked, match="armed"):
        planner().plan(snapshot((absent,), (armed,)), registry())


def test_unknown_evidence_hides_parallel_dispatch_group() -> None:
    order = operation(ORDER_OP, "create_order", 1, resource_id="order-1")
    payment = operation(PAYMENT_OP, "charge_payment", 2, resource_id="account-7")
    unknown = operation(
        UNKNOWN_OP,
        "reserve_inventory",
        4,
        status=OperationStatus.OUTCOME_UNKNOWN,
    )
    current = snapshot(
        (order, payment, unknown),
        (
            obligation(ORDER_OP, "cancel_order"),
            obligation(PAYMENT_OP, "refund_payment"),
            obligation(UNKNOWN_OP, "release_inventory", status=ObligationStatus.ARMED),
        ),
    )
    independent = registry(
        payment_dependencies=(),
        inventory_dependencies=(),
        order_independent=("charge_payment",),
        payment_independent=("create_order",),
    )

    frontier = planner().plan(current, independent)

    assert frontier.runnable == ()
    assert frontier.parallel_groups == ()


def test_missing_forward_operation_and_compensation_definition_fail_closed() -> None:
    missing = obligation(PAYMENT_OP, "refund_payment")
    empty = snapshot((), (missing,))

    with pytest.raises(CompensationBlocked, match="forward operation"):
        planner().plan(empty, registry())

    forward = operation(PAYMENT_OP, "charge_payment", 2)
    forward_only = ToolRegistry((definition("charge_payment", compensate_with="refund_payment"),))
    with pytest.raises(CompensationBlocked, match="not registered"):
        planner().plan(snapshot((forward,), (missing,)), forward_only)


def repair_operation(
    forward: OperationRecord,
    status: OperationStatus,
) -> OperationRecord:
    return forward.model_copy(
        update={
            "operation_id": REPAIR_OP,
            "direction": Direction.COMPENSATION,
            "tool_name": "refund_payment",
            "status": status,
            "compensates_operation_id": forward.operation_id,
        }
    )


@pytest.mark.parametrize(
    ("obligation_status", "repair_status", "message"),
    [
        (ObligationStatus.IN_PROGRESS, OperationStatus.EFFECT_CONFIRMED, "in-progress"),
        (ObligationStatus.SATISFIED, OperationStatus.PARTIAL_EFFECT_CONFIRMED, "satisfied"),
    ],
)
def test_inactive_obligation_requires_matching_compensation_evidence(
    obligation_status: ObligationStatus,
    repair_status: OperationStatus,
    message: str,
) -> None:
    forward = operation(PAYMENT_OP, "charge_payment", 2)
    repair = repair_operation(forward, repair_status)
    inactive = obligation(PAYMENT_OP, "refund_payment").model_copy(
        update={"status": obligation_status, "compensation_operation_id": REPAIR_OP}
    )

    with pytest.raises(CompensationBlocked, match=message):
        planner().plan(snapshot((forward, repair), (inactive,)), registry())


def test_in_progress_obligation_with_misbound_repair_target_fails_closed() -> None:
    # Given
    forward = operation(PAYMENT_OP, "charge_payment", 2)
    repair = OperationRecord.model_validate(
        repair_operation(forward, OperationStatus.DISPATCHED).model_dump(mode="python")
        | {"compensates_operation_id": ORDER_OP}
    )
    inactive = CompensationObligation.model_validate(
        obligation(PAYMENT_OP, "refund_payment").model_dump(mode="python")
        | {
            "status": ObligationStatus.IN_PROGRESS,
            "compensation_operation_id": REPAIR_OP,
        }
    )

    # When / Then
    with pytest.raises(CompensationBlocked, match="compensation target changed"):
        planner().plan(snapshot((forward, repair), (inactive,)), registry())


def test_valid_satisfied_and_absent_obligations_leave_no_runnable_repair() -> None:
    forward = operation(PAYMENT_OP, "charge_payment", 2)
    repair = repair_operation(forward, OperationStatus.EFFECT_CONFIRMED)
    satisfied = obligation(PAYMENT_OP, "refund_payment").model_copy(
        update={"status": ObligationStatus.SATISFIED, "compensation_operation_id": REPAIR_OP}
    )

    resolved = planner().plan(snapshot((forward, repair), (satisfied,)), registry())

    assert resolved.ordered == ()
    assert resolved.runnable == ()


def test_inflight_and_absent_forward_states_are_reported_without_compensation() -> None:
    inflight = operation(
        PAYMENT_OP,
        "charge_payment",
        2,
        status=OperationStatus.INTENT_DURABLE,
    )
    armed = obligation(PAYMENT_OP, "refund_payment", status=ObligationStatus.ARMED)

    pending = planner().plan(snapshot((inflight,), (armed,)), registry())

    assert pending.blocked_by_inflight == (PAYMENT_OP,)
    absent = inflight.model_copy(update={"status": OperationStatus.NO_EFFECT_CONFIRMED})
    not_required = armed.model_copy(update={"status": ObligationStatus.NOT_REQUIRED})
    assert planner().plan(snapshot((absent,), (not_required,)), registry()).ordered == ()


def test_not_required_obligation_without_absence_evidence_fails_closed() -> None:
    # Given
    forward = operation(PAYMENT_OP, "charge_payment", 2)
    not_required = CompensationObligation.model_validate(
        obligation(PAYMENT_OP, "refund_payment").model_dump(mode="python")
        | {"status": ObligationStatus.NOT_REQUIRED}
    )

    # When / Then
    with pytest.raises(CompensationBlocked, match="lacks absence evidence"):
        planner().plan(snapshot((forward,), (not_required,)), registry())


def test_eligible_obligation_without_proven_effect_fails_closed() -> None:
    # Given
    unproven = operation(
        PAYMENT_OP,
        "charge_payment",
        2,
        status=OperationStatus.NO_EFFECT_CONFIRMED,
    )
    eligible = obligation(PAYMENT_OP, "refund_payment", receipts=unproven.receipts)

    # When / Then
    with pytest.raises(CompensationBlocked, match="lacks proven forward effect"):
        planner().plan(snapshot((unproven,), (eligible,)), registry())


def test_eligible_obligation_without_exact_receipt_fails_closed() -> None:
    # Given
    forward = OperationRecord.model_validate(
        operation(PAYMENT_OP, "charge_payment", 2).model_dump(mode="python") | {"receipts": ()}
    )
    eligible = CompensationObligation(
        forward_operation_id=PAYMENT_OP,
        compensation_tool_name="refund_payment",
        status=ObligationStatus.ELIGIBLE,
        receipts=(),
    )

    # When / Then
    with pytest.raises(CompensationBlocked, match="no exact receipt"):
        planner().plan(snapshot((forward,), (eligible,)), registry())


def test_durable_compensation_tool_must_match_registered_forward_definition() -> None:
    # Given
    forward = operation(PAYMENT_OP, "charge_payment", 2)
    mismatched = obligation(PAYMENT_OP, "cancel_order", receipts=forward.receipts)

    # When / Then
    with pytest.raises(CompensationBlocked, match="registered compensation"):
        planner().plan(snapshot((forward,), (mismatched,)), registry())


def test_parallelism_fails_closed_for_asymmetry_or_missing_resource_selector() -> None:
    order = operation(ORDER_OP, "create_order", 1, resource_id="order-1")
    payment = operation(PAYMENT_OP, "charge_payment", 2, resource_id="account-7")
    current = snapshot(
        (order, payment),
        (
            obligation(ORDER_OP, "cancel_order"),
            obligation(PAYMENT_OP, "refund_payment"),
        ),
    )
    asymmetric = registry(
        payment_dependencies=(),
        inventory_dependencies=(),
        order_independent=("charge_payment",),
    )
    assert len(planner().plan(current, asymmetric).runnable) == 1

    definitions = list(asymmetric.effect_definitions())
    charge_index = next(
        index for index, item in enumerate(definitions) if item.name == "charge_payment"
    )
    definitions[charge_index] = replace(
        definitions[charge_index],
        compensation_independent_with=("create_order",),
        compensation_resource_selector=(),
    )
    create_index = next(
        index for index, item in enumerate(definitions) if item.name == "create_order"
    )
    definitions[create_index] = replace(
        definitions[create_index], compensation_independent_with=("charge_payment",)
    )
    assert len(planner().plan(current, ToolRegistry(definitions)).runnable) == 1


def test_parallelism_requires_the_same_resource_selector_path() -> None:
    order = operation(ORDER_OP, "create_order", 1).model_copy(
        update={"redacted_command": {"source": "left", "resource_id": "shared"}}
    )
    payment = operation(PAYMENT_OP, "charge_payment", 2).model_copy(
        update={"redacted_command": {"destination": "right", "resource_id": "shared"}}
    )
    current = snapshot(
        (order, payment),
        (obligation(ORDER_OP, "cancel_order"), obligation(PAYMENT_OP, "refund_payment")),
    )
    definitions = list(
        registry(
            payment_dependencies=(),
            inventory_dependencies=(),
            order_independent=("charge_payment",),
            payment_independent=("create_order",),
        ).effect_definitions()
    )
    definitions = _replace_selector(definitions, "create_order", ("source",))
    definitions = _replace_selector(definitions, "charge_payment", ("destination",))

    frontier = planner().plan(current, ToolRegistry(definitions))

    assert len(frontier.runnable) == 1
    assert frontier.parallel_groups == ()


def _replace_selector(
    definitions: list[EffectToolDefinition[BaseModel]], name: str, selector: tuple[str, ...]
) -> list[EffectToolDefinition[BaseModel]]:
    index = next(index for index, item in enumerate(definitions) if item.name == name)
    definitions[index] = replace(definitions[index], compensation_resource_selector=selector)
    return definitions


def test_partial_compensation_is_a_human_blocker_not_runnable_work() -> None:
    forward = operation(PAYMENT_OP, "charge_payment", 2)
    repair = repair_operation(forward, OperationStatus.PARTIAL_EFFECT_CONFIRMED)
    partial = obligation(PAYMENT_OP, "refund_payment").model_copy(
        update={"status": ObligationStatus.IN_PROGRESS, "compensation_operation_id": REPAIR_OP}
    )

    frontier = planner().plan(snapshot((forward, repair), (partial,)), registry())

    assert frontier.ordered == ()
    assert frontier.runnable == ()
    assert frontier.blocked_by_partial == (REPAIR_OP,)


def test_compensation_intent_binds_forward_generation_and_exact_receipts() -> None:
    forward = operation(
        PAYMENT_OP,
        "charge_payment",
        2,
        generation=7,
        receipts=({"payment_id": "p1"},),
    )
    current = snapshot(
        (forward,),
        (obligation(PAYMENT_OP, "refund_payment", receipts=forward.receipts),),
    )
    item = planner().plan(current, registry()).ordered[0]
    event = CompensationIntentRecorded(
        event_id="evt_0000000000009010",
        saga_id=SAGA_ID,
        saga_seq=10,
        definition_version=current.definition_version,
        fence_token=1,
        actor="kernel",
        trace_id="trace_0000000000009001",
        recorded_at=datetime(2026, 9, 7, tzinfo=UTC),
        operation_id=item.operation_id,
        step_instance_id=item.step_instance_id,
        direction=Direction.COMPENSATION,
        semantic_generation=item.semantic_generation,
        delivery_attempt=1,
        tool_name=item.tool_name,
        redacted_command={"resource_id": "account-7", "kind": "repair"},
        command_hash=HASH,
        compensates_operation_id=item.forward_operation_id,
        forward_receipts=item.receipts,
    )

    updated = reduce_event(current, event)

    assert updated.operations[item.operation_id].semantic_generation == 7
    assert updated.obligations[PAYMENT_OP].compensation_operation_id == item.operation_id
