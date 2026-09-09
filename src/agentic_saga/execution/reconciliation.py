from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from agentic_saga.contracts.clock import Clock, SystemClock
from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    OperationId,
    canonical_json,
    sha256_json,
    thaw_json_object,
)
from agentic_saga.contracts.events import HumanRequired, LedgerEvent, ReconciliationRecorded
from agentic_saga.contracts.outcomes import (
    SAFE_RECONCILE_CONFLICT_REASON,
    SAFE_RECONCILE_UNSUPPORTED_REASON,
    ReconcileConflict,
    ReconcilePending,
    ReconcileUnsupported,
    ReconciliationOutcome,
    generated_outcome_correlation,
    normalize_reconciliation_outcome,
    safe_reconciliation_json,
)
from agentic_saga.contracts.redaction import RedactionPolicy
from agentic_saga.contracts.tools import (
    EffectToolDefinition,
    ReconcileContext,
    ToolRegistry,
)
from agentic_saga.execution.leases import LeaseService
from agentic_saga.kernel.identity import framed_sha256
from agentic_saga.kernel.ports import (
    InjectedStoreFailure,
    KernelStore,
    Lease,
    OutboxCommand,
    ReconciliationJob,
    RecoveryPolicy,
    RecoveryProofExpired,
    StoreConflict,
    TransitionBatch,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import OperationRecord, OperationStatus, SagaSnapshot

type ReconciliationAction = Literal["confirm", "retry_same_id", "wait", "human"]
type _FailureCode = Literal["no_work", "authority_lost", "human_required"]
type _Actor = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
_RECONCILIATION_ADAPTER: TypeAdapter[ReconciliationOutcome] = TypeAdapter(ReconciliationOutcome)
_DEFAULT_CLAIM_DURATION = timedelta(minutes=1)
_DEFAULT_LEASE_DURATION = timedelta(minutes=5)
_DEFAULT_TIMEOUT = timedelta(seconds=30)
_ID_DOMAIN = b"agentic-saga-reconciliation-v1"
_RECONCILIATION_HUMAN_REASON = "reconciliation_unsafe"
_POST_RECONCILIATION_HUMAN_REASON = "forward_work_requires_approval_after_reconciliation"


class ReconciliationDecision(BaseModel):
    """Choose the next safe action after checking an uncertain effect outcome."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    action: ReconciliationAction
    check_after: datetime | None = None

    @model_validator(mode="after")
    def require_wait_time(self) -> ReconciliationDecision:
        if (self.action == "wait") != (self.check_after is not None):
            raise ValueError("only wait decisions require check_after")
        if self.check_after is not None and not _is_utc(self.check_after):
            raise ValueError("check_after must use UTC")
        return self


class RecoveryHorizon(BaseModel):
    """Bound how long evidence must survive for retry and operator recovery."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    maximum_retry_delay: timedelta = Field(gt=timedelta(0))
    operator_response_window: timedelta = Field(gt=timedelta(0))
    clock_skew_allowance: timedelta = Field(ge=timedelta(0))

    @property
    def total(self) -> timedelta:
        return self.maximum_retry_delay + self.operator_response_window + self.clock_skew_allowance

    def public_policy(self) -> RecoveryPolicy:
        return RecoveryPolicy(
            maximum_retry_delay_microseconds=_microseconds(self.maximum_retry_delay),
            operator_response_window_microseconds=_microseconds(self.operator_response_window),
            clock_skew_allowance_microseconds=_microseconds(self.clock_skew_allowance),
        )


class ReconciliationFailure(BaseModel):
    """Expose a stable, non-sensitive reason reconciliation could not proceed."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    code: _FailureCode


class ReconciliationResult(BaseModel):
    """Return either a verified reconciliation decision or a typed safe failure."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    operation_id: OperationId | None
    decision: ReconciliationDecision | None
    failure: ReconciliationFailure | None


class _CommandUnavailable(ValueError):
    """Raised when durable command bytes cannot use the historical definition."""


@dataclass(frozen=True)
class _PreparedReconciliation:
    definition: EffectToolDefinition[BaseModel]
    operation: OperationRecord
    command: BaseModel


def retention_covers_horizon(
    now: datetime,
    first_dispatch_at: datetime,
    retention_seconds: int | None,
    horizon: RecoveryHorizon,
) -> bool:
    """Return whether provider evidence retention spans the full recovery horizon."""

    if retention_seconds is None:
        return False
    try:
        return now + horizon.total <= first_dispatch_at + timedelta(seconds=retention_seconds)
    except OverflowError:
        return False


class ReconciliationPlanner:
    """Pure fail-closed mapping from authoritative evidence to the only safe action."""

    def decide(
        self, outcome: ReconciliationOutcome, idempotency_covers_horizon: bool
    ) -> ReconciliationDecision:
        action = _planned_action(outcome, idempotency_covers_horizon)
        if isinstance(outcome, ReconcilePending):
            return ReconciliationDecision(action=action, check_after=outcome.check_after)
        return ReconciliationDecision(action=action)


def _planned_action(
    outcome: ReconciliationOutcome, idempotency_covers_horizon: bool
) -> ReconciliationAction:
    actions: dict[str, ReconciliationAction] = {
        "reconcile_effect_confirmed": "confirm",
        "reconcile_no_effect_confirmed": "retry_same_id",
        "reconcile_pending": "wait",
        "reconcile_conflict": "human",
    }
    if isinstance(outcome, ReconcileUnsupported):
        return "retry_same_id" if idempotency_covers_horizon else "human"
    return actions[outcome.kind]


def _microseconds(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _is_utc(value: datetime) -> bool:
    return value.utcoffset() == UTC.utcoffset(value)


def _positive_duration(value: timedelta, name: str) -> timedelta:
    if value <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return value


def _safe_now(clock: Clock) -> datetime:
    value = clock.now()
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("reconciler clock must return UTC")
    return value


def _result(
    operation_id: OperationId | None,
    decision: ReconciliationDecision | None = None,
    failure: _FailureCode | None = None,
) -> ReconciliationResult:
    detail = None if failure is None else ReconciliationFailure(code=failure)
    return ReconciliationResult(operation_id=operation_id, decision=decision, failure=detail)


def _events_and_projection(
    event: ReconciliationRecorded,
    projected: SagaSnapshot,
    human: HumanRequired | None,
) -> tuple[tuple[LedgerEvent, ...], SagaSnapshot]:
    if human is None:
        return (event,), projected
    return (event, human), reduce_event(projected, human)


def _stable_id(prefix: str, phase: str, job: ReconciliationJob) -> str:
    values = (phase, job.saga_id, job.operation_id, str(job.claim_generation))
    digest = framed_sha256(_ID_DOMAIN, *(value.encode() for value in values))
    return f"{prefix}_{digest}"


class Reconciler:
    """Claims one ambiguous operation and records one fenced recovery decision."""

    def __init__(  # noqa: PLR0913
        self,
        store: KernelStore,
        registry: ToolRegistry,
        *,
        lease_service: LeaseService | None = None,
        clock: Clock | None = None,
        horizon: RecoveryHorizon | None = None,
        claim_duration: timedelta = _DEFAULT_CLAIM_DURATION,
        lease_duration: timedelta = _DEFAULT_LEASE_DURATION,
        reconcile_timeout: timedelta = _DEFAULT_TIMEOUT,
        redaction_policy: RedactionPolicy | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._leases = lease_service or LeaseService(store)
        self._clock = clock or SystemClock()
        self._horizon = horizon or RecoveryHorizon(
            maximum_retry_delay=timedelta(minutes=5),
            operator_response_window=timedelta(hours=1),
            clock_skew_allowance=timedelta(seconds=30),
        )
        self._claim_duration = _positive_duration(claim_duration, "claim duration")
        self._lease_duration = _positive_duration(lease_duration, "lease duration")
        self._timeout = _positive_duration(reconcile_timeout, "reconcile timeout")
        self._planner = ReconciliationPlanner()
        self._redaction = redaction_policy or RedactionPolicy()

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return self._redaction

    async def reconcile_one(self, worker_id: _Actor) -> ReconciliationResult:
        policy = self._horizon.public_policy()
        digest = sha256_json(policy.model_dump(mode="json"))
        try:
            job = self._store.claim_reconciliation(worker_id, self._claim_duration, policy, digest)
        except StoreConflict:
            job = self._store.claim_reconciliation_frozen(worker_id, self._claim_duration)
        if job is None:
            return _result(None, failure="no_work")
        authority = self._acquire_and_bind(job, worker_id)
        if authority is None:
            return _result(job.operation_id, failure="authority_lost")
        bound, lease = authority
        return await self._reconcile_claim(bound, lease, digest)

    def _acquire_and_bind(
        self, job: ReconciliationJob, worker_id: str
    ) -> tuple[ReconciliationJob, Lease] | None:
        try:
            lease = self._leases.acquire(job.saga_id, worker_id, self._lease_duration)
            bound = self._store.bind_reconciliation_claim(job, lease)
        except StoreConflict:
            self._try_release(job)
            return None
        return bound, lease

    async def _reconcile_claim(
        self, job: ReconciliationJob, lease: Lease, expected_policy_digest: str
    ) -> ReconciliationResult:
        await asyncio.sleep(0)
        if not self._has_authority(job, lease):
            return _result(job.operation_id, failure="authority_lost")
        if job.recovery_policy_digest != expected_policy_digest:
            return self._persist_unavailable(job, lease)
        try:
            prepared = self._prepare(job)
        except _CommandUnavailable:
            return self._persist_unavailable(job, lease)
        return await self._call_provider(job, lease, prepared)

    def _has_authority(self, job: ReconciliationJob, lease: Lease) -> bool:
        try:
            self._store.validate_reconciliation_authority(job, lease)
        except StoreConflict:
            return False
        return True

    def _prepare(self, job: ReconciliationJob) -> _PreparedReconciliation:
        snapshot = self._store.load_snapshot(job.saga_id)
        operation = snapshot.operations.get(job.operation_id)
        if operation is None or operation.status is not OperationStatus.OUTCOME_UNKNOWN:
            raise _CommandUnavailable
        definition = self._definition(job, operation)
        command = self._typed_command(job, definition)
        return _PreparedReconciliation(definition, operation, command)

    def _definition(
        self, job: ReconciliationJob, operation: OperationRecord
    ) -> EffectToolDefinition[BaseModel]:
        try:
            definition = self._registry.definition(operation.tool_name)
        except LookupError as error:
            raise _CommandUnavailable from error
        if not isinstance(definition, EffectToolDefinition):
            raise _CommandUnavailable
        command = self._outbox(job)
        if not _definition_matches_command(definition, command):
            raise _CommandUnavailable
        return definition

    def _outbox(self, job: ReconciliationJob) -> OutboxCommand:
        command = self._store.load_outbox(job.command_id)
        if command.operation_id != job.operation_id:
            raise _CommandUnavailable
        return command

    def _typed_command(
        self, job: ReconciliationJob, definition: EffectToolDefinition[BaseModel]
    ) -> BaseModel:
        durable = self._outbox(job)
        try:
            command = definition.input_model.model_validate(
                thaw_json_object(durable.command), strict=True
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise _CommandUnavailable from error
        if canonical_json(command.model_dump(mode="json")) != canonical_json(durable.command):
            raise _CommandUnavailable
        if sha256_json(command.model_dump(mode="json")) != durable.command_hash:
            raise _CommandUnavailable
        return command

    async def _call_provider(
        self,
        job: ReconciliationJob,
        lease: Lease,
        prepared: _PreparedReconciliation,
    ) -> ReconciliationResult:
        covers = self._retention_covers(job, prepared.definition)
        if not prepared.definition.capabilities.reconciliation_supported:
            outcome = ReconcileUnsupported(reason=SAFE_RECONCILE_UNSUPPORTED_REASON)
            return self._persist(job, lease, outcome, covers)
        return await self._invoke_adapter(job, lease, prepared, covers)

    async def _invoke_adapter(
        self,
        job: ReconciliationJob,
        lease: Lease,
        prepared: _PreparedReconciliation,
        covers: bool,
    ) -> ReconciliationResult:
        try:
            started, context = self._start_lookup(prepared, job, lease)
        except (StoreConflict, _CommandUnavailable):
            return _result(job.operation_id, failure="authority_lost")
        try:
            async with asyncio.timeout(self._timeout.total_seconds()):
                raw = await prepared.definition.adapter.reconcile(prepared.command, context)
        except asyncio.CancelledError:
            await self._persist_cancelled(started, lease)
            raise
        except Exception:
            return self._persist_unavailable(started, lease)
        return self._finish_lookup(started, lease, raw, covers)

    def _finish_lookup(
        self, job: ReconciliationJob, lease: Lease, raw: object, covers: bool
    ) -> ReconciliationResult:
        if not self._has_authority(job, lease):
            return _result(job.operation_id, failure="authority_lost")
        return self._persist_raw(job, lease, raw, covers)

    def _start_lookup(
        self,
        prepared: _PreparedReconciliation,
        job: ReconciliationJob,
        lease: Lease,
    ) -> tuple[ReconciliationJob, ReconcileContext]:
        context = self._reconcile_context(job, lease, prepared.operation)
        started = self._store.begin_reconciliation_lookup(job, lease)
        return started, context

    def _reconcile_context(
        self, job: ReconciliationJob, lease: Lease, operation: OperationRecord
    ) -> ReconcileContext:
        if operation.correlation is None:
            raise _CommandUnavailable
        return ReconcileContext(
            saga_id=job.saga_id,
            step_instance_id=operation.step_instance_id,
            operation_id=job.operation_id,
            fence_token=lease.fence_token,
            delivery_attempt=operation.delivery_attempt,
            forward_receipts=self._forward_receipts(job, operation),
            correlation=operation.correlation,
        )

    def _forward_receipts(
        self, job: ReconciliationJob, operation: OperationRecord
    ) -> tuple[JsonObject, ...]:
        if operation.direction is Direction.FORWARD:
            return ()
        snapshot = self._store.load_snapshot(job.saga_id)
        target = operation.compensates_operation_id
        obligation = None if target is None else snapshot.obligations.get(target)
        if obligation is None or not obligation.receipts:
            raise _CommandUnavailable
        return obligation.receipts

    def _persist_raw(
        self, job: ReconciliationJob, lease: Lease, raw: object, covers: bool
    ) -> ReconciliationResult:
        try:
            outcome = _RECONCILIATION_ADAPTER.validate_python(raw, strict=True)
        except ValidationError:
            return self._persist_unavailable(job, lease)
        fallback = generated_outcome_correlation(
            job.saga_id, job.operation_id, job.claim_generation, "reconciliation_pending"
        )
        safe = normalize_reconciliation_outcome(
            outcome, fallback_correlation=fallback, redaction_policy=self._redaction
        )
        return self._persist(job, lease, safe, covers)

    def _persist_unavailable(self, job: ReconciliationJob, lease: Lease) -> ReconciliationResult:
        outcome = ReconcileConflict(reason=SAFE_RECONCILE_CONFLICT_REASON)
        return self._persist(job, lease, outcome, False)

    async def _persist_cancelled(self, job: ReconciliationJob, lease: Lease) -> None:
        task = asyncio.create_task(self._persist_cancelled_task(job, lease))
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        await task

    async def _persist_cancelled_task(self, job: ReconciliationJob, lease: Lease) -> None:
        await asyncio.sleep(0)
        self._persist_unavailable(job, lease)

    def _persist(
        self,
        job: ReconciliationJob,
        lease: Lease,
        outcome: ReconciliationOutcome,
        covers: bool,
    ) -> ReconciliationResult:
        decision = self._planner.decide(outcome, covers)
        try:
            batch = self._batch(job, lease, outcome, decision)
            self._commit_with_retry(job, lease, batch, decision)
        except RecoveryProofExpired:
            return self._persist_expired(job, lease, outcome)
        except StoreConflict:
            self._try_reschedule(job)
            return _result(job.operation_id, failure="authority_lost")
        failure: _FailureCode | None = "human_required" if decision.action == "human" else None
        return _result(job.operation_id, decision, failure)

    def _persist_expired(
        self, job: ReconciliationJob, lease: Lease, outcome: ReconciliationOutcome
    ) -> ReconciliationResult:
        decision = ReconciliationDecision(action="human")
        try:
            batch = self._batch(job, lease, outcome, decision)
            self._commit_with_retry(job, lease, batch, decision)
        except StoreConflict:
            self._try_reschedule(job)
            return _result(job.operation_id, failure="authority_lost")
        return _result(job.operation_id, decision, "human_required")

    def _commit_with_retry(
        self,
        job: ReconciliationJob,
        lease: Lease,
        batch: TransitionBatch,
        decision: ReconciliationDecision,
    ) -> None:
        while True:
            try:
                self._store.commit_reconciliation(
                    job, lease, batch, decision.action, decision.check_after
                )
            except InjectedStoreFailure:
                self._store.lookup_transition_receipt(batch.transition_id)
                continue
            return

    def _batch(
        self,
        job: ReconciliationJob,
        lease: Lease,
        outcome: ReconciliationOutcome,
        decision: ReconciliationDecision,
    ) -> TransitionBatch:
        snapshot = self._store.load_snapshot(job.saga_id)
        event = self._event(job, lease, snapshot, outcome, decision)
        followup = self._requires_human_followup(job, lease, decision)
        projected = reduce_event(snapshot, event)
        human = self._followup_event(job, lease, projected, decision, followup)
        events, projected = _events_and_projection(event, projected, human)
        return TransitionBatch(
            transition_id=_stable_id("txn", "decision", job),
            saga_id=job.saga_id,
            expected_seq=snapshot.seq,
            expected_fence_token=lease.fence_token,
            lease_owner=lease.owner,
            events=events,
            projection=projected,
        )

    def _requires_human_followup(
        self, job: ReconciliationJob, lease: Lease, decision: ReconciliationDecision
    ) -> bool:
        if decision.action not in {"confirm", "retry_same_id"}:
            return False
        return self._store.reconciliation_requires_human_followup(job, lease)

    def _followup_event(
        self,
        job: ReconciliationJob,
        lease: Lease,
        snapshot: SagaSnapshot,
        decision: ReconciliationDecision,
        followup: bool,
    ) -> HumanRequired | None:
        if decision.action != "human" and not followup:
            return None
        return self._human_event(job, lease, snapshot, followup)

    def _event(
        self,
        job: ReconciliationJob,
        lease: Lease,
        snapshot: SagaSnapshot,
        outcome: ReconciliationOutcome,
        decision: ReconciliationDecision,
    ) -> ReconciliationRecorded:
        operation = snapshot.operations[job.operation_id]
        result = safe_reconciliation_json(outcome)
        fields = self._event_base(job, lease, snapshot, "evidence")
        effect = _effect_fields(operation)
        proof = {
            "reconciliation_attempt": job.claim_generation,
            "outcome": outcome,
            "action": decision.action,
            "redacted_result": result,
            "result_hash": sha256_json(result),
            "recovery_policy_digest": job.recovery_policy_digest,
        }
        return ReconciliationRecorded.model_validate(fields | effect | proof)

    def _human_event(
        self, job: ReconciliationJob, lease: Lease, snapshot: SagaSnapshot, followup: bool
    ) -> HumanRequired:
        fields = self._event_base(job, lease, snapshot, "human")
        reason = _POST_RECONCILIATION_HUMAN_REASON if followup else _RECONCILIATION_HUMAN_REASON
        return HumanRequired.model_validate(fields | {"reason_code": reason})

    def _event_base(
        self,
        job: ReconciliationJob,
        lease: Lease,
        snapshot: SagaSnapshot,
        phase: str,
    ) -> dict[str, object]:
        return {
            "event_id": _stable_id("evt", phase, job),
            "saga_id": job.saga_id,
            "saga_seq": snapshot.seq + 1,
            "definition_version": snapshot.definition_version,
            "fence_token": lease.fence_token,
            "actor": "reconciler",
            "trace_id": _stable_id("trace", "trace", job),
            "recorded_at": _claimed_at(job),
        }

    def _retention_covers(
        self, job: ReconciliationJob, definition: EffectToolDefinition[BaseModel]
    ) -> bool:
        return retention_covers_horizon(
            _claimed_at(job),
            job.first_dispatch_at,
            definition.capabilities.idempotency_retention_seconds,
            self._horizon,
        )

    def _try_reschedule(self, job: ReconciliationJob) -> None:
        self._try_release(job)

    def _try_release(self, job: ReconciliationJob) -> None:
        try:
            self._store.release_reconciliation(job)
        except StoreConflict:
            return


def _claimed_at(job: ReconciliationJob) -> datetime:
    if job.claimed_at is None:
        raise StoreConflict("reconciliation claim has no durable timestamp")
    return job.claimed_at


def _definition_matches_command(
    definition: EffectToolDefinition[BaseModel], command: OutboxCommand
) -> bool:
    versions = (definition.definition_version, definition.command_schema_version)
    durable_versions = (command.definition_version, command.command_schema_version)
    proof = command.capability_proof
    return (
        versions == durable_versions
        and proof is not None
        and (proof.capabilities == definition.capabilities)
    )


def _effect_fields(operation: OperationRecord) -> dict[str, object]:
    return {
        "operation_id": operation.operation_id,
        "step_instance_id": operation.step_instance_id,
        "direction": operation.direction,
        "semantic_generation": operation.semantic_generation,
        "delivery_attempt": operation.delivery_attempt,
        "tool_name": operation.tool_name,
        "redacted_command": operation.redacted_command,
        "command_hash": operation.command_hash,
    }


__all__ = [
    "Reconciler",
    "ReconciliationAction",
    "ReconciliationDecision",
    "ReconciliationFailure",
    "ReconciliationPlanner",
    "ReconciliationResult",
    "RecoveryHorizon",
    "retention_covers_horizon",
]
