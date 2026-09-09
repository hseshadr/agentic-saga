from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agentic_saga.contracts.actions import AgentProposal, Finish, HumanDecision, ToolCall
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    OperationId,
    Reversibility,
    SagaId,
    sha256_json,
    thaw_json_object,
)
from agentic_saga.contracts.events import EffectIntentRecorded, SagaCreated, SagaStarted
from agentic_saga.contracts.runtime import ExecutionBudget, SagaStatus, TerminalRequirement
from agentic_saga.contracts.tools import (
    EffectAdapter,
    EffectContext,
    EffectToolDefinition,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.execution.dispatcher import Dispatcher
from agentic_saga.execution.leases import LeaseService
from agentic_saga.execution.reconciliation import Reconciler
from agentic_saga.execution.unwind import EmergencyUnwinder, UnwindAction, UnwindTrigger
from agentic_saga.kernel.failpoints import DurabilityFailpoint, DurabilityPoint
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.invariants import (
    InvariantEvidence,
    InvariantResult,
    TerminalGate,
)
from agentic_saga.kernel.policy import (
    PolicyContext,
    PolicyEngine,
    PolicyRules,
    ProposedEffectIdentity,
)
from agentic_saga.kernel.ports import (
    EffectCapabilityProof,
    Lease,
    OutboxCommand,
    StoreFailpoint,
    TransitionBatch,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.runtime import (
    EventMetadataFactory,
    InvariantEvidenceProvider,
    PolicyContextProvider,
    SagaKernel,
    StableIdFactory,
)
from agentic_saga.kernel.state import OperationRecord, OperationStatus, SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.support.durable_tool import DurableFakeTool, DurableToolEffect

SAGA_ID: SagaId = "saga_0000000000008001"
STEP_ID = "step_00008001"
NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
TOOL_NAME = "durable_action"
REPAIR_TOOL_NAME = "durable_reversal"
DEFINITION_VERSION = "durable-action-v1"
COMMAND_SCHEMA_VERSION = "action-command-v1"
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


class CrashFailpoint(Protocol):
    def hit(self, point: DurabilityPoint | StoreFailpoint) -> None: ...


class HarnessScope(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    zones: list[str]


class HarnessCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    quantity: int = Field(strict=True, gt=0)
    mode: Literal["standard"]
    secret_ref: str
    target_ids: list[int]
    scope: HarnessScope


def _allow(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return True


def _deny(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return False


def _not_duplicate(
    identity: ProposedEffectIdentity, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    del identity, snapshot, context
    return False


def _verify(
    decision: HumanDecision,
    proposal: ToolCall,
    snapshot: SagaSnapshot,
    context: PolicyContext,
) -> bool:
    del decision, proposal, snapshot, context
    return False


def _verify_human(decision: HumanDecision, snapshot: SagaSnapshot) -> bool:
    del snapshot
    return decision.auth_proof == "verified-human-proof"


def _budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=10,
        tool_call_limit=10,
        elapsed_ms_limit=10_000,
        token_limit=10_000,
    )


@dataclass(frozen=True)
class HarnessContexts(PolicyContextProvider):
    def build(self, snapshot: SagaSnapshot, proposal: AgentProposal, lease: Lease) -> PolicyContext:
        del snapshot
        return _harness_context(proposal, lease)


@dataclass
class CrashContexts(PolicyContextProvider):
    compensation_target: OperationId | None = None

    def build(self, snapshot: SagaSnapshot, proposal: AgentProposal, lease: Lease) -> PolicyContext:
        del snapshot
        return _crash_context(proposal, lease, self.compensation_target)


def _harness_context(proposal: AgentProposal, lease: Lease) -> PolicyContext:
    step_id = "step_00008002" if proposal.proposal_id == "proposal_00008002" else STEP_ID
    return PolicyContext(
        step_instance_id=step_id,
        direction=Direction.FORWARD,
        semantic_generation=0,
        fence_token=lease.fence_token,
        budget=_budget(),
        turns_used=1,
        tool_calls_used=1,
        elapsed_ms=1,
        tokens_used=1,
        policy_evidence={"source": "harness"},
        resource_identity={"resource_id": "resource_8"},
    )


def _crash_context(
    proposal: AgentProposal, lease: Lease, target: OperationId | None
) -> PolicyContext:
    context = _harness_context(proposal, lease)
    if target is None:
        return context
    return context.model_copy(
        update={
            "direction": Direction.COMPENSATION,
            "step_instance_id": STEP_ID,
            "compensates_operation_id": target,
        }
    )


def _capabilities(provider: DurableFakeTool) -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400 if provider.deduplication_enabled else None,
        reconciliation_supported=provider.reconciliation_enabled,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=False,
    )


def _definition(
    provider: DurableFakeTool,
    capabilities: ToolCapabilities | None = None,
    *,
    adapter: EffectAdapter[HarnessCommand] | None = None,
    name: str = TOOL_NAME,
    compensate_with: str | None = None,
) -> EffectToolDefinition[HarnessCommand]:
    version = DEFINITION_VERSION if name == TOOL_NAME else f"{name}-v1"
    selected = capabilities or _capabilities(provider)
    return _make_definition(adapter or provider, selected, name, compensate_with, version)


def _make_definition(
    adapter: EffectAdapter[HarnessCommand],
    capabilities: ToolCapabilities,
    name: str,
    compensate_with: str | None,
    version: str,
) -> EffectToolDefinition[HarnessCommand]:
    return EffectToolDefinition(
        name,
        version,
        COMMAND_SCHEMA_VERSION,
        HarnessCommand,
        adapter,
        capabilities,
        compensate_with,
    )


def _rules() -> PolicyRules:
    return PolicyRules(
        is_duplicate_effect=_not_duplicate,
        approval_required=_deny,
        approval_verifier=_verify,
    )


def _deny_human(decision: HumanDecision, snapshot: SagaSnapshot) -> bool:
    del decision, snapshot
    return False


def _created() -> SagaCreated:
    return SagaCreated(
        event_id="evt_0000000000008001",
        saga_id=SAGA_ID,
        saga_seq=1,
        definition_version="harness-saga-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000008001",
        recorded_at=NOW,
        definition_name="harness",
        definition_fingerprint="f" * 64,
        redacted_goal={"resource_id": "resource_8"},
    )


def _started(lease: Lease) -> SagaStarted:
    return SagaStarted(
        event_id="evt_0000000000008002",
        saga_id=SAGA_ID,
        saga_seq=2,
        definition_version="harness-saga-v1",
        fence_token=lease.fence_token,
        actor="kernel",
        trace_id="trace_0000000000008001",
        recorded_at=NOW,
    )


def _start_batch(snapshot: SagaSnapshot, lease: Lease, event: SagaStarted) -> TransitionBatch:
    return TransitionBatch(
        transition_id="txn_0000000000008001",
        saga_id=SAGA_ID,
        expected_seq=snapshot.seq,
        expected_fence_token=lease.fence_token,
        lease_owner=lease.owner,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )


def _proposal_arguments() -> JsonObject:
    return _JSON_OBJECT_ADAPTER.validate_python(
        {
            "resource_id": "resource_8",
            "quantity": 10_000,
            "mode": "standard",
            "secret_ref": "vault://services/credential/v1",
            "target_ids": [1, 2],
            "scope": {"zones": ["primary", "secondary"]},
        }
    )


def _proposal() -> ToolCall:
    return ToolCall(
        proposal_id="proposal_00008001",
        tool_name=TOOL_NAME,
        arguments=_proposal_arguments(),
        based_on_saga_seq=2,
        rationale="Apply the durable harness action.",
    )


@dataclass(frozen=True)
class _HarnessInfrastructure:
    clock: FakeClock
    store: SQLiteKernelStore
    provider: DurableFakeTool
    registry: ToolRegistry
    lease: Lease
    kernel: SagaKernel


def _infrastructure(
    directory: Path,
    capabilities: ToolCapabilities | None = None,
    adapter: EffectAdapter[HarnessCommand] | None = None,
    compensate_with: str | None = None,
    human_enabled: bool = False,
) -> _HarnessInfrastructure:
    clock = FakeClock(NOW)
    human_verifier = _verify_human if human_enabled else _deny_human
    store = SQLiteKernelStore.initialize(
        directory / "saga.db", clock=clock, human_resolution_verifier=human_verifier
    )
    provider = DurableFakeTool.initialize(directory / "provider.db", TOOL_NAME)
    if capabilities is not None:
        provider.set_fencing_supported(capabilities.fencing_supported)
    forward = _definition(provider, capabilities, adapter=adapter, compensate_with=compensate_with)
    repairs = () if compensate_with is None else (_definition(provider, name=compensate_with),)
    registry = ToolRegistry((forward, *repairs))
    lease = _start(store)
    kernel = _kernel(store, registry, clock)
    return _HarnessInfrastructure(clock, store, provider, registry, lease, kernel)


def _operation_id(value: OperationId | None) -> OperationId:
    if value is None:
        raise AssertionError("harness proposal did not create an operation")
    return value


@dataclass
class KernelHarness:
    store: SQLiteKernelStore
    clock: FakeClock
    registry: ToolRegistry
    lease: Lease
    kernel: SagaKernel
    dispatcher: Dispatcher
    provider: DurableFakeTool
    command: HarnessCommand
    operation_id: OperationId

    @classmethod
    def create(cls, directory: Path) -> KernelHarness:
        parts = _infrastructure(directory)
        result = parts.kernel.submit_proposal(SAGA_ID, _proposal(), parts.lease)
        return cls._from_parts(parts, _operation_id(result.operation_id))

    @classmethod
    def create_with_capabilities(
        cls,
        directory: Path,
        capabilities: ToolCapabilities,
        adapter: EffectAdapter[HarnessCommand] | None = None,
    ) -> KernelHarness:
        parts = _infrastructure(directory, capabilities, adapter)
        result = parts.kernel.submit_proposal(SAGA_ID, _proposal(), parts.lease)
        return cls._from_parts(parts, _operation_id(result.operation_id))

    @classmethod
    def create_reversible(cls, directory: Path) -> KernelHarness:
        parts = _infrastructure(directory, compensate_with=REPAIR_TOOL_NAME)
        result = parts.kernel.submit_proposal(SAGA_ID, _proposal(), parts.lease)
        return cls._from_parts(parts, _operation_id(result.operation_id))

    @classmethod
    def create_reversible_with_capabilities(
        cls, directory: Path, capabilities: ToolCapabilities
    ) -> KernelHarness:
        parts = _infrastructure(directory, capabilities, compensate_with=REPAIR_TOOL_NAME)
        result = parts.kernel.submit_proposal(SAGA_ID, _proposal(), parts.lease)
        return cls._from_parts(parts, _operation_id(result.operation_id))

    @classmethod
    def create_human_enabled(cls, directory: Path) -> KernelHarness:
        parts = _infrastructure(directory, human_enabled=True)
        result = parts.kernel.submit_proposal(SAGA_ID, _proposal(), parts.lease)
        return cls._from_parts(parts, _operation_id(result.operation_id))

    @classmethod
    def _from_parts(cls, parts: _HarnessInfrastructure, operation_id: OperationId) -> KernelHarness:
        public_command = thaw_json_object(_proposal().arguments)
        command = HarnessCommand.model_validate(public_command, strict=True)
        dispatcher = Dispatcher(parts.store, parts.registry, clock=parts.clock)
        return cls(
            parts.store,
            parts.clock,
            parts.registry,
            parts.lease,
            parts.kernel,
            dispatcher,
            parts.provider,
            command,
            operation_id,
        )

    async def redeliver_same_operation(self, attempt: int) -> None:
        context = EffectContext(
            saga_id=SAGA_ID,
            step_instance_id=STEP_ID,
            operation_id=self.operation_id,
            fence_token=self.lease.fence_token,
            delivery_attempt=attempt,
        )
        await self.provider.execute(self.command, context)

    async def dispatch_with_response_loss(self) -> None:
        self.provider.enable_response_loss_after_effect()
        await self.dispatcher.dispatch_one(self.lease.owner)
        self.provider.disable_response_loss()

    async def restart_and_reconcile(self) -> None:
        self.store = SQLiteKernelStore.open(self.store.path, clock=self.clock)
        self.provider = DurableFakeTool.open(self.provider.path, TOOL_NAME)
        self.registry = ToolRegistry((_definition(self.provider),))
        self.dispatcher = Dispatcher(self.store, self.registry, clock=self.clock)
        reconciler = Reconciler(self.store, self.registry, clock=self.clock)
        await reconciler.reconcile_one(self.lease.owner)

    def add_runnable_operation(self) -> str:
        snapshot = self.store.load_snapshot(self.lease.saga_id)
        operation_id = OperationIdentityFactory(b"task-8-harness").create(
            snapshot.saga_id, "step_00008002", Direction.FORWARD, 0
        )
        command_id = StableIdFactory(b"task-8-harness").command_id(operation_id)
        command = _proposal_arguments()
        event = self._additional_effect(snapshot, operation_id, command)
        outbox = self._additional_outbox(operation_id, command_id, command)
        self.store.commit_transition(self._additional_batch(snapshot, event, outbox))
        return command_id

    def _additional_batch(
        self, snapshot: SagaSnapshot, event: EffectIntentRecorded, outbox: OutboxCommand
    ) -> TransitionBatch:
        return TransitionBatch(
            transition_id="txn_0000000000008002",
            saga_id=snapshot.saga_id,
            expected_seq=snapshot.seq,
            expected_fence_token=self.lease.fence_token,
            lease_owner=self.lease.owner,
            events=(event,),
            projection=reduce_event(snapshot, event),
            outbox_commands=(outbox,),
        )

    def _additional_effect(
        self, snapshot: SagaSnapshot, operation_id: OperationId, command: JsonObject
    ) -> EffectIntentRecorded:
        fields = self._additional_effect_fields(snapshot, operation_id, command)
        return EffectIntentRecorded.model_validate(fields | {"compensate_with": None})

    def _additional_effect_fields(
        self, snapshot: SagaSnapshot, operation_id: OperationId, command: JsonObject
    ) -> dict[str, object]:
        return self._additional_event_base(snapshot) | {
            "operation_id": operation_id,
            "step_instance_id": "step_00008002",
            "direction": Direction.FORWARD,
            "semantic_generation": 0,
            "delivery_attempt": 1,
            "tool_name": TOOL_NAME,
            "redacted_command": command,
            "command_hash": sha256_json(command),
        }

    def _additional_event_base(self, snapshot: SagaSnapshot) -> dict[str, object]:
        return {
            "event_id": "evt_0000000000008003",
            "saga_id": snapshot.saga_id,
            "saga_seq": snapshot.seq + 1,
            "definition_version": snapshot.definition_version,
            "fence_token": self.lease.fence_token,
            "actor": "kernel",
            "trace_id": "trace_0000000000008002",
            "recorded_at": self.clock.now(),
        }

    def _additional_outbox(
        self, operation_id: OperationId, command_id: str, command: JsonObject
    ) -> OutboxCommand:
        return OutboxCommand.model_validate(
            self._additional_outbox_fields(operation_id, command_id, command)
        )

    def _additional_outbox_fields(
        self, operation_id: OperationId, command_id: str, command: JsonObject
    ) -> dict[str, object]:
        identity = self._additional_outbox_identity(operation_id, command_id)
        definition = self.registry.definition(TOOL_NAME)
        assert isinstance(definition, EffectToolDefinition)
        capabilities = definition.capabilities
        return identity | {
            "command": command,
            "command_hash": sha256_json(command),
            "available_at": self.clock.now(),
            "capability_proof": EffectCapabilityProof(
                capabilities=capabilities,
                capability_digest=sha256_json(capabilities.model_dump(mode="json")),
            ),
        }

    def _additional_outbox_identity(
        self, operation_id: OperationId, command_id: str
    ) -> dict[str, object]:
        return {
            "command_id": command_id,
            "saga_id": self.lease.saga_id,
            "operation_id": operation_id,
            "tool_name": TOOL_NAME,
            "definition_version": DEFINITION_VERSION,
            "command_schema_version": COMMAND_SCHEMA_VERSION,
            "step_instance_id": "step_00008002",
            "direction": Direction.FORWARD,
            "semantic_generation": 0,
        }


_EFFECT_STATUSES = frozenset(
    {OperationStatus.EFFECT_CONFIRMED, OperationStatus.PARTIAL_EFFECT_CONFIRMED}
)


@dataclass(frozen=True)
class _CrashEvidenceProvider(InvariantEvidenceProvider):
    forward_provider: DurableFakeTool
    repair_provider: DurableFakeTool

    def evaluate(
        self, saga_id: str, snapshot: SagaSnapshot, target_status: SagaStatus
    ) -> InvariantEvidence:
        actual = (self.forward_provider.effects, self.repair_provider.effects)
        expected = _expected_effects(snapshot)
        result = _recovery_result(actual, expected)
        return InvariantEvidence(
            saga_id=saga_id,
            definition_version=snapshot.definition_version,
            evaluated_at_seq=snapshot.seq,
            target_status=target_status,
            invariant_version="crash-recovery-v1",
            results=(result,),
        )


def _expected_effects(
    snapshot: SagaSnapshot,
) -> tuple[tuple[DurableToolEffect, ...], tuple[DurableToolEffect, ...]]:
    return (
        _ledger_effects(snapshot, Direction.FORWARD),
        _ledger_effects(snapshot, Direction.COMPENSATION),
    )


def _ledger_effects(snapshot: SagaSnapshot, direction: Direction) -> tuple[DurableToolEffect, ...]:
    operations = (
        item
        for item in snapshot.operations.values()
        if item.direction is direction and item.status in _EFFECT_STATUSES
    )
    return tuple(sorted(_durable_effect(item) for item in operations))


def _durable_effect(operation: OperationRecord) -> DurableToolEffect:
    return DurableToolEffect(
        operation.tool_name,
        operation.operation_id,
        operation.command_hash,
        _provider_receipt(operation),
    )


def _provider_receipt(operation: OperationRecord) -> str:
    if len(operation.receipts) != 1:
        return "ledger_receipt_shape_mismatch"
    value = operation.receipts[0].get("provider_receipt")
    return value if isinstance(value, str) else "ledger_receipt_shape_mismatch"


def _recovery_result(
    actual: tuple[tuple[DurableToolEffect, ...], tuple[DurableToolEffect, ...]],
    expected: tuple[tuple[DurableToolEffect, ...], tuple[DurableToolEffect, ...]],
) -> InvariantResult:
    inputs = _JSON_OBJECT_ADAPTER.validate_python(
        {
            "actual_effect_count": [len(items) for items in actual],
            "ledger_effect_count": [len(items) for items in expected],
        },
        strict=True,
    )
    return InvariantResult(
        rule_id="durable_recovery_verified",
        passed=actual == expected,
        inputs=inputs,
        explanation="Durable provider effects match the recovered ledger.",
    )


def _crash_gate() -> TerminalGate:
    requirement = TerminalRequirement(
        invariant_version="crash-recovery-v1",
        required_rule_ids=("durable_recovery_verified",),
    )
    targets = (
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    )
    return TerminalGate({target: requirement for target in targets})


def _crash_registry(
    forward_provider: DurableFakeTool, repair_provider: DurableFakeTool
) -> ToolRegistry:
    return ToolRegistry(
        (
            _definition(forward_provider, compensate_with=REPAIR_TOOL_NAME),
            _definition(repair_provider, name=REPAIR_TOOL_NAME),
        )
    )


def _crash_kernel(  # noqa: PLR0913, PLR0917
    store: SQLiteKernelStore,
    registry: ToolRegistry,
    contexts: CrashContexts,
    clock: FakeClock,
    failpoint: DurabilityFailpoint,
    evidence_provider: InvariantEvidenceProvider,
) -> SagaKernel:
    return SagaKernel(
        store=store,
        policy=_policy(registry),
        registry=registry,
        policy_context_provider=contexts,
        event_metadata_factory=EventMetadataFactory(clock),
        id_factory=StableIdFactory(namespace=b"task-8-harness"),
        terminal_gate=_crash_gate(),
        invariant_evidence_provider=evidence_provider,
        failpoint=failpoint,
    )


@dataclass
class CrashKernelHarness:
    store: SQLiteKernelStore
    clock: FakeClock
    registry: ToolRegistry
    lease: Lease
    kernel: SagaKernel
    dispatcher: Dispatcher
    forward_provider: DurableFakeTool
    repair_provider: DurableFakeTool
    contexts: CrashContexts

    @classmethod
    def initialize(cls, directory: Path, failpoint: CrashFailpoint) -> CrashKernelHarness:
        clock = FakeClock(NOW)
        store = SQLiteKernelStore.initialize(
            directory / "saga.db",
            failpoint=failpoint,
            clock=clock,
            durability_failpoint=failpoint,
        )
        forward = DurableFakeTool.open(directory / "forward.db", TOOL_NAME, failpoint)
        repair = DurableFakeTool.open(directory / "repair.db", REPAIR_TOOL_NAME, failpoint)
        registry = _crash_registry(forward, repair)
        lease = _start(store)
        return cls._build(store, forward, repair, registry, lease, clock, failpoint)

    @classmethod
    def open(cls, directory: Path, failpoint: CrashFailpoint) -> CrashKernelHarness:
        clock = FakeClock(NOW + timedelta(minutes=10))
        store = SQLiteKernelStore.open(
            directory / "saga.db",
            failpoint=failpoint,
            clock=clock,
            durability_failpoint=failpoint,
        )
        forward = DurableFakeTool.open(directory / "forward.db", TOOL_NAME, failpoint)
        repair = DurableFakeTool.open(directory / "repair.db", REPAIR_TOOL_NAME, failpoint)
        registry = _crash_registry(forward, repair)
        lease = LeaseService(store).acquire(SAGA_ID, "worker-recovery", timedelta(minutes=5))
        return cls._build(store, forward, repair, registry, lease, clock, failpoint)

    @classmethod
    def _build(  # noqa: PLR0913, PLR0917
        cls,
        store: SQLiteKernelStore,
        forward: DurableFakeTool,
        repair: DurableFakeTool,
        registry: ToolRegistry,
        lease: Lease,
        clock: FakeClock,
        failpoint: CrashFailpoint,
    ) -> CrashKernelHarness:
        contexts = CrashContexts()
        evidence = _CrashEvidenceProvider(forward, repair)
        kernel = _crash_kernel(store, registry, contexts, clock, failpoint, evidence)
        dispatcher = Dispatcher(store, registry, clock=clock, failpoint=failpoint)
        return cls(store, clock, registry, lease, kernel, dispatcher, forward, repair, contexts)

    def ensure_forward_intent(self) -> None:
        if self.operation(Direction.FORWARD) is not None:
            return
        snapshot = self.store.load_snapshot(SAGA_ID)
        proposal = _proposal().model_copy(update={"based_on_saga_seq": snapshot.seq})
        result = self.kernel.submit_proposal(SAGA_ID, proposal, self.lease)
        if not result.accepted:
            raise AssertionError("forward intent was not accepted")

    async def settle(self, direction: Direction) -> None:
        for _ in range(4):
            if self.store.load_snapshot(SAGA_ID).status is SagaStatus.HUMAN_REQUIRED:
                return
            operation = self.operation(direction)
            if operation is None or _settled(operation.status):
                return
            await self._advance(operation.status)
        raise AssertionError("operation did not settle")

    async def _advance(self, status: OperationStatus) -> None:
        if status in {OperationStatus.INTENT_DURABLE, OperationStatus.DISPATCHED}:
            await self.dispatcher.dispatch_saga(SAGA_ID, self.lease.owner)
            return
        if status is OperationStatus.OUTCOME_UNKNOWN:
            reconciler = Reconciler(self.store, self.registry, clock=self.clock)
            await reconciler.reconcile_one(self.lease.owner)

    def ensure_compensation_started(self) -> None:
        snapshot = self.store.load_snapshot(SAGA_ID)
        if snapshot.status is SagaStatus.COMPENSATING:
            return
        decision = EmergencyUnwinder(self.store, clock=self.clock).execute(
            SAGA_ID, self.registry, UnwindTrigger.AGENT_UNAVAILABLE, self.lease
        )
        if decision.action is not UnwindAction.COMPENSATE:
            raise AssertionError("confirmed reversible effect did not enter compensation")

    def ensure_compensation_intent(self) -> None:
        existing = self.operation(Direction.COMPENSATION)
        target = self.operation(Direction.FORWARD)
        if target is None:
            raise AssertionError("compensation has no forward target")
        self.contexts.compensation_target = target.operation_id
        if existing is not None:
            return
        snapshot = self.store.load_snapshot(SAGA_ID)
        proposal = _compensation_proposal(snapshot.seq)
        result = self.kernel.submit_proposal(SAGA_ID, proposal, self.lease)
        if not result.accepted:
            raise AssertionError("compensation intent was not accepted")

    def finish(self, status: SagaStatus) -> None:
        snapshot = self.store.load_snapshot(SAGA_ID)
        proposal = Finish.model_validate(
            {
                "proposal_id": "proposal_00008003",
                "based_on_saga_seq": snapshot.seq,
                "rationale": "Fresh crash recovery invariants pass.",
                "target_status": status.value,
            }
        )
        result = self.kernel.assign_terminal(SAGA_ID, proposal, self.lease)
        if not result.accepted:
            raise AssertionError(f"terminal transition failed: {result.code}")

    def operation(self, direction: Direction) -> OperationRecord | None:
        snapshot = self.store.load_snapshot(SAGA_ID)
        values = tuple(item for item in snapshot.operations.values() if item.direction is direction)
        if len(values) > 1:
            raise AssertionError("crash harness found duplicate logical operations")
        return values[0] if values else None


def _compensation_proposal(seq: int) -> ToolCall:
    proposal = _proposal()
    return proposal.model_copy(
        update={
            "proposal_id": "proposal_00008002",
            "tool_name": REPAIR_TOOL_NAME,
            "based_on_saga_seq": seq,
            "rationale": "Apply the exact verified recovery action.",
        }
    )


def _settled(status: OperationStatus) -> bool:
    return status in {
        OperationStatus.EFFECT_CONFIRMED,
        OperationStatus.NO_EFFECT_CONFIRMED,
        OperationStatus.PARTIAL_EFFECT_CONFIRMED,
    }


def _start(store: SQLiteKernelStore) -> Lease:
    snapshot = store.create_saga(_created())
    lease = LeaseService(store).acquire(SAGA_ID, "worker-a", timedelta(minutes=5))
    event = _started(lease)
    store.commit_transition(_start_batch(snapshot, lease, event))
    return lease


def _kernel(
    store: SQLiteKernelStore,
    registry: ToolRegistry,
    clock: FakeClock,
) -> SagaKernel:
    return SagaKernel(
        store=store,
        policy=_policy(registry),
        registry=registry,
        policy_context_provider=HarnessContexts(),
        event_metadata_factory=EventMetadataFactory(clock),
        id_factory=StableIdFactory(namespace=b"task-8-harness"),
    )


def _policy(registry: ToolRegistry) -> PolicyEngine:
    return PolicyEngine(
        registry=registry,
        identity_factory=OperationIdentityFactory(namespace=b"task-8-harness"),
        rules=_rules(),
    )


__all__ = [
    "COMMAND_SCHEMA_VERSION",
    "DEFINITION_VERSION",
    "NOW",
    "SAGA_ID",
    "STEP_ID",
    "TOOL_NAME",
    "CrashKernelHarness",
    "HarnessCommand",
    "KernelHarness",
]
