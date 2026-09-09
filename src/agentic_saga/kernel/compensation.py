from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    OperationId,
    StepInstanceId,
    canonical_json,
)
from agentic_saga.contracts.tools import (
    DependencyCycle,
    EffectToolDefinition,
    ToolRegistry,
    UnknownToolError,
)
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.state import (
    CompensationObligation,
    ObligationStatus,
    OperationRecord,
    OperationStatus,
    SagaSnapshot,
)

_DEFAULT_IDENTITY = OperationIdentityFactory(b"agentic-saga-compensation-v1")
_PROVEN_EFFECTS = frozenset(
    {OperationStatus.EFFECT_CONFIRMED, OperationStatus.PARTIAL_EFFECT_CONFIRMED}
)
_INFLIGHT = frozenset(
    {OperationStatus.PLANNED, OperationStatus.INTENT_DURABLE, OperationStatus.DISPATCHED}
)


class CompensationBlocked(ValueError):
    """Raised when durable evidence cannot safely identify a compensation."""


class CompensationItem(BaseModel):
    """One verified compensation target containing only durable public evidence."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    forward_operation_id: OperationId
    operation_id: OperationId
    step_instance_id: StepInstanceId
    semantic_generation: int = Field(strict=True, ge=0)
    tool_name: str
    receipts: tuple[JsonObject, ...]


class CompensationFrontier(BaseModel):
    """Deterministic repair order and the subset safe to dispatch now."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    ordered: tuple[CompensationItem, ...]
    runnable: tuple[CompensationItem, ...]
    parallel_groups: tuple[tuple[CompensationItem, ...], ...]
    blocked_by_unknown: tuple[OperationId, ...]
    blocked_by_inflight: tuple[OperationId, ...]
    blocked_by_partial: tuple[OperationId, ...]


@dataclass(frozen=True)
class _Node:
    operation: OperationRecord
    definition: EffectToolDefinition[BaseModel]
    item: CompensationItem


@dataclass(frozen=True)
class CompensationPlanner:
    """Derive the currently safe compensation frontier from durable saga evidence."""

    identity_factory: OperationIdentityFactory = _DEFAULT_IDENTITY

    def plan(self, snapshot: SagaSnapshot, registry: ToolRegistry) -> CompensationFrontier:
        _validate_registry(registry)
        nodes = _eligible_nodes(snapshot, registry, self.identity_factory)
        layers = _reverse_layers(nodes)
        ordered = tuple(node.item for layer in layers for node in layer)
        unknown = _unknown_ids(snapshot)
        inflight = _inflight_ids(snapshot)
        partial = _partial_ids(snapshot)
        runnable = _runnable(layers, unknown, inflight, partial)
        return _frontier(ordered, runnable, unknown, inflight, partial)


def _frontier(
    ordered: tuple[CompensationItem, ...],
    runnable: tuple[CompensationItem, ...],
    unknown: tuple[OperationId, ...],
    inflight: tuple[OperationId, ...],
    partial: tuple[OperationId, ...],
) -> CompensationFrontier:
    groups = (runnable,) if len(runnable) > 1 else ()
    return CompensationFrontier(
        ordered=ordered,
        runnable=runnable,
        parallel_groups=groups,
        blocked_by_unknown=unknown,
        blocked_by_inflight=inflight,
        blocked_by_partial=partial,
    )


def _effect_definition(registry: ToolRegistry, name: str) -> EffectToolDefinition[BaseModel]:
    try:
        definition = registry.definition(name)
    except UnknownToolError as error:
        raise CompensationBlocked("compensation definition is not registered") from error
    if not isinstance(definition, EffectToolDefinition):
        raise CompensationBlocked("compensation definition is not an effect")
    return definition


def _validated_node(
    snapshot: SagaSnapshot,
    registry: ToolRegistry,
    identity_factory: OperationIdentityFactory,
    obligation: CompensationObligation,
) -> _Node | None:
    operation = snapshot.operations.get(obligation.forward_operation_id)
    if operation is None:
        raise CompensationBlocked("compensation obligation has no forward operation")
    _validate_obligation_identity(operation, obligation)
    if obligation.status is not ObligationStatus.ELIGIBLE:
        _validate_inactive_obligation(snapshot, operation, obligation)
        return None
    return _eligible_node(snapshot, registry, identity_factory, operation, obligation)


def _validate_obligation_identity(
    operation: OperationRecord, obligation: CompensationObligation
) -> None:
    if operation.direction is not Direction.FORWARD:
        raise CompensationBlocked("compensation obligation does not target a forward effect")
    if operation.operation_id != obligation.forward_operation_id:
        raise CompensationBlocked("compensation obligation identity changed")


def _validate_inactive_obligation(
    snapshot: SagaSnapshot,
    operation: OperationRecord,
    obligation: CompensationObligation,
) -> None:
    if obligation.status is ObligationStatus.ARMED:
        _validate_armed(operation, obligation)
    elif obligation.status is ObligationStatus.IN_PROGRESS:
        _validate_in_progress(snapshot, obligation)
    elif obligation.status is ObligationStatus.SATISFIED:
        _validate_satisfied(snapshot, obligation)
    elif obligation.status is ObligationStatus.NOT_REQUIRED:
        _validate_not_required(operation)


def _validate_armed(operation: OperationRecord, obligation: CompensationObligation) -> None:
    valid = _INFLIGHT | {OperationStatus.OUTCOME_UNKNOWN}
    if operation.status not in valid or obligation.receipts:
        raise CompensationBlocked("armed obligation does not match unresolved forward evidence")


def _validate_in_progress(snapshot: SagaSnapshot, obligation: CompensationObligation) -> None:
    repair = _compensation_operation(snapshot, obligation, "in-progress")
    valid = _INFLIGHT | {
        OperationStatus.OUTCOME_UNKNOWN,
        OperationStatus.PARTIAL_EFFECT_CONFIRMED,
    }
    if repair.status not in valid:
        raise CompensationBlocked("in-progress obligation lacks an unresolved compensation")


def _validate_satisfied(snapshot: SagaSnapshot, obligation: CompensationObligation) -> None:
    repair = _compensation_operation(snapshot, obligation, "satisfied")
    if repair.status is not OperationStatus.EFFECT_CONFIRMED:
        raise CompensationBlocked("satisfied obligation lacks confirmed compensation evidence")


def _compensation_operation(
    snapshot: SagaSnapshot,
    obligation: CompensationObligation,
    status: str,
) -> OperationRecord:
    operation_id = obligation.compensation_operation_id
    repair = None if operation_id is None else snapshot.operations.get(operation_id)
    if repair is None or repair.direction is not Direction.COMPENSATION:
        raise CompensationBlocked(f"{status} obligation lacks compensation evidence")
    if repair.compensates_operation_id != obligation.forward_operation_id:
        raise CompensationBlocked(f"{status} compensation target changed")
    return repair


def _validate_not_required(operation: OperationRecord) -> None:
    if operation.status is not OperationStatus.NO_EFFECT_CONFIRMED:
        raise CompensationBlocked("not-required obligation lacks absence evidence")


def _eligible_node(
    snapshot: SagaSnapshot,
    registry: ToolRegistry,
    identity_factory: OperationIdentityFactory,
    operation: OperationRecord,
    obligation: CompensationObligation,
) -> _Node:
    _validate_eligible_evidence(operation, obligation)
    definition = _effect_definition(registry, operation.tool_name)
    _validate_definition(registry, definition, obligation)
    item = _compensation_item(snapshot, operation, obligation, identity_factory)
    return _Node(operation, definition, item)


def _validate_eligible_evidence(
    operation: OperationRecord, obligation: CompensationObligation
) -> None:
    if operation.direction is not Direction.FORWARD:
        raise CompensationBlocked("eligible obligation lacks proven forward effect")
    _require_proven_status(operation)
    if operation.operation_id != obligation.forward_operation_id:
        raise CompensationBlocked("compensation obligation identity changed")
    _require_exact_receipts(operation, obligation)


def _require_proven_status(operation: OperationRecord) -> None:
    if operation.status not in _PROVEN_EFFECTS:
        raise CompensationBlocked("eligible obligation lacks proven forward effect")


def _require_exact_receipts(operation: OperationRecord, obligation: CompensationObligation) -> None:
    if operation.receipts != obligation.receipts:
        raise CompensationBlocked("compensation receipt evidence does not match forward effect")
    if not operation.receipts:
        raise CompensationBlocked("proven compensation target has no exact receipt")


def _validate_definition(
    registry: ToolRegistry,
    definition: EffectToolDefinition[BaseModel],
    obligation: CompensationObligation,
) -> None:
    if definition.compensate_with != obligation.compensation_tool_name:
        raise CompensationBlocked("registered compensation does not match durable obligation")
    _effect_definition(registry, obligation.compensation_tool_name)


def _compensation_item(
    snapshot: SagaSnapshot,
    operation: OperationRecord,
    obligation: CompensationObligation,
    identity_factory: OperationIdentityFactory,
) -> CompensationItem:
    operation_id = _compensation_operation_id(snapshot, operation, identity_factory)
    return CompensationItem(
        forward_operation_id=operation.operation_id,
        operation_id=operation_id,
        step_instance_id=operation.step_instance_id,
        semantic_generation=operation.semantic_generation,
        tool_name=obligation.compensation_tool_name,
        receipts=operation.receipts,
    )


def _compensation_operation_id(
    snapshot: SagaSnapshot,
    operation: OperationRecord,
    identity_factory: OperationIdentityFactory,
) -> OperationId:
    return identity_factory.create(
        snapshot.saga_id,
        operation.step_instance_id,
        Direction.COMPENSATION,
        operation.semantic_generation,
    )


def _eligible_nodes(
    snapshot: SagaSnapshot,
    registry: ToolRegistry,
    identity_factory: OperationIdentityFactory,
) -> tuple[_Node, ...]:
    candidates = (
        _validated_node(snapshot, registry, identity_factory, obligation)
        for obligation in snapshot.obligations.values()
    )
    nodes = (node for node in candidates if node is not None)
    return tuple(sorted(nodes, key=_node_key))


def _node_key(node: _Node) -> tuple[str, str]:
    return node.operation.step_instance_id, node.operation.operation_id


def _dependency_ids(node: _Node, nodes: tuple[_Node, ...]) -> set[OperationId]:
    names = frozenset(node.definition.compensation_dependencies)
    return {item.operation.operation_id for item in nodes if item.operation.tool_name in names}


def _reverse_layers(nodes: tuple[_Node, ...]) -> tuple[tuple[_Node, ...], ...]:
    pending = {node.operation.operation_id: _dependency_ids(node, nodes) for node in nodes}
    by_id = {node.operation.operation_id: node for node in nodes}
    forward_layers = _topological_layers(pending, by_id)
    return tuple(reversed(forward_layers))


def _topological_layers(
    pending: dict[OperationId, set[OperationId]],
    by_id: dict[OperationId, _Node],
) -> tuple[tuple[_Node, ...], ...]:
    layers: list[tuple[_Node, ...]] = []
    while pending:
        ready = _ready_operations(pending)
        _require_ready(ready)
        layers.append(tuple(sorted((by_id[key] for key in ready), key=_node_key)))
        _remove_ready(pending, ready)
    return tuple(layers)


def _ready_operations(
    pending: dict[OperationId, set[OperationId]],
) -> tuple[OperationId, ...]:
    values = (key for key, dependencies in pending.items() if not dependencies)
    return tuple(sorted(values, key=str))


def _require_ready(ready: tuple[object, ...]) -> None:
    if not ready:
        raise DependencyCycle("compensation dependency graph contains a replay cycle")


def _remove_ready(
    pending: dict[OperationId, set[OperationId]], ready: tuple[OperationId, ...]
) -> None:
    for operation_id in ready:
        del pending[operation_id]
    for dependencies in pending.values():
        dependencies.difference_update(ready)


def _unknown_ids(snapshot: SagaSnapshot) -> tuple[OperationId, ...]:
    return tuple(
        sorted(
            operation.operation_id
            for operation in snapshot.operations.values()
            if operation.status is OperationStatus.OUTCOME_UNKNOWN
        )
    )


def _inflight_ids(snapshot: SagaSnapshot) -> tuple[OperationId, ...]:
    return tuple(
        sorted(
            operation.operation_id
            for operation in snapshot.operations.values()
            if operation.status in _INFLIGHT
        )
    )


def _partial_ids(snapshot: SagaSnapshot) -> tuple[OperationId, ...]:
    return tuple(
        sorted(
            operation.operation_id
            for operation in snapshot.operations.values()
            if operation.direction is Direction.COMPENSATION
            and operation.status is OperationStatus.PARTIAL_EFFECT_CONFIRMED
        )
    )


def _runnable(
    layers: tuple[tuple[_Node, ...], ...],
    unknown: tuple[OperationId, ...],
    inflight: tuple[OperationId, ...],
    partial: tuple[OperationId, ...],
) -> tuple[CompensationItem, ...]:
    if _frontier_is_blocked(layers, unknown, inflight, partial):
        return ()
    return _runnable_layer(layers[0])


def _frontier_is_blocked(
    layers: tuple[tuple[_Node, ...], ...],
    unknown: tuple[OperationId, ...],
    inflight: tuple[OperationId, ...],
    partial: tuple[OperationId, ...],
) -> bool:
    return not layers or bool(unknown) or bool(inflight) or bool(partial)


def _runnable_layer(layer: tuple[_Node, ...]) -> tuple[CompensationItem, ...]:
    if _parallel_safe(layer):
        return tuple(node.item for node in layer)
    return (layer[0].item,)


def _parallel_safe(nodes: tuple[_Node, ...]) -> bool:
    return len(nodes) > 1 and all(
        _pair_is_independent(left, right)
        for index, left in enumerate(nodes)
        for right in nodes[index + 1 :]
    )


def _pair_is_independent(left: _Node, right: _Node) -> bool:
    if not _declared_independent(left, right) or not _same_selector(left, right):
        return False
    left_resource = _selected_resource(left)
    right_resource = _selected_resource(right)
    return (
        left_resource is not None and right_resource is not None and left_resource != right_resource
    )


def _declared_independent(left: _Node, right: _Node) -> bool:
    return (
        right.definition.name in left.definition.compensation_independent_with
        and left.definition.name in right.definition.compensation_independent_with
    )


def _same_selector(left: _Node, right: _Node) -> bool:
    selector = left.definition.compensation_resource_selector
    return bool(selector) and selector == right.definition.compensation_resource_selector


def _selected_resource(node: _Node) -> bytes | None:
    selector = node.definition.compensation_resource_selector
    value: object = node.operation.redacted_command
    if not selector:
        return None
    for member in selector:
        if not isinstance(value, Mapping) or member not in value:
            return None
        value = value[member]
    return canonical_json(value)


def _validate_registry(registry: ToolRegistry) -> None:
    definitions = registry.effect_definitions()
    names = frozenset(item.name for item in definitions)
    graph = {item.name: set(item.compensation_dependencies) for item in definitions}
    if any(not dependencies <= names for dependencies in graph.values()):
        raise CompensationBlocked("compensation dependency is not registered")
    _topological_names(graph)


def _topological_names(pending: dict[str, set[str]]) -> None:
    while pending:
        ready = _ready_names(pending)
        _require_ready(ready)
        _remove_names(pending, ready)


def _ready_names(pending: dict[str, set[str]]) -> tuple[str, ...]:
    return tuple(sorted(name for name, dependencies in pending.items() if not dependencies))


def _remove_names(pending: dict[str, set[str]], ready: tuple[str, ...]) -> None:
    for name in ready:
        del pending[name]
    for dependencies in pending.values():
        dependencies.difference_update(ready)


__all__ = [
    "CompensationBlocked",
    "CompensationFrontier",
    "CompensationItem",
    "CompensationPlanner",
    "DependencyCycle",
]
