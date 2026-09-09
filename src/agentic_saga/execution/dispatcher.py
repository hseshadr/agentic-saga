from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, TypeAdapter, ValidationError

from agentic_saga.contracts.clock import Clock, SystemClock
from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    JsonPayloadError,
    OperationId,
    SagaId,
    canonical_json,
    sha256_json,
    thaw_json_object,
)
from agentic_saga.contracts.events import (
    DispatchAbortedBeforeEntry,
    DispatchStarted,
    EffectOutcomeRecorded,
    LedgerEvent,
)
from agentic_saga.contracts.outcomes import (
    EffectOutcome,
    OutcomeUnknown,
    generated_outcome_correlation,
    normalize_effect_outcome,
    safe_outcome_json,
)
from agentic_saga.contracts.redaction import RedactionPolicy
from agentic_saga.contracts.tools import EffectContext, EffectToolDefinition, ToolRegistry
from agentic_saga.kernel.failpoints import (
    DurabilityFailpoint,
    DurabilityPoint,
    NoOpDurabilityFailpoint,
)
from agentic_saga.kernel.identity import framed_sha256
from agentic_saga.kernel.ports import (
    ClaimedCommand,
    InjectedStoreFailure,
    KernelStore,
    Lease,
    StoreConflict,
    TransitionBatch,
)
from agentic_saga.kernel.reducer import InvalidTransition, reduce_event
from agentic_saga.kernel.state import OperationStatus, SagaSnapshot

type _FailureCode = Literal[
    "no_work",
    "authority_lost",
    "command_unavailable",
    "outcome_unknown",
    "prior_dispatch_unknown",
]
type _Actor = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
_OUTCOME_ADAPTER: TypeAdapter[EffectOutcome] = TypeAdapter(EffectOutcome)
_DEFAULT_CLAIM_DURATION = timedelta(minutes=1)
_DEFAULT_EFFECT_TIMEOUT = timedelta(seconds=30)
_ID_DOMAIN = b"agentic-saga-dispatch-v1"


class DispatchFailure(BaseModel):
    """A fixed public failure category with no provider detail."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    code: _FailureCode


class DispatchResult(BaseModel):
    """Safe result of processing at most one durable outbox command."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    operation_id: OperationId | None
    outcome: EffectOutcome | None
    failure: DispatchFailure | None


class _CommandUnavailable(ValueError):
    """Raised internally when durable bytes cannot use the registered effect contract."""


@dataclass(frozen=True)
class _PreparedEffect:
    definition: EffectToolDefinition[BaseModel]
    command: BaseModel


@dataclass(frozen=True)
class _PreparedInvocation:
    effect: _PreparedEffect
    context: EffectContext


def _failure(code: _FailureCode, operation_id: OperationId | None = None) -> DispatchResult:
    return DispatchResult(
        operation_id=operation_id,
        outcome=None,
        failure=DispatchFailure(code=code),
    )


def _processed(
    operation_id: OperationId, outcome: EffectOutcome, code: _FailureCode | None = None
) -> DispatchResult:
    failure = None if code is None else DispatchFailure(code=code)
    return DispatchResult(operation_id=operation_id, outcome=outcome, failure=failure)


def _stable_id(prefix: str, phase: str, claimed: ClaimedCommand) -> str:
    values = (phase, claimed.saga_id, claimed.operation_id, claimed.claim_id)
    digest = framed_sha256(_ID_DOMAIN, *(value.encode() for value in values))
    return f"{prefix}_{digest}"


def _event_base(
    claimed: ClaimedCommand,
    lease: Lease,
    snapshot: SagaSnapshot,
    phase: str,
    recorded_at: datetime,
) -> dict[str, object]:
    return {
        "event_id": _stable_id("evt", phase, claimed),
        "saga_id": claimed.saga_id,
        "saga_seq": snapshot.seq + 1,
        "definition_version": snapshot.definition_version,
        "fence_token": lease.fence_token,
        "actor": "dispatcher",
        "trace_id": _stable_id("trace", "trace", claimed),
        "recorded_at": recorded_at,
    }


def _effect_fields(claimed: ClaimedCommand) -> dict[str, object]:
    return {
        "operation_id": claimed.operation_id,
        "step_instance_id": claimed.step_instance_id,
        "direction": claimed.direction,
        "semantic_generation": claimed.semantic_generation,
        "delivery_attempt": claimed.delivery_attempt,
        "tool_name": claimed.tool_name,
        "redacted_command": claimed.command,
        "command_hash": claimed.command_hash,
    }


def _batch(
    claimed: ClaimedCommand,
    lease: Lease,
    snapshot: SagaSnapshot,
    event: LedgerEvent,
    phase: str,
) -> TransitionBatch:
    return TransitionBatch(
        transition_id=_stable_id("txn", phase, claimed),
        saga_id=claimed.saga_id,
        expected_seq=snapshot.seq,
        expected_fence_token=lease.fence_token,
        lease_owner=lease.owner,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )


def _safe_now(clock: Clock) -> datetime:
    value = clock.now()
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("dispatcher clock must return UTC")
    return value


def _positive_duration(value: timedelta, name: str) -> timedelta:
    if value <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return value


class Dispatcher:
    """Claims one command and drives its fenced, intent-first delivery lifecycle."""

    def __init__(  # noqa: PLR0913
        self,
        store: KernelStore,
        registry: ToolRegistry,
        *,
        clock: Clock | None = None,
        claim_duration: timedelta = _DEFAULT_CLAIM_DURATION,
        effect_timeout: timedelta = _DEFAULT_EFFECT_TIMEOUT,
        failpoint: DurabilityFailpoint | None = None,
        redaction_policy: RedactionPolicy | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._clock = clock or SystemClock()
        self._claim_duration = _positive_duration(claim_duration, "claim duration")
        self._effect_timeout = _positive_duration(effect_timeout, "effect timeout")
        self._failpoint = failpoint or NoOpDurabilityFailpoint()
        self._redaction = redaction_policy or RedactionPolicy()

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return self._redaction

    async def dispatch_one(self, worker_id: _Actor) -> DispatchResult:
        claimed = self._store.claim_outbox(worker_id, self._claim_duration)
        return await self._dispatch_claimed(claimed, worker_id)

    async def dispatch_saga(self, saga_id: SagaId, worker_id: _Actor) -> DispatchResult:
        claimed = self._store.claim_outbox_for_saga(saga_id, worker_id, self._claim_duration)
        return await self._dispatch_claimed(claimed, worker_id)

    async def _dispatch_claimed(
        self, claimed: ClaimedCommand | None, worker_id: _Actor
    ) -> DispatchResult:
        if claimed is None:
            return _failure("no_work")
        await self._pre_dispatch_checkpoint(claimed)
        lease = self._current_lease(claimed, worker_id)
        if lease is None:
            self._try_release(claimed)
            return _failure("authority_lost", claimed.operation_id)
        return await self._dispatch_claim(claimed, lease)

    async def _pre_dispatch_checkpoint(self, claimed: ClaimedCommand) -> None:
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            self._try_release(claimed)
            raise

    def _current_lease(self, claimed: ClaimedCommand, worker_id: str) -> Lease | None:
        current = self._store.lease_state(claimed.saga_id)
        if current is None:
            return None
        if current.expires_at <= _safe_now(self._clock):
            return None
        if (current.owner, current.fence_token) != (worker_id, claimed.saga_fence_token):
            return None
        return current

    def _try_release(self, claimed: ClaimedCommand) -> None:
        try:
            self._store.release_outbox(claimed)
        except StoreConflict:
            return

    async def _dispatch_claim(self, claimed: ClaimedCommand, lease: Lease) -> DispatchResult:
        snapshot = self._store.load_snapshot(claimed.saga_id)
        operation = snapshot.operations.get(claimed.operation_id)
        if operation is None:
            self._try_release(claimed)
            return _failure("command_unavailable", claimed.operation_id)
        if operation.status is OperationStatus.DISPATCHED:
            return self._park_prior_dispatch(claimed, lease)
        if operation.status is not OperationStatus.INTENT_DURABLE:
            self._try_release(claimed)
            return _failure("command_unavailable", claimed.operation_id)
        return await self._dispatch_intent(claimed, lease, snapshot)

    async def _dispatch_intent(
        self, claimed: ClaimedCommand, lease: Lease, snapshot: SagaSnapshot
    ) -> DispatchResult:
        started = self._try_start(claimed, lease, snapshot)
        if started is None:
            return self._release_after_authority_loss(claimed)
        try:
            invocation = self._prepare_invocation(claimed, lease)
        except _CommandUnavailable:
            return self._park_unavailable(claimed, lease)
        await self._pre_entry_checkpoint(claimed, lease)
        if not self._has_dispatch_authority(claimed, lease):
            return _failure("authority_lost", claimed.operation_id)
        return await self._call_effect(claimed, lease, invocation)

    def _release_after_authority_loss(self, claimed: ClaimedCommand) -> DispatchResult:
        self._try_release(claimed)
        return _failure("authority_lost", claimed.operation_id)

    def _has_dispatch_authority(self, claimed: ClaimedCommand, lease: Lease) -> bool:
        try:
            self._store.validate_dispatch_authority(claimed, lease)
        except StoreConflict:
            return False
        return True

    def _try_start(
        self, claimed: ClaimedCommand, lease: Lease, snapshot: SagaSnapshot
    ) -> SagaSnapshot | None:
        event = self._dispatch_event(claimed, lease, snapshot)
        batch = _batch(claimed, lease, snapshot, event, "dispatch-start")
        try:
            started = self._start_with_uncertain_retry(claimed, batch)
            self._failpoint.hit(DurabilityPoint.AFTER_DISPATCH_RECORD)
            return started
        except StoreConflict:
            return None

    def _start_with_uncertain_retry(
        self, claimed: ClaimedCommand, batch: TransitionBatch
    ) -> SagaSnapshot:
        try:
            return self._store.start_dispatch(claimed, batch)
        except InjectedStoreFailure:
            if self._store.lookup_transition_receipt(batch.transition_id) is None:
                raise
            return self._store.start_dispatch(claimed, batch)

    def _dispatch_event(
        self, claimed: ClaimedCommand, lease: Lease, snapshot: SagaSnapshot
    ) -> DispatchStarted:
        fields = _event_base(claimed, lease, snapshot, "dispatch", _safe_now(self._clock))
        return DispatchStarted.model_validate(fields | _effect_fields(claimed))

    def _prepare_effect(self, claimed: ClaimedCommand) -> _PreparedEffect:
        definition = self._registered_effect(claimed)
        try:
            public_command = thaw_json_object(claimed.command)
            command = definition.input_model.model_validate(public_command, strict=True)
        except (TypeError, ValueError, ValidationError) as error:
            raise _CommandUnavailable from error
        self._verify_typed_bytes(claimed, command)
        return _PreparedEffect(definition, command)

    def _prepare_invocation(self, claimed: ClaimedCommand, lease: Lease) -> _PreparedInvocation:
        effect = self._prepare_effect(claimed)
        return _PreparedInvocation(effect, self._effect_context(claimed, lease))

    def _registered_effect(self, claimed: ClaimedCommand) -> EffectToolDefinition[BaseModel]:
        try:
            definition = self._registry.definition(claimed.tool_name)
        except LookupError as error:
            raise _CommandUnavailable from error
        if not isinstance(definition, EffectToolDefinition):
            raise _CommandUnavailable
        versions = (definition.definition_version, definition.command_schema_version)
        if versions != (claimed.definition_version, claimed.command_schema_version):
            raise _CommandUnavailable
        self._verify_capabilities(claimed, definition)
        return definition

    @staticmethod
    def _verify_capabilities(
        claimed: ClaimedCommand, definition: EffectToolDefinition[BaseModel]
    ) -> None:
        proof = claimed.envelope.capability_proof
        if proof is None or proof.capabilities != definition.capabilities:
            raise _CommandUnavailable
        payload = definition.capabilities.model_dump(mode="json")
        if proof.capability_digest != sha256_json(payload):
            raise _CommandUnavailable

    @staticmethod
    def _verify_typed_bytes(claimed: ClaimedCommand, command: BaseModel) -> None:
        typed = command.model_dump(mode="json")
        if canonical_json(typed) != canonical_json(claimed.command):
            raise _CommandUnavailable
        if sha256_json(typed) != claimed.command_hash:
            raise _CommandUnavailable

    async def _pre_entry_checkpoint(self, claimed: ClaimedCommand, lease: Lease) -> None:
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            self._abort_before_entry(claimed, lease)
            raise

    def _abort_before_entry(self, claimed: ClaimedCommand, lease: Lease) -> None:
        snapshot = self._store.load_snapshot(claimed.saga_id)
        fields = _event_base(claimed, lease, snapshot, "abort", _safe_now(self._clock))
        event = DispatchAbortedBeforeEntry.model_validate(fields | _effect_fields(claimed))
        batch = _batch(claimed, lease, snapshot, event, "dispatch-abort")
        action = partial(self._store.abort_dispatch, claimed, batch)
        self._retry_uncertain_transition(batch, action)

    def _retry_uncertain_transition(
        self, batch: TransitionBatch, action: Callable[[], SagaSnapshot]
    ) -> bool:
        while True:
            try:
                action()
            except StoreConflict:
                return False
            except InjectedStoreFailure:
                self._store.lookup_transition_receipt(batch.transition_id)
                continue
            return True

    async def _call_effect(
        self, claimed: ClaimedCommand, lease: Lease, invocation: _PreparedInvocation
    ) -> DispatchResult:
        try:
            raw = await self._execute_with_timeout(invocation.effect, invocation.context)
        except asyncio.CancelledError:
            await self._persist_cancelled(claimed, lease)
            raise
        except TimeoutError:
            return self._persist_generated_unknown(claimed, lease, "adapter_timeout")
        except Exception:
            return self._persist_generated_unknown(claimed, lease, "adapter_exception")
        return self._persist_adapter_outcome(claimed, lease, raw)

    async def _execute_with_timeout(
        self, prepared: _PreparedEffect, context: EffectContext
    ) -> object:
        async with asyncio.timeout(self._effect_timeout.total_seconds()):
            return await prepared.definition.adapter.execute(prepared.command, context)

    def _effect_context(self, claimed: ClaimedCommand, lease: Lease) -> EffectContext:
        return EffectContext(
            saga_id=claimed.saga_id,
            step_instance_id=claimed.step_instance_id,
            operation_id=claimed.operation_id,
            fence_token=lease.fence_token,
            delivery_attempt=claimed.delivery_attempt,
            forward_receipts=self._forward_receipts(claimed),
        )

    def _forward_receipts(self, claimed: ClaimedCommand) -> tuple[JsonObject, ...]:
        if claimed.direction is Direction.FORWARD:
            return ()
        snapshot = self._store.load_snapshot(claimed.saga_id)
        operation = snapshot.operations.get(claimed.operation_id)
        target = None if operation is None else operation.compensates_operation_id
        return self._obligation_receipts(snapshot, target)

    @staticmethod
    def _obligation_receipts(
        snapshot: SagaSnapshot, target: OperationId | None
    ) -> tuple[JsonObject, ...]:
        if target is None:
            raise _CommandUnavailable
        obligation = snapshot.obligations.get(target)
        if obligation is None or not obligation.receipts:
            raise _CommandUnavailable
        return obligation.receipts

    async def _persist_cancelled(self, claimed: ClaimedCommand, lease: Lease) -> None:
        outcome = self._generated_unknown(claimed, "adapter_cancelled")
        task = asyncio.create_task(self._persist_unknown_async(claimed, lease, outcome))
        await self._await_shielded(task)

    async def _persist_unknown_async(
        self, claimed: ClaimedCommand, lease: Lease, outcome: OutcomeUnknown
    ) -> None:
        await asyncio.sleep(0)
        batch = self._authorized_outcome_batch(claimed, lease, outcome)
        if batch is None:
            return
        action = partial(self._finish, claimed, outcome, batch)
        self._retry_uncertain_transition(batch, action)

    @staticmethod
    async def _await_shielded(task: asyncio.Task[None]) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        await task

    def _persist_generated_unknown(
        self, claimed: ClaimedCommand, lease: Lease, category: str
    ) -> DispatchResult:
        return self._persist_safe_outcome(
            claimed, lease, self._generated_unknown(claimed, category), "outcome_unknown"
        )

    def _generated_unknown(self, claimed: ClaimedCommand, category: str) -> OutcomeUnknown:
        return OutcomeUnknown(correlation=self._correlation(claimed, category))

    def _persist_adapter_outcome(
        self, claimed: ClaimedCommand, lease: Lease, raw: object
    ) -> DispatchResult:
        safe = self._normalized_adapter_outcome(claimed, raw)
        if safe is None:
            return self._persist_generated_unknown(claimed, lease, "adapter_invalid_outcome")
        code: _FailureCode | None = "outcome_unknown" if isinstance(safe, OutcomeUnknown) else None
        return self._persist_safe_outcome(claimed, lease, safe, code)

    def _normalized_adapter_outcome(
        self, claimed: ClaimedCommand, raw: object
    ) -> EffectOutcome | None:
        try:
            outcome = _OUTCOME_ADAPTER.validate_python(raw)
            safe = normalize_effect_outcome(
                outcome,
                fallback_correlation=self._correlation(claimed, "provider_unknown"),
                redaction_policy=self._redaction,
            )
            safe_outcome_json(safe)
        except (JsonPayloadError, ValidationError):
            return None
        return safe

    def _persist_safe_outcome(
        self,
        claimed: ClaimedCommand,
        lease: Lease,
        outcome: EffectOutcome,
        code: _FailureCode | None,
    ) -> DispatchResult:
        batch = self._authorized_outcome_batch(claimed, lease, outcome)
        if batch is None:
            return _failure("authority_lost", claimed.operation_id)
        try:
            self._finish(claimed, outcome, batch)
        except StoreConflict:
            if not self._has_dispatch_authority(claimed, lease):
                return _failure("authority_lost", claimed.operation_id)
            raise
        return _processed(claimed.operation_id, outcome, code)

    def _authorized_outcome_batch(
        self, claimed: ClaimedCommand, lease: Lease, outcome: EffectOutcome
    ) -> TransitionBatch | None:
        if not self._has_dispatch_authority(claimed, lease):
            return None
        try:
            return self._safe_outcome_batch(claimed, lease, outcome)
        except InvalidTransition:
            if self._has_dispatch_authority(claimed, lease):
                raise
            return None

    def _safe_outcome_batch(
        self, claimed: ClaimedCommand, lease: Lease, outcome: EffectOutcome
    ) -> TransitionBatch:
        snapshot = self._store.load_snapshot(claimed.saga_id)
        event = self._outcome_event(claimed, lease, snapshot, outcome)
        return _batch(claimed, lease, snapshot, event, "dispatch-outcome")

    def _outcome_event(
        self,
        claimed: ClaimedCommand,
        lease: Lease,
        snapshot: SagaSnapshot,
        outcome: EffectOutcome,
    ) -> EffectOutcomeRecorded:
        result = safe_outcome_json(outcome)
        fields = _event_base(claimed, lease, snapshot, "outcome", _safe_now(self._clock))
        proof = {"outcome": outcome, "redacted_result": result, "result_hash": sha256_json(result)}
        return EffectOutcomeRecorded.model_validate(fields | _effect_fields(claimed) | proof)

    def _finish(
        self, claimed: ClaimedCommand, outcome: EffectOutcome, batch: TransitionBatch
    ) -> SagaSnapshot:
        if isinstance(outcome, OutcomeUnknown):
            snapshot = self._store.park_outbox(claimed, batch)
        else:
            snapshot = self._store.complete_outbox(claimed, batch)
        self._failpoint.hit(DurabilityPoint.AFTER_OUTCOME_COMMIT)
        return snapshot

    def _park_unavailable(self, claimed: ClaimedCommand, lease: Lease) -> DispatchResult:
        outcome = OutcomeUnknown(correlation=self._correlation(claimed, "command_unavailable"))
        result = self._persist_safe_outcome(claimed, lease, outcome, "command_unavailable")
        return result

    def _park_prior_dispatch(self, claimed: ClaimedCommand, lease: Lease) -> DispatchResult:
        outcome = OutcomeUnknown(correlation=self._correlation(claimed, "prior_dispatch"))
        return self._persist_safe_outcome(claimed, lease, outcome, "prior_dispatch_unknown")

    @staticmethod
    def _correlation(claimed: ClaimedCommand, category: str) -> str:
        return generated_outcome_correlation(
            claimed.saga_id,
            claimed.operation_id,
            claimed.delivery_attempt,
            category,
        )


__all__ = ["DispatchFailure", "DispatchResult", "Dispatcher"]
