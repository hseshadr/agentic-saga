from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel

from agentic_saga.contracts.common import Direction, JsonObject, OperationId, sha256_json
from agentic_saga.contracts.events import (
    AgentTurnFailed,
    AgentTurnReserved,
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
    ProposalRejected,
    ReadObserved,
    ReadStarted,
    ReadUnavailable,
    ReconciliationRecorded,
    RecoveryPlanAccepted,
    RecoveryPlanRejected,
    RecoveryPlanRequired,
    SagaCreated,
    SagaStarted,
    TerminalAssigned,
    TerminalDenied,
)
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    NoEffectConfirmed,
    OutcomeUnknown,
    PartialEffectConfirmed,
    ReconcileEffectConfirmed,
    ReconcilePending,
    effect_outcome_is_safe,
    safe_outcome_json,
)
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.kernel.state import (
    CompensationObligation,
    ObligationStatus,
    OperationRecord,
    OperationStatus,
    SagaSnapshot,
)


class InvalidTransition(ValueError):
    """Raised when an event is not legal for the current projection."""


class SequenceGap(InvalidTransition):
    """Raised when a ledger event is missing, duplicated, or reordered."""


_TERMINAL_STATUSES = frozenset(
    {
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    }
)
_OUTCOME_SOURCES = frozenset({OperationStatus.DISPATCHED, OperationStatus.OUTCOME_UNKNOWN})
_DISPATCH_SOURCES = frozenset({OperationStatus.INTENT_DURABLE, OperationStatus.NO_EFFECT_CONFIRMED})
_MUTATION_BLOCKERS = frozenset({OperationStatus.DISPATCHED, OperationStatus.OUTCOME_UNKNOWN})
_RESUMABLE_STATUSES = frozenset(
    {OperationStatus.INTENT_DURABLE, OperationStatus.NO_EFFECT_CONFIRMED}
)
_TERMINAL_PENDING_STATUSES = frozenset({OperationStatus.INTENT_DURABLE, OperationStatus.DISPATCHED})
_HUMAN_REQUIRED_SOURCES = frozenset(
    {
        SagaStatus.RUNNING,
        SagaStatus.RECONCILING_UNKNOWN,
        SagaStatus.RECOVERY_PLAN_REQUIRED,
        SagaStatus.COMPENSATING,
    }
)
_COMPENSATION_START_SOURCES = frozenset(
    {SagaStatus.RUNNING, SagaStatus.RECOVERY_PLAN_REQUIRED, SagaStatus.HUMAN_REQUIRED}
)
_TERMINAL_TRANSITIONS: Mapping[SagaStatus, frozenset[SagaStatus]] = {
    SagaStatus.RUNNING: frozenset({SagaStatus.SUCCEEDED_VERIFIED, SagaStatus.ABORTED_CLEAN}),
    SagaStatus.COMPENSATING: frozenset({SagaStatus.COMPENSATED_VERIFIED}),
    SagaStatus.HUMAN_REQUIRED: frozenset({SagaStatus.RESOLVED_WITH_EXCEPTION}),
}


@dataclass(frozen=True)
class _OutcomeProjection:
    status: OperationStatus
    receipts: tuple[JsonObject, ...] = ()
    correlation: str | None = None


@dataclass(frozen=True)
class _SnapshotChanges:
    status: SagaStatus | None = None
    operations: Mapping[OperationId, OperationRecord] | None = None
    obligations: Mapping[OperationId, CompensationObligation] | None = None
    last_invariant_seq: int | None = None
    last_invariant_passed: bool | None = None
    last_invariant_target: SagaStatus | None = None
    last_invariant_version: str | None = None
    last_invariant_evidence_digest: str | None = None
    pending_approval: bool | None = None
    resume_status: SagaStatus | None = None
    consumed_approval_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _SnapshotContext:
    prior: SagaSnapshot
    event: LedgerEvent
    changes: _SnapshotChanges


@dataclass(frozen=True)
class _InvariantProjection:
    seq: int | None
    passed: bool | None
    target: SagaStatus | None
    version: str | None
    digest: str | None


_NO_CHANGES = _SnapshotChanges()


def _confirmed_projection(outcome: EffectOutcome) -> _OutcomeProjection:
    confirmed = cast(EffectConfirmed, outcome)
    return _OutcomeProjection(OperationStatus.EFFECT_CONFIRMED, (confirmed.receipt,))


def _no_effect_projection(outcome: EffectOutcome) -> _OutcomeProjection:
    cast(NoEffectConfirmed, outcome)
    return _OutcomeProjection(OperationStatus.NO_EFFECT_CONFIRMED)


def _partial_projection(outcome: EffectOutcome) -> _OutcomeProjection:
    partial = cast(PartialEffectConfirmed, outcome)
    return _OutcomeProjection(OperationStatus.PARTIAL_EFFECT_CONFIRMED, partial.receipts)


def _unknown_projection(outcome: EffectOutcome) -> _OutcomeProjection:
    unknown = cast(OutcomeUnknown, outcome)
    return _OutcomeProjection(OperationStatus.OUTCOME_UNKNOWN, correlation=unknown.correlation)


type _OutcomeHandler = Callable[[EffectOutcome], _OutcomeProjection]
_OUTCOME_HANDLERS: Mapping[type[BaseModel], _OutcomeHandler] = {
    EffectConfirmed: _confirmed_projection,
    NoEffectConfirmed: _no_effect_projection,
    PartialEffectConfirmed: _partial_projection,
    OutcomeUnknown: _unknown_projection,
}


def _project_outcome(outcome: EffectOutcome) -> _OutcomeProjection:
    handler = _OUTCOME_HANDLERS.get(type(outcome))
    if handler is None:
        raise InvalidTransition(f"unknown effect outcome: {type(outcome).__name__}")
    return handler(outcome)


def _current_or[T](current: T, changed: T | None) -> T:
    return current if changed is None else changed


def _next_snapshot(
    snapshot: SagaSnapshot,
    event: LedgerEvent,
    changes: _SnapshotChanges = _NO_CHANGES,
) -> SagaSnapshot:
    return _materialize_snapshot(_SnapshotContext(snapshot, event, changes))


def _materialize_snapshot(context: _SnapshotContext) -> SagaSnapshot:
    snapshot = _materialize_core(context)
    proof = _project_invariant_markers(context.prior, context.changes)
    return _with_invariant_markers(snapshot, proof)


def _materialize_core(context: _SnapshotContext) -> SagaSnapshot:
    prior, delta = context.prior, context.changes
    return SagaSnapshot(
        saga_id=prior.saga_id,
        seq=context.event.saga_seq,
        status=_current_or(prior.status, delta.status),
        definition_version=prior.definition_version,
        operations=_current_or(prior.operations, delta.operations),
        obligations=_current_or(prior.obligations, delta.obligations),
        pending_approval=_current_or(prior.pending_approval, delta.pending_approval),
        resume_status=_current_or(prior.resume_status, delta.resume_status),
        consumed_approval_ids=_current_or(prior.consumed_approval_ids, delta.consumed_approval_ids),
    )


def _with_invariant_markers(snapshot: SagaSnapshot, proof: _InvariantProjection) -> SagaSnapshot:
    return snapshot.model_copy(
        update={
            "last_invariant_seq": proof.seq,
            "last_invariant_passed": proof.passed,
            "last_invariant_target": proof.target,
            "last_invariant_version": proof.version,
            "last_invariant_evidence_digest": proof.digest,
        }
    )


def _project_invariant_markers(
    prior: SagaSnapshot, delta: _SnapshotChanges
) -> _InvariantProjection:
    return _InvariantProjection(
        seq=_current_or(prior.last_invariant_seq, delta.last_invariant_seq),
        passed=_current_or(prior.last_invariant_passed, delta.last_invariant_passed),
        target=_current_or(prior.last_invariant_target, delta.last_invariant_target),
        version=_current_or(prior.last_invariant_version, delta.last_invariant_version),
        digest=_current_or(
            prior.last_invariant_evidence_digest, delta.last_invariant_evidence_digest
        ),
    )


def _require_status(snapshot: SagaSnapshot, allowed: frozenset[SagaStatus], action: str) -> None:
    if snapshot.status not in allowed:
        raise InvalidTransition(f"{action} is not legal from {snapshot.status.value}")


def _require_operation(snapshot: SagaSnapshot, operation_id: OperationId) -> OperationRecord:
    operation = snapshot.operations.get(operation_id)
    if operation is None:
        raise InvalidTransition("operation has no durable intent")
    return operation


def _operation_metadata(record: OperationRecord) -> tuple[object, ...]:
    return (
        record.step_instance_id,
        record.direction,
        record.semantic_generation,
        record.tool_name,
        record.command_hash,
    )


type _OperationEvent = (
    DispatchStarted | DispatchAbortedBeforeEntry | EffectOutcomeRecorded | ReconciliationRecorded
)


def _event_metadata(event: _OperationEvent) -> tuple[object, ...]:
    return (
        event.step_instance_id,
        event.direction,
        event.semantic_generation,
        event.tool_name,
        event.command_hash,
    )


def _require_stable_metadata(record: OperationRecord, event: _OperationEvent) -> None:
    if _operation_metadata(record) != _event_metadata(event):
        raise InvalidTransition("operation metadata changed after durable intent")


def _new_operation(
    event: EffectIntentRecorded | CompensationIntentRecorded,
    compensates_operation_id: OperationId | None = None,
) -> OperationRecord:
    operation = _base_operation(event)
    return operation.model_copy(update={"compensates_operation_id": compensates_operation_id})


def _base_operation(
    event: EffectIntentRecorded | CompensationIntentRecorded,
) -> OperationRecord:
    return OperationRecord(
        operation_id=event.operation_id,
        step_instance_id=event.step_instance_id,
        direction=event.direction,
        semantic_generation=event.semantic_generation,
        delivery_attempt=event.delivery_attempt,
        tool_name=event.tool_name,
        status=OperationStatus.INTENT_DURABLE,
        redacted_command=event.redacted_command,
        command_hash=event.command_hash,
    )


def _new_obligation(event: EffectIntentRecorded) -> CompensationObligation | None:
    if event.compensate_with is None:
        return None
    return CompensationObligation(
        forward_operation_id=event.operation_id,
        compensation_tool_name=event.compensate_with,
        status=ObligationStatus.ARMED,
    )


def _require_new_operation(snapshot: SagaSnapshot, operation_id: OperationId) -> None:
    if operation_id in snapshot.operations:
        raise InvalidTransition("operation intent already exists")


def _handle_duplicate_create(snapshot: SagaSnapshot, event: LedgerEvent) -> SagaSnapshot:
    del snapshot, event
    raise InvalidTransition("SagaCreated is legal only as the first event")


def _handle_saga_started(snapshot: SagaSnapshot, event: LedgerEvent) -> SagaSnapshot:
    _require_status(snapshot, frozenset({SagaStatus.CREATED}), "SagaStarted")
    return _next_snapshot(snapshot, event, _SnapshotChanges(status=SagaStatus.RUNNING))


def _handle_effect_intent(snapshot: SagaSnapshot, raw_event: LedgerEvent) -> SagaSnapshot:
    event = cast(EffectIntentRecorded, raw_event)
    _require_effect_intent_state(snapshot)
    _require_mutation_quiescence(snapshot)
    if event.direction is not Direction.FORWARD:
        raise InvalidTransition("effect intent requires forward direction")
    _require_new_operation(snapshot, event.operation_id)
    operations = dict(snapshot.operations) | {event.operation_id: _new_operation(event)}
    obligations = _add_obligation(snapshot, event)
    status = SagaStatus.RUNNING if snapshot.status is SagaStatus.HUMAN_REQUIRED else None
    changes = _SnapshotChanges(status=status, operations=operations, obligations=obligations)
    return _next_snapshot(snapshot, event, changes)


def _require_effect_intent_state(snapshot: SagaSnapshot) -> None:
    allowed = frozenset({SagaStatus.RUNNING, SagaStatus.HUMAN_REQUIRED})
    _require_status(snapshot, allowed, "effect intent")
    if snapshot.status is SagaStatus.HUMAN_REQUIRED and snapshot.pending_approval:
        raise InvalidTransition("effect intent requires verified human resolution")


def _add_obligation(
    snapshot: SagaSnapshot, event: EffectIntentRecorded
) -> Mapping[OperationId, CompensationObligation]:
    obligations = dict(snapshot.obligations)
    obligation = _new_obligation(event)
    if obligation is not None:
        obligations[event.operation_id] = obligation
    return obligations


def _required_dispatch_attempt(record: OperationRecord) -> int:
    if record.status is OperationStatus.NO_EFFECT_CONFIRMED or record.reconciliation_retry:
        return record.delivery_attempt + 1
    return record.delivery_attempt


def _handle_dispatch_started(snapshot: SagaSnapshot, raw_event: LedgerEvent) -> SagaSnapshot:
    event = cast(DispatchStarted, raw_event)
    operation = _require_operation(snapshot, event.operation_id)
    _validate_dispatch_started(snapshot, operation, event)
    updated = operation.model_copy(
        update={
            "status": OperationStatus.DISPATCHED,
            "delivery_attempt": event.delivery_attempt,
            "reconciliation_retry": False,
        }
    )
    return _replace_operation(snapshot, event, updated)


def _validate_dispatch_started(
    snapshot: SagaSnapshot, operation: OperationRecord, event: DispatchStarted
) -> None:
    _require_dispatch_state(snapshot)
    _require_mutation_quiescence(snapshot)
    _require_stable_metadata(operation, event)
    _require_dispatch_direction(snapshot, event)
    if operation.status not in _DISPATCH_SOURCES:
        raise InvalidTransition("dispatch requires durable intent")
    if event.delivery_attempt != _required_dispatch_attempt(operation):
        raise InvalidTransition("dispatch delivery attempt is not monotonic")


def _require_dispatch_state(snapshot: SagaSnapshot) -> None:
    allowed = frozenset({SagaStatus.RUNNING, SagaStatus.COMPENSATING})
    if snapshot.status not in allowed:
        raise InvalidTransition(f"{snapshot.status.value} is quiescent; dispatch is forbidden")


def _require_dispatch_direction(snapshot: SagaSnapshot, event: DispatchStarted) -> None:
    expected = {
        SagaStatus.RUNNING: Direction.FORWARD,
        SagaStatus.COMPENSATING: Direction.COMPENSATION,
    }[snapshot.status]
    if event.direction is not expected:
        raise InvalidTransition("dispatch direction does not match the active Saga phase")


def _require_mutation_quiescence(snapshot: SagaSnapshot) -> None:
    if snapshot.pending_approval:
        raise InvalidTransition("pending human approval is quiescent; mutation is forbidden")
    if any(item.status in _MUTATION_BLOCKERS for item in snapshot.operations.values()):
        raise InvalidTransition("in-flight or unknown operations require quiescent reconciliation")


def _replace_operation(
    snapshot: SagaSnapshot, event: LedgerEvent, operation: OperationRecord
) -> SagaSnapshot:
    operations = dict(snapshot.operations)
    operations[operation.operation_id] = operation
    return _next_snapshot(snapshot, event, _SnapshotChanges(operations=operations))


def _handle_effect_outcome(snapshot: SagaSnapshot, raw_event: LedgerEvent) -> SagaSnapshot:
    event = cast(EffectOutcomeRecorded, raw_event)
    operation = _require_operation(snapshot, event.operation_id)
    if operation.status is OperationStatus.OUTCOME_UNKNOWN:
        raise InvalidTransition("unknown outcomes require dedicated reconciliation evidence")
    _require_safe_outcome(event)
    _require_outcome_source(operation, event)
    projection = _project_outcome(event.outcome)
    updated = _operation_with_outcome(operation, event, projection)
    operations = dict(snapshot.operations) | {event.operation_id: updated}
    changes = _outcome_changes(snapshot, operations, updated)
    return _next_snapshot(snapshot, event, changes)


def _reconciled_operation(
    operation: OperationRecord, event: ReconciliationRecorded
) -> OperationRecord:
    if event.action == "confirm":
        return _confirmed_reconciliation(operation, event)
    if event.action == "retry_same_id":
        return _retry_reconciliation(operation, event)
    if event.action == "wait":
        pending = cast(ReconcilePending, event.outcome)
        return operation.model_copy(update={"correlation": pending.correlation})
    return operation


def _confirmed_reconciliation(
    operation: OperationRecord, event: ReconciliationRecorded
) -> OperationRecord:
    outcome = cast(ReconcileEffectConfirmed, event.outcome)
    return operation.model_copy(
        update={
            "status": OperationStatus.EFFECT_CONFIRMED,
            "redacted_result": event.redacted_result,
            "result_hash": event.result_hash,
            "receipts": (outcome.receipt,),
            "correlation": None,
        }
    )


def _retry_reconciliation(
    operation: OperationRecord, event: ReconciliationRecorded
) -> OperationRecord:
    return operation.model_copy(
        update={
            "status": OperationStatus.INTENT_DURABLE,
            "redacted_result": event.redacted_result,
            "result_hash": event.result_hash,
            "correlation": None,
            "reconciliation_retry": True,
        }
    )


def _handle_reconciliation(snapshot: SagaSnapshot, raw_event: LedgerEvent) -> SagaSnapshot:
    event = cast(ReconciliationRecorded, raw_event)
    operation = _require_operation(snapshot, event.operation_id)
    _require_stable_metadata(operation, event)
    if operation.status is not OperationStatus.OUTCOME_UNKNOWN:
        raise InvalidTransition("reconciliation requires an unknown operation")
    updated = _reconciled_operation(operation, event)
    operations = dict(snapshot.operations) | {event.operation_id: updated}
    changes = _reconciliation_changes(snapshot, event, operations, updated)
    return _next_snapshot(snapshot, event, changes)


def _reconciliation_changes(
    snapshot: SagaSnapshot,
    event: ReconciliationRecorded,
    operations: Mapping[OperationId, OperationRecord],
    updated: OperationRecord,
) -> _SnapshotChanges:
    if event.action in {"wait", "human"}:
        return _SnapshotChanges(operations=operations)
    if event.action == "retry_same_id":
        status = _reconciliation_resume_status(snapshot, operations, updated)
        return _SnapshotChanges(status=status, operations=operations)
    return _outcome_changes(snapshot, operations, updated)


def _reconciliation_resume_status(
    snapshot: SagaSnapshot,
    operations: Mapping[OperationId, OperationRecord],
    operation: OperationRecord,
) -> SagaStatus:
    if _has_operation_status(operations, OperationStatus.OUTCOME_UNKNOWN):
        return SagaStatus.RECONCILING_UNKNOWN
    return snapshot.resume_status or _resumed_status(operation)


def _require_safe_outcome(event: EffectOutcomeRecorded) -> None:
    representation = safe_outcome_json(event.outcome)
    if not effect_outcome_is_safe(event.outcome) or event.redacted_result != representation:
        raise InvalidTransition("effect outcome is not the canonical safe representation")
    if event.result_hash != sha256_json(representation):
        raise InvalidTransition("effect outcome hash does not match canonical safe bytes")


def _handle_dispatch_aborted(snapshot: SagaSnapshot, raw_event: LedgerEvent) -> SagaSnapshot:
    event = cast(DispatchAbortedBeforeEntry, raw_event)
    operation = _require_operation(snapshot, event.operation_id)
    _require_stable_metadata(operation, event)
    if operation.status is not OperationStatus.DISPATCHED:
        raise InvalidTransition("pre-entry abort requires prior dispatch")
    if event.delivery_attempt != operation.delivery_attempt:
        raise InvalidTransition("abort delivery attempt does not match dispatch")
    updated = operation.model_copy(update={"status": OperationStatus.INTENT_DURABLE})
    return _replace_operation(snapshot, event, updated)


def _outcome_changes(
    snapshot: SagaSnapshot,
    operations: Mapping[OperationId, OperationRecord],
    updated: OperationRecord,
) -> _SnapshotChanges:
    obligations = _project_obligations(snapshot, updated)
    status, resume_status = _state_after_outcome(snapshot, operations, updated)
    return _SnapshotChanges(
        status=status,
        operations=operations,
        obligations=obligations,
        resume_status=resume_status,
    )


def _require_outcome_source(operation: OperationRecord, event: EffectOutcomeRecorded) -> None:
    _require_stable_metadata(operation, event)
    if operation.status not in _OUTCOME_SOURCES:
        raise InvalidTransition("effect outcome requires prior dispatch")
    if event.delivery_attempt != operation.delivery_attempt:
        raise InvalidTransition("outcome delivery attempt does not match dispatch")


def _operation_with_outcome(
    operation: OperationRecord,
    event: EffectOutcomeRecorded,
    projection: _OutcomeProjection,
) -> OperationRecord:
    return operation.model_copy(
        update={
            "status": projection.status,
            "redacted_result": event.redacted_result,
            "result_hash": event.result_hash,
            "receipts": projection.receipts,
            "correlation": projection.correlation,
        }
    )


_FORWARD_OBLIGATION_STATUS: Mapping[OperationStatus, ObligationStatus] = {
    OperationStatus.EFFECT_CONFIRMED: ObligationStatus.ELIGIBLE,
    OperationStatus.PARTIAL_EFFECT_CONFIRMED: ObligationStatus.ELIGIBLE,
    OperationStatus.NO_EFFECT_CONFIRMED: ObligationStatus.NOT_REQUIRED,
    OperationStatus.OUTCOME_UNKNOWN: ObligationStatus.ARMED,
}
_COMPENSATION_OBLIGATION_STATUS: Mapping[OperationStatus, ObligationStatus] = {
    OperationStatus.EFFECT_CONFIRMED: ObligationStatus.SATISFIED,
    OperationStatus.PARTIAL_EFFECT_CONFIRMED: ObligationStatus.IN_PROGRESS,
    OperationStatus.NO_EFFECT_CONFIRMED: ObligationStatus.ELIGIBLE,
    OperationStatus.OUTCOME_UNKNOWN: ObligationStatus.IN_PROGRESS,
}


def _project_obligations(
    snapshot: SagaSnapshot, operation: OperationRecord
) -> Mapping[OperationId, CompensationObligation]:
    if operation.direction is Direction.FORWARD:
        return _project_forward_obligation(snapshot, operation)
    return _project_compensation_obligation(snapshot, operation)


def _project_forward_obligation(
    snapshot: SagaSnapshot, operation: OperationRecord
) -> Mapping[OperationId, CompensationObligation]:
    obligations = dict(snapshot.obligations)
    obligation = obligations.get(operation.operation_id)
    if obligation is None:
        return obligations
    status = _FORWARD_OBLIGATION_STATUS[operation.status]
    obligations[operation.operation_id] = obligation.model_copy(
        update={"status": status, "receipts": operation.receipts}
    )
    return obligations


def _project_compensation_obligation(
    snapshot: SagaSnapshot, operation: OperationRecord
) -> Mapping[OperationId, CompensationObligation]:
    forward_id = operation.compensates_operation_id
    if forward_id is None or forward_id not in snapshot.obligations:
        raise InvalidTransition("compensation operation has no forward obligation")
    obligations = dict(snapshot.obligations)
    obligation = obligations[forward_id]
    status = _COMPENSATION_OBLIGATION_STATUS[operation.status]
    obligations[forward_id] = obligation.model_copy(update={"status": status})
    return obligations


def _state_after_outcome(
    snapshot: SagaSnapshot,
    operations: Mapping[OperationId, OperationRecord],
    operation: OperationRecord,
) -> tuple[SagaStatus, SagaStatus]:
    resume_status = _outcome_resume_status(snapshot, operation)
    if _partial_compensation_requires_human(operation):
        return SagaStatus.HUMAN_REQUIRED, SagaStatus.COMPENSATING
    if snapshot.status is SagaStatus.HUMAN_REQUIRED:
        return _human_state_after_outcome(snapshot, operations, resume_status)
    if _has_operation_status(operations, OperationStatus.OUTCOME_UNKNOWN):
        return SagaStatus.RECONCILING_UNKNOWN, resume_status
    if snapshot.status is SagaStatus.RECONCILING_UNKNOWN:
        return resume_status, resume_status
    return snapshot.status, resume_status


def _partial_compensation_requires_human(operation: OperationRecord) -> bool:
    return (
        operation.direction is Direction.COMPENSATION
        and operation.status is OperationStatus.PARTIAL_EFFECT_CONFIRMED
    )


def _outcome_resume_status(snapshot: SagaSnapshot, operation: OperationRecord) -> SagaStatus:
    if snapshot.status in {SagaStatus.RUNNING, SagaStatus.COMPENSATING}:
        return snapshot.status
    return snapshot.resume_status or _resumed_status(operation)


def _human_state_after_outcome(
    snapshot: SagaSnapshot,
    operations: Mapping[OperationId, OperationRecord],
    resume_status: SagaStatus,
) -> tuple[SagaStatus, SagaStatus]:
    if snapshot.pending_approval or _has_mutation_blocker(operations):
        return SagaStatus.HUMAN_REQUIRED, resume_status
    if _has_resumable_operation(operations):
        return resume_status, resume_status
    return SagaStatus.HUMAN_REQUIRED, resume_status


def _resumed_status(operation: OperationRecord) -> SagaStatus:
    if operation.direction is Direction.COMPENSATION:
        return SagaStatus.COMPENSATING
    return SagaStatus.RUNNING


def _has_operation_status(
    operations: Mapping[OperationId, OperationRecord], status: OperationStatus
) -> bool:
    return any(item.status is status for item in operations.values())


def _has_mutation_blocker(operations: Mapping[OperationId, OperationRecord]) -> bool:
    return any(item.status in _MUTATION_BLOCKERS for item in operations.values())


def _has_resumable_operation(operations: Mapping[OperationId, OperationRecord]) -> bool:
    return any(item.status in _RESUMABLE_STATUSES for item in operations.values())


def _has_terminal_pending_operation(
    operations: Mapping[OperationId, OperationRecord],
) -> bool:
    return any(item.status in _TERMINAL_PENDING_STATUSES for item in operations.values())


def _handle_recovery_required(snapshot: SagaSnapshot, event: LedgerEvent) -> SagaSnapshot:
    _require_status(snapshot, frozenset({SagaStatus.RUNNING}), "recovery plan request")
    changes = _SnapshotChanges(status=SagaStatus.RECOVERY_PLAN_REQUIRED)
    return _next_snapshot(snapshot, event, changes)


def _handle_recovery_accepted(snapshot: SagaSnapshot, event: LedgerEvent) -> SagaSnapshot:
    _require_status(
        snapshot, frozenset({SagaStatus.RECOVERY_PLAN_REQUIRED}), "recovery plan acceptance"
    )
    return _next_snapshot(snapshot, event, _SnapshotChanges(status=SagaStatus.RUNNING))


def _handle_recovery_rejected(snapshot: SagaSnapshot, event: LedgerEvent) -> SagaSnapshot:
    allowed = frozenset({SagaStatus.RUNNING, SagaStatus.RECOVERY_PLAN_REQUIRED})
    _require_status(snapshot, allowed, "recovery plan rejection")
    changes = _SnapshotChanges(status=SagaStatus.RECOVERY_PLAN_REQUIRED)
    return _next_snapshot(snapshot, event, changes)


def _handle_denial(snapshot: SagaSnapshot, event: LedgerEvent) -> SagaSnapshot:
    return _next_snapshot(snapshot, event)


def _handle_approval_consumed(snapshot: SagaSnapshot, raw_event: LedgerEvent) -> SagaSnapshot:
    event = cast(ApprovalConsumed, raw_event)
    if event.decision_id in snapshot.consumed_approval_ids:
        raise InvalidTransition("approval decision was already consumed")
    consumed = (*snapshot.consumed_approval_ids, event.decision_id)
    changes = _SnapshotChanges(consumed_approval_ids=consumed)
    return _next_snapshot(snapshot, event, changes)


def _eligible_obligations(snapshot: SagaSnapshot) -> tuple[CompensationObligation, ...]:
    return tuple(
        item for item in snapshot.obligations.values() if item.status is ObligationStatus.ELIGIBLE
    )


def _handle_compensation_started(snapshot: SagaSnapshot, event: LedgerEvent) -> SagaSnapshot:
    if snapshot.status is SagaStatus.HUMAN_REQUIRED and snapshot.pending_approval:
        raise InvalidTransition("compensation requires verified human resolution")
    _require_mutation_quiescence(snapshot)
    _require_status(snapshot, _COMPENSATION_START_SOURCES, "compensation start")
    _require_no_forward_intent(snapshot)
    if not _eligible_obligations(snapshot):
        raise InvalidTransition("compensation requires an eligible obligation")
    changes = _SnapshotChanges(
        status=SagaStatus.COMPENSATING,
        resume_status=SagaStatus.COMPENSATING,
    )
    return _next_snapshot(snapshot, event, changes)


def _require_no_forward_intent(snapshot: SagaSnapshot) -> None:
    queued = any(
        item.direction is Direction.FORWARD and item.status is OperationStatus.INTENT_DURABLE
        for item in snapshot.operations.values()
    )
    if queued:
        raise InvalidTransition("compensation cannot strand a forward durable intent")


def _handle_compensation_intent(snapshot: SagaSnapshot, raw_event: LedgerEvent) -> SagaSnapshot:
    event = cast(CompensationIntentRecorded, raw_event)
    _require_status(snapshot, frozenset({SagaStatus.COMPENSATING}), "compensation intent")
    _require_mutation_quiescence(snapshot)
    _require_new_operation(snapshot, event.operation_id)
    obligation = _require_eligible_obligation(snapshot, event.compensates_operation_id)
    _require_compensation_metadata(event, obligation)
    _require_compensation_generation(snapshot, event, obligation)
    return _record_compensation_intent(snapshot, event, obligation)


def _record_compensation_intent(
    snapshot: SagaSnapshot,
    event: CompensationIntentRecorded,
    obligation: CompensationObligation,
) -> SagaSnapshot:
    operation = _new_operation(event, event.compensates_operation_id)
    operations = dict(snapshot.operations) | {event.operation_id: operation}
    obligations = _arm_compensation(snapshot, event, obligation)
    changes = _SnapshotChanges(operations=operations, obligations=obligations)
    return _next_snapshot(snapshot, event, changes)


def _arm_compensation(
    snapshot: SagaSnapshot,
    event: CompensationIntentRecorded,
    obligation: CompensationObligation,
) -> Mapping[OperationId, CompensationObligation]:
    obligations = dict(snapshot.obligations)
    obligations[event.compensates_operation_id] = obligation.model_copy(
        update={
            "status": ObligationStatus.IN_PROGRESS,
            "compensation_operation_id": event.operation_id,
        }
    )
    return obligations


def _require_compensation_generation(
    snapshot: SagaSnapshot,
    event: CompensationIntentRecorded,
    obligation: CompensationObligation,
) -> None:
    forward = _require_operation(snapshot, obligation.forward_operation_id)
    if event.semantic_generation != forward.semantic_generation:
        raise InvalidTransition("compensation semantic generation must match its forward effect")


def _require_compensation_metadata(
    event: CompensationIntentRecorded, obligation: CompensationObligation
) -> None:
    if event.direction is not Direction.COMPENSATION:
        raise InvalidTransition("compensation intent requires compensation direction")
    if event.tool_name != obligation.compensation_tool_name:
        raise InvalidTransition("compensation tool does not match the armed obligation")
    if event.forward_receipts != obligation.receipts:
        raise InvalidTransition("compensation intent receipts do not match forward evidence")
    if obligation.compensation_operation_id is not None:
        raise InvalidTransition("compensation retry must reuse its existing operation identity")


def _require_eligible_obligation(
    snapshot: SagaSnapshot, operation_id: OperationId
) -> CompensationObligation:
    obligation = snapshot.obligations.get(operation_id)
    if obligation is None or obligation.status is not ObligationStatus.ELIGIBLE:
        raise InvalidTransition("compensation target is not eligible")
    return obligation


def _handle_invariant_evaluated(snapshot: SagaSnapshot, raw_event: LedgerEvent) -> SagaSnapshot:
    event = cast(InvariantEvaluated, raw_event)
    if event.evaluated_at_seq != snapshot.seq:
        raise InvalidTransition("invariant evidence does not evaluate the prior sequence")
    changes = _SnapshotChanges(
        last_invariant_seq=event.saga_seq,
        last_invariant_passed=event.all_passed,
        last_invariant_target=SagaStatus(event.target_status),
        last_invariant_version=event.invariant_version,
        last_invariant_evidence_digest=event.evidence_digest,
    )
    return _next_snapshot(snapshot, event, changes)


def _handle_human_required(snapshot: SagaSnapshot, event: LedgerEvent) -> SagaSnapshot:
    _require_status(
        snapshot,
        _HUMAN_REQUIRED_SOURCES,
        "human escalation from an active Saga phase",
    )
    changes = _SnapshotChanges(
        status=SagaStatus.HUMAN_REQUIRED,
        pending_approval=True,
        resume_status=_resume_status(snapshot),
    )
    return _next_snapshot(snapshot, event, changes)


def _handle_human_resolution(snapshot: SagaSnapshot, raw_event: LedgerEvent) -> SagaSnapshot:
    event = cast(HumanResolutionRecorded, raw_event)
    _require_status(snapshot, frozenset({SagaStatus.HUMAN_REQUIRED}), "human resolution")
    if not event.verification_result:
        return _next_snapshot(snapshot, event)
    if event.decision_id in snapshot.consumed_approval_ids:
        raise InvalidTransition("human decision was already consumed")
    consumed = (*snapshot.consumed_approval_ids, event.decision_id)
    status = _human_resolution_status(snapshot, event.action)
    changes = _SnapshotChanges(
        status=status,
        pending_approval=event.action == "reject",
        consumed_approval_ids=consumed,
    )
    return _next_snapshot(snapshot, event, changes)


def _human_resolution_status(snapshot: SagaSnapshot, action: str) -> SagaStatus:
    if action == "reject":
        return SagaStatus.HUMAN_REQUIRED
    if action == "reconcile" and _unknown_operations(snapshot):
        return SagaStatus.RECONCILING_UNKNOWN
    return _status_after_human_resolution(snapshot)


def _resume_status(snapshot: SagaSnapshot) -> SagaStatus:
    if snapshot.status in {SagaStatus.RUNNING, SagaStatus.COMPENSATING}:
        return snapshot.status
    return snapshot.resume_status or SagaStatus.RUNNING


def _status_after_human_resolution(snapshot: SagaSnapshot) -> SagaStatus:
    if _has_mutation_blocker(snapshot.operations):
        return SagaStatus.HUMAN_REQUIRED
    if _has_resumable_operation(snapshot.operations):
        return snapshot.resume_status or SagaStatus.RUNNING
    return SagaStatus.HUMAN_REQUIRED


def _unknown_operations(snapshot: SagaSnapshot) -> tuple[OperationId, ...]:
    return tuple(
        item.operation_id
        for item in snapshot.operations.values()
        if item.status is OperationStatus.OUTCOME_UNKNOWN
    )


def _handle_terminal_assigned(snapshot: SagaSnapshot, raw_event: LedgerEvent) -> SagaSnapshot:
    event = cast(TerminalAssigned, raw_event)
    target = SagaStatus(event.status)
    _require_terminal_proof(snapshot, target)
    _require_terminal_quiescence(snapshot)
    _require_terminal_transition(snapshot, target)
    _require_resolved_compensation(snapshot, target)
    return _next_snapshot(snapshot, event, _SnapshotChanges(status=target))


def _require_terminal_proof(snapshot: SagaSnapshot, target: SagaStatus) -> None:
    if snapshot.last_invariant_seq != snapshot.seq:
        raise InvalidTransition("terminal assignment requires immediately prior invariant evidence")
    if snapshot.last_invariant_passed is not True:
        raise InvalidTransition("terminal assignment requires passing invariant evidence")
    if snapshot.last_invariant_target is not target:
        raise InvalidTransition("terminal assignment does not match the invariant proof target")


def _require_terminal_quiescence(snapshot: SagaSnapshot) -> None:
    if snapshot.pending_approval:
        raise InvalidTransition("terminal assignment cannot bypass pending human approval")
    if _unknown_operations(snapshot):
        raise InvalidTransition("terminal assignment cannot hide unknown operations")
    _require_no_terminal_pending_operations(snapshot)


def _require_terminal_transition(snapshot: SagaSnapshot, target: SagaStatus) -> None:
    allowed = _TERMINAL_TRANSITIONS.get(snapshot.status, frozenset())
    if target not in allowed:
        raise InvalidTransition("terminal target is not legal from the current Saga phase")


def _require_no_terminal_pending_operations(snapshot: SagaSnapshot) -> None:
    if _has_terminal_pending_operation(snapshot.operations):
        raise InvalidTransition("terminal assignment cannot hide runnable operations")


def _require_resolved_compensation(snapshot: SagaSnapshot, target: SagaStatus) -> None:
    if target is not SagaStatus.COMPENSATED_VERIFIED:
        return
    resolved = {ObligationStatus.SATISFIED, ObligationStatus.NOT_REQUIRED}
    if any(item.status not in resolved for item in snapshot.obligations.values()):
        raise InvalidTransition("terminal assignment has unresolved compensation")


type _EventHandler = Callable[[SagaSnapshot, LedgerEvent], SagaSnapshot]
_HANDLERS: Mapping[type[BaseModel], _EventHandler] = {
    SagaCreated: _handle_duplicate_create,
    SagaStarted: _handle_saga_started,
    AgentTurnReserved: _handle_denial,
    AgentTurnFailed: _handle_denial,
    ReadStarted: _handle_denial,
    ReadObserved: _handle_denial,
    ReadUnavailable: _handle_denial,
    EffectIntentRecorded: _handle_effect_intent,
    DispatchStarted: _handle_dispatch_started,
    DispatchAbortedBeforeEntry: _handle_dispatch_aborted,
    EffectOutcomeRecorded: _handle_effect_outcome,
    ReconciliationRecorded: _handle_reconciliation,
    RecoveryPlanRequired: _handle_recovery_required,
    RecoveryPlanAccepted: _handle_recovery_accepted,
    RecoveryPlanRejected: _handle_recovery_rejected,
    ProposalRejected: _handle_denial,
    ApprovalConsumed: _handle_approval_consumed,
    CompensationStarted: _handle_compensation_started,
    CompensationIntentRecorded: _handle_compensation_intent,
    InvariantEvaluated: _handle_invariant_evaluated,
    HumanRequired: _handle_human_required,
    HumanResolutionRecorded: _handle_human_resolution,
    TerminalAssigned: _handle_terminal_assigned,
    TerminalDenied: _handle_denial,
}


def _create_snapshot(raw_event: LedgerEvent) -> SagaSnapshot:
    if type(raw_event) is not SagaCreated:
        raise InvalidTransition("a Saga requires SagaCreated as its first event")
    event = raw_event
    if event.saga_seq != 1:
        raise SequenceGap("SagaCreated must be at sequence 1")
    return SagaSnapshot(
        saga_id=event.saga_id,
        seq=event.saga_seq,
        status=SagaStatus.CREATED,
        definition_version=event.definition_version,
        operations={},
        obligations={},
    )


def _validate_common_transition(snapshot: SagaSnapshot, event: LedgerEvent) -> None:
    if snapshot.status in _TERMINAL_STATUSES:
        raise InvalidTransition("no transition is legal after terminal state")
    if event.saga_seq != snapshot.seq + 1:
        raise SequenceGap(f"expected {snapshot.seq + 1}, received {event.saga_seq}")
    if event.saga_id != snapshot.saga_id:
        raise InvalidTransition("Saga ID changed during replay")
    if event.definition_version != snapshot.definition_version:
        raise InvalidTransition("definition version changed during replay")


def reduce_event(snapshot: SagaSnapshot | None, event: LedgerEvent) -> SagaSnapshot:
    """Apply one ordered ledger event to the deterministic saga projection."""

    if snapshot is None:
        return _create_snapshot(event)
    handler = _HANDLERS.get(type(event))
    if handler is None:
        raise InvalidTransition(f"unknown event type: {type(event).__name__}")
    _validate_common_transition(snapshot, event)
    return handler(snapshot, event)


def rebuild_projection(events: Iterable[LedgerEvent]) -> SagaSnapshot:
    """Reconstruct a saga projection by reducing its complete ordered ledger."""

    snapshot: SagaSnapshot | None = None
    for event in events:
        snapshot = reduce_event(snapshot, event)
    if snapshot is None:
        raise SequenceGap("a Saga requires SagaCreated at sequence 1")
    return snapshot


__all__ = ["InvalidTransition", "SequenceGap", "rebuild_projection", "reduce_event"]
