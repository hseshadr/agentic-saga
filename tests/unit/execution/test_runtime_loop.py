from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter

from agentic_saga.contracts.actions import AgentProposal, Escalate, HumanDecision, ToolCall
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    Reversibility,
)
from agentic_saga.contracts.events import (
    AgentTurnFailed,
    AgentTurnReserved,
    EffectIntentRecorded,
    EffectOutcomeRecorded,
    HumanRequired,
    ProposalRejected,
    ReadObserved,
    ReadStarted,
    ReadUnavailable,
    SagaCreated,
    SagaStarted,
    TerminalDenied,
)
from agentic_saga.contracts.redaction import RedactionPolicy
from agentic_saga.contracts.runtime import (
    ExecutionBudget,
    SagaGoal,
    SagaObservation,
    SagaStatus,
    TerminalRequirement,
    ToolDescriptor,
)
from agentic_saga.contracts.tools import (
    EffectToolDefinition,
    ReadToolDefinition,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.contracts.tools import (
    ReadAdapter as ReadAdapterProtocol,
)
from agentic_saga.execution.dispatcher import Dispatcher
from agentic_saga.execution.leases import LeaseService
from agentic_saga.execution.reconciliation import Reconciler
from agentic_saga.execution.runtime import (
    DefinitionRuntimeMismatch,
    SagaRuntime,
    _await_with_authority,
    _LeaseAuthority,
    _saga_id,
)
from agentic_saga.execution.unwind import EmergencyUnwinder
from agentic_saga.kernel.definitions import DefinitionCatalog, SagaDefinition
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.policy import (
    PolicyContext,
    PolicyEngine,
    PolicyRules,
    ProposedEffectIdentity,
)
from agentic_saga.kernel.ports import Lease, LeaseUnavailable, StoreConflict
from agentic_saga.kernel.runtime import EventMetadataFactory, PolicyContextProvider, SagaKernel
from agentic_saga.kernel.runtime import StableIdFactory as KernelIds
from agentic_saga.kernel.state import SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.support.durable_agent import DurableFakeAgent
from tests.support.durable_tool import DurableFakeTool

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def _false(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
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


def _advertise(tool_name: str, snapshot: SagaSnapshot) -> dict[str, bool]:
    del tool_name, snapshot
    return {"sequence_bound": True}


def _hide(tool_name: str, snapshot: SagaSnapshot) -> None:
    del tool_name, snapshot


def _budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=4,
        tool_call_limit=3,
        elapsed_ms_limit=4_000,
        token_limit=400,
    )


@dataclass(frozen=True)
class Contexts(PolicyContextProvider):
    budget: ExecutionBudget

    def build(self, snapshot: SagaSnapshot, proposal: AgentProposal, lease: Lease) -> PolicyContext:
        del proposal
        return PolicyContext(
            step_instance_id=f"step_{snapshot.seq:08d}",
            direction=Direction.FORWARD,
            semantic_generation=0,
            fence_token=lease.fence_token,
            budget=self.budget,
            turns_used=0,
            tool_calls_used=0,
            elapsed_ms=0,
            tokens_used=0,
            policy_evidence={"source": "runtime-test"},
            resource_identity={"resource": "generic"},
        )


def _policy(registry: ToolRegistry, *, hide_tools: bool = False) -> PolicyEngine:
    rules = PolicyRules(
        is_duplicate_effect=_not_duplicate,
        approval_required=_false,
        approval_verifier=_verify,
        tool_advertisement=_hide if hide_tools else _advertise,
    )
    return PolicyEngine(
        registry=registry,
        identity_factory=OperationIdentityFactory(namespace=b"runtime-loop"),
        rules=rules,
    )


def _requirement(rule: str) -> TerminalRequirement:
    return TerminalRequirement(invariant_version="runtime-v1", required_rule_ids=(rule,))


def _definition(
    registry: ToolRegistry,
    execution_budget: ExecutionBudget | None = None,
    *,
    hide_tools: bool = False,
    policy: PolicyEngine | None = None,
) -> SagaDefinition:
    selected_policy = policy or _policy(registry, hide_tools=hide_tools)
    return SagaDefinition(
        name="generic_runtime",
        version="runtime-v1",
        registry=registry,
        policy=selected_policy,
        success_invariants=_requirement("success"),
        compensation_invariants=_requirement("compensation"),
        clean_abort_invariants=_requirement("clean_abort"),
        exception_invariants=_requirement("operator_accepted"),
        budget=execution_budget or _budget(),
    )


class ReadCommand(BaseModel):
    model_config = {"strict": True, "extra": "forbid", "frozen": True}

    public_id: str


class ReadResult(BaseModel):
    model_config = {"strict": True, "extra": "forbid", "frozen": True}

    state: str


@dataclass
class ReadAdapter:
    calls: int = 0
    revision: str = "runtime-read-v1"

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"adapter_version": self.revision}, strict=True)

    async def read(self, command: ReadCommand) -> ReadResult:
        self.calls += 1
        return ReadResult(state=f"seen:{command.public_id}")


@dataclass
class StalledReadAdapter:
    calls: int = 0

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"adapter_version": "stalled-read-v1"}, strict=True)

    async def read(self, command: ReadCommand) -> ReadResult:
        del command
        self.calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@dataclass
class ReadTakeoverAdapter:
    clock: FakeClock | None = None
    leases: LeaseService | None = None
    saga_id: str | None = None

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"adapter_version": "takeover-read-v1"}, strict=True)

    async def read(self, command: ReadCommand) -> ReadResult:
        del command
        if self.clock is None or self.leases is None or self.saga_id is None:
            raise AssertionError("adapter must be bound after harness construction")
        self.clock.advance(timedelta(minutes=6))
        self.leases.acquire(self.saga_id, "read-takeover", timedelta(minutes=5))
        return ReadResult(state="untrusted-after-takeover")


@dataclass(frozen=True)
class ClockAdvancingAgent:
    clock: FakeClock

    async def next_action(
        self,
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> AgentProposal:
        del available_tools
        self.clock.advance(timedelta(minutes=6))
        return Escalate(
            proposal_id="proposal_after_slow_work",
            based_on_saga_seq=observation.saga_seq,
            reason_code="operator_review",
            rationale="The bounded operation completed after the original authority expired.",
        )


@dataclass(frozen=True)
class LeaseTakeoverAgent:
    clock: FakeClock
    leases: LeaseService

    async def next_action(
        self,
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> AgentProposal:
        del available_tools
        self.clock.advance(timedelta(minutes=6))
        self.leases.acquire(observation.saga_id, "second-runtime", timedelta(minutes=5))
        return Escalate(
            proposal_id="proposal_after_takeover",
            based_on_saga_seq=observation.saga_seq,
            reason_code="operator_review",
            rationale="A second runtime acquired authority after expiry.",
        )


def _capabilities(provider: DurableFakeTool) -> ToolCapabilities:
    del provider
    return ToolCapabilities(
        idempotency_retention_seconds=3_600,
        reconciliation_supported=False,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.IRREVERSIBLE,
        partial_effects_possible=False,
    )


def _agent(
    actions: list[str],
    *,
    tool_name: str = "observe_generic",
    arguments: JsonObject | None = None,
    mode: str = "scripted",
) -> DurableFakeAgent:
    return DurableFakeAgent.initialize(
        actions,
        tool_name=tool_name,
        arguments=arguments or {"public_id": "resource_1"},
        mode=mode,
    )


async def _wait_for_agent_call(agent: DurableFakeAgent) -> None:
    async with asyncio.timeout(5):
        while agent.calls == 0:  # noqa: ASYNC110 - durable cross-process entry barrier
            await asyncio.sleep(0)


@dataclass(frozen=True)
class Harness:
    runtime: SagaRuntime
    store: SQLiteKernelStore
    definition: SagaDefinition
    policy: PolicyEngine
    clock: FakeClock
    kernel: SagaKernel
    dispatcher: Dispatcher
    reconciler: Reconciler
    unwinder: EmergencyUnwinder
    leases: LeaseService


def _harness(
    path: Path,
    registry: ToolRegistry | None = None,
    execution_budget: ExecutionBudget | None = None,
    *,
    hide_tools: bool = False,
) -> Harness:
    clock = FakeClock(NOW)
    store = SQLiteKernelStore.initialize(path / "runtime.db", clock=clock)
    selected = registry or ToolRegistry()
    policy = _policy(selected, hide_tools=hide_tools)
    definition = _definition(selected, execution_budget, hide_tools=hide_tools, policy=policy)
    kernel = SagaKernel(
        store=store,
        policy=policy,
        registry=selected,
        policy_context_provider=Contexts(definition.budget),
        event_metadata_factory=EventMetadataFactory(clock),
        id_factory=KernelIds(namespace=b"runtime-loop"),
    )
    leases = LeaseService(store)
    dispatcher = Dispatcher(store, selected, clock=clock)
    reconciler = Reconciler(store, selected, lease_service=leases, clock=clock)
    unwinder = EmergencyUnwinder(store, clock=clock)
    runtime = SagaRuntime(
        kernel=kernel,
        dispatcher=dispatcher,
        reconciler=reconciler,
        unwinder=unwinder,
        leases=leases,
        definitions=DefinitionCatalog(),
        clock=clock,
    )
    return Harness(
        runtime,
        store,
        definition,
        policy,
        clock,
        kernel,
        dispatcher,
        reconciler,
        unwinder,
        leases,
    )


def _read_registry(adapter: ReadAdapterProtocol[ReadCommand, ReadResult]) -> ToolRegistry:
    return ToolRegistry((ReadToolDefinition("observe_generic", ReadCommand, ReadResult, adapter),))


def _effect_registry(provider: DurableFakeTool) -> ToolRegistry:
    definition = EffectToolDefinition(
        "mutate_generic",
        "mutate-v1",
        "command-v1",
        ReadCommand,
        provider,
        _capabilities(provider),
        None,
    )
    return ToolRegistry((definition,))


@pytest.mark.asyncio
async def test_runtime_reserves_one_turn_then_escalates_quiescently(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    agent = _agent(["escalate"])
    goal = SagaGoal(goal_id="goal_runtime_1", text="Handle safely.", context={"public": "yes"})

    result = await harness.runtime.start(definition=harness.definition, goal=goal, agent=agent)

    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert result.autonomous_quiescent
    assert agent.maximum_concurrent_calls == 1
    assert [item.saga_seq for item in agent.observations] == [3]
    events = harness.store.read_events(result.saga_id)
    reservation = next(item for item in events if isinstance(item, AgentTurnReserved))
    assert reservation.turn_index == 1
    assert tuple(type(item) for item in events) == (
        SagaCreated,
        SagaStarted,
        AgentTurnReserved,
        HumanRequired,
    )


@pytest.mark.asyncio
async def test_runtime_releases_human_required_lease_for_second_resolver(tmp_path: Path) -> None:
    # Given
    harness = _harness(tmp_path)

    # When
    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_release_lease", text="Release safely.", context={}),
        agent=_agent(["escalate"]),
    )

    # Then
    assert harness.store.lease_state(result.saga_id) is None
    resolver = harness.leases.acquire(result.saga_id, "second-resolver", timedelta(minutes=5))
    assert resolver.owner == "second-resolver"


@pytest.mark.asyncio
async def test_runtime_renews_authority_after_awaited_work_exceeds_lease(
    tmp_path: Path,
) -> None:
    # Given
    harness = _harness(tmp_path)

    # When
    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_renew_lease", text="Renew safely.", context={}),
        agent=ClockAdvancingAgent(harness.clock),
    )

    # Then
    events = harness.store.read_events(result.saga_id)
    required = next(item for item in events if isinstance(item, HumanRequired))
    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert required.fence_token == 2
    assert harness.store.lease_state(result.saga_id) is None


@pytest.mark.asyncio
async def test_runtime_cannot_write_or_release_after_awaited_work_loses_authority(
    tmp_path: Path,
) -> None:
    # Given
    harness = _harness(tmp_path)
    goal = SagaGoal(goal_id="goal_lease_takeover", text="Fence stale work.", context={})

    # When / Then
    with pytest.raises(LeaseUnavailable):
        await harness.runtime.start(
            definition=harness.definition,
            goal=goal,
            agent=LeaseTakeoverAgent(harness.clock, harness.leases),
        )
    current = harness.store.lease_state(_saga_id(goal))
    assert current is not None
    assert current.owner == "second-runtime"
    assert not any(
        isinstance(event, HumanRequired) for event in harness.store.read_events(_saga_id(goal))
    )


@pytest.mark.asyncio
async def test_runtime_does_not_record_read_outcome_after_authority_takeover(
    tmp_path: Path,
) -> None:
    # Given
    adapter = ReadTakeoverAdapter()
    harness = _harness(tmp_path, _read_registry(adapter))
    goal = SagaGoal(goal_id="goal_read_takeover", text="Fence the read.", context={})
    adapter.clock = harness.clock
    adapter.leases = harness.leases
    adapter.saga_id = _saga_id(goal)

    # When / Then
    with pytest.raises(LeaseUnavailable):
        await harness.runtime.start(
            definition=harness.definition,
            goal=goal,
            agent=_agent(["read"]),
        )
    events = harness.store.read_events(_saga_id(goal))
    assert not any(isinstance(event, (ReadObserved, ReadUnavailable)) for event in events)


@pytest.mark.asyncio
async def test_authority_failure_does_not_create_awaitable_work(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    goal = SagaGoal(goal_id="goal_pre_renew", text="Fence work.", context={})
    await harness.runtime.start(
        definition=harness.definition, goal=goal, agent=_agent(["escalate"])
    )
    saga_id = _saga_id(goal)
    lease = harness.leases.acquire(saga_id, "first-runtime", timedelta(minutes=5))
    harness.leases.release(lease)
    harness.leases.acquire(saga_id, "second-runtime", timedelta(minutes=5))
    authority = _LeaseAuthority(harness.leases, lease, timedelta(minutes=5))
    called = False

    async def run() -> None:
        nonlocal called
        called = True

    with pytest.raises(LeaseUnavailable):
        await _await_with_authority(authority, run)
    assert not called


@pytest.mark.asyncio
async def test_authority_loss_does_not_mask_primary_work_failure(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    goal = SagaGoal(goal_id="goal_primary_error", text="Fence work.", context={})
    await harness.runtime.start(
        definition=harness.definition, goal=goal, agent=_agent(["escalate"])
    )
    saga_id = _saga_id(goal)
    lease = harness.leases.acquire(saga_id, "first-runtime", timedelta(minutes=5))
    authority = _LeaseAuthority(harness.leases, lease, timedelta(minutes=5))

    async def fail_after_takeover() -> None:
        harness.clock.advance(timedelta(minutes=6))
        harness.leases.acquire(saga_id, "second-runtime", timedelta(minutes=5))
        raise RuntimeError("primary work failure")

    with pytest.raises(RuntimeError, match="primary work failure"):
        await _await_with_authority(authority, fail_after_takeover)


@pytest.mark.asyncio
async def test_start_rejects_same_goal_identity_with_changed_goal(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    first = SagaGoal(goal_id="goal_runtime_2", text="First.", context={})
    changed = SagaGoal(goal_id="goal_runtime_2", text="Changed.", context={})

    agent = _agent(["escalate"])
    await harness.runtime.start(definition=harness.definition, goal=first, agent=agent)
    with pytest.raises(ValueError, match="identity"):
        await harness.runtime.start(definition=harness.definition, goal=changed, agent=agent)


@pytest.mark.asyncio
async def test_start_same_identity_is_idempotent_after_time_advances(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    goal = SagaGoal(goal_id="goal_runtime_same", text="Resume exactly.", context={})
    first = await harness.runtime.start(
        definition=harness.definition, goal=goal, agent=_agent(["escalate"])
    )
    harness.clock.advance(timedelta(hours=1))

    repeated = await harness.runtime.start(
        definition=harness.definition, goal=goal, agent=_agent([])
    )

    assert repeated == first


@pytest.mark.asyncio
async def test_read_records_intent_and_observation_then_offers_fresh_sequence(
    tmp_path: Path,
) -> None:
    adapter = ReadAdapter()
    harness = _harness(tmp_path, _read_registry(adapter))
    agent = _agent(["read", "escalate"])

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_read", text="Observe safely.", context={}),
        agent=agent,
    )

    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert adapter.calls == 1
    assert [item.saga_seq for item in agent.observations] == [3, 6]
    events = harness.store.read_events(result.saga_id)
    assert sum(isinstance(item, ReadStarted) for item in events) == 1
    assert sum(isinstance(item, ReadObserved) for item in events) == 1


@pytest.mark.asyncio
async def test_stalled_read_records_safe_unavailable_outcome_within_turn_budget(
    tmp_path: Path,
) -> None:
    # Given
    adapter = StalledReadAdapter()
    budget = _budget().model_copy(update={"elapsed_ms_limit": 4})
    harness = _harness(tmp_path, _read_registry(adapter), execution_budget=budget)
    agent = _agent(["read", "escalate"])

    # When
    async with asyncio.timeout(1):
        result = await harness.runtime.start(
            definition=harness.definition,
            goal=SagaGoal(goal_id="goal_read_timeout", text="Bound reads.", context={}),
            agent=agent,
        )

    # Then
    events = harness.store.read_events(result.saga_id)
    unavailable = next(item for item in events if isinstance(item, ReadUnavailable))
    assert unavailable.reason_code == "read_unavailable"
    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert adapter.calls == 1
    assert [item.saga_seq for item in agent.observations] == [3, 6]
    last_action = agent.observations[-1].last_action
    assert last_action is not None
    assert last_action["event_type"] == "read_unavailable"
    assert last_action["reason_code"] == "read_unavailable"
    assert "raw" not in str(last_action)


@pytest.mark.asyncio
async def test_invalid_read_is_durable_and_never_enters_adapter(tmp_path: Path) -> None:
    adapter = ReadAdapter()
    harness = _harness(tmp_path, _read_registry(adapter))
    agent = _agent(["read", "escalate"], arguments={"unexpected": "public"})

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_read_denied", text="Deny safely.", context={}),
        agent=agent,
    )

    events = harness.store.read_events(result.saga_id)
    assert adapter.calls == 0
    denial = next(item for item in events if isinstance(item, ProposalRejected))
    assert denial.reason_code == "invalid_command"
    assert not any(isinstance(item, ReadStarted) for item in events)


@pytest.mark.asyncio
async def test_runtime_advertises_only_policy_eligible_public_descriptors(tmp_path: Path) -> None:
    adapter = ReadAdapter()
    harness = _harness(tmp_path, _read_registry(adapter), hide_tools=True)
    agent = _agent(["escalate"])

    await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_hidden_tool", text="Hide denied tools.", context={}),
        agent=agent,
    )

    assert agent.tool_sets == [()]


@pytest.mark.asyncio
async def test_runtime_advertises_only_public_policy_constraints(tmp_path: Path) -> None:
    harness = _harness(tmp_path, _read_registry(ReadAdapter()))
    agent = _agent(["escalate"])

    await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_public_tool", text="Show public tools.", context={}),
        agent=agent,
    )

    assert len(agent.tool_sets[0]) == 1
    assert agent.tool_sets[0][0].name == "observe_generic"
    assert agent.tool_sets[0][0].policy_constraints == {"sequence_bound": True}


@pytest.mark.asyncio
async def test_effect_routes_only_through_intent_outbox_and_dispatcher(tmp_path: Path) -> None:
    provider = DurableFakeTool.initialize(tmp_path / "provider.db", "mutate_generic")
    harness = _harness(tmp_path, _effect_registry(provider))
    agent = _agent(["effect", "escalate"], tool_name="mutate_generic")

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_effect", text="Mutate safely.", context={}),
        agent=agent,
    )

    events = harness.store.read_events(result.saga_id)
    assert provider.execute_call_count == 1
    assert sum(isinstance(item, EffectIntentRecorded) for item in events) == 1
    assert sum(isinstance(item, EffectOutcomeRecorded) for item in events) == 1


@pytest.mark.asyncio
async def test_false_finish_is_durable_then_agent_gets_new_observation(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    agent = _agent(["finish", "escalate"])

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_finish", text="Finish safely.", context={}),
        agent=agent,
    )

    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert [item.saga_seq for item in agent.observations] == [3, 5]
    events = harness.store.read_events(result.saga_id)
    assert any(isinstance(item, TerminalDenied) for item in events)


@pytest.mark.asyncio
async def test_zero_budget_never_enters_agent_and_escalates_safely(tmp_path: Path) -> None:
    exhausted = _budget().model_copy(update={"turn_limit": 0})
    harness = _harness(tmp_path, execution_budget=exhausted)

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_budget", text="Bound safely.", context={}),
        agent=_agent([]),
    )

    assert result.state is SagaStatus.HUMAN_REQUIRED
    events = harness.store.read_events(result.saga_id)
    assert not any(isinstance(item, AgentTurnReserved) for item in events)


@pytest.mark.asyncio
async def test_repeated_invalid_proposals_use_invalid_limit_trigger(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    agent = _agent(["invalid", "invalid", "invalid"])

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_invalid_limit", text="Bound invalid work.", context={}),
        agent=agent,
    )

    assert agent.calls == 3
    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert result.human_required_reason == "runtime_invalid_proposal_limit"


@pytest.mark.asyncio
async def test_non_divisible_reservations_never_exceed_remaining_budget(tmp_path: Path) -> None:
    budget = ExecutionBudget(
        turn_limit=3,
        tool_call_limit=3,
        elapsed_ms_limit=10_000,
        token_limit=10,
    )
    harness = _harness(tmp_path, execution_budget=budget)
    agent = _agent(["finish", "finish", "escalate"])

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_remainder", text="Clamp reservations.", context={}),
        agent=agent,
    )

    reservations = tuple(
        item
        for item in harness.store.read_events(result.saga_id)
        if isinstance(item, AgentTurnReserved)
    )
    assert [item.reserved_elapsed_ms for item in reservations] == [3_333, 3_333, 3_333]
    assert [item.reserved_tokens for item in reservations] == [3, 3, 3]


@pytest.mark.asyncio
async def test_malformed_agent_output_is_spent_and_never_persisted(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    agent = _agent([], mode="malformed")
    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_invalid", text="Fail safely.", context={}),
        agent=agent,
    )

    events = harness.store.read_events(result.saga_id)
    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert any(isinstance(item, AgentTurnFailed) for item in events)
    assert "raw-secret" not in str(tuple(item.model_dump(mode="json") for item in events))


@pytest.mark.asyncio
async def test_arbitrary_agent_exception_is_normalized_without_detail(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    agent = _agent([], mode="exception")
    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_exception", text="Fail safely.", context={}),
        agent=agent,
    )

    events = harness.store.read_events(result.saga_id)
    encoded = str(tuple(item.model_dump(mode="json") for item in events))
    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert "raw-provider-secret" not in encoded


@pytest.mark.asyncio
async def test_resume_never_reenters_an_unresolved_reserved_turn(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    blocking = _agent([], mode="block")
    goal = SagaGoal(goal_id="goal_runtime_crash", text="Crash safely.", context={})
    task = asyncio.create_task(
        harness.runtime.start(definition=harness.definition, goal=goal, agent=blocking)
    )
    await _wait_for_agent_call(blocking)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await harness.runtime.resume(saga_id=_saga_id(goal), agent=_agent([]))

    events = harness.store.read_events(result.saga_id)
    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert sum(isinstance(item, AgentTurnReserved) for item in events) == 1
    failure = next(item for item in events if isinstance(item, AgentTurnFailed))
    assert failure.reason_code == "agent_turn_unresolved"


@pytest.mark.asyncio
async def test_cooperative_agent_timeout_records_one_failed_turn(tmp_path: Path) -> None:
    budget = _budget().model_copy(update={"turn_limit": 1, "elapsed_ms_limit": 500})
    harness = _harness(tmp_path, execution_budget=budget)
    blocking = _agent([], mode="block")

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_agent_timeout", text="Stop safely.", context={}),
        agent=blocking,
    )

    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert blocking.calls == 1
    events = harness.store.read_events(result.saga_id)
    assert sum(isinstance(item, AgentTurnReserved) for item in events) == 1
    assert any(isinstance(item, AgentTurnFailed) for item in events)


@pytest.mark.asyncio
async def test_start_fails_closed_before_create_for_mismatched_kernel_definition(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    mismatched = _definition(ToolRegistry())
    goal = SagaGoal(goal_id="goal_runtime_mismatch", text="Fail closed.", context={})

    with pytest.raises(DefinitionRuntimeMismatch):
        await harness.runtime.start(definition=mismatched, goal=goal, agent=_agent(["escalate"]))
    with pytest.raises(StoreConflict):
        harness.store.load_snapshot(_saga_id(goal))


@pytest.mark.asyncio
async def test_definition_redaction_mismatch_fails_before_durable_creation(
    tmp_path: Path,
) -> None:
    # Given
    harness = _harness(tmp_path)
    mismatched = replace(
        harness.definition,
        redaction_policy=RedactionPolicy(sensitive_keys=("email",)),
    )
    goal = SagaGoal(goal_id="goal_privacy_mismatch", text="Fail closed.", context={})

    # When / Then
    with pytest.raises(DefinitionRuntimeMismatch, match="redaction"):
        await harness.runtime.start(definition=mismatched, goal=goal, agent=_agent(["escalate"]))
    with pytest.raises(StoreConflict):
        harness.store.load_snapshot(_saga_id(goal))


@pytest.mark.parametrize(
    ("matching_dispatcher", "mismatch"),
    [(False, "dispatcher"), (True, "reconciler")],
)
def test_runtime_rejects_mismatched_effect_services_for_custom_definition(
    tmp_path: Path,
    matching_dispatcher: bool,
    mismatch: str,
) -> None:
    # Given
    harness = _harness(tmp_path)
    privacy = RedactionPolicy(sensitive_keys=("email", "name", "address"))
    definition = replace(harness.definition, redaction_policy=privacy)
    goal = SagaGoal(goal_id="goal_effect_privacy_mismatch", text="Fail closed.", context={})
    agent = _agent(["escalate"])
    kernel = SagaKernel(
        store=harness.store,
        policy=harness.policy,
        registry=definition.registry,
        policy_context_provider=Contexts(definition.budget),
        event_metadata_factory=EventMetadataFactory(harness.clock),
        id_factory=KernelIds(namespace=b"runtime-loop"),
        redaction_policy=privacy,
    )
    dispatcher = harness.dispatcher
    if matching_dispatcher:
        dispatcher = Dispatcher(
            harness.store,
            definition.registry,
            clock=harness.clock,
            redaction_policy=privacy,
        )

    # When / Then
    with pytest.raises(DefinitionRuntimeMismatch, match=mismatch):
        SagaRuntime(
            kernel=kernel,
            dispatcher=dispatcher,
            reconciler=harness.reconciler,
            unwinder=harness.unwinder,
            leases=harness.leases,
            definitions=DefinitionCatalog((definition,)),
            clock=harness.clock,
        )
    with pytest.raises(StoreConflict):
        harness.store.load_snapshot(_saga_id(goal))
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_same_runtime_serializes_concurrent_resume_behind_active_agent(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    blocking = _agent([], mode="block")
    goal = SagaGoal(goal_id="goal_runtime_concurrent", text="Serialize safely.", context={})
    active = asyncio.create_task(
        harness.runtime.start(definition=harness.definition, goal=goal, agent=blocking)
    )
    await _wait_for_agent_call(blocking)
    follower = asyncio.create_task(harness.runtime.resume(saga_id=_saga_id(goal), agent=_agent([])))
    cycled = asyncio.Event()
    asyncio.get_running_loop().call_soon(cycled.set)
    await cycled.wait()

    assert not follower.done()
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    result = await follower
    assert result.state is SagaStatus.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_should_release_guards_after_high_cardinality_completed_sagas(
    tmp_path: Path,
) -> None:
    # Given
    harness = _harness(tmp_path)
    agent = _agent(["escalate"] * 32)

    # When
    for index in range(32):
        goal = SagaGoal(goal_id=f"goal_guard_{index}", text="Finish safely.", context={})
        await harness.runtime.start(definition=harness.definition, goal=goal, agent=agent)

    # Then
    assert harness.runtime._guards == {}


@pytest.mark.asyncio
async def test_should_keep_guard_while_holder_remains_after_waiter_cancellation(
    tmp_path: Path,
) -> None:
    # Given
    harness = _harness(tmp_path)
    blocking = _agent([], mode="block")
    goal = SagaGoal(goal_id="goal_guard_cancel", text="Serialize safely.", context={})
    active = asyncio.create_task(
        harness.runtime.start(definition=harness.definition, goal=goal, agent=blocking)
    )
    await _wait_for_agent_call(blocking)
    waiter = asyncio.create_task(harness.runtime.resume(saga_id=_saga_id(goal), agent=_agent([])))
    await asyncio.sleep(0)

    # When
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    follower = asyncio.create_task(harness.runtime.resume(saga_id=_saga_id(goal), agent=_agent([])))
    await asyncio.sleep(0)

    # Then
    assert not follower.done()
    assert len(harness.runtime._guards) == 1
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert (await follower).state is SagaStatus.HUMAN_REQUIRED
    assert harness.runtime._guards == {}


@pytest.mark.asyncio
async def test_should_allow_different_sagas_to_enter_agents_concurrently(tmp_path: Path) -> None:
    # Given
    harness = _harness(tmp_path)
    first_agent = _agent([], mode="block")
    second_agent = _agent([], mode="block")
    first = SagaGoal(goal_id="goal_guard_first", text="Run independently.", context={})
    second = SagaGoal(goal_id="goal_guard_second", text="Run independently.", context={})

    # When
    tasks = (
        asyncio.create_task(
            harness.runtime.start(definition=harness.definition, goal=first, agent=first_agent)
        ),
        asyncio.create_task(
            harness.runtime.start(definition=harness.definition, goal=second, agent=second_agent)
        ),
    )
    await asyncio.gather(_wait_for_agent_call(first_agent), _wait_for_agent_call(second_agent))

    # Then
    assert (first_agent.calls, second_agent.calls) == (1, 1)
    for task in tasks:
        task.cancel()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(item, asyncio.CancelledError) for item in outcomes)
    assert harness.runtime._guards == {}
