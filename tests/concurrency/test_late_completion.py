from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from agentic_saga.contracts.events import EffectOutcomeRecorded
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.tools import EffectToolDefinition
from agentic_saga.execution import Dispatcher, LeaseService, Reconciler
from agentic_saga.execution.unwind import EmergencyUnwinder, UnwindAction, UnwindTrigger
from agentic_saga.kernel.state import OperationStatus
from tests.support.durable_tool import DurableToolFault
from tests.support.kernel_harness import TOOL_NAME, KernelHarness


async def _wait_for_effect(harness: KernelHarness) -> None:
    while harness.provider.execute_call_count == 0:  # noqa: ASYNC110 - provider barrier
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_stale_late_completion_is_rejected_then_resolved_without_redelivery(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create(tmp_path)
    harness.provider.set_fault(DurableToolFault.BLOCK_AFTER_EFFECT)
    registry = harness.registry
    dispatch = asyncio.create_task(
        Dispatcher(harness.store, registry, clock=harness.clock).dispatch_one(harness.lease.owner)
    )
    await _wait_for_effect(harness)
    harness.clock.advance(timedelta(minutes=6))
    lease = LeaseService(harness.store).acquire(
        harness.lease.saga_id, "worker-b", timedelta(minutes=5)
    )

    decision = EmergencyUnwinder(harness.store, clock=harness.clock).execute(
        harness.lease.saga_id, registry, UnwindTrigger.AGENT_UNAVAILABLE, lease
    )
    harness.provider.release_blocked_effect()
    late = await dispatch
    stale_events = tuple(
        event
        for event in harness.store.read_events(harness.lease.saga_id)
        if isinstance(event, EffectOutcomeRecorded)
    )
    parked = await Dispatcher(harness.store, registry, clock=harness.clock).dispatch_one(
        lease.owner
    )
    reconciled = await Reconciler(harness.store, registry, clock=harness.clock).reconcile_one(
        lease.owner
    )

    assert decision.action is UnwindAction.RECONCILE
    assert late.failure is not None and late.failure.code == "authority_lost"
    assert stale_events == ()
    assert parked.failure is not None and parked.failure.code == "prior_dispatch_unknown"
    assert reconciled.failure is None
    assert harness.provider.execute_call_count == 1
    assert harness.provider.effect_count(harness.operation_id) == 1
    assert (
        harness.store.load_snapshot(harness.lease.saga_id).operations[harness.operation_id].status
        is OperationStatus.EFFECT_CONFIRMED
    )


@pytest.mark.asyncio
async def test_non_fencing_provider_stays_reconciling_until_stable_lookup(
    tmp_path: Path,
) -> None:
    initial = KernelHarness.create(tmp_path)
    definition = initial.registry.definition(TOOL_NAME)
    assert isinstance(definition, EffectToolDefinition)
    other = tmp_path / "non-fencing"
    other.mkdir()
    capabilities = definition.capabilities.model_copy(update={"fencing_supported": False})
    harness = KernelHarness.create_with_capabilities(other, capabilities)
    await harness.dispatch_with_response_loss()
    harness.provider.set_reconciliation_pending(harness.clock.now() + timedelta(minutes=30))

    first = await Reconciler(harness.store, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )
    decision = EmergencyUnwinder(harness.store, clock=harness.clock).execute(
        harness.lease.saga_id,
        harness.registry,
        UnwindTrigger.BUDGET_EXHAUSTED,
        harness.lease,
    )

    assert first.decision is not None and first.decision.action == "wait"
    assert decision.action is UnwindAction.RECONCILE
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    assert snapshot.status is SagaStatus.RECONCILING_UNKNOWN
    assert harness.provider.execute_call_count == 1

    harness.clock.advance(timedelta(minutes=30))
    harness.provider.set_reconciliation_confirmed()
    second = await Reconciler(harness.store, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    assert second.failure is None
    assert harness.provider.execute_call_count == 1
    assert harness.provider.effect_count(harness.operation_id) == 1
