from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agentic_saga.contracts.actions import ToolCall
from agentic_saga.contracts.events import SagaCreated, SagaStarted
from agentic_saga.contracts.outcomes import EffectConfirmed, EffectOutcome, NoEffectConfirmed
from agentic_saga.contracts.tools import EffectContext
from agentic_saga.execution import Dispatcher, DispatchResult, LeaseService
from agentic_saga.kernel.ports import Lease, TransitionBatch
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import SagaSnapshot
from tests.support.durable_tool import DurableFakeTool, DurableToolCall
from tests.support.kernel_harness import STEP_ID, KernelHarness


class _VersionedResourceCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    expected_provider_version: int = Field(strict=True, ge=0)


def _second_operation_id(operation_id: str) -> str:
    replacement = "0" if operation_id[-1] != "0" else "1"
    return f"{operation_id[:-1]}{replacement}"


def _context(saga_id: str, operation_id: str, fence_token: int) -> EffectContext:
    return EffectContext(
        saga_id=saga_id,
        step_instance_id=STEP_ID,
        operation_id=operation_id,
        fence_token=fence_token,
        delivery_attempt=1,
    )


def _created_event(harness: KernelHarness, saga_id: str) -> SagaCreated:
    return SagaCreated(
        event_id="evt_0000000000008009",
        saga_id=saga_id,
        saga_seq=1,
        definition_version="harness-saga-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000008009",
        recorded_at=harness.clock.now(),
        definition_name="harness",
        definition_fingerprint="f" * 64,
        redacted_goal={"order_id": "order_9"},
    )


def _create_second_saga(harness: KernelHarness, saga_id: str) -> None:
    harness.store.create_saga(_created_event(harness, saga_id))


def _started_event(
    harness: KernelHarness, saga_id: str, lease: Lease, snapshot: SagaSnapshot
) -> SagaStarted:
    return SagaStarted(
        event_id="evt_0000000000008010",
        saga_id=saga_id,
        saga_seq=2,
        definition_version=snapshot.definition_version,
        fence_token=lease.fence_token,
        actor="kernel",
        trace_id="trace_0000000000008009",
        recorded_at=harness.clock.now(),
    )


def _start_batch(
    saga_id: str, lease: Lease, snapshot: SagaSnapshot, started: SagaStarted
) -> TransitionBatch:
    return TransitionBatch(
        transition_id="txn_0000000000008009",
        saga_id=saga_id,
        expected_seq=snapshot.seq,
        expected_fence_token=lease.fence_token,
        lease_owner=lease.owner,
        events=(started,),
        projection=reduce_event(snapshot, started),
    )


def _start_second_saga(harness: KernelHarness, saga_id: str) -> None:
    lease = LeaseService(harness.store).acquire(saga_id, "worker-second", timedelta(minutes=5))
    snapshot = harness.store.load_snapshot(saga_id)
    started = _started_event(harness, saga_id, lease, snapshot)
    harness.store.commit_transition(_start_batch(saga_id, lease, snapshot, started))


def _submit_second_intent(harness: KernelHarness, saga_id: str) -> str:
    lease = LeaseService(harness.store).acquire(saga_id, "worker-second", timedelta(minutes=5))
    proposal = ToolCall(
        proposal_id="proposal_00008009",
        tool_name="durable_action",
        arguments=harness.command.model_dump(mode="json") | {"resource_id": "resource_9"},
        based_on_saga_seq=2,
        rationale="Apply a separate durable resource action.",
    )
    result = harness.kernel.submit_proposal(saga_id, proposal, lease)
    assert result.accepted
    assert result.operation_id is not None
    return result.operation_id


def _assert_saga_stream(harness: KernelHarness, saga_id: str) -> None:
    events = harness.store.read_events(saga_id)
    assert {event.saga_id for event in events} == {saga_id}
    assert tuple(event.saga_seq for event in events) == tuple(range(1, len(events) + 1))
    assert harness.store.rebuild_and_verify(saga_id) == harness.store.load_snapshot(saga_id)


def _assert_unversioned_race(harness: KernelHarness, other_id: str) -> None:
    calls = harness.provider.calls
    assert harness.provider.effect_count(harness.operation_id) == 1
    assert harness.provider.effect_count(other_id) == 1
    assert sorted(harness.provider.accepted_fences) == [1, 2]
    assert {call.saga_id for call in calls} == {
        "saga_0000000000008001",
        "saga_0000000000008002",
    }
    assert {call.resource_ref for call in calls} == {"resource_8"}


def _assert_scoped_receipts(harness: KernelHarness, calls: tuple[DurableToolCall, ...]) -> None:
    call_receipts = {call.operation_id: call.receipt_ref for call in calls}
    effect_receipts = {
        effect.operation_id: effect.receipt_ref for effect in harness.provider.effects
    }
    assert call_receipts == effect_receipts
    assert len(set(effect_receipts.values())) == 2


def _assert_scoped_completion(
    harness: KernelHarness, other_saga: str, other_operation: str
) -> None:
    calls = harness.provider.calls
    operations = {harness.operation_id, other_operation}
    expected_scopes = {
        (harness.lease.saga_id, harness.operation_id, "resource_8"),
        (other_saga, other_operation, "resource_9"),
    }
    assert {call.operation_id for call in calls} == operations
    actual_scopes = {(call.saga_id, call.operation_id, call.resource_ref) for call in calls}
    assert actual_scopes == expected_scopes
    _assert_scoped_receipts(harness, calls)
    assert all(harness.provider.effect_count(item) == 1 for item in operations)
    _assert_saga_stream(harness, harness.lease.saga_id)
    _assert_saga_stream(harness, other_saga)
    assert harness.store.integrity_check() == "ok"
    assert harness.store.foreign_key_check() == ()


async def _overlap(
    provider: DurableFakeTool,
    command: BaseModel,
    first: EffectContext,
    second: EffectContext,
) -> tuple[EffectOutcome, EffectOutcome]:
    entered, release = provider.blocking_barrier()
    first_task = asyncio.create_task(provider.execute(command, first))
    await entered.wait()
    second_outcome = await provider.execute(command, second)
    release.set()
    return await first_task, second_outcome


async def _dispatch_scoped(
    harness: KernelHarness, other_saga: str
) -> tuple[DispatchResult, DispatchResult]:
    dispatcher = Dispatcher(harness.store, harness.registry, clock=harness.clock)
    primary = await dispatcher.dispatch_saga(harness.lease.saga_id, harness.lease.owner)
    assert harness.store.runnable_count(other_saga) == 1
    second = await dispatcher.dispatch_saga(other_saga, "worker-second")
    return primary, second


def _versioned_case(
    harness: KernelHarness,
) -> tuple[_VersionedResourceCommand, EffectContext, EffectContext]:
    resource = "inventory:public-last-unit"
    harness.provider.set_fencing_supported(False)
    harness.provider.set_resource_inventory(resource, remaining=1)
    first = _context("saga_0000000000008001", harness.operation_id, 2)
    second = _context("saga_0000000000008002", _second_operation_id(harness.operation_id), 1)
    command = _VersionedResourceCommand(resource_id=resource, expected_provider_version=0)
    return command, first, second


def _assert_versioned_calls(harness: KernelHarness, other_id: str) -> None:
    winner = tuple(call for call in harness.provider.calls if call.operation_id == other_id)
    loser = tuple(
        call for call in harness.provider.calls if call.operation_id == harness.operation_id
    )
    assert len(winner) == 2 and len(loser) == 1
    assert {call.saga_id for call in winner} == {"saga_0000000000008002"}
    assert loser[0].saga_id == "saga_0000000000008001"
    assert loser[0].receipt_ref is None
    assert winner[0].receipt_ref is not None
    assert winner[0].receipt_ref == winner[1].receipt_ref


def _assert_versioned_outcomes(
    harness: KernelHarness,
    other_id: str,
    outcomes: tuple[EffectOutcome, EffectOutcome],
    retry: EffectOutcome,
) -> None:
    assert isinstance(outcomes[0], NoEffectConfirmed)
    assert isinstance(outcomes[1], EffectConfirmed)
    assert retry == outcomes[1]
    assert harness.provider.effect_count(harness.operation_id) == 0
    assert harness.provider.effect_count(other_id) == 1
    _assert_versioned_calls(harness, other_id)


@pytest.mark.asyncio
async def test_unversioned_shared_resource_race_is_not_misrepresented_as_kernel_isolation(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create(tmp_path)
    other_id = _second_operation_id(harness.operation_id)
    harness.provider.set_fencing_supported(False)
    first = _context("saga_0000000000008001", harness.operation_id, 2)
    second = _context("saga_0000000000008002", other_id, 1)
    await _overlap(harness.provider, harness.command, first, second)
    _assert_unversioned_race(harness, other_id)
    assert b"vault://services/credential/v1" not in harness.provider.durable_bytes()


@pytest.mark.asyncio
async def test_saga_scoped_workers_do_not_claim_each_others_outbox_rows(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    other_saga = "saga_0000000000008002"
    _create_second_saga(harness, other_saga)
    _start_second_saga(harness, other_saga)
    other_operation = _submit_second_intent(harness, other_saga)
    primary, second = await _dispatch_scoped(harness, other_saga)
    assert primary.failure is None
    assert primary.operation_id == harness.operation_id
    assert second.failure is None
    assert second.operation_id == other_operation
    assert harness.store.runnable_count(harness.lease.saga_id) == 0
    assert harness.store.runnable_count(other_saga) == 0
    assert other_operation != harness.operation_id
    _assert_scoped_completion(harness, other_saga, other_operation)


@pytest.mark.asyncio
async def test_versioned_provider_resource_allows_one_effect_for_two_sagas(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create(tmp_path)
    command, first, second = _versioned_case(harness)
    outcomes = await _overlap(harness.provider, command, first, second)
    retry = await harness.provider.execute(command, second)
    _assert_versioned_outcomes(harness, second.operation_id, outcomes, retry)


@pytest.mark.asyncio
async def test_secret_shaped_resource_is_rejected_without_durable_attempt(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    command = _VersionedResourceCommand(
        resource_id="Bearer super-secret", expected_provider_version=0
    )
    context = _context("saga_0000000000008001", harness.operation_id, 1)
    with pytest.raises(ValueError, match="public"):
        await harness.provider.execute(command, context)
    assert harness.provider.calls == ()
    assert b"Bearer super-secret" not in harness.provider.durable_bytes()
