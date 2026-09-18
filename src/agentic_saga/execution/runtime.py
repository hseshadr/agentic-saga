from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_hex
from typing import Protocol, cast, runtime_checkable

from pydantic import BaseModel, TypeAdapter, ValidationError

from agentic_saga.contracts.actions import AgentProposal, Escalate, Finish, ToolCall
from agentic_saga.contracts.clock import Clock, SystemClock
from agentic_saga.contracts.common import JsonObject, SagaId, sha256_json, thaw_json_object
from agentic_saga.contracts.events import (
    AgentTurnFailed,
    AgentTurnReserved,
    CompensationIntentRecorded,
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    HumanRequired,
    InvariantEvaluated,
    LedgerEvent,
    ProposalRejected,
    ReadObserved,
    ReadStarted,
    ReadUnavailable,
    ReconciliationRecorded,
    SagaCreated,
    SagaStarted,
    TerminalDenied,
)
from agentic_saga.contracts.redaction import RedactionPolicy, contains_sensitive_json, redact_json
from agentic_saga.contracts.runtime import (
    AgentDriver,
    ControlProposalCapabilities,
    ExecutionBudget,
    ReadEvidence,
    SagaGoal,
    SagaObservation,
    SagaResult,
    SagaStatus,
    ToolDescriptor,
)
from agentic_saga.contracts.tools import (
    EffectToolDefinition,
    ReadToolDefinition,
    UnknownToolError,
)
from agentic_saga.execution.dispatcher import Dispatcher
from agentic_saga.execution.leases import LeaseService
from agentic_saga.execution.reconciliation import Reconciler
from agentic_saga.execution.unwind import (
    EmergencyUnwinder,
    UnwindAction,
    UnwindTrigger,
)
from agentic_saga.kernel.definitions import DefinitionCatalog, DefinitionConflict, SagaDefinition
from agentic_saga.kernel.ports import (
    InjectedStoreFailure,
    Lease,
    LeaseLost,
    LeaseUnavailable,
    ProposalIdentityConflict,
    StoreConflict,
    TransitionBatch,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.runtime import SagaKernel
from agentic_saga.kernel.state import OperationStatus, SagaSnapshot

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_AGENT_PROPOSAL: TypeAdapter[AgentProposal] = TypeAdapter(AgentProposal)
_LEASE_DURATION = timedelta(minutes=5)
_INVALID_LIMIT = 3
_OWNER_SUFFIX_LENGTH = 32
_MAX_OWNER_LENGTH = 200
_ID_DOMAIN = b"agentic-saga-runtime-v1"
_TERMINAL = frozenset(
    {
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    }
)
_AUTONOMOUS_STOPPED = _TERMINAL | {SagaStatus.HUMAN_REQUIRED}
_OBSERVATION_EVENTS = (
    ProposalRejected,
    TerminalDenied,
    EffectOutcomeRecorded,
    ReconciliationRecorded,
    ReadObserved,
    ReadUnavailable,
)
_EVIDENCE_INVALIDATING_EVENTS = (
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    CompensationIntentRecorded,
    ReconciliationRecorded,
)
_AGENT_FAILURE_REASONS = {
    "invalid_response": "agent_invalid_response",
    "request_rejected": "agent_request_rejected",
    "rate_limit_exhausted": "agent_rate_limit_exhausted",
    "server_error_exhausted": "agent_server_error_exhausted",
    "transport_exhausted": "agent_transport_exhausted",
    "internal": "agent_internal",
}


class DefinitionRuntimeMismatch(ValueError):
    """Raised when pinned definition dependencies differ from the active kernel."""


@runtime_checkable
class _CategorizedAgentFailure(Protocol):
    @property
    def category(self) -> object: ...


@dataclass(frozen=True)
class _BudgetState:
    remaining: ExecutionBudget
    turn_count: int
    invalid_count: int


@dataclass(frozen=True)
class _AgentAnswer:
    proposal: AgentProposal | None = None
    failure_reason: str | None = None


@dataclass
class _LeaseAuthority:
    service: LeaseService
    lease: Lease
    duration: timedelta
    active: bool = True

    def renew(self) -> None:
        if not self.active:
            raise LeaseUnavailable("runtime lease authority was lost")
        current = self.lease
        try:
            renewed = self.service.renew(current, self.duration)
        except LeaseLost:
            renewed = self._reacquire(current)
        except LeaseUnavailable:
            self.active = False
            raise
        except Exception:
            self.active = False
            raise
        self.lease = renewed

    def _reacquire(self, expired: Lease) -> Lease:
        try:
            return self.service.acquire(expired.saga_id, expired.owner, self.duration)
        except Exception:
            self.active = False
            raise

    def release(self) -> None:
        if self.active:
            self.service.release(self.lease)


@dataclass(frozen=True)
class _LoopContext:
    authority: _LeaseAuthority
    definition: SagaDefinition
    goal: SagaGoal
    agent: AgentDriver

    @property
    def lease(self) -> Lease:
        return self.authority.lease


@dataclass(frozen=True)
class _ToolRequest:
    definition: SagaDefinition
    proposal: ToolCall
    turn_id: str


@dataclass(frozen=True)
class _ReadInvocation:
    tool: ReadToolDefinition[BaseModel, BaseModel]
    command: BaseModel
    started: ReadStarted
    policy: RedactionPolicy
    timeout_ms: int


@dataclass
class _GuardEntry:
    lock: asyncio.Lock
    references: int = 0


@dataclass(frozen=True)
class _RuntimeServices:
    kernel: SagaKernel
    dispatcher: Dispatcher
    reconciler: Reconciler
    unwinder: EmergencyUnwinder
    leases: LeaseService
    definitions: DefinitionCatalog


async def _await_with_authority[T](
    authority: _LeaseAuthority, work: Callable[[], Awaitable[T]]
) -> T:
    authority.renew()
    try:
        result = await work()
    except BaseException as error:
        _renew_preserving_error(authority, error)
        raise
    authority.renew()
    return result


def _renew_preserving_error(authority: _LeaseAuthority, error: BaseException) -> None:
    try:
        authority.renew()
    except Exception as renewal_error:
        error.add_note(f"lease renewal also failed: {type(renewal_error).__name__}")


def _release_preserving_error(authority: _LeaseAuthority, error: BaseException) -> None:
    try:
        authority.release()
    except Exception as release_error:
        error.add_note(f"lease release also failed: {type(release_error).__name__}")


def _stable_id(prefix: str, *values: str) -> str:
    framed = (len(value.encode()).to_bytes(8, "big") + value.encode() for value in values)
    digest = sha256(_ID_DOMAIN + b"".join(framed)).hexdigest()
    return f"{prefix}_{digest}"


def _saga_id(goal: SagaGoal) -> SagaId:
    return _stable_id("saga", goal.goal_id)


def _safe_now(clock: Clock) -> datetime:
    value = clock.now()
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("runtime clock must return UTC")
    return value


def _validated_agent_answer(raw: object) -> _AgentAnswer:
    try:
        proposal = _AGENT_PROPOSAL.validate_python(raw, strict=True)
    except ValidationError:
        return _AgentAnswer(failure_reason="agent_invalid_response")
    return _AgentAnswer(proposal=proposal)


def _agent_failure_reason(error: Exception) -> str:
    category = _agent_failure_category(error)
    if category is None:
        return "agent_internal"
    return _AGENT_FAILURE_REASONS.get(category, "agent_internal")


def _agent_failure_category(error: Exception) -> str | None:
    if not isinstance(error, _CategorizedAgentFailure):
        return None
    try:
        category = error.category
    except Exception:
        return None
    return category if isinstance(category, str) else None


def _unique_worker_id(worker_id: str | None) -> str:
    label = worker_id or "runtime"
    suffix = token_hex(_OWNER_SUFFIX_LENGTH // 2)
    prefix = label[: _MAX_OWNER_LENGTH - len(suffix) - 1]
    return f"{prefix}-{suffix}"


def _public_json(value: object, policy: RedactionPolicy) -> JsonObject:
    redacted = redact_json(value, policy)
    return _JSON_OBJECT.validate_python(redacted)


def _require_public_goal(goal: SagaGoal, policy: RedactionPolicy) -> None:
    public = goal.model_dump(mode="json")
    if contains_sensitive_json(public, policy):
        raise ValueError("goal contains definition-sensitive material")


def _validated_redaction(policy: RedactionPolicy, role: str) -> RedactionPolicy:
    if type(policy) is not RedactionPolicy:
        raise DefinitionRuntimeMismatch(f"{role} redaction must be an exact RedactionPolicy")
    try:
        validated = RedactionPolicy(sensitive_keys=policy.sensitive_keys)
    except ValidationError:
        raise DefinitionRuntimeMismatch(f"{role} redaction is invalid") from None
    if validated != policy:
        raise DefinitionRuntimeMismatch(f"{role} redaction is invalid")
    return policy


def _event_base(
    snapshot: SagaSnapshot, lease: Lease, phase: str, recorded_at: datetime
) -> dict[str, object]:
    transition_id = _stable_id("txn", snapshot.saga_id, str(snapshot.seq), phase)
    return {
        "event_id": _stable_id("evt", transition_id),
        "saga_id": snapshot.saga_id,
        "saga_seq": snapshot.seq + 1,
        "definition_version": snapshot.definition_version,
        "fence_token": lease.fence_token,
        "actor": "runtime",
        "trace_id": _stable_id("trace", transition_id),
        "recorded_at": recorded_at,
    }


def _creation_base(
    saga_id: SagaId,
    definition: SagaDefinition,
    transition_id: str,
    recorded_at: datetime,
) -> dict[str, object]:
    fields = _creation_event_fields(saga_id, transition_id, recorded_at)
    return fields | {
        "definition_version": definition.version,
        "definition_name": definition.name,
        "definition_fingerprint": definition.fingerprint,
    }


def _creation_event_fields(
    saga_id: SagaId, transition_id: str, recorded_at: datetime
) -> dict[str, object]:
    return {
        "event_id": _stable_id("evt", transition_id),
        "saga_id": saga_id,
        "saga_seq": 1,
        "fence_token": None,
        "actor": "runtime",
        "trace_id": _stable_id("trace", transition_id),
        "recorded_at": recorded_at,
    }


class SagaRuntime:
    """Runs one durable, bounded agent action per authoritative observation."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        kernel: SagaKernel,
        dispatcher: Dispatcher,
        reconciler: Reconciler,
        unwinder: EmergencyUnwinder,
        leases: LeaseService,
        definitions: DefinitionCatalog,
        clock: Clock | None = None,
        lease_duration: timedelta = _LEASE_DURATION,
        worker_id: str | None = None,
    ) -> None:
        services = _RuntimeServices(kernel, dispatcher, reconciler, unwinder, leases, definitions)
        self._initialize(services, clock, lease_duration, worker_id)

    def _initialize(
        self,
        services: _RuntimeServices,
        clock: Clock | None,
        lease_duration: timedelta,
        worker_id: str | None,
    ) -> None:
        self._set_services(services)
        self._set_runtime_config(clock, lease_duration, worker_id)

    def _set_services(self, services: _RuntimeServices) -> None:
        self._require_consistent_redaction(services)
        self._kernel = services.kernel
        self._store = services.kernel.store
        self._dispatcher = services.dispatcher
        self._reconciler = services.reconciler
        self._unwinder = services.unwinder
        self._leases = services.leases
        self._definitions = services.definitions

    @staticmethod
    def _require_consistent_redaction(services: _RuntimeServices) -> None:
        expected = _validated_redaction(services.kernel.redaction_policy, "kernel")
        dispatcher = _validated_redaction(services.dispatcher.redaction_policy, "dispatcher")
        reconciler = _validated_redaction(services.reconciler.redaction_policy, "reconciler")
        if dispatcher != expected:
            raise DefinitionRuntimeMismatch("dispatcher redaction differs from active kernel")
        if reconciler != expected:
            raise DefinitionRuntimeMismatch("reconciler redaction differs from active kernel")

    def _set_runtime_config(
        self, clock: Clock | None, lease_duration: timedelta, worker_id: str | None
    ) -> None:
        self._clock = clock or SystemClock()
        self._lease_duration = lease_duration
        self._worker_id = _unique_worker_id(worker_id)
        self._guards: dict[SagaId, _GuardEntry] = {}

    async def start(
        self,
        *,
        definition: SagaDefinition,
        goal: SagaGoal,
        agent: AgentDriver,
    ) -> SagaResult:
        _require_public_goal(goal, definition.redaction_policy)
        self._require_kernel_definition(definition)
        self._definitions.register(definition)
        saga_id = _saga_id(goal)
        async with self._guard(saga_id):
            self._create_or_validate(saga_id, definition, goal)
            return await self._drive(saga_id, definition, goal, agent)

    async def resume(self, *, saga_id: SagaId, agent: AgentDriver) -> SagaResult:
        async with self._guard(saga_id):
            self._store.rebuild_and_verify(saga_id)
            created = self._created_event(saga_id)
            definition = self._definitions.resolve(
                created.definition_name, created.definition_version
            )
            self._require_definition_fingerprint(created, definition)
            self._require_kernel_definition(definition)
            goal = SagaGoal.model_validate(thaw_json_object(created.redacted_goal), strict=True)
            _require_public_goal(goal, definition.redaction_policy)
            return await self._drive(saga_id, definition, goal, agent)

    @asynccontextmanager
    async def _guard(self, saga_id: SagaId) -> AsyncIterator[None]:
        entry = self._retain_guard(saga_id)
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            self._release_guard(saga_id, entry)

    def _retain_guard(self, saga_id: SagaId) -> _GuardEntry:
        entry = self._guards.get(saga_id)
        if entry is None:
            entry = _GuardEntry(asyncio.Lock())
            self._guards[saga_id] = entry
        entry.references += 1
        return entry

    def _release_guard(self, saga_id: SagaId, entry: _GuardEntry) -> None:
        entry.references -= 1
        if entry.references == 0:
            del self._guards[saga_id]

    def _require_kernel_definition(self, definition: SagaDefinition) -> None:
        if self._kernel.registry is not definition.registry:
            raise DefinitionRuntimeMismatch("active kernel registry differs from pinned definition")
        if self._kernel.policy is not definition.policy:
            raise DefinitionRuntimeMismatch("active kernel policy differs from pinned definition")
        if self._kernel.redaction_policy != definition.redaction_policy:
            message = "active kernel redaction differs from pinned definition"
            raise DefinitionRuntimeMismatch(message)

    @staticmethod
    def _require_definition_fingerprint(created: SagaCreated, definition: SagaDefinition) -> None:
        try:
            fingerprint = definition.fingerprint
        except DefinitionConflict as error:
            raise DefinitionRuntimeMismatch("runtime definition mutated after pinning") from error
        if created.definition_fingerprint != fingerprint:
            raise DefinitionRuntimeMismatch("pinned definition fingerprint differs from runtime")

    def _create_or_validate(
        self, saga_id: SagaId, definition: SagaDefinition, goal: SagaGoal
    ) -> None:
        event = self._creation_event(saga_id, definition, goal)
        try:
            self._store.create_saga(event)
        except StoreConflict:
            self._store.rebuild_and_verify(saga_id)
            if not _same_creation_identity(self._created_event(saga_id), event):
                message = "Saga identity was reused with changed goal or definition"
                raise ValueError(message) from None

    def _creation_event(
        self, saga_id: SagaId, definition: SagaDefinition, goal: SagaGoal
    ) -> SagaCreated:
        transition_id = _stable_id("txn", saga_id, "create")
        fields = _creation_base(saga_id, definition, transition_id, _safe_now(self._clock))
        redacted_goal = _public_json(goal.model_dump(mode="json"), definition.redaction_policy)
        return SagaCreated.model_validate(fields | {"redacted_goal": redacted_goal})

    def _created_event(self, saga_id: SagaId) -> SagaCreated:
        events = self._store.read_events(saga_id)
        if not events or not isinstance(events[0], SagaCreated):
            raise StoreConflict("Saga has no valid creation identity")
        return events[0]

    async def _drive(
        self,
        saga_id: SagaId,
        definition: SagaDefinition,
        goal: SagaGoal,
        agent: AgentDriver,
    ) -> SagaResult:
        dormant = self._store.load_snapshot(saga_id)
        if dormant.status in _AUTONOMOUS_STOPPED:
            return self._result(dormant)
        authority = self._acquire_authority(saga_id)
        context = _LoopContext(authority, definition, goal, agent)
        return await self._drive_active(saga_id, context)

    async def _drive_active(self, saga_id: SagaId, context: _LoopContext) -> SagaResult:
        try:
            self._ensure_started(saga_id, context.lease)
            result = await self._loop(saga_id, context)
        except BaseException as error:
            _release_preserving_error(context.authority, error)
            raise
        context.authority.release()
        return result

    def _acquire_authority(self, saga_id: SagaId) -> _LeaseAuthority:
        lease = self._leases.acquire(saga_id, self._worker_id, self._lease_duration)
        return _LeaseAuthority(self._leases, lease, self._lease_duration)

    def _ensure_started(self, saga_id: SagaId, lease: Lease) -> SagaSnapshot:
        snapshot = self._store.load_snapshot(saga_id)
        if snapshot.status is not SagaStatus.CREATED:
            return snapshot
        event = SagaStarted.model_validate(
            _event_base(snapshot, lease, "start", _safe_now(self._clock))
        )
        return self._commit(snapshot, lease, event, "start")

    async def _loop(self, saga_id: SagaId, context: _LoopContext) -> SagaResult:
        while True:
            stopped = await _await_with_authority(
                context.authority, lambda: self._cycle(saga_id, context)
            )
            if stopped is not None:
                return stopped

    async def _cycle(self, saga_id: SagaId, context: _LoopContext) -> SagaResult | None:
        snapshot = self._store.load_snapshot(saga_id)
        stopped = await self._pre_agent(snapshot, context)
        if stopped is not None or self._store.load_snapshot(saga_id).seq != snapshot.seq:
            return stopped
        return await self._agent_cycle(snapshot, context)

    async def _pre_agent(self, snapshot: SagaSnapshot, context: _LoopContext) -> SagaResult | None:
        stopped = await self._state_stop(snapshot, context)
        if stopped is not None:
            return stopped
        if self._last_event_is_reservation(snapshot.saga_id):
            return await self._unresolved_turn(snapshot, context)
        if self._last_event_is_read_start(snapshot.saga_id):
            await self._repeat_read(snapshot, context)
        if _has_pending_intent(snapshot):
            return await self._dispatch_pending(snapshot, context)
        return None

    async def _state_stop(self, snapshot: SagaSnapshot, context: _LoopContext) -> SagaResult | None:
        if snapshot.status in _AUTONOMOUS_STOPPED:
            return self._result(snapshot)
        if snapshot.status is SagaStatus.RECONCILING_UNKNOWN:
            return await self._reconciliation_stop(snapshot, context)
        return None

    async def _dispatch_pending(
        self, snapshot: SagaSnapshot, context: _LoopContext
    ) -> SagaResult | None:
        await _await_with_authority(
            context.authority,
            lambda: self._dispatcher.dispatch_saga(snapshot.saga_id, self._worker_id),
        )
        current = self._store.load_snapshot(snapshot.saga_id)
        if current.seq != snapshot.seq:
            return None
        return self._result(current)

    async def _reconciliation_stop(
        self, snapshot: SagaSnapshot, context: _LoopContext
    ) -> SagaResult | None:
        current = await self._reconcile(snapshot, context.authority)
        if current.status is SagaStatus.RECONCILING_UNKNOWN:
            return self._result(current)
        return None

    async def _unresolved_turn(self, snapshot: SagaSnapshot, context: _LoopContext) -> SagaResult:
        return await self._fail_and_unwind(
            snapshot,
            context,
            "agent_turn_unresolved",
        )

    async def _agent_cycle(
        self, snapshot: SagaSnapshot, context: _LoopContext
    ) -> SagaResult | None:
        events = self._store.read_events(snapshot.saga_id)
        accounting = _accounting(events, context.definition)
        stopped = await self._budget_stop(snapshot, context, accounting)
        if stopped is not None:
            return stopped
        return await self._reserved_cycle(snapshot, context, accounting)

    async def _budget_stop(
        self, snapshot: SagaSnapshot, context: _LoopContext, accounting: _BudgetState
    ) -> SagaResult | None:
        trigger = _stop_trigger(accounting, context.definition)
        if trigger is None:
            return None
        return await self._unwind(snapshot, context, trigger)

    async def _reserved_cycle(
        self, snapshot: SagaSnapshot, context: _LoopContext, accounting: _BudgetState
    ) -> SagaResult | None:
        controls = self._kernel.advertised_controls(snapshot, context.lease)
        current, reservation = self._reserve(snapshot, context, accounting)
        observation = self._observation(current, context.goal, context.definition, controls)
        answer = await _await_with_authority(
            context.authority,
            lambda: self._ask(context.agent, observation, context.definition, reservation),
        )
        if answer.proposal is None:
            return await self._agent_failed(current, context, reservation.turn_id, answer)
        return await self._apply_untrusted(current, context, answer.proposal, reservation.turn_id)

    async def _apply_untrusted(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        proposal: AgentProposal,
        turn_id: str,
    ) -> SagaResult | None:
        try:
            await self._apply(snapshot, context, proposal, turn_id)
        except ProposalIdentityConflict:
            return await self._fail_and_unwind(
                snapshot, context, "proposal_identity_reused", turn_id
            )
        return None

    async def _agent_failed(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        turn_id: str,
        answer: _AgentAnswer,
    ) -> SagaResult | None:
        reason = answer.failure_reason or "agent_internal"
        if reason == "agent_invalid_response":
            self._record_failure(snapshot, context.lease, turn_id, reason)
            return None
        return await self._fail_and_unwind(
            snapshot,
            context,
            reason,
            turn_id,
        )

    async def _ask(
        self,
        agent: AgentDriver,
        observation: SagaObservation,
        definition: SagaDefinition,
        reservation: AgentTurnReserved,
    ) -> _AgentAnswer:
        snapshot = self._store.load_snapshot(observation.saga_id)
        descriptors = _eligible_descriptors(definition, snapshot)
        try:
            async with asyncio.timeout(reservation.reserved_elapsed_ms / 1_000):
                raw = await agent.next_action(observation, descriptors)
        except TimeoutError:
            return _AgentAnswer(failure_reason="agent_deadline_exceeded")
        except Exception as error:
            return _AgentAnswer(failure_reason=_agent_failure_reason(error))
        return _validated_agent_answer(raw)

    async def _apply(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        proposal: AgentProposal,
        turn_id: str,
    ) -> SagaSnapshot:
        if isinstance(proposal, ToolCall):
            return await self._apply_tool(snapshot, context, proposal, turn_id)
        if isinstance(proposal, Finish):
            self._kernel.assign_terminal(snapshot.saga_id, proposal, context.lease)
        else:
            self._kernel.submit_proposal(snapshot.saga_id, proposal, context.lease)
        return self._store.load_snapshot(snapshot.saga_id)

    async def _apply_tool(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        proposal: ToolCall,
        turn_id: str,
    ) -> SagaSnapshot:
        try:
            tool = context.definition.registry.definition(proposal.tool_name)
        except UnknownToolError:
            return self._reject_tool_proposal(snapshot, context.lease, proposal)
        request = _ToolRequest(context.definition, proposal, turn_id)
        return await self._apply_registered_tool(snapshot, context, request, tool)

    async def _apply_registered_tool(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        request: _ToolRequest,
        tool: ReadToolDefinition[BaseModel, BaseModel] | EffectToolDefinition[BaseModel],
    ) -> SagaSnapshot:
        if isinstance(tool, ReadToolDefinition):
            return await self._read(snapshot, context, request, tool)
        return await self._effect(snapshot, context, request.proposal)

    def _reject_tool_proposal(
        self, snapshot: SagaSnapshot, lease: Lease, proposal: ToolCall
    ) -> SagaSnapshot:
        self._kernel.submit_proposal(snapshot.saga_id, proposal, lease)
        return self._store.load_snapshot(snapshot.saga_id)

    async def _effect(
        self, snapshot: SagaSnapshot, context: _LoopContext, proposal: ToolCall
    ) -> SagaSnapshot:
        result = self._kernel.submit_proposal(snapshot.saga_id, proposal, context.lease)
        if result.accepted:
            await _await_with_authority(
                context.authority,
                lambda: self._dispatcher.dispatch_saga(snapshot.saga_id, self._worker_id),
            )
        return self._store.load_snapshot(snapshot.saga_id)

    async def _read(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        request: _ToolRequest,
        definition: ReadToolDefinition[BaseModel, BaseModel],
    ) -> SagaSnapshot:
        authorization = self._kernel.authorize_read(
            snapshot.saga_id, request.proposal, context.lease
        )
        if not authorization.accepted:
            return self._store.load_snapshot(snapshot.saga_id)
        return await self._authorized_read(snapshot, context, request, definition)

    async def _authorized_read(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        request: _ToolRequest,
        definition: ReadToolDefinition[BaseModel, BaseModel],
    ) -> SagaSnapshot:
        try:
            command = _read_command(definition, request.proposal)
        except ValidationError:
            return self._reject_tool_proposal(snapshot, context.lease, request.proposal)
        snapshot, invocation = self._start_read(snapshot, context, request, definition, command)
        return await self._invoke_read(snapshot, context.authority, invocation)

    def _start_read(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        request: _ToolRequest,
        definition: ReadToolDefinition[BaseModel, BaseModel],
        command: BaseModel,
    ) -> tuple[SagaSnapshot, _ReadInvocation]:
        started = self._read_started(snapshot, context.lease, request, command)
        snapshot = self._commit(snapshot, context.lease, started, f"read-start:{request.turn_id}")
        return snapshot, self._read_invocation(snapshot, context, definition, command, started)

    def _read_invocation(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        definition: ReadToolDefinition[BaseModel, BaseModel],
        command: BaseModel,
        started: ReadStarted,
    ) -> _ReadInvocation:
        return _ReadInvocation(
            definition,
            command,
            started,
            context.definition.redaction_policy,
            self._read_timeout_ms(snapshot.saga_id, started.turn_id),
        )

    async def _invoke_read(
        self,
        snapshot: SagaSnapshot,
        authority: _LeaseAuthority,
        invocation: _ReadInvocation,
    ) -> SagaSnapshot:
        try:
            public = await _await_with_authority(authority, lambda: _bounded_read(invocation))
        except (LeaseLost, LeaseUnavailable):
            raise
        except Exception:
            if not authority.active:
                raise
            return self._commit_read_unavailable(snapshot, authority.lease, invocation.started)
        return self._commit_read_observed(snapshot, authority.lease, invocation.started, public)

    def _commit_read_unavailable(
        self, snapshot: SagaSnapshot, lease: Lease, started: ReadStarted
    ) -> SagaSnapshot:
        unavailable = self._read_unavailable(snapshot, lease, started)
        return self._commit(snapshot, lease, unavailable, f"read-unavailable:{started.turn_id}")

    def _commit_read_observed(
        self, snapshot: SagaSnapshot, lease: Lease, started: ReadStarted, public: JsonObject
    ) -> SagaSnapshot:
        observed = self._read_observed(snapshot, lease, started, public)
        return self._commit(snapshot, lease, observed, f"read-observed:{started.turn_id}")

    async def _repeat_read(self, snapshot: SagaSnapshot, context: _LoopContext) -> SagaSnapshot:
        started = cast(ReadStarted, self._store.read_events(snapshot.saga_id)[-1])
        tool = context.definition.registry.definition(started.tool_name)
        if not isinstance(tool, ReadToolDefinition):
            return self._record_failure(
                snapshot, context.lease, started.turn_id, "read_definition_unavailable"
            )
        command = tool.input_model.model_validate(
            thaw_json_object(started.redacted_command), strict=True
        )
        timeout_ms = self._read_timeout_ms(snapshot.saga_id, started.turn_id)
        invocation = _ReadInvocation(
            tool, command, started, context.definition.redaction_policy, timeout_ms
        )
        return await self._invoke_read(snapshot, context.authority, invocation)

    def _read_timeout_ms(self, saga_id: SagaId, turn_id: str) -> int:
        for event in reversed(self._store.read_events(saga_id)):
            if isinstance(event, AgentTurnReserved) and event.turn_id == turn_id:
                return event.reserved_elapsed_ms
        raise StoreConflict("read has no durable turn reservation")

    def _reserve(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        accounting: _BudgetState,
    ) -> tuple[SagaSnapshot, AgentTurnReserved]:
        turn_id = _stable_id("turn", snapshot.saga_id, str(snapshot.seq))
        values = _reservation_values(context.definition, accounting.remaining)
        event = AgentTurnReserved.model_validate(
            _event_base(snapshot, context.lease, f"reserve:{turn_id}", _safe_now(self._clock))
            | {"turn_id": turn_id, "turn_index": accounting.turn_count + 1}
            | values
        )
        return self._commit(snapshot, context.lease, event, f"reserve:{turn_id}"), event

    def _observation(
        self,
        snapshot: SagaSnapshot,
        goal: SagaGoal,
        definition: SagaDefinition,
        controls: ControlProposalCapabilities,
    ) -> SagaObservation:
        events = self._store.read_events(snapshot.saga_id)
        evidence = _read_evidence(
            events, definition.redaction_policy, definition.budget.tool_call_limit
        )
        return SagaObservation(
            saga_id=snapshot.saga_id,
            saga_seq=snapshot.seq,
            state=snapshot.status,
            goal=goal,
            last_action=_last_observation(events, definition.redaction_policy),
            projection=_public_json(snapshot.model_dump(mode="json"), definition.redaction_policy),
            remaining_budget=_accounting(events, definition).remaining,
            read_evidence=evidence,
            proposal_controls=controls,
        )

    async def _fail_and_unwind(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        reason: str,
        turn_id: str | None = None,
    ) -> SagaResult:
        last_event = self._store.read_events(snapshot.saga_id)[-1]
        durable_turn = turn_id or cast(AgentTurnReserved, last_event).turn_id
        snapshot = self._record_failure(snapshot, context.lease, durable_turn, reason)
        trigger = UnwindTrigger.AGENT_UNAVAILABLE
        if reason == "invalid_proposal_limit":
            trigger = UnwindTrigger.INVALID_PROPOSAL_LIMIT
        return await self._unwind(snapshot, context, trigger)

    async def _unwind(
        self,
        snapshot: SagaSnapshot,
        context: _LoopContext,
        trigger: UnwindTrigger,
    ) -> SagaResult:
        decision = self._unwinder.execute(
            snapshot.saga_id, context.definition.registry, trigger, context.lease
        )
        current = self._store.load_snapshot(snapshot.saga_id)
        current = await self._apply_unwind_decision(current, context, decision.action)
        if current.status not in _TERMINAL and current.status is not SagaStatus.HUMAN_REQUIRED:
            current = self._escalate_failure(current, context.lease, trigger.value)
        return self._result(current)

    async def _apply_unwind_decision(
        self, snapshot: SagaSnapshot, context: _LoopContext, action: UnwindAction
    ) -> SagaSnapshot:
        if action is UnwindAction.RECONCILE:
            return await self._reconcile(snapshot, context.authority)
        if action is UnwindAction.ABORT_CLEAN:
            return self._finish_abort(snapshot, context.lease)
        return snapshot

    async def _reconcile(self, snapshot: SagaSnapshot, authority: _LeaseAuthority) -> SagaSnapshot:
        before = snapshot.seq
        await _await_with_authority(
            authority, lambda: self._reconciler.reconcile_one(self._worker_id)
        )
        current = self._store.load_snapshot(snapshot.saga_id)
        if current.seq == before:
            return current
        return current

    def _finish_abort(self, snapshot: SagaSnapshot, lease: Lease) -> SagaSnapshot:
        proposal = Finish(
            proposal_id=_stable_id("proposal", snapshot.saga_id, str(snapshot.seq), "abort"),
            based_on_saga_seq=snapshot.seq,
            rationale="Durable unwind evidence permits a clean abort.",
            target_status="aborted_clean",
        )
        self._kernel.assign_terminal(snapshot.saga_id, proposal, lease)
        return self._store.load_snapshot(snapshot.saga_id)

    def _escalate_failure(self, snapshot: SagaSnapshot, lease: Lease, reason: str) -> SagaSnapshot:
        proposal = Escalate(
            proposal_id=_stable_id("proposal", snapshot.saga_id, str(snapshot.seq), "human"),
            based_on_saga_seq=snapshot.seq,
            reason_code=f"runtime_{reason}",
            rationale="Autonomous runtime cannot safely continue.",
        )
        self._kernel.submit_proposal(snapshot.saga_id, proposal, lease)
        return self._store.load_snapshot(snapshot.saga_id)

    def _record_failure(
        self, snapshot: SagaSnapshot, lease: Lease, turn_id: str, reason: str
    ) -> SagaSnapshot:
        event = AgentTurnFailed.model_validate(
            _event_base(snapshot, lease, f"failure:{turn_id}", _safe_now(self._clock))
            | {"turn_id": turn_id, "reason_code": reason}
        )
        return self._commit(snapshot, lease, event, f"failure:{turn_id}")

    def _commit(
        self, snapshot: SagaSnapshot, lease: Lease, event: LedgerEvent, phase: str
    ) -> SagaSnapshot:
        batch = _batch(snapshot, lease, event, phase)
        try:
            return self._store.commit_transition(batch)
        except InjectedStoreFailure:
            receipt = self._store.lookup_transition_receipt(batch.transition_id)
            if receipt is None:
                raise
            return receipt.projection

    def _read_started(
        self,
        snapshot: SagaSnapshot,
        lease: Lease,
        request: _ToolRequest,
        command: BaseModel,
    ) -> ReadStarted:
        phase = f"read-start:{request.turn_id}"
        fields = _event_base(snapshot, lease, phase, _safe_now(self._clock))
        public = _public_json(command.model_dump(mode="json"), request.definition.redaction_policy)
        payload = _read_start_payload(request.proposal, request.turn_id, public)
        return ReadStarted.model_validate(fields | payload)

    def _read_observed(
        self, snapshot: SagaSnapshot, lease: Lease, started: ReadStarted, result: JsonObject
    ) -> ReadObserved:
        fields = _event_base(
            snapshot,
            lease,
            f"read-observed:{started.turn_id}",
            _safe_now(self._clock),
        )
        payload = _read_observed_payload(started, result)
        return ReadObserved.model_validate(fields | payload)

    def _read_unavailable(
        self, snapshot: SagaSnapshot, lease: Lease, started: ReadStarted
    ) -> ReadUnavailable:
        fields = _event_base(
            snapshot,
            lease,
            f"read-unavailable:{started.turn_id}",
            _safe_now(self._clock),
        )
        return ReadUnavailable.model_validate(fields | _read_unavailable_payload(started))

    def _result(self, snapshot: SagaSnapshot) -> SagaResult:
        reason = _human_reason(self._store.read_events(snapshot.saga_id))
        quiescent = snapshot.status in _TERMINAL or snapshot.status is SagaStatus.HUMAN_REQUIRED
        return SagaResult(
            saga_id=snapshot.saga_id,
            state=snapshot.status,
            saga_seq=snapshot.seq,
            autonomous_quiescent=quiescent,
            human_required_reason=reason,
        )

    def _last_event_is_reservation(self, saga_id: SagaId) -> bool:
        return isinstance(self._store.read_events(saga_id)[-1], AgentTurnReserved)

    def _last_event_is_read_start(self, saga_id: SagaId) -> bool:
        return isinstance(self._store.read_events(saga_id)[-1], ReadStarted)


def _batch(snapshot: SagaSnapshot, lease: Lease, event: LedgerEvent, phase: str) -> TransitionBatch:
    return TransitionBatch(
        transition_id=_stable_id("txn", snapshot.saga_id, str(snapshot.seq), phase),
        saga_id=snapshot.saga_id,
        expected_seq=snapshot.seq,
        expected_fence_token=lease.fence_token,
        lease_owner=lease.owner,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )


def _reservation_amount(limit: int, turns: int) -> int:
    if limit <= 0 or turns <= 0:
        return 0
    return limit // turns


def _reservation_values(definition: SagaDefinition, remaining: ExecutionBudget) -> dict[str, int]:
    budget = definition.budget
    return {
        "reserved_elapsed_ms": _capped_reservation(
            budget.elapsed_ms_limit, budget.turn_limit, remaining.elapsed_ms_limit
        ),
        "reserved_tokens": _capped_reservation(
            budget.token_limit, budget.turn_limit, remaining.token_limit
        ),
    }


def _capped_reservation(limit: int, turns: int, remaining: int) -> int:
    return min(_reservation_amount(limit, turns), remaining)


async def _invoke_read_adapter(
    definition: ReadToolDefinition[BaseModel, BaseModel],
    command: BaseModel,
    policy: RedactionPolicy,
) -> JsonObject:
    raw = await definition.adapter.read(command)
    result = definition.result_model.model_validate(raw, strict=True)
    return _public_json(result.model_dump(mode="json"), policy)


async def _bounded_read(invocation: _ReadInvocation) -> JsonObject:
    async with asyncio.timeout(invocation.timeout_ms / 1_000):
        return await _invoke_read_adapter(invocation.tool, invocation.command, invocation.policy)


def _read_command(
    definition: ReadToolDefinition[BaseModel, BaseModel], proposal: ToolCall
) -> BaseModel:
    arguments = thaw_json_object(proposal.arguments)
    return definition.input_model.model_validate(arguments, strict=True)


def _eligible_descriptors(
    definition: SagaDefinition, snapshot: SagaSnapshot
) -> tuple[ToolDescriptor, ...]:
    candidates = (
        _policy_descriptor(definition, snapshot, tool) for tool in definition.registry.definitions()
    )
    return tuple(item for item in candidates if item is not None)


def _policy_descriptor(
    definition: SagaDefinition,
    snapshot: SagaSnapshot,
    tool: ReadToolDefinition[BaseModel, BaseModel] | EffectToolDefinition[BaseModel],
) -> ToolDescriptor | None:
    constraints = definition.policy.advertised_constraints(tool.name, snapshot)
    if constraints is None:
        return None
    return ToolDescriptor.from_definition(tool).model_copy(
        update={"policy_constraints": _public_json(constraints, definition.redaction_policy)}
    )


def _read_start_payload(proposal: ToolCall, turn_id: str, command: JsonObject) -> dict[str, object]:
    return {
        "turn_id": turn_id,
        "proposal_id": proposal.proposal_id,
        "tool_name": proposal.tool_name,
        "redacted_command": command,
        "command_hash": sha256_json(command),
    }


def _read_observed_payload(started: ReadStarted, result: JsonObject) -> dict[str, object]:
    return {
        "turn_id": started.turn_id,
        "proposal_id": started.proposal_id,
        "tool_name": started.tool_name,
        "redacted_result": result,
        "result_hash": sha256_json(result),
    }


def _read_unavailable_payload(started: ReadStarted) -> dict[str, object]:
    return {
        "turn_id": started.turn_id,
        "proposal_id": started.proposal_id,
        "tool_name": started.tool_name,
        "reason_code": "read_unavailable",
    }


def _accounting(events: Sequence[LedgerEvent], definition: SagaDefinition) -> _BudgetState:
    reservations = tuple(item for item in events if isinstance(item, AgentTurnReserved))
    tool_events = (EffectIntentRecorded, CompensationIntentRecorded, ReadStarted)
    tool_calls = sum(isinstance(item, tool_events) for item in events)
    invalid_events = (ProposalRejected, TerminalDenied, AgentTurnFailed)
    invalid = sum(isinstance(item, invalid_events) for item in events)
    remaining = _remaining_budget(definition.budget, reservations, tool_calls)
    return _BudgetState(remaining, len(reservations), invalid)


def _remaining_budget(
    budget: ExecutionBudget,
    reservations: tuple[AgentTurnReserved, ...],
    tool_calls: int,
) -> ExecutionBudget:
    elapsed_used, tokens_used = _reserved_usage(reservations)
    return budget.model_copy(
        update={
            "turn_limit": max(0, budget.turn_limit - len(reservations)),
            "tool_call_limit": max(0, budget.tool_call_limit - tool_calls),
            "elapsed_ms_limit": max(0, budget.elapsed_ms_limit - elapsed_used),
            "token_limit": max(0, budget.token_limit - tokens_used),
        }
    )


def _reserved_usage(reservations: tuple[AgentTurnReserved, ...]) -> tuple[int, int]:
    elapsed = sum(item.reserved_elapsed_ms for item in reservations)
    tokens = sum(item.reserved_tokens for item in reservations)
    return elapsed, tokens


def _stop_trigger(accounting: _BudgetState, definition: SagaDefinition) -> UnwindTrigger | None:
    if accounting.invalid_count >= _INVALID_LIMIT:
        return UnwindTrigger.INVALID_PROPOSAL_LIMIT
    if _budget_exhausted(accounting, definition):
        return UnwindTrigger.BUDGET_EXHAUSTED
    return None


def _budget_exhausted(accounting: _BudgetState, definition: SagaDefinition) -> bool:
    values = accounting.remaining
    return (
        accounting.turn_count >= definition.budget.turn_limit
        or min(
            values.tool_call_limit,
            values.elapsed_ms_limit,
            values.token_limit,
        )
        <= 0
    )


def _same_creation_identity(left: SagaCreated, right: SagaCreated) -> bool:
    return (
        left.saga_id == right.saga_id
        and left.definition_name == right.definition_name
        and left.definition_version == right.definition_version
        and left.definition_fingerprint == right.definition_fingerprint
        and left.redacted_goal == right.redacted_goal
    )


def _has_pending_intent(snapshot: SagaSnapshot) -> bool:
    return any(
        operation.status is OperationStatus.INTENT_DURABLE
        for operation in snapshot.operations.values()
    )


def _last_observation(events: Sequence[LedgerEvent], policy: RedactionPolicy) -> JsonObject | None:
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if not isinstance(event, _OBSERVATION_EVENTS):
            continue
        public = _public_json(event.model_dump(mode="json"), policy)
        if not isinstance(event, TerminalDenied):
            return public
        return _with_invariant_evidence(events, index, event, public, policy)
    return None


def _with_invariant_evidence(
    events: Sequence[LedgerEvent],
    index: int,
    denied: TerminalDenied,
    public: JsonObject,
    policy: RedactionPolicy,
) -> JsonObject:
    proof = _matching_invariant(events, index, denied)
    if proof is None:
        return public
    payload = thaw_json_object(public)
    payload["invariant_evidence"] = thaw_json_object(_invariant_summary(proof, policy))
    return _public_json(payload, policy)


def _matching_invariant(
    events: Sequence[LedgerEvent], index: int, denied: TerminalDenied
) -> InvariantEvaluated | None:
    if index == 0:
        return None
    proof = events[index - 1]
    if not isinstance(proof, InvariantEvaluated):
        return None
    if proof.saga_seq + 1 != denied.saga_seq or proof.target_status != denied.target_status:
        return None
    return proof


def _invariant_summary(proof: InvariantEvaluated, policy: RedactionPolicy) -> JsonObject:
    values = {
        "target_status": proof.target_status,
        "evaluated_at_seq": proof.evaluated_at_seq,
        "invariant_version": proof.invariant_version,
        "results": thaw_json_object(proof.results),
        "all_passed": proof.all_passed,
    }
    return _public_json(values, policy)


def _read_evidence(
    events: Sequence[LedgerEvent], policy: RedactionPolicy, limit: int
) -> tuple[ReadEvidence, ...]:
    starts: dict[tuple[str, str, str], ReadStarted] = {}
    completed: list[ReadEvidence] = []
    latest_change_seq = _latest_external_change_seq(events)
    for event in events:
        if isinstance(event, ReadStarted):
            starts[_read_key(event)] = event
        elif isinstance(event, (ReadObserved, ReadUnavailable)):
            completed.append(
                _completed_read_evidence(starts[_read_key(event)], event, policy, latest_change_seq)
            )
    return tuple(completed[-limit:]) if limit > 0 else ()


def _latest_external_change_seq(events: Sequence[LedgerEvent]) -> int:
    return max(
        (event.saga_seq for event in events if isinstance(event, _EVIDENCE_INVALIDATING_EVENTS)),
        default=0,
    )


def _completed_read_evidence(
    started: ReadStarted,
    outcome: ReadObserved | ReadUnavailable,
    policy: RedactionPolicy,
    latest_change_seq: int,
) -> ReadEvidence:
    values: dict[str, object] = {
        "tool_name": outcome.tool_name,
        "command": _public_json(started.redacted_command, policy),
        "observed_at_saga_seq": outcome.saga_seq,
        "freshness": "stale" if latest_change_seq > outcome.saga_seq else "fresh",
    }
    if isinstance(outcome, ReadObserved):
        values["result"] = _public_json(outcome.redacted_result, policy)
    else:
        values["unavailable_reason"] = outcome.reason_code
    return ReadEvidence.model_validate(values, strict=True)


def _read_key(event: ReadStarted | ReadObserved | ReadUnavailable) -> tuple[str, str, str]:
    return event.turn_id, event.proposal_id, event.tool_name


def _human_reason(events: Sequence[LedgerEvent]) -> str | None:
    for event in reversed(events):
        if isinstance(event, HumanRequired):
            return event.reason_code
    return None


__all__ = ["DefinitionRuntimeMismatch", "SagaRuntime"]
