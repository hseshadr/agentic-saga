from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.tools import EffectToolDefinition
from agentic_saga.execution import (
    EmergencyUnwinder,
    LeaseService,
    Reconciler,
    ReconciliationResult,
)
from agentic_saga.execution.unwind import UnwindAction, UnwindDecision, UnwindTrigger
from agentic_saga.kernel.ports import Lease
from tests.support.durable_tool import DurableToolFault
from tests.support.kernel_harness import TOOL_NAME, KernelHarness


def _non_fencing_harness(directory: Path) -> KernelHarness:
    seed = directory / "seed"
    target = directory / "non-fencing"
    seed.mkdir()
    target.mkdir()
    initial = KernelHarness.create(seed)
    definition = initial.registry.definition(TOOL_NAME)
    assert isinstance(definition, EffectToolDefinition)
    capabilities = definition.capabilities.model_copy(update={"fencing_supported": False})
    return KernelHarness.create_with_capabilities(target, capabilities)


def _non_fencing_reversible_harness(directory: Path) -> KernelHarness:
    initial = _non_fencing_harness(directory)
    definition = initial.registry.definition(TOOL_NAME)
    assert isinstance(definition, EffectToolDefinition)
    target = directory / "reversible"
    target.mkdir()
    return KernelHarness.create_reversible_with_capabilities(target, definition.capabilities)


async def _pending_unknown(harness: KernelHarness) -> Lease:
    await harness.dispatch_with_response_loss()
    check_after = harness.clock.now() + timedelta(minutes=30)
    harness.provider.set_reconciliation_pending(check_after)
    harness.clock.advance(timedelta(minutes=6))
    return LeaseService(harness.store).acquire(
        harness.lease.saga_id, "worker-new", timedelta(minutes=5)
    )


def _unwind(harness: KernelHarness, lease: Lease, trigger: UnwindTrigger) -> UnwindDecision:
    unwinder = EmergencyUnwinder(harness.store, clock=harness.clock)
    return unwinder.execute(harness.lease.saga_id, harness.registry, trigger, lease)


async def _reconcile(harness: KernelHarness, lease: Lease) -> ReconciliationResult:
    reconciler = Reconciler(harness.store, harness.registry, clock=harness.clock)
    return await reconciler.reconcile_one(lease.owner)


def _make_lookup_stable(harness: KernelHarness) -> None:
    harness.clock.advance(timedelta(minutes=30))
    harness.provider.set_reconciliation_confirmed()


def _assert_waiting(
    harness: KernelHarness, result: ReconciliationResult, decision: UnwindDecision
) -> None:
    assert result.decision is not None and result.decision.action == "wait"
    assert decision.action is UnwindAction.RECONCILE
    assert harness.provider.effect_count(harness.operation_id) == 1
    assert {call.operation_id for call in harness.provider.calls} == {harness.operation_id}
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    assert snapshot.status is SagaStatus.RECONCILING_UNKNOWN


@pytest.mark.asyncio
async def test_non_fencing_late_work_preserves_operation_identity_and_blocks_compensation(
    tmp_path: Path,
) -> None:
    harness = _non_fencing_harness(tmp_path)
    assert not harness.provider.fencing_supported
    lease = await _pending_unknown(harness)
    decision = _unwind(harness, lease, UnwindTrigger.BUDGET_EXHAUSTED)
    result = await _reconcile(harness, lease)
    _assert_waiting(harness, result, decision)


@pytest.mark.asyncio
async def test_non_fencing_unknown_never_compensates_before_stable_lookup(tmp_path: Path) -> None:
    harness = _non_fencing_reversible_harness(tmp_path)
    pending_lease = await _pending_unknown(harness)
    pending = await _reconcile(harness, pending_lease)
    blocked = _unwind(harness, pending_lease, UnwindTrigger.BUDGET_EXHAUSTED)
    _make_lookup_stable(harness)
    confirmed_lease = LeaseService(harness.store).acquire(
        harness.lease.saga_id, "worker-new", timedelta(minutes=5)
    )
    confirmed = await _reconcile(harness, confirmed_lease)
    eligible = _unwind(harness, confirmed_lease, UnwindTrigger.AGENT_UNAVAILABLE)
    assert pending.decision is not None and pending.decision.action == "wait"
    assert blocked.action is UnwindAction.RECONCILE
    assert confirmed.decision is not None and confirmed.decision.action == "confirm"
    assert eligible.action is UnwindAction.COMPENSATE
    assert all(not call.forward_receipts for call in harness.provider.calls)


@pytest.mark.asyncio
async def test_non_fencing_confirmed_absence_retries_exactly_the_same_operation_id(
    tmp_path: Path,
) -> None:
    harness = _non_fencing_harness(tmp_path)
    harness.provider.set_fault(DurableToolFault.NO_EFFECT_FAILURE)
    first = await harness.dispatcher.dispatch_saga(harness.lease.saga_id, harness.lease.owner)
    retried = await Reconciler(harness.store, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )
    harness.provider.set_fault(DurableToolFault.NONE)
    second = await harness.dispatcher.dispatch_saga(harness.lease.saga_id, harness.lease.owner)
    calls = harness.provider.calls
    assert first.failure is not None and first.failure.code == "outcome_unknown"
    assert retried.decision is not None and retried.decision.action == "retry_same_id"
    assert second.failure is None
    assert {call.operation_id for call in calls} == {harness.operation_id}
    assert tuple(call.delivery_attempt for call in calls) == (1, 2)
    assert harness.provider.effect_count(harness.operation_id) == 1
