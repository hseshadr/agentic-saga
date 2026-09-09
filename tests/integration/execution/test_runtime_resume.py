from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from agentic_saga.contracts.common import JsonObject, Reversibility
from agentic_saga.contracts.events import AgentTurnReserved, ReadObserved, ReadStarted
from agentic_saga.contracts.redaction import RedactionPolicy
from agentic_saga.contracts.runtime import SagaGoal, SagaStatus
from agentic_saga.contracts.tools import (
    EffectToolDefinition,
    ReadToolDefinition,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.execution.dispatcher import Dispatcher, DispatchResult
from agentic_saga.execution.leases import LeaseService
from agentic_saga.execution.reconciliation import Reconciler
from agentic_saga.execution.runtime import DefinitionRuntimeMismatch, SagaRuntime, _saga_id
from agentic_saga.execution.unwind import EmergencyUnwinder
from agentic_saga.kernel.definitions import DefinitionCatalog, DefinitionUnavailable, SagaDefinition
from agentic_saga.kernel.ports import LeaseUnavailable, StoreCorruption
from agentic_saga.kernel.runtime import EventMetadataFactory, SagaKernel, StableIdFactory
from agentic_saga.storage import SQLiteKernelStore
from tests.support.durable_tool import DurableFakeTool
from tests.support.kernel_harness import HarnessCommand
from tests.unit.execution.test_runtime_loop import (
    Contexts,
    Harness,
    ReadAdapter,
    ReadCommand,
    ReadResult,
    _agent,
    _definition,
    _effect_registry,
    _harness,
    _policy,
    _read_registry,
    _requirement,
    _wait_for_agent_call,
)

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


@dataclass
class CrashOnceReadAdapter:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"adapter_version": "crash-read-v1"}, strict=True)

    async def read(self, command: ReadCommand) -> ReadResult:
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await asyncio.Event().wait()
        return ReadResult(state=f"seen:{command.public_id}")


@dataclass
class BlockingDispatcher(Dispatcher):
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return RedactionPolicy()

    async def dispatch_one(self, worker_id: str) -> DispatchResult:
        del worker_id
        return await self._block()

    async def dispatch_saga(self, saga_id: str, worker_id: str) -> DispatchResult:
        del saga_id, worker_id
        return await self._block()

    async def _block(self) -> DispatchResult:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_resume_repeats_only_unobserved_read_from_durable_command(tmp_path: Path) -> None:
    adapter = CrashOnceReadAdapter()
    registry = ToolRegistry(
        (ReadToolDefinition("observe_generic", ReadCommand, ReadResult, adapter),)
    )
    harness = _harness(tmp_path, registry)
    goal = SagaGoal(goal_id="goal_read_resume", text="Resume the read safely.", context={})
    task = asyncio.create_task(
        harness.runtime.start(
            definition=harness.definition,
            goal=goal,
            agent=_agent(["read"]),
        )
    )
    await adapter.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    result = await harness.runtime.resume(saga_id=_saga_id(goal), agent=_agent(["escalate"]))

    events = harness.store.read_events(result.saga_id)
    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert adapter.calls == 2
    assert sum(isinstance(item, ReadStarted) for item in events) == 1
    assert sum(isinstance(item, ReadObserved) for item in events) == 1
    assert sum(isinstance(item, AgentTurnReserved) for item in events) == 2


@pytest.mark.asyncio
async def test_resume_dispatches_durable_intent_before_next_agent_turn(tmp_path: Path) -> None:
    provider = DurableFakeTool.initialize(tmp_path / "provider.db", "mutate_generic")
    harness = _harness(tmp_path, _effect_registry(provider))
    blocking = BlockingDispatcher()
    runtime = SagaRuntime(
        kernel=harness.kernel,
        dispatcher=blocking,
        reconciler=harness.reconciler,
        unwinder=harness.unwinder,
        leases=harness.leases,
        definitions=DefinitionCatalog(),
        clock=harness.clock,
    )
    goal = SagaGoal(goal_id="goal_intent_resume", text="Deliver intent safely.", context={})
    active = asyncio.create_task(
        runtime.start(
            definition=harness.definition,
            goal=goal,
            agent=_agent(["effect"], tool_name="mutate_generic"),
        )
    )
    await blocking.entered.wait()
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    harness.clock.advance(timedelta(minutes=6))
    reopened = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)

    result = await _reopened_runtime(harness, reopened).resume(
        saga_id=_saga_id(goal), agent=_agent(["escalate"])
    )

    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert provider.execute_call_count == 1


@pytest.mark.asyncio
async def test_runtime_dispatches_its_saga_without_global_queue_starvation(
    tmp_path: Path,
) -> None:
    provider = DurableFakeTool.initialize(tmp_path / "provider.db", "mutate_generic")
    harness = _harness(tmp_path, _effect_registry(provider))
    blocking = BlockingDispatcher()
    first_runtime = SagaRuntime(
        kernel=harness.kernel,
        dispatcher=blocking,
        reconciler=harness.reconciler,
        unwinder=harness.unwinder,
        leases=harness.leases,
        definitions=DefinitionCatalog(),
        clock=harness.clock,
    )
    first_goal = SagaGoal(goal_id="goal_queue_first", text="Remain queued.", context={})
    first = asyncio.create_task(
        first_runtime.start(
            definition=harness.definition,
            goal=first_goal,
            agent=_agent(["effect"], tool_name="mutate_generic"),
        )
    )
    await blocking.entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    harness.clock.advance(timedelta(seconds=1))

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_queue_second", text="Dispatch independently.", context={}),
        agent=_agent(["effect", "escalate"], tool_name="mutate_generic"),
    )

    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert provider.execute_call_count == 1


@pytest.mark.asyncio
async def test_runtime_ledger_survives_backup_reopen_and_projection_replay(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    goal = SagaGoal(goal_id="goal_runtime_backup", text="Preserve evidence.", context={})
    result = await harness.runtime.start(
        definition=harness.definition,
        goal=goal,
        agent=_agent(["escalate"]),
    )
    backup = tmp_path / "runtime-backup.db"
    harness.store.backup_to(backup)

    restored = SQLiteKernelStore.open(backup)
    rebuilt = restored.rebuild_and_verify(result.saga_id)

    assert rebuilt == restored.load_snapshot(result.saga_id)
    assert rebuilt.status is SagaStatus.HUMAN_REQUIRED
    assert any(isinstance(item, AgentTurnReserved) for item in restored.read_events(result.saga_id))


@pytest.mark.asyncio
async def test_resume_rebuilds_and_rejects_projection_drift_before_agent(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    goal = SagaGoal(goal_id="goal_runtime_drift", text="Reject drift.", context={})
    result = await harness.runtime.start(
        definition=harness.definition,
        goal=goal,
        agent=_agent(["escalate"]),
    )
    snapshot = harness.store.load_snapshot(result.saga_id)
    tampered = snapshot.model_copy(update={"pending_approval": False})
    encoded = json.dumps(
        tampered.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute(
            "UPDATE sagas SET projection_json = ? WHERE saga_id = ?", (encoded, result.saga_id)
        )

    with pytest.raises(StoreCorruption, match="projection does not match"):
        await harness.runtime.resume(saga_id=result.saga_id, agent=_agent([]))


def _durable_registry(provider: DurableFakeTool) -> ToolRegistry:
    capabilities = ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.IRREVERSIBLE,
        partial_effects_possible=False,
    )
    definition = EffectToolDefinition(
        "durable_runtime",
        "durable-runtime-v1",
        "harness-command-v1",
        HarnessCommand,
        provider,
        capabilities,
        None,
    )
    return ToolRegistry((definition,))


@pytest.mark.asyncio
async def test_lost_effect_response_reconciles_before_fresh_agent_observation(
    tmp_path: Path,
) -> None:
    provider = DurableFakeTool.initialize(tmp_path / "provider.db", "durable_runtime")
    provider.enable_response_loss_after_effect()
    harness = _harness(tmp_path, _durable_registry(provider))
    agent = _agent(
        ["effect", "escalate"],
        tool_name="durable_runtime",
        arguments=_JSON_OBJECT.validate_python(
            {
                "resource_id": "resource_1",
                "quantity": 1,
                "mode": "standard",
                "secret_ref": "vault://services/credential/v1",
                "target_ids": [1],
                "scope": {"zones": ["primary"]},
            }
        ),
    )

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_runtime_lookup", text="Reconcile safely.", context={}),
        agent=agent,
    )

    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert provider.execute_call_count == 1
    assert provider.reconcile_call_count == 1
    assert agent.calls == 2


def _reopened_runtime(
    harness: Harness,
    store: SQLiteKernelStore,
    definition: SagaDefinition | None = None,
    worker_id: str | None = None,
) -> SagaRuntime:
    selected = definition or harness.definition
    kernel = SagaKernel(
        store=store,
        policy=selected.policy,
        registry=selected.registry,
        policy_context_provider=Contexts(selected.budget),
        event_metadata_factory=EventMetadataFactory(harness.clock),
        id_factory=StableIdFactory(namespace=b"runtime-loop"),
        redaction_policy=selected.redaction_policy,
    )
    leases = LeaseService(store)
    return SagaRuntime(
        kernel=kernel,
        dispatcher=Dispatcher(
            store,
            selected.registry,
            clock=harness.clock,
            redaction_policy=selected.redaction_policy,
        ),
        reconciler=Reconciler(
            store,
            selected.registry,
            lease_service=leases,
            clock=harness.clock,
            redaction_policy=selected.redaction_policy,
        ),
        unwinder=EmergencyUnwinder(store, clock=harness.clock),
        leases=leases,
        definitions=DefinitionCatalog((selected,)),
        clock=harness.clock,
        worker_id=worker_id,
    )


@pytest.mark.asyncio
async def test_same_worker_label_does_not_share_live_runtime_authority(tmp_path: Path) -> None:
    # Given two process-like runtimes with the same operational worker label
    harness = _harness(tmp_path)
    first = _reopened_runtime(harness, harness.store, worker_id="orders-worker")
    second = _reopened_runtime(harness, harness.store, worker_id="orders-worker")
    blocked = _agent([], mode="block")
    goal = SagaGoal(goal_id="goal_unique_authority", text="Hold authority.", context={})
    running = asyncio.create_task(
        first.start(definition=harness.definition, goal=goal, agent=blocked)
    )
    await _wait_for_agent_call(blocked)

    # When / Then the second process cannot renew or share the first process's lease
    with pytest.raises(LeaseUnavailable):
        await second.resume(saga_id=_saga_id(goal), agent=_agent(["escalate"]))
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


@pytest.mark.asyncio
async def test_maximum_worker_label_still_produces_bounded_unique_authority(tmp_path: Path) -> None:
    # Given
    harness = _harness(tmp_path)
    runtime = _reopened_runtime(harness, harness.store, worker_id="w" * 200)

    # When
    result = await runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_max_worker", text="Bound owner identity.", context={}),
        agent=_agent(["escalate"]),
    )

    # Then
    assert result.state is SagaStatus.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_reopen_of_human_saga_makes_zero_agent_or_adapter_calls(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    goal = SagaGoal(goal_id="goal_runtime_reopen", text="Remain quiescent.", context={})
    first = await harness.runtime.start(
        definition=harness.definition,
        goal=goal,
        agent=_agent(["escalate"]),
    )
    reopened = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)

    result = await _reopened_runtime(harness, reopened).resume(
        saga_id=first.saga_id,
        agent=_agent([]),
    )

    assert result.state is SagaStatus.HUMAN_REQUIRED


def _changed_policy(definition: SagaDefinition) -> SagaDefinition:
    return replace(definition, policy=_policy(definition.registry, hide_tools=True))


def _changed_tools(definition: SagaDefinition) -> SagaDefinition:
    del definition
    return _definition(_read_registry(ReadAdapter()))


def _changed_invariants(definition: SagaDefinition) -> SagaDefinition:
    return replace(definition, success_invariants=_requirement("different_success"))


def _changed_budget(definition: SagaDefinition) -> SagaDefinition:
    budget = definition.budget.model_copy(update={"token_limit": 401})
    return replace(definition, budget=budget)


def _changed_redaction(definition: SagaDefinition) -> SagaDefinition:
    policy = RedactionPolicy(sensitive_keys=("email",))
    return replace(definition, redaction_policy=policy)


@pytest.mark.parametrize(
    "change",
    (_changed_policy, _changed_tools, _changed_invariants, _changed_budget, _changed_redaction),
    ids=("policy", "tools", "invariants", "budget", "redaction"),
)
@pytest.mark.asyncio
async def test_fresh_process_resume_rejects_same_version_with_changed_definition(
    tmp_path: Path, change: object
) -> None:
    # Given
    harness = _harness(tmp_path)
    goal = SagaGoal(goal_id="goal_runtime_fingerprint", text="Pin behavior.", context={})
    first = await harness.runtime.start(
        definition=harness.definition,
        goal=goal,
        agent=_agent(["escalate"]),
    )
    assert callable(change)
    changed = change(harness.definition)
    reopened = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)

    # When / Then
    with pytest.raises(DefinitionRuntimeMismatch, match="fingerprint"):
        await _reopened_runtime(harness, reopened, changed).resume(
            saga_id=first.saga_id,
            agent=_agent([]),
        )


@pytest.mark.asyncio
async def test_same_process_resume_rejects_mutated_adapter_configuration(tmp_path: Path) -> None:
    # Given
    adapter = ReadAdapter()
    harness = _harness(tmp_path, _read_registry(adapter))
    goal = SagaGoal(goal_id="goal_mutated_adapter", text="Pin adapter config.", context={})
    first = await harness.runtime.start(
        definition=harness.definition,
        goal=goal,
        agent=_agent(["escalate"]),
    )
    adapter.revision = "runtime-read-v2"

    # When / Then
    with pytest.raises(DefinitionRuntimeMismatch, match="mutated"):
        await harness.runtime.resume(saga_id=first.saga_id, agent=_agent([]))


@pytest.mark.asyncio
async def test_expired_lease_takeover_never_reenters_unresolved_agent_turn(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    goal = SagaGoal(goal_id="goal_runtime_takeover", text="Take over safely.", context={})
    blocking = _agent([], mode="block")
    active = asyncio.create_task(
        harness.runtime.start(definition=harness.definition, goal=goal, agent=blocking)
    )
    await _wait_for_agent_call(blocking)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    harness.clock.advance(timedelta(minutes=6))
    reopened = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)

    result = await _reopened_runtime(harness, reopened).resume(
        saga_id=_saga_id(goal), agent=_agent([])
    )

    assert result.state is SagaStatus.HUMAN_REQUIRED
    events = reopened.read_events(result.saga_id)
    assert sum(isinstance(item, AgentTurnReserved) for item in events) == 1


@pytest.mark.asyncio
async def test_resume_fails_closed_when_historical_definition_is_unavailable(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    goal = SagaGoal(goal_id="goal_definition_missing", text="Pin code.", context={})
    first = await harness.runtime.start(
        definition=harness.definition,
        goal=goal,
        agent=_agent(["escalate"]),
    )
    runtime = SagaRuntime(
        kernel=harness.kernel,
        dispatcher=harness.dispatcher,
        reconciler=harness.reconciler,
        unwinder=harness.unwinder,
        leases=harness.leases,
        definitions=DefinitionCatalog(),
        clock=harness.clock,
    )

    with pytest.raises(DefinitionUnavailable):
        await runtime.resume(saga_id=first.saga_id, agent=_agent([]))
