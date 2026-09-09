from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import BaseModel, ConfigDict

from agentic_saga.contracts.actions import AgentProposal, Finish, HumanDecision, ToolCall
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import Direction, JsonObject, Reversibility, thaw_json_object
from agentic_saga.contracts.events import (
    CompensationIntentRecorded,
    CompensationStarted,
    LedgerEvent,
    SagaCreated,
    SagaStarted,
)
from agentic_saga.contracts.outcomes import (
    EffectOutcome,
)
from agentic_saga.contracts.runtime import ExecutionBudget, SagaStatus, TerminalRequirement
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.execution.dispatcher import Dispatcher
from agentic_saga.execution.leases import LeaseService
from agentic_saga.execution.reconciliation import Reconciler
from agentic_saga.kernel.compensation import CompensationItem, CompensationPlanner
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
    InjectedStoreFailure,
    Lease,
    OutboxState,
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
from agentic_saga.kernel.state import (
    ObligationStatus,
    OperationStatus,
    SagaSnapshot,
)
from agentic_saga.storage import SQLiteKernelStore
from tests.support.durable_tool import (
    DurableFakeTool,
    DurableToolFault,
    ReceiptCheckingDurableFakeTool,
)

SAGA_ID = "saga_0000000000009010"
FORWARD_STEP = "step_00009010"
NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
NAMESPACE = b"task-10-compensation"
SECRET = b"raw-auth-must-never-persist"


class FlowCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    kind: Literal["repair"]
    credential_ref: str


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


def _budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=20,
        tool_call_limit=20,
        elapsed_ms_limit=10_000,
        token_limit=10_000,
    )


@dataclass
class Contexts(PolicyContextProvider):
    step_instance_id: str = FORWARD_STEP
    direction: Direction = Direction.FORWARD
    semantic_generation: int = 3
    compensates_operation_id: str | None = None

    def build(self, snapshot: SagaSnapshot, proposal: AgentProposal, lease: Lease) -> PolicyContext:
        del snapshot, proposal
        return PolicyContext(
            step_instance_id=self.step_instance_id,
            direction=self.direction,
            semantic_generation=self.semantic_generation,
            fence_token=lease.fence_token,
            budget=_budget(),
            turns_used=1,
            tool_calls_used=1,
            elapsed_ms=1,
            tokens_used=1,
            policy_evidence={"source": "task-10"},
            resource_identity={"resource_id": "account-7"},
            compensates_operation_id=self.compensates_operation_id,
        )


class Evidence(InvariantEvidenceProvider):
    def evaluate(
        self, saga_id: str, snapshot: SagaSnapshot, target_status: SagaStatus
    ) -> InvariantEvidence:
        result = InvariantResult(
            rule_id="repair_verified",
            passed=True,
            inputs={"source": "authoritative-provider"},
            explanation="Every repair invariant was freshly verified.",
        )
        return InvariantEvidence(
            saga_id=saga_id,
            definition_version=snapshot.definition_version,
            evaluated_at_seq=snapshot.seq,
            target_status=target_status,
            invariant_version="repair-invariants-v1",
            results=(result,),
        )


def _gate() -> TerminalGate:
    requirement = TerminalRequirement(
        invariant_version="repair-invariants-v1",
        required_rule_ids=("repair_verified",),
    )
    return TerminalGate({status: requirement for status in _terminal_statuses()})


def _terminal_statuses() -> tuple[SagaStatus, ...]:
    return (
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    )


def _capabilities(lookup: bool) -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400 if lookup else None,
        reconciliation_supported=lookup,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=True,
    )


def _definition(
    name: str,
    provider: DurableFakeTool,
    *,
    lookup: bool,
    compensate_with: str | None,
    expected_receipts: tuple[JsonObject, ...] = (),
) -> EffectToolDefinition[FlowCommand]:
    capabilities = _capabilities(lookup)
    adapter = _repair_adapter(provider, name, expected_receipts)
    return EffectToolDefinition(
        name=name,
        definition_version=f"{name}-v1",
        command_schema_version="flow-command-v1",
        input_model=FlowCommand,
        adapter=adapter,
        capabilities=capabilities,
        compensate_with=compensate_with,
    )


def _repair_adapter(
    provider: DurableFakeTool, name: str, receipts: tuple[JsonObject, ...]
) -> DurableFakeTool:
    if not receipts:
        return provider
    return ReceiptCheckingDurableFakeTool(provider.path, name, receipts)


def _rules() -> PolicyRules:
    return PolicyRules(
        is_duplicate_effect=_not_duplicate,
        approval_required=_deny,
        approval_verifier=_verify,
    )


def _created() -> SagaCreated:
    return SagaCreated(
        event_id="evt_0000000000009010",
        saga_id=SAGA_ID,
        saga_seq=1,
        definition_version="task-10-saga-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000009010",
        recorded_at=NOW,
        definition_name="task-10-flow",
        definition_fingerprint="f" * 64,
        redacted_goal={"order_ref": "opaque://order/9010/v1"},
    )


def _start(store: SQLiteKernelStore, clock: FakeClock) -> Lease:
    snapshot = store.create_saga(_created())
    lease = LeaseService(store).acquire(SAGA_ID, "worker-a", timedelta(minutes=5))
    event = _started(snapshot, lease, clock)
    store.commit_transition(_batch(snapshot, lease, event, "txn_0000000000009010"))
    return lease


def _started(snapshot: SagaSnapshot, lease: Lease, clock: FakeClock) -> SagaStarted:
    return SagaStarted(
        event_id="evt_0000000000009011",
        saga_id=SAGA_ID,
        saga_seq=snapshot.seq + 1,
        definition_version=snapshot.definition_version,
        fence_token=lease.fence_token,
        actor="kernel",
        trace_id="trace_0000000000009010",
        recorded_at=clock.now(),
    )


def _batch(
    snapshot: SagaSnapshot, lease: Lease, event: LedgerEvent, transition_id: str
) -> TransitionBatch:
    projected = reduce_event(snapshot, event)
    return TransitionBatch(
        transition_id=transition_id,
        saga_id=snapshot.saga_id,
        expected_seq=snapshot.seq,
        expected_fence_token=lease.fence_token,
        lease_owner=lease.owner,
        events=(event,),
        projection=projected,
    )


def _proposal(tool_name: str, seq: int, proposal_id: str) -> ToolCall:
    return ToolCall(
        proposal_id=proposal_id,
        tool_name=tool_name,
        arguments={
            "resource_id": "account-7",
            "kind": "repair",
            "credential_ref": "vault://payments/credential/v1",
        },
        based_on_saga_seq=seq,
        rationale="Apply the exact verified repair.",
    )


@dataclass
class FlowHarness:
    directory: Path
    clock: FakeClock
    store: SQLiteKernelStore
    lease: Lease
    contexts: Contexts
    forward_provider: DurableFakeTool
    repair_provider: DurableFakeTool
    registry: ToolRegistry
    kernel: SagaKernel
    compensation: CompensationItem | None = None

    @classmethod
    def create(cls, directory: Path, *, partial: bool = False, lookup: bool = True) -> FlowHarness:
        clock = FakeClock(NOW)
        store = SQLiteKernelStore.initialize(directory / "saga.db", clock=clock)
        forward = DurableFakeTool.initialize(directory / "forward.db", "charge_payment")
        repair = DurableFakeTool.initialize(directory / "repair.db", "refund_payment")
        if partial:
            forward.set_fault(DurableToolFault.PARTIAL_EFFECT)
        expected = _expected_forward_receipts(forward, partial)
        registry = _registry(forward, repair, lookup, expected)
        lease = _start(store, clock)
        contexts = Contexts()
        kernel = _kernel(store, registry, contexts, clock)
        return cls(
            directory,
            clock,
            store,
            lease,
            contexts,
            forward,
            repair,
            registry,
            kernel,
        )

    async def confirm_forward(self) -> None:
        proposal = _proposal("charge_payment", 2, "proposal_00009010")
        result = self.kernel.submit_proposal(SAGA_ID, proposal, self.lease)
        assert result.accepted
        await Dispatcher(self.store, self.registry, clock=self.clock).dispatch_one(self.lease.owner)
        assert self.store.load_snapshot(SAGA_ID).seq == 5

    def start_compensation(self) -> CompensationItem:
        snapshot = self.store.load_snapshot(SAGA_ID)
        event = CompensationStarted(
            event_id="evt_0000000000009012",
            saga_id=SAGA_ID,
            saga_seq=snapshot.seq + 1,
            definition_version=snapshot.definition_version,
            fence_token=self.lease.fence_token,
            actor="kernel",
            trace_id="trace_0000000000009012",
            recorded_at=self.clock.now(),
        )
        self.store.commit_transition(_batch(snapshot, self.lease, event, "txn_0000000000009012"))
        current = self.store.load_snapshot(SAGA_ID)
        item = (
            CompensationPlanner(OperationIdentityFactory(NAMESPACE))
            .plan(current, self.registry)
            .runnable[0]
        )
        self.compensation = item
        self._bind_context(item)
        return item

    def _bind_context(self, item: CompensationItem) -> None:
        self.contexts.step_instance_id = item.step_instance_id
        self.contexts.direction = Direction.COMPENSATION
        self.contexts.semantic_generation = item.semantic_generation
        self.contexts.compensates_operation_id = item.forward_operation_id

    def submit_compensation(self, proposal_id: str = "proposal_00009011") -> str:
        snapshot = self.store.load_snapshot(SAGA_ID)
        proposal = _proposal("refund_payment", snapshot.seq, proposal_id)
        result = self.kernel.submit_proposal(SAGA_ID, proposal, self.lease)
        assert result.accepted and result.operation_id is not None
        return result.operation_id

    async def dispatch_compensation(self) -> None:
        dispatcher = Dispatcher(self.store, self.registry, clock=self.clock)
        await dispatcher.dispatch_one(self.lease.owner)

    async def reconcile(self) -> None:
        reconciler = Reconciler(self.store, self.registry, clock=self.clock)
        await reconciler.reconcile_one(self.lease.owner)
        current = self.store.lease_state(SAGA_ID)
        assert current is not None
        self.lease = Lease.model_validate(current.model_dump())

    def restart(self) -> None:
        self.store = SQLiteKernelStore.open(self.store.path, clock=self.clock)
        self.forward_provider = DurableFakeTool.open(self.forward_provider.path, "charge_payment")
        self.repair_provider = DurableFakeTool.open(self.repair_provider.path, "refund_payment")
        expected = () if self.compensation is None else self.compensation.receipts
        self.registry = _registry(self.forward_provider, self.repair_provider, True, expected)
        self.kernel = _kernel(self.store, self.registry, self.contexts, self.clock)

    def finish(self) -> None:
        snapshot = self.store.load_snapshot(SAGA_ID)
        proposal = Finish(
            proposal_id="proposal_00009012",
            based_on_saga_seq=snapshot.seq,
            rationale="Fresh authoritative repair invariants pass.",
            target_status=SagaStatus.COMPENSATED_VERIFIED.value,
        )
        result = self.kernel.assign_terminal(SAGA_ID, proposal, self.lease)
        assert result.accepted


@dataclass
class OneShotFailpoint:
    target: StoreFailpoint
    fired: bool = False

    def hit(self, point: StoreFailpoint) -> None:
        if point is self.target and not self.fired:
            self.fired = True
            raise InjectedStoreFailure(point)


@dataclass
class ArmableOneShotFailpoint:
    target: StoreFailpoint
    armed: bool = False
    fired: bool = False

    def hit(self, point: StoreFailpoint) -> None:
        if self.armed and point is self.target and not self.fired:
            self.fired = True
            raise InjectedStoreFailure(point)


def _registry(
    forward: DurableFakeTool,
    repair: DurableFakeTool,
    lookup: bool,
    expected_receipts: tuple[JsonObject, ...] = (),
) -> ToolRegistry:
    return ToolRegistry(
        (
            _definition("charge_payment", forward, lookup=True, compensate_with="refund_payment"),
            _definition(
                "refund_payment",
                repair,
                lookup=lookup,
                compensate_with=None,
                expected_receipts=expected_receipts,
            ),
        )
    )


def _expected_forward_receipts(provider: DurableFakeTool, partial: bool) -> tuple[JsonObject, ...]:
    if not partial:
        return ()
    operation_id = OperationIdentityFactory(NAMESPACE).create(
        SAGA_ID, FORWARD_STEP, Direction.FORWARD, 3
    )
    return provider.partial_receipts(operation_id)


def _kernel(
    store: SQLiteKernelStore,
    registry: ToolRegistry,
    contexts: Contexts,
    clock: FakeClock,
) -> SagaKernel:
    policy = PolicyEngine(
        registry=registry,
        identity_factory=OperationIdentityFactory(NAMESPACE),
        rules=_rules(),
    )
    return SagaKernel(
        store=store,
        policy=policy,
        registry=registry,
        policy_context_provider=contexts,
        event_metadata_factory=EventMetadataFactory(clock),
        id_factory=StableIdFactory(NAMESPACE),
        terminal_gate=_gate(),
        invariant_evidence_provider=Evidence(),
    )


def _reopen_harness_store(harness: FlowHarness, failpoint: OneShotFailpoint) -> None:
    harness.store = SQLiteKernelStore.open(
        harness.store.path, clock=harness.clock, failpoint=failpoint
    )
    harness.kernel = _kernel(harness.store, harness.registry, harness.contexts, harness.clock)


def _compensation_command_id(item: CompensationItem) -> str:
    return StableIdFactory(NAMESPACE).command_id(item.operation_id)


@pytest.mark.asyncio
async def test_compensation_uses_normal_dispatch_exact_receipts_and_fresh_terminal(
    tmp_path: Path,
) -> None:
    harness = FlowHarness.create(tmp_path, partial=True)
    await harness.confirm_forward()
    item = harness.start_compensation()

    operation_id = harness.submit_compensation()
    await harness.dispatch_compensation()
    harness.finish()

    assert operation_id == item.operation_id
    assert harness.repair_provider.calls[-1].forward_receipts == item.receipts
    assert harness.store.load_snapshot(SAGA_ID).status is SagaStatus.COMPENSATED_VERIFIED
    event = next(
        item
        for item in harness.store.read_events(SAGA_ID)
        if isinstance(item, CompensationIntentRecorded)
    )
    assert event.forward_receipts == item.receipts


@pytest.mark.asyncio
async def test_transient_retry_reuses_one_compensation_identity(tmp_path: Path) -> None:
    harness = FlowHarness.create(tmp_path)
    await harness.confirm_forward()
    item = harness.start_compensation()
    harness.submit_compensation()
    harness.repair_provider.fail_transiently_once()

    await harness.dispatch_compensation()
    await harness.reconcile()
    await harness.dispatch_compensation()
    harness.finish()

    assert harness.repair_provider.execute_call_count == 2
    assert harness.repair_provider.effect_count(item.operation_id) == 1
    assert harness.store.load_snapshot(SAGA_ID).status is SagaStatus.COMPENSATED_VERIFIED


@pytest.mark.asyncio
async def test_lost_response_reconciles_after_restart_without_duplicate_effect(
    tmp_path: Path,
) -> None:
    harness = FlowHarness.create(tmp_path, partial=True)
    await harness.confirm_forward()
    item = harness.start_compensation()
    harness.submit_compensation()
    harness.repair_provider.enable_response_loss_after_effect()

    await harness.dispatch_compensation()
    harness.repair_provider.disable_response_loss()
    harness.restart()
    await harness.reconcile()
    harness.finish()

    assert harness.repair_provider.execute_call_count == 1
    assert harness.repair_provider.effect_count(item.operation_id) == 1
    assert harness.store.load_snapshot(SAGA_ID).status is SagaStatus.COMPENSATED_VERIFIED


@pytest.mark.asyncio
async def test_unverifiable_compensation_routes_human_and_denies_terminal(tmp_path: Path) -> None:
    harness = FlowHarness.create(tmp_path, lookup=False)
    await harness.confirm_forward()
    harness.start_compensation()
    harness.submit_compensation()
    harness.repair_provider.enable_response_loss_after_effect()

    await harness.dispatch_compensation()
    await harness.reconcile()
    snapshot = harness.store.load_snapshot(SAGA_ID)
    finish = Finish(
        proposal_id="proposal_00009013",
        based_on_saga_seq=snapshot.seq,
        rationale="Unverifiable repair must be denied.",
        target_status=SagaStatus.COMPENSATED_VERIFIED.value,
    )
    result = harness.kernel.assign_terminal(SAGA_ID, finish, harness.lease)

    assert snapshot.status is SagaStatus.HUMAN_REQUIRED
    assert not result.accepted
    assert harness.store.load_snapshot(SAGA_ID).status is SagaStatus.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_duplicate_proposal_creates_one_intent_and_privacy_is_preserved(
    tmp_path: Path,
) -> None:
    harness = FlowHarness.create(tmp_path)
    await harness.confirm_forward()
    harness.start_compensation()
    snapshot = harness.store.load_snapshot(SAGA_ID)
    proposal = _proposal("refund_payment", snapshot.seq, "proposal_00009011")

    first = harness.kernel.submit_proposal(SAGA_ID, proposal, harness.lease)
    second = harness.kernel.submit_proposal(SAGA_ID, proposal, harness.lease)
    await harness.dispatch_compensation()

    events = harness.store.read_events(SAGA_ID)
    intents = tuple(item for item in events if isinstance(item, CompensationIntentRecorded))
    durable = harness.store.path.read_bytes() + harness.repair_provider.durable_bytes()
    assert first == second
    assert first.operation_id is not None
    assert len(intents) == 1
    assert SECRET not in durable
    assert b"vault://payments/credential/v1" in durable
    assert thaw_json_object(intents[0].forward_receipts[0])


@pytest.mark.asyncio
async def test_takeover_during_receipt_load_prevents_compensation_provider_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = FlowHarness.create(tmp_path)
    await harness.confirm_forward()
    item = harness.start_compensation()
    harness.submit_compensation()
    triggered = _take_over_on_dispatched_receipt_load(harness, item, monkeypatch)

    result = await Dispatcher(harness.store, harness.registry, clock=harness.clock).dispatch_one(
        harness.lease.owner
    )

    assert triggered == [True]
    assert result.failure is not None and result.failure.code == "authority_lost"
    assert harness.repair_provider.execute_call_count == 0
    assert harness.repair_provider.effect_count(item.operation_id) == 0


@pytest.mark.asyncio
async def test_partial_compensation_is_durably_human_required_and_quiescent(
    tmp_path: Path,
) -> None:
    harness = FlowHarness.create(tmp_path)
    await harness.confirm_forward()
    item = harness.start_compensation()
    harness.submit_compensation()
    harness.repair_provider.set_fault(DurableToolFault.PARTIAL_EFFECT)

    await harness.dispatch_compensation()
    second = await Dispatcher(harness.store, harness.registry, clock=harness.clock).dispatch_one(
        harness.lease.owner
    )
    snapshot = harness.store.load_snapshot(SAGA_ID)

    assert snapshot.status is SagaStatus.HUMAN_REQUIRED
    assert snapshot.obligations[item.forward_operation_id].status is ObligationStatus.IN_PROGRESS
    frontier = CompensationPlanner(OperationIdentityFactory(NAMESPACE)).plan(
        snapshot, harness.registry
    )
    assert frontier.runnable == ()
    assert frontier.blocked_by_partial == (item.operation_id,)
    assert second.failure is not None and second.failure.code == "no_work"
    assert harness.repair_provider.execute_call_count == 1


@pytest.mark.parametrize(
    "point", (StoreFailpoint.BEFORE_COMMIT, StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
)
@pytest.mark.asyncio
async def test_compensation_intent_is_crash_atomic_and_idempotent(
    tmp_path: Path, point: StoreFailpoint
) -> None:
    harness = FlowHarness.create(tmp_path)
    await harness.confirm_forward()
    item = harness.start_compensation()
    snapshot = harness.store.load_snapshot(SAGA_ID)
    proposal = _proposal("refund_payment", snapshot.seq, "proposal_00009014")
    failpoint = OneShotFailpoint(point)
    _reopen_harness_store(harness, failpoint)

    with pytest.raises(InjectedStoreFailure):
        harness.kernel.submit_proposal(SAGA_ID, proposal, harness.lease)
    harness.restart()
    result = harness.kernel.submit_proposal(SAGA_ID, proposal, harness.lease)

    intents = tuple(
        event
        for event in harness.store.read_events(SAGA_ID)
        if isinstance(event, CompensationIntentRecorded)
    )
    assert failpoint.fired and result.operation_id == item.operation_id
    assert len(intents) == 1
    assert harness.store.outbox_state(_compensation_command_id(item)) is OutboxState.RUNNABLE


@pytest.mark.asyncio
async def test_compensation_intent_survives_backup_restore_before_provider_entry(
    tmp_path: Path,
) -> None:
    harness = FlowHarness.create(tmp_path)
    await harness.confirm_forward()
    item = harness.start_compensation()
    harness.submit_compensation()
    backup = tmp_path / "backup.db"
    harness.store.backup_to(backup)
    restored = SQLiteKernelStore.restore_from(backup, tmp_path / "restored.db")
    harness.store = SQLiteKernelStore.open(restored.path, clock=harness.clock)
    harness.kernel = _kernel(harness.store, harness.registry, harness.contexts, harness.clock)

    await harness.dispatch_compensation()
    harness.finish()

    assert harness.store.rebuild_and_verify(SAGA_ID) == harness.store.load_snapshot(SAGA_ID)
    assert harness.repair_provider.execute_call_count == 1
    assert harness.repair_provider.effect_count(item.operation_id) == 1


@pytest.mark.parametrize(
    "point", (StoreFailpoint.BEFORE_COMMIT, StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
)
@pytest.mark.asyncio
async def test_compensation_outcome_failpoints_recover_without_duplicate_effect(
    tmp_path: Path, point: StoreFailpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = FlowHarness.create(tmp_path, partial=True)
    await harness.confirm_forward()
    item = harness.start_compensation()
    harness.submit_compensation()
    failpoint = ArmableOneShotFailpoint(point)
    harness.store = SQLiteKernelStore.open(
        harness.store.path, clock=harness.clock, failpoint=failpoint
    )
    _arm_after_effect(harness, failpoint, monkeypatch)

    with pytest.raises(InjectedStoreFailure):
        await harness.dispatch_compensation()
    harness.restart()
    operation = harness.store.load_snapshot(SAGA_ID).operations[item.operation_id]
    if operation.status is OperationStatus.DISPATCHED:
        harness.clock.advance(timedelta(minutes=2))
        await harness.dispatch_compensation()
        await harness.reconcile()

    assert failpoint.fired
    assert harness.repair_provider.execute_call_count == 1
    assert harness.repair_provider.effect_count(item.operation_id) == 1
    obligation = harness.store.load_snapshot(SAGA_ID).obligations[item.forward_operation_id]
    assert obligation.status is ObligationStatus.SATISFIED


@pytest.mark.asyncio
async def test_takeover_after_compensation_effect_reconciles_without_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = FlowHarness.create(tmp_path)
    await harness.confirm_forward()
    item = harness.start_compensation()
    harness.submit_compensation()
    _take_over_after_effect(harness, monkeypatch)

    first = await Dispatcher(harness.store, harness.registry, clock=harness.clock).dispatch_one(
        harness.lease.owner
    )
    second = await Dispatcher(harness.store, harness.registry, clock=harness.clock).dispatch_one(
        "worker-b"
    )
    reconciled = await Reconciler(
        harness.store, harness.registry, clock=harness.clock
    ).reconcile_one("worker-b")

    snapshot = harness.store.load_snapshot(SAGA_ID)
    assert first.failure is not None and first.failure.code == "authority_lost"
    assert second.failure is not None and second.failure.code == "prior_dispatch_unknown"
    assert harness.repair_provider.execute_call_count == 1
    assert harness.repair_provider.effect_count(item.operation_id) == 1
    assert reconciled.decision is not None and reconciled.decision.action == "confirm"
    assert snapshot.obligations[item.forward_operation_id].status is ObligationStatus.SATISFIED


def _take_over_on_dispatched_receipt_load(
    harness: FlowHarness, item: CompensationItem, monkeypatch: pytest.MonkeyPatch
) -> list[bool]:
    original = harness.store.load_snapshot
    triggered: list[bool] = []

    def load(saga_id: str) -> SagaSnapshot:
        current = original(saga_id)
        operation = current.operations.get(item.operation_id)
        if operation is not None and operation.status.value == "dispatched" and not triggered:
            triggered.append(True)
            harness.clock.advance(timedelta(minutes=5))
            LeaseService(harness.store).acquire(SAGA_ID, "worker-b", timedelta(minutes=5))
        return current

    monkeypatch.setattr(harness.store, "load_snapshot", load)
    return triggered


def _take_over_after_effect(harness: FlowHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _compensation_adapter(harness)
    original = adapter.execute

    async def execute(command: BaseModel, context: EffectContext) -> EffectOutcome:
        outcome = await original(command, context)
        harness.clock.advance(timedelta(minutes=5))
        LeaseService(harness.store).acquire(SAGA_ID, "worker-b", timedelta(minutes=5))
        return outcome

    monkeypatch.setattr(adapter, "execute", execute)


def _arm_after_effect(
    harness: FlowHarness,
    failpoint: ArmableOneShotFailpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _compensation_adapter(harness)
    original = adapter.execute

    async def execute(command: BaseModel, context: EffectContext) -> EffectOutcome:
        outcome = await original(command, context)
        failpoint.armed = True
        return outcome

    monkeypatch.setattr(adapter, "execute", execute)


def _compensation_adapter(harness: FlowHarness) -> DurableFakeTool:
    definition = harness.registry.definition("refund_payment")
    if not isinstance(definition, EffectToolDefinition):
        raise AssertionError("repair definition must be effectful")
    return cast(DurableFakeTool, definition.adapter)
