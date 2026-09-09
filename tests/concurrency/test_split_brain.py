from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from agentic_saga.contracts.tools import EffectContext
from agentic_saga.execution import (
    Dispatcher,
    DispatchResult,
    LeaseService,
    Reconciler,
    ReconciliationResult,
)
from agentic_saga.kernel.ports import Lease
from agentic_saga.kernel.state import OperationStatus
from tests.support.kernel_harness import SAGA_ID, STEP_ID, KernelHarness


def _assert_store_is_consistent(harness: KernelHarness) -> None:
    events = harness.store.read_events(SAGA_ID)
    assert harness.store.integrity_check() == "ok"
    assert harness.store.foreign_key_check() == ()
    assert tuple(event.saga_seq for event in events) == tuple(range(1, len(events) + 1))
    assert harness.store.rebuild_and_verify(SAGA_ID) == harness.store.load_snapshot(SAGA_ID)


async def _accept_new_fence(harness: KernelHarness, fence_token: int) -> None:
    context = EffectContext(
        saga_id=SAGA_ID,
        step_instance_id=STEP_ID,
        operation_id=harness.operation_id,
        fence_token=fence_token,
        delivery_attempt=2,
    )
    await harness.provider.execute(harness.command, context)


async def _blocked_dispatch(
    harness: KernelHarness,
) -> tuple[asyncio.Event, asyncio.Task[DispatchResult]]:
    entered, release = harness.provider.blocking_barrier()
    task = asyncio.create_task(harness.dispatcher.dispatch_saga(SAGA_ID, harness.lease.owner))
    await entered.wait()
    return release, task


def _take_over(harness: KernelHarness) -> Lease:
    harness.clock.advance(timedelta(minutes=6))
    return LeaseService(harness.store).acquire(SAGA_ID, "worker-new", timedelta(minutes=5))


async def _dispatch(harness: KernelHarness, lease: Lease) -> DispatchResult:
    dispatcher = Dispatcher(harness.store, harness.registry, clock=harness.clock)
    return await dispatcher.dispatch_saga(SAGA_ID, lease.owner)


async def _reconcile(harness: KernelHarness, lease: Lease) -> ReconciliationResult:
    reconciler = Reconciler(harness.store, harness.registry, clock=harness.clock)
    return await reconciler.reconcile_one(lease.owner)


def _assert_stale_outcome(
    harness: KernelHarness, stale: DispatchResult, parked: DispatchResult, lease: Lease
) -> None:
    assert stale.failure is not None and stale.failure.code == "authority_lost"
    assert parked.failure is not None and parked.failure.code == "prior_dispatch_unknown"
    assert harness.provider.accepted_fences == (lease.fence_token,)
    assert harness.provider.execute_call_count == 2
    assert harness.provider.effect_count(harness.operation_id) == 1


def _assert_confirmed(harness: KernelHarness) -> None:
    operation = harness.store.load_snapshot(SAGA_ID).operations[harness.operation_id]
    assert operation.status is OperationStatus.EFFECT_CONFIRMED


@pytest.mark.asyncio
async def test_stale_worker_cannot_append_after_takeover_or_duplicate_provider_effect(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create(tmp_path)
    release, old = await _blocked_dispatch(harness)
    lease = _take_over(harness)
    await _accept_new_fence(harness, lease.fence_token)
    events_before = harness.store.read_events(SAGA_ID)
    release.set()
    stale = await old
    parked = await _dispatch(harness, lease)
    await _reconcile(harness, lease)
    assert harness.store.read_events(SAGA_ID)[: len(events_before)] == events_before
    _assert_stale_outcome(harness, stale, parked, lease)
    _assert_confirmed(harness)
    _assert_store_is_consistent(harness)


@pytest.mark.asyncio
async def test_old_return_cannot_break_retried_live_delivery_after_takeover(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    release, old = await _blocked_dispatch(harness)
    lease = _take_over(harness)
    parked = await _dispatch(harness, lease)
    recovered = await _reconcile(harness, lease)
    live = await _dispatch(harness, lease)
    release.set()
    stale = await old
    assert recovered.decision is not None and recovered.decision.action == "retry_same_id"
    assert live.failure is None
    _assert_stale_outcome(harness, stale, parked, lease)
    assert harness.store.runnable_count(SAGA_ID) == 0
    assert harness.store.reconciliation_job(harness.operation_id) is not None
    _assert_store_is_consistent(harness)
