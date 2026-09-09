from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from pydantic import BaseModel, ConfigDict

from agentic_saga.contracts.clock import Clock, SystemClock
from agentic_saga.contracts.common import Direction, OperationId, Reversibility, SagaId, sha256_json
from agentic_saga.contracts.events import CompensationStarted, HumanRequired, LedgerEvent
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.tools import (
    DependencyCycle,
    EffectToolDefinition,
    ToolRegistry,
    UnknownToolError,
)
from agentic_saga.kernel.compensation import (
    CompensationBlocked,
    CompensationFrontier,
    CompensationPlanner,
)
from agentic_saga.kernel.ports import (
    InjectedStoreFailure,
    KernelStore,
    Lease,
    TransitionBatch,
    UnwindQuiescence,
    UnwindToolEvidence,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import OperationStatus, SagaSnapshot

_POTENTIALLY_LIVE = frozenset(
    {
        OperationStatus.PLANNED,
        OperationStatus.INTENT_DURABLE,
        OperationStatus.DISPATCHED,
        OperationStatus.OUTCOME_UNKNOWN,
    }
)
_PROVEN_EFFECT = frozenset(
    {OperationStatus.EFFECT_CONFIRMED, OperationStatus.PARTIAL_EFFECT_CONFIRMED}
)


class UnwindTrigger(StrEnum):
    """Identify why normal agent-driven orchestration can no longer continue."""

    AGENT_UNAVAILABLE = "agent_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INVALID_PROPOSAL_LIMIT = "invalid_proposal_limit"


class UnwindAction(StrEnum):
    """Describe the deterministic next step selected during emergency unwind."""

    RECONCILE = "reconcile"
    COMPENSATE = "compensate"
    HUMAN_REQUIRED = "human_required"
    ABORT_CLEAN = "abort_clean"


class UnwindDecision(BaseModel):
    """Pure, stable recovery classification derived from public durable evidence."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    trigger: UnwindTrigger
    action: UnwindAction
    reason_codes: tuple[str, ...] = ()
    blocker_operation_ids: tuple[OperationId, ...] = ()
    frontier: CompensationFrontier | None = None


class QuiescenceVerifier:
    """Reads one transactionally stable, lease-bound durable quiescence proof."""

    def __init__(self, store: KernelStore) -> None:
        self._store = store

    def verify(self, saga_id: SagaId, lease: Lease, registry: ToolRegistry) -> UnwindQuiescence:
        proof = self._store.inspect_unwind_quiescence(saga_id, lease)
        blockers = _historical_evidence_blockers(proof.tool_evidence, registry)
        return proof.model_copy(update={"historical_tool_blockers": blockers})


@dataclass(frozen=True)
class _ExecutionContext:
    snapshot: SagaSnapshot
    decision: UnwindDecision
    lease: Lease
    recorded_at: datetime


class EmergencyUnwinder:
    """Selects recovery work without treating a snapshot as a quiescence proof."""

    def __init__(
        self,
        store: KernelStore | None = None,
        *,
        planner: CompensationPlanner | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._planner = planner or CompensationPlanner()
        self._clock = clock or SystemClock()

    def plan(
        self, snapshot: SagaSnapshot, registry: ToolRegistry, trigger: UnwindTrigger
    ) -> UnwindDecision:
        unresolved = _unresolved_forward_ids(snapshot)
        if unresolved:
            return _decision(trigger, UnwindAction.RECONCILE, "forward_work_unresolved", unresolved)
        historical = _historical_registry_blockers(snapshot, registry)
        if historical:
            return _historical_failure(trigger, historical)
        try:
            frontier = self._planner.plan(snapshot, registry)
        except (CompensationBlocked, DependencyCycle):
            return _decision(trigger, UnwindAction.HUMAN_REQUIRED, "compensation_evidence_invalid")
        return _resolved_decision(snapshot, registry, trigger, frontier)

    def execute(
        self,
        saga_id: SagaId,
        registry: ToolRegistry,
        trigger: UnwindTrigger,
        lease: Lease,
    ) -> UnwindDecision:
        store = _required_store(self._store)
        snapshot = store.load_snapshot(saga_id)
        decision = self.plan(snapshot, registry, trigger)
        context = _ExecutionContext(snapshot, decision, lease, _safe_now(self._clock))
        return self._execute_decision(store, registry, context)

    def _execute_decision(
        self,
        store: KernelStore,
        registry: ToolRegistry,
        context: _ExecutionContext,
    ) -> UnwindDecision:
        decision = context.decision
        if decision.action in {UnwindAction.RECONCILE, UnwindAction.ABORT_CLEAN}:
            return decision
        proof = QuiescenceVerifier(store).verify(context.snapshot.saga_id, context.lease, registry)
        decision = _decision_after_proof(decision, proof)
        if decision.action is UnwindAction.COMPENSATE and not proof.safe:
            return _proof_reconciliation(decision, proof)
        _commit_unwind(store, _with_decision(context, decision))
        return decision


def _decision(
    trigger: UnwindTrigger,
    action: UnwindAction,
    reason: str | None = None,
    blockers: tuple[OperationId, ...] = (),
    frontier: CompensationFrontier | None = None,
) -> UnwindDecision:
    reasons = () if reason is None else (reason,)
    return UnwindDecision(
        trigger=trigger,
        action=action,
        reason_codes=reasons,
        blocker_operation_ids=tuple(sorted(blockers)),
        frontier=frontier,
    )


def _resolved_decision(
    snapshot: SagaSnapshot,
    registry: ToolRegistry,
    trigger: UnwindTrigger,
    frontier: CompensationFrontier,
) -> UnwindDecision:
    blocked = _blocked_compensation(snapshot, registry, trigger, frontier)
    if blocked is not None:
        return blocked
    if frontier.ordered:
        return _decision(trigger, UnwindAction.COMPENSATE, frontier=frontier)
    return _no_frontier_decision(snapshot, trigger)


def _blocked_compensation(
    snapshot: SagaSnapshot,
    registry: ToolRegistry,
    trigger: UnwindTrigger,
    frontier: CompensationFrontier,
) -> UnwindDecision | None:
    irreversible = _irreversible_ids(snapshot, registry)
    if irreversible:
        return _decision(trigger, UnwindAction.HUMAN_REQUIRED, "irreversible_effect", irreversible)
    ambiguous = _ambiguous_compensation(trigger, frontier)
    if ambiguous is not None:
        return ambiguous
    return _stalled_compensation(snapshot, trigger, frontier)


def _stalled_compensation(
    snapshot: SagaSnapshot, trigger: UnwindTrigger, frontier: CompensationFrontier
) -> UnwindDecision | None:
    if snapshot.status is not SagaStatus.COMPENSATING or not frontier.ordered:
        return None
    blockers = tuple(item.forward_operation_id for item in frontier.ordered)
    return _decision(
        trigger, UnwindAction.HUMAN_REQUIRED, "compensation_progress_stalled", blockers
    )


def _ambiguous_compensation(
    trigger: UnwindTrigger, frontier: CompensationFrontier
) -> UnwindDecision | None:
    if not frontier.blocked_by_partial:
        return None
    return _decision(
        trigger,
        UnwindAction.HUMAN_REQUIRED,
        "compensation_outcome_ambiguous",
        frontier.blocked_by_partial,
    )


def _no_frontier_decision(snapshot: SagaSnapshot, trigger: UnwindTrigger) -> UnwindDecision:
    proven = _proven_effect_ids(snapshot)
    if proven:
        return _decision(trigger, UnwindAction.HUMAN_REQUIRED, "effect_not_repairable", proven)
    return _decision(trigger, UnwindAction.ABORT_CLEAN)


def _unresolved_forward_ids(snapshot: SagaSnapshot) -> tuple[OperationId, ...]:
    values = (
        operation.operation_id
        for operation in snapshot.operations.values()
        if operation.direction is Direction.FORWARD and operation.status in _POTENTIALLY_LIVE
    )
    return tuple(sorted(values))


def _proven_effect_ids(snapshot: SagaSnapshot) -> tuple[OperationId, ...]:
    values = (
        operation.operation_id
        for operation in snapshot.operations.values()
        if operation.direction is Direction.FORWARD and operation.status in _PROVEN_EFFECT
    )
    return tuple(sorted(values))


def _irreversible_ids(snapshot: SagaSnapshot, registry: ToolRegistry) -> tuple[OperationId, ...]:
    values = (
        operation.operation_id
        for operation in snapshot.operations.values()
        if operation.operation_id in _proven_effect_ids(snapshot)
        and _definition_is_irreversible(registry, operation.tool_name)
    )
    return tuple(sorted(values))


def _definition_is_irreversible(registry: ToolRegistry, tool_name: str) -> bool:
    definition = registry.definition(tool_name)
    if not isinstance(definition, EffectToolDefinition):
        return True
    return definition.capabilities.reversibility is Reversibility.IRREVERSIBLE


def _historical_registry_blockers(
    snapshot: SagaSnapshot, registry: ToolRegistry
) -> tuple[OperationId, ...]:
    values = (
        operation.operation_id
        for operation in snapshot.operations.values()
        if operation.status in _PROVEN_EFFECT
        and not _registry_has_effect(registry, operation.tool_name)
    )
    return tuple(sorted(values))


def _registry_has_effect(registry: ToolRegistry, tool_name: str) -> bool:
    try:
        return isinstance(registry.definition(tool_name), EffectToolDefinition)
    except (UnknownToolError, TypeError, ValueError):
        return False


def _historical_evidence_blockers(
    evidence: tuple[UnwindToolEvidence, ...], registry: ToolRegistry
) -> tuple[OperationId, ...]:
    values = (item.operation_id for item in evidence if not _evidence_matches(item, registry))
    return tuple(sorted(values))


def _evidence_matches(evidence: UnwindToolEvidence, registry: ToolRegistry) -> bool:
    try:
        definition = registry.definition(evidence.tool_name)
    except (UnknownToolError, TypeError, ValueError):
        return False
    if not isinstance(definition, EffectToolDefinition):
        return False
    versions = (definition.definition_version, definition.command_schema_version)
    durable = (evidence.definition_version, evidence.command_schema_version)
    capability = sha256_json(definition.capabilities.model_dump(mode="json"))
    return versions == durable and capability == evidence.capability_digest


def _historical_failure(
    trigger: UnwindTrigger, blockers: tuple[OperationId, ...]
) -> UnwindDecision:
    return _decision(
        trigger,
        UnwindAction.HUMAN_REQUIRED,
        "historical_tool_evidence_unavailable",
        blockers,
    )


def _decision_after_proof(decision: UnwindDecision, proof: UnwindQuiescence) -> UnwindDecision:
    if proof.historical_tool_blockers:
        return _historical_failure(decision.trigger, proof.historical_tool_blockers)
    return decision


def _with_decision(context: _ExecutionContext, decision: UnwindDecision) -> _ExecutionContext:
    return _ExecutionContext(context.snapshot, decision, context.lease, context.recorded_at)


def _required_store(store: KernelStore | None) -> KernelStore:
    if store is None:
        raise RuntimeError("unwind execution requires durable storage")
    return store


def _proof_reconciliation(decision: UnwindDecision, proof: UnwindQuiescence) -> UnwindDecision:
    blockers = set(proof.forward_outbox_blockers)
    blockers.update(proof.unresolved_forward_blockers)
    blockers.update(proof.reconciliation_blockers)
    return _decision(
        decision.trigger,
        UnwindAction.RECONCILE,
        "durable_forward_work_unresolved",
        tuple(sorted(blockers)),
    )


def _commit_unwind(store: KernelStore, context: _ExecutionContext) -> None:
    event = _unwind_event(context)
    batch = _unwind_batch(context, event)
    digest = sha256_json(context.decision.model_dump(mode="json"))
    try:
        _write_unwind(store, event, batch, digest)
    except InjectedStoreFailure:
        if store.lookup_transition_receipt(batch.transition_id) is None:
            raise
        _write_unwind(store, event, batch, digest)


def _write_unwind(
    store: KernelStore, event: LedgerEvent, batch: TransitionBatch, digest: str
) -> None:
    if isinstance(event, HumanRequired):
        store.suspend_for_human(batch, digest)
    else:
        store.commit_transition(batch)


def _unwind_event(context: _ExecutionContext) -> LedgerEvent:
    fields = _unwind_event_base(context)
    if context.decision.action is UnwindAction.COMPENSATE:
        return CompensationStarted.model_validate(fields)
    reason = context.decision.reason_codes[0] if context.decision.reason_codes else "unsafe_state"
    return HumanRequired.model_validate(fields | {"reason_code": f"emergency_unwind_{reason}"})


def _unwind_event_base(context: _ExecutionContext) -> dict[str, object]:
    transition_id = _unwind_id("txn", context)
    return {
        "event_id": _unwind_id("evt", context),
        "saga_id": context.snapshot.saga_id,
        "saga_seq": context.snapshot.seq + 1,
        "definition_version": context.snapshot.definition_version,
        "fence_token": context.lease.fence_token,
        "actor": "emergency-unwinder",
        "trace_id": _stable_id("trace", transition_id),
        "recorded_at": context.recorded_at,
    }


def _unwind_batch(context: _ExecutionContext, event: LedgerEvent) -> TransitionBatch:
    return TransitionBatch(
        transition_id=_unwind_id("txn", context),
        saga_id=context.snapshot.saga_id,
        expected_seq=context.snapshot.seq,
        expected_fence_token=context.lease.fence_token,
        lease_owner=context.lease.owner,
        events=(event,),
        projection=reduce_event(context.snapshot, event),
    )


def _unwind_id(prefix: str, context: _ExecutionContext) -> str:
    material = (
        f"{context.snapshot.saga_id}:{context.snapshot.seq}:"
        f"{context.decision.trigger.value}:{context.decision.action.value}"
    )
    return _stable_id(prefix, material)


def _stable_id(prefix: str, material: str) -> str:
    digest = sha256(b"agentic-saga-unwind-v1\0" + material.encode()).hexdigest()
    return f"{prefix}_{digest}"


def _safe_now(clock: Clock) -> datetime:
    value = clock.now()
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("unwind clock must return UTC")
    return value


__all__ = [
    "EmergencyUnwinder",
    "QuiescenceVerifier",
    "UnwindAction",
    "UnwindDecision",
    "UnwindTrigger",
]
