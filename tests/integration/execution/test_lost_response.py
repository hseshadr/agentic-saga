from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from typing import cast

import pytest
from pydantic import BaseModel

from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import JsonObject, Reversibility, canonical_json, sha256_json
from agentic_saga.contracts.events import SagaCreated
from agentic_saga.contracts.outcomes import (
    EffectOutcome,
    ReconcileEffectConfirmed,
    ReconciliationOutcome,
)
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.execution.dispatcher import Dispatcher
from agentic_saga.execution.leases import LeaseService
from agentic_saga.execution.reconciliation import (
    Reconciler,
    ReconciliationResult,
    RecoveryHorizon,
)
from agentic_saga.kernel.ports import (
    ClaimedCommand,
    InjectedStoreFailure,
    Lease,
    OutboxCommand,
    OutboxState,
    ReconciliationJob,
    ReconciliationJobState,
    RecoveryPolicy,
    StoreConflict,
    StoreCorruption,
    StoreFailpoint,
    TransitionBatch,
)
from agentic_saga.kernel.state import OperationRecord, OperationStatus, SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.support.durable_tool import DurableFakeTool
from tests.support.kernel_harness import TOOL_NAME, KernelHarness
from tests.support.reconciliation_adapter import ProbeAdapter, initialize_probe, probe_counts


class _OneShotFailpoint:
    def __init__(self, target: StoreFailpoint) -> None:
        self.target = target
        self.fired = False

    def hit(self, point: StoreFailpoint) -> None:
        if point is self.target and not self.fired:
            self.fired = True
            raise InjectedStoreFailure(point)


class _LookupProbeAdapter:
    def __init__(self, provider: DurableFakeTool, probe: ProbeAdapter) -> None:
        self._provider = provider
        self._probe = probe

    async def execute(self, command: BaseModel, context: EffectContext) -> EffectOutcome:
        return await self._provider.execute(command, context)

    async def reconcile(
        self, command: BaseModel, context: ReconcileContext
    ) -> ReconciliationOutcome:
        return await self._probe.reconcile(command, context)


def _race_claim(path: Path, barrier: Barrier, owner: str) -> ReconciliationJob | None:
    store = SQLiteKernelStore.open(path)
    barrier.wait()
    policy = RecoveryHorizon(
        maximum_retry_delay=timedelta(minutes=5),
        operator_response_window=timedelta(hours=1),
        clock_skew_allowance=timedelta(seconds=30),
    ).public_policy()
    digest = sha256_json(policy.model_dump(mode="json"))
    return store.claim_reconciliation(owner, timedelta(seconds=30), policy, digest)


@pytest.mark.asyncio
async def test_should_create_one_job_atomically_with_unknown_disposition(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)

    # When
    await harness.dispatch_with_response_loss()
    job = harness.store.reconciliation_job(harness.operation_id)

    # Then
    assert job is not None
    assert job.state is ReconciliationJobState.DUE
    assert job.operation_id == harness.operation_id


@pytest.mark.asyncio
async def test_should_allow_one_barrier_concurrent_due_job_claim(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    barrier = Barrier(2)

    # When
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda owner: _race_claim(harness.store.path, barrier, owner),
                ("worker-b", "worker-c"),
            )
        )

    # Then
    assert sum(result is not None for result in results) == 1


@pytest.mark.asyncio
async def test_should_preserve_reconciliation_job_through_open_backup_restore(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    backup = tmp_path / "backup.db"
    restored_path = tmp_path / "restored.db"

    harness.store.backup_to(backup)
    restored = SQLiteKernelStore.restore_from(backup, restored_path)

    job = restored.reconciliation_job(harness.operation_id)
    assert job is not None
    assert job.state is ReconciliationJobState.DUE


@pytest.mark.asyncio
async def test_should_reconcile_lost_success_without_second_effect(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)

    # When
    await harness.dispatch_with_response_loss()
    await harness.restart_and_reconcile()

    # Then
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    assert snapshot.operations[harness.operation_id].status is OperationStatus.EFFECT_CONFIRMED
    assert harness.provider.effect_count(harness.operation_id) == 1
    assert harness.provider.execute_call_count == 1
    assert harness.provider.reconcile_call_count == 1
    job = harness.store.reconciliation_job(harness.operation_id)
    assert job is not None and job.lookup_attempt == 1 and job.lookup_started_at is not None


@pytest.mark.asyncio
async def test_reconciliation_result_preserves_operation_identity(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()

    result = await _reconcile(harness)

    assert result.operation_id == harness.operation_id


@pytest.mark.asyncio
async def test_should_escalate_opaque_provider_without_blind_retry(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    harness.provider.disable_reconciliation_and_deduplication()

    # When
    await harness.dispatch_with_response_loss()
    await harness.restart_and_reconcile()

    # Then
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    assert snapshot.status is SagaStatus.HUMAN_REQUIRED
    assert harness.provider.execute_call_count == 1
    assert harness.provider.reconcile_call_count == 0


@pytest.mark.asyncio
async def test_should_not_lookup_pending_job_before_authoritative_due_time(
    tmp_path: Path,
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    due = harness.clock.now() + timedelta(minutes=10)
    harness.provider.set_reconciliation_pending(due)
    await harness.dispatch_with_response_loss()

    # When
    await harness.restart_and_reconcile()
    calls = harness.provider.reconcile_call_count
    early = await _reconcile(harness)

    # Then
    assert early.failure is not None and early.failure.code == "no_work"
    assert harness.provider.reconcile_call_count == calls == 1
    harness.provider.set_reconciliation_confirmed()
    harness.clock.advance(timedelta(minutes=10))
    await _reconcile(harness)
    assert harness.provider.reconcile_call_count == 2


@pytest.mark.asyncio
async def test_should_escalate_changed_recovery_horizon_without_second_lookup(
    tmp_path: Path,
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    due = harness.clock.now() + timedelta(minutes=1)
    harness.provider.set_reconciliation_pending(due)
    await harness.dispatch_with_response_loss()
    await harness.restart_and_reconcile()
    first_job = harness.store.reconciliation_job(harness.operation_id)
    changed = _changed_horizon()
    harness.clock.advance(timedelta(minutes=1))

    # When
    result = await _reconcile(harness, changed)

    # Then
    assert first_job is not None and first_job.recovery_policy_digest is not None
    assert result.failure is not None and result.failure.code == "human_required"
    assert harness.provider.reconcile_call_count == 1


@pytest.mark.asyncio
async def test_should_requeue_same_identity_and_increment_next_delivery_attempt(
    tmp_path: Path,
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    harness.provider.set_reconciliation_no_effect()

    # When
    await harness.restart_and_reconcile()
    before = harness.store.load_snapshot(harness.lease.saga_id).operations[harness.operation_id]
    harness.provider.set_reconciliation_confirmed()
    await harness.dispatcher.dispatch_one(harness.lease.owner)

    # Then
    after = harness.store.load_snapshot(harness.lease.saga_id).operations[harness.operation_id]
    job = harness.store.reconciliation_job(harness.operation_id)
    _assert_same_identity_retry(harness, before, after, job)


@pytest.mark.asyncio
async def test_should_rearm_requeued_job_after_second_lost_response(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    harness.provider.set_reconciliation_no_effect()
    await harness.restart_and_reconcile()

    # When
    await harness.dispatch_with_response_loss()
    harness.provider.set_reconciliation_confirmed()
    await harness.restart_and_reconcile()

    # Then
    operation = harness.store.load_snapshot(harness.lease.saga_id).operations[harness.operation_id]
    assert operation.status is OperationStatus.EFFECT_CONFIRMED
    assert harness.provider.execute_call_count == 2
    assert harness.provider.effect_count(harness.operation_id) == 1
    assert harness.provider.reconcile_call_count == 2


@pytest.mark.asyncio
async def test_should_reopen_requeued_history_while_normal_claim_is_held(tmp_path: Path) -> None:
    # Given
    harness = await _requeued_harness(tmp_path)
    claimed = harness.store.claim_outbox(harness.lease.owner, timedelta(minutes=1))
    assert claimed is not None and claimed.delivery_attempt == 2

    # When / Then
    _assert_open_backup_restore(harness, "claimed")
    assert harness.provider.execute_call_count == 1
    assert harness.provider.effect_count(harness.operation_id) == 1


@pytest.mark.asyncio
async def test_should_reopen_requeued_history_after_normal_dispatch_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    harness = await _requeued_harness(tmp_path)
    checkpoint = _BlockSecondCheckpoint()
    monkeypatch.setattr("agentic_saga.execution.dispatcher.asyncio.sleep", checkpoint)
    task = asyncio.create_task(harness.dispatcher.dispatch_one(harness.lease.owner))
    await checkpoint.entered.wait()

    # When / Then
    _assert_open_backup_restore(harness, "started")
    operation = harness.store.load_snapshot(harness.lease.saga_id).operations[harness.operation_id]
    assert operation.status is OperationStatus.DISPATCHED
    assert harness.provider.execute_call_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_should_reopen_requeued_history_after_normal_success(tmp_path: Path) -> None:
    # Given
    harness = await _requeued_harness(tmp_path)
    result = await harness.dispatcher.dispatch_one(harness.lease.owner)
    assert result.failure is None

    # When / Then
    _assert_open_backup_restore(harness, "completed")
    operation = harness.store.load_snapshot(harness.lease.saga_id).operations[harness.operation_id]
    assert operation.status is OperationStatus.EFFECT_CONFIRMED
    assert harness.provider.execute_call_count == 2
    assert harness.provider.effect_count(harness.operation_id) == 1


def _assert_open_backup_restore(harness: KernelHarness, label: str) -> None:
    opened = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)
    backup = harness.store.path.with_name(f"{label}-backup.db")
    restored_path = harness.store.path.with_name(f"{label}-restored.db")
    harness.store.backup_to(backup)
    SQLiteKernelStore.restore_from(backup, restored_path)
    restored = SQLiteKernelStore.open(restored_path, clock=harness.clock)
    expected = harness.store.reconciliation_job(harness.operation_id)
    assert opened.reconciliation_job(harness.operation_id) == expected
    assert restored.reconciliation_job(harness.operation_id) == expected


@pytest.mark.asyncio
async def test_should_preserve_retry_attempt_after_pre_entry_authority_loss(
    tmp_path: Path,
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    harness.provider.set_reconciliation_no_effect()
    await harness.restart_and_reconcile()
    harness.clock.advance(timedelta(minutes=5))

    # When
    result = await harness.dispatcher.dispatch_one(harness.lease.owner)
    claimed = harness.store.claim_outbox("worker-b", timedelta(minutes=1))

    # Then
    assert result.failure is not None and result.failure.code == "authority_lost"
    assert claimed is not None and claimed.delivery_attempt == 2
    assert claimed.claim_generation > 1


@pytest.mark.asyncio
async def test_should_preserve_retry_attempt_after_pre_entry_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    harness = await _requeued_harness(tmp_path)
    checkpoint = _BlockSecondCheckpoint()
    monkeypatch.setattr("agentic_saga.execution.dispatcher.asyncio.sleep", checkpoint)

    # When / Then
    task = asyncio.create_task(harness.dispatcher.dispatch_one(harness.lease.owner))
    await checkpoint.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    operation = harness.store.load_snapshot(harness.lease.saga_id).operations[harness.operation_id]
    assert operation.status is OperationStatus.INTENT_DURABLE
    claimed = harness.store.claim_outbox(harness.lease.owner, timedelta(minutes=1))
    assert claimed is not None and claimed.delivery_attempt == 2
    assert claimed.claim_generation > 1


async def _requeued_harness(tmp_path: Path) -> KernelHarness:
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    harness.provider.set_reconciliation_no_effect()
    await harness.restart_and_reconcile()
    return harness


class _BlockSecondCheckpoint:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def __call__(self, *_args: object) -> None:
        self.calls += 1
        if self.calls == 1:
            return
        self.entered.set()
        await self.release.wait()


def _assert_same_identity_retry(
    harness: KernelHarness,
    before: OperationRecord,
    after: OperationRecord,
    job: ReconciliationJob | None,
) -> None:
    assert before.status is OperationStatus.INTENT_DURABLE
    assert before.operation_id == after.operation_id == harness.operation_id
    assert before.command_hash == after.command_hash
    assert before.delivery_attempt == 1 and after.delivery_attempt == 2
    assert harness.provider.effect_count(harness.operation_id) == 1
    assert harness.provider.execute_call_count == 2
    assert job is not None and job.state is ReconciliationJobState.REQUEUED


@pytest.mark.asyncio
async def test_should_escalate_definition_mismatch_without_provider_lookup(
    tmp_path: Path,
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    definition = harness.registry.definition(TOOL_NAME)
    assert isinstance(definition, EffectToolDefinition)
    changed = replace(definition, definition_version="durable-charge-v2")
    registry = ToolRegistry((changed,))

    # When
    result = await Reconciler(harness.store, registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    # Then
    assert result.failure is not None and result.failure.code == "human_required"
    assert harness.provider.reconcile_call_count == 0
    assert harness.store.load_snapshot(harness.lease.saga_id).status is SagaStatus.HUMAN_REQUIRED


def _probe_harness(directory: Path, mode: str) -> tuple[KernelHarness, Path]:
    target = directory / "probe-harness"
    target.mkdir()
    harness = KernelHarness.create(target)
    definition = harness.registry.definition(TOOL_NAME)
    assert isinstance(definition, EffectToolDefinition)
    probe = target / "provider-probe.db"
    initialize_probe(probe)
    outcome = ReconcileEffectConfirmed(receipt={"payment_id": "safe"})
    config = {
        "mode": mode,
        "probe_path": str(probe),
        "kernel_path": str(target / "saga.db"),
        "now": "2026-09-06T12:00:00+00:00",
        "outcome": outcome.model_dump(mode="json"),
    }
    probe_adapter = ProbeAdapter(cast(JsonObject, config))
    combined = _LookupProbeAdapter(harness.provider, probe_adapter)
    harness.registry = ToolRegistry((replace(definition, adapter=combined),))
    harness.dispatcher = Dispatcher(harness.store, harness.registry, clock=harness.clock)
    return harness, probe


async def _reconcile(
    harness: KernelHarness, horizon: RecoveryHorizon | None = None
) -> ReconciliationResult:
    return await Reconciler(
        harness.store, harness.registry, clock=harness.clock, horizon=horizon
    ).reconcile_one(harness.lease.owner)


def _changed_horizon() -> RecoveryHorizon:
    return RecoveryHorizon(
        maximum_retry_delay=timedelta(minutes=6),
        operator_response_window=timedelta(hours=1),
        clock_skew_allowance=timedelta(seconds=30),
    )


def _assert_no_private_marker(path: Path, marker: str) -> None:
    durable = path.read_bytes()
    assert marker.encode() not in durable
    assert sha256(marker.encode()).hexdigest().encode() not in durable


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["secret_receipt", "malformed", "exception"])
async def test_should_normalize_private_or_invalid_provider_evidence(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    marker = f"private-reconciliation-{mode}"
    monkeypatch.setenv("AGENTIC_SAGA_PROVIDER_TEST_SECRET", marker)
    harness, probe = _probe_harness(tmp_path, mode)
    await harness.dispatch_with_response_loss()

    # When
    await _reconcile(harness)

    # Then
    _assert_no_private_marker(harness.store.path, marker)
    assert probe_counts(probe) == (0, 1)


@pytest.mark.asyncio
async def test_should_reject_lease_takeover_before_provider_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    original = harness.store.validate_reconciliation_authority
    takeover = _TakeoverBeforeProvider(harness, original)
    monkeypatch.setattr(harness.store, "validate_reconciliation_authority", takeover)

    # When
    result = await Reconciler(harness.store, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    # Then
    assert result.failure is not None and result.failure.code == "authority_lost"
    assert harness.provider.reconcile_call_count == 0
    job = harness.store.reconciliation_job(harness.operation_id)
    assert job is not None
    assert harness.store.outbox_state(job.command_id) is OutboxState.PARKED


class _TakeoverBeforeProvider:
    def __init__(
        self,
        harness: KernelHarness,
        original: Callable[[ReconciliationJob, Lease], None],
    ) -> None:
        self.harness = harness
        self.original = original
        self.called = False

    def __call__(self, job: ReconciliationJob, lease: Lease) -> None:
        if not self.called:
            self.called = True
            self.harness.clock.advance(timedelta(minutes=5))
            LeaseService(self.harness.store).acquire(
                self.harness.lease.saga_id, "worker-b", timedelta(minutes=5)
            )
        self.original(job, lease)


@pytest.mark.asyncio
async def test_should_reject_lease_takeover_after_provider_await(tmp_path: Path) -> None:
    # Given
    harness, _ = _probe_harness(tmp_path, "takeover")
    await harness.dispatch_with_response_loss()
    before = harness.store.read_events(harness.lease.saga_id)

    # When
    result = await _reconcile(harness)

    # Then
    assert result.failure is not None and result.failure.code == "authority_lost"
    assert result.operation_id == harness.operation_id
    assert harness.store.read_events(harness.lease.saga_id) == before


@pytest.mark.asyncio
async def test_should_suspend_other_runnable_work_on_human_escalation(
    tmp_path: Path,
) -> None:
    # Given
    harness, held, additional = await _two_effects_ready_for_human(tmp_path)
    harness.provider.set_reconciliation_conflict()

    # When
    await harness.restart_and_reconcile()

    # Then
    _assert_preserved_suspension(harness, held)
    assert harness.provider.execute_call_count == 2
    assert harness.provider.effect_count(additional.operation_id) == 1
    assert harness.provider.effect_count(harness.operation_id) == 1
    assert (
        SQLiteKernelStore.open(harness.store.path).outbox_state(held.command_id)
        is OutboxState.SUSPENDED_FOR_HUMAN
    )


async def _two_effects_ready_for_human(
    tmp_path: Path,
) -> tuple[KernelHarness, ClaimedCommand, OutboxCommand]:
    harness = await _requeued_harness(tmp_path)
    command_id = harness.add_runnable_operation()
    additional = harness.store.load_outbox(command_id)
    held = harness.store.claim_outbox(harness.lease.owner, timedelta(minutes=1))
    assert held is not None and held.operation_id == harness.operation_id
    await harness.dispatch_with_response_loss()
    harness.store.release_outbox(held)
    return harness, held, additional


def _assert_preserved_suspension(harness: KernelHarness, held: ClaimedCommand) -> None:
    assert harness.store.outbox_state(held.command_id) is OutboxState.SUSPENDED_FOR_HUMAN
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        history = connection.execute(
            "SELECT claim_generation, claim_fence_token, delivery_attempt "
            "FROM outbox_commands WHERE command_id = ?",
            (held.command_id,),
        ).fetchone()
    assert history == (2, harness.lease.fence_token, 1)


@pytest.mark.asyncio
async def test_should_reschedule_instead_of_falsely_escalating_with_claimed_work(
    tmp_path: Path,
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    command_id = harness.add_runnable_operation()
    await harness.dispatch_with_response_loss()
    claimed = harness.store.claim_outbox("dispatch-worker", timedelta(minutes=1))
    harness.provider.set_reconciliation_conflict()
    before = harness.store.read_events(harness.lease.saga_id)

    # When
    result = await Reconciler(harness.store, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    # Then
    assert claimed is not None and claimed.command_id == command_id
    assert result.failure is not None and result.failure.code == "authority_lost"
    assert harness.store.read_events(harness.lease.saga_id) == before
    job = harness.store.reconciliation_job(harness.operation_id)
    assert job is not None and job.state is ReconciliationJobState.WAITING


@pytest.mark.asyncio
@pytest.mark.parametrize("clock_offset", [timedelta(days=-1), timedelta(days=1)])
async def test_should_schedule_bind_failure_from_store_time(
    tmp_path: Path, clock_offset: timedelta
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    caller_clock = FakeClock(harness.clock.now() + clock_offset)
    reconciler = Reconciler(harness.store, harness.registry, clock=caller_clock)

    # When
    result = await reconciler.reconcile_one("worker-b")
    job = harness.store.reconciliation_job(harness.operation_id)
    second = await reconciler.reconcile_one("worker-b")

    # Then
    assert result.failure is not None and result.failure.code == "authority_lost"
    assert result.operation_id == harness.operation_id
    assert job is not None and job.due_at == harness.clock.now() + timedelta(minutes=5)
    assert second.failure is not None and second.failure.code == "no_work"
    assert harness.store.reconciliation_job(harness.operation_id) == job


@pytest.mark.asyncio
async def test_should_schedule_bind_failure_from_frozen_policy(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    _freeze_default_recovery_policy(harness)
    changed = _retry_horizon(timedelta(hours=2))

    # When
    before = harness.clock.now()
    result = await Reconciler(
        harness.store, harness.registry, clock=harness.clock, horizon=changed
    ).reconcile_one("worker-b")
    job = harness.store.reconciliation_job(harness.operation_id)

    # Then
    assert result.failure is not None and result.failure.code == "authority_lost"
    assert job is not None and job.due_at == before + timedelta(minutes=5)
    assert job.claim_generation == 2 and job.claim_fence_token == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("clock_offset", [timedelta(days=-1), timedelta(days=1)])
async def test_should_schedule_commit_conflict_from_store_time(
    tmp_path: Path, clock_offset: timedelta, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    harness.provider.set_reconciliation_confirmed()
    caller_clock = FakeClock(harness.clock.now() + clock_offset)

    def reject_commit(
        job: ReconciliationJob,
        lease: Lease,
        batch: TransitionBatch,
        action: str,
        check_after: datetime | None,
    ) -> SagaSnapshot:
        del job, lease, batch, action, check_after
        raise StoreConflict("forced commit conflict")

    monkeypatch.setattr(harness.store, "commit_reconciliation", reject_commit)
    reconciler = Reconciler(harness.store, harness.registry, clock=caller_clock)

    # When
    result = await reconciler.reconcile_one(harness.lease.owner)
    job = harness.store.reconciliation_job(harness.operation_id)
    second = await reconciler.reconcile_one(harness.lease.owner)

    # Then
    assert result.failure is not None and result.failure.code == "authority_lost"
    assert result.operation_id == harness.operation_id
    assert job is not None and job.due_at == harness.clock.now() + timedelta(minutes=5)
    assert second.failure is not None and second.failure.code == "no_work"
    assert harness.store.reconciliation_job(harness.operation_id) == job


@pytest.mark.asyncio
async def test_should_schedule_commit_conflict_from_frozen_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    _freeze_default_recovery_policy(harness)

    def reject_commit(*args: object, **kwargs: object) -> SagaSnapshot:
        del args, kwargs
        raise StoreConflict("forced commit conflict")

    monkeypatch.setattr(harness.store, "commit_reconciliation", reject_commit)
    changed = _retry_horizon(timedelta(hours=2))

    # When
    before = harness.clock.now()
    result = await Reconciler(
        harness.store, harness.registry, clock=harness.clock, horizon=changed
    ).reconcile_one(harness.lease.owner)
    job = harness.store.reconciliation_job(harness.operation_id)

    # Then
    assert result.failure is not None and result.failure.code == "authority_lost"
    assert job is not None and job.due_at == before + timedelta(minutes=5)
    assert job.claim_generation == 2
    assert job.claim_fence_token == harness.lease.fence_token


def _freeze_default_recovery_policy(harness: KernelHarness) -> None:
    policy = _retry_horizon(timedelta(minutes=5)).public_policy()
    digest = sha256_json(policy.model_dump(mode="json"))
    job = harness.store.claim_reconciliation("policy-freezer", timedelta(minutes=1), policy, digest)
    assert job is not None
    harness.store.release_reconciliation(job)
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute(
            "UPDATE reconciliation_jobs SET due_at = ? WHERE job_id = ?",
            (harness.clock.now().isoformat(), job.job_id),
        )


def _retry_horizon(delay: timedelta) -> RecoveryHorizon:
    return RecoveryHorizon(
        maximum_retry_delay=delay,
        operator_response_window=timedelta(hours=1),
        clock_skew_allowance=timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_should_not_escalate_while_another_recovery_lookup_is_claimed(
    tmp_path: Path,
) -> None:
    # Given
    harness, other = await _competing_recovery_harness(tmp_path)
    harness.provider.set_reconciliation_conflict()

    # When
    result = await _reconcile(harness)

    # Then
    assert result.failure is not None and result.failure.code == "authority_lost"
    _assert_rescheduled_with_one_lookup(harness)
    await _finish_after_competing_lookup(harness, other)


async def _competing_recovery_harness(
    tmp_path: Path,
) -> tuple[KernelHarness, ClaimedCommand]:
    harness = KernelHarness.create(tmp_path)
    other_command_id = harness.add_runnable_operation()
    first = harness.store.claim_outbox(harness.lease.owner, timedelta(minutes=1))
    other = harness.store.claim_outbox(harness.lease.owner, timedelta(minutes=1))
    assert first is not None and other is not None and other.command_id == other_command_id
    harness.store.release_outbox(first)
    _insert_claimed_recovery_job(harness, other)
    await harness.dispatch_with_response_loss()
    return harness, other


def _assert_rescheduled_with_one_lookup(harness: KernelHarness) -> None:
    assert harness.provider.execute_call_count == 1
    assert harness.provider.reconcile_call_count == 1
    job = harness.store.reconciliation_job(harness.operation_id)
    assert job is not None and job.state is ReconciliationJobState.WAITING


async def _finish_after_competing_lookup(harness: KernelHarness, other: ClaimedCommand) -> None:
    _remove_claimed_recovery_job(harness, other)
    harness.clock.advance(timedelta(minutes=5))
    final = await _reconcile(harness)
    assert final.failure is not None and final.failure.code == "human_required"
    assert harness.store.runnable_count(harness.lease.saga_id) == 0
    assert harness.provider.reconcile_call_count == 2


def _insert_claimed_recovery_job(harness: KernelHarness, claimed: ClaimedCommand) -> None:
    policy = RecoveryHorizon(
        maximum_retry_delay=timedelta(minutes=5),
        operator_response_window=timedelta(hours=1),
        clock_skew_allowance=timedelta(seconds=30),
    ).public_policy()
    operation_id = claimed.operation_id
    digest = sha256_json(policy.model_dump(mode="json"))
    job_id = (
        "recon_"
        + sha256(b"agentic-saga-reconciliation-job-v1\0" + operation_id.encode()).hexdigest()
    )
    _write_claimed_recovery_job(harness, claimed.command_id, operation_id, job_id, policy, digest)


def _write_claimed_recovery_job(  # noqa: PLR0913, PLR0917
    harness: KernelHarness,
    command_id: str,
    operation_id: str,
    job_id: str,
    policy: RecoveryPolicy,
    digest: str,
) -> None:
    now = harness.clock.now().isoformat()
    expiry = (harness.clock.now() + timedelta(minutes=1)).isoformat()
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute(
            "UPDATE outbox_commands SET state = 'parked' WHERE command_id = ?", (command_id,)
        )
        connection.execute(
            "INSERT INTO reconciliation_jobs (job_id, command_id, saga_id, operation_id, "
            "state, due_at, first_dispatch_at, claim_id, claim_owner, claim_expires_at, "
            "claim_generation, claim_fence_token, recovery_policy_json, "
            "recovery_policy_digest, claimed_at) VALUES (?, ?, ?, ?, 'claimed', ?, ?, ?, ?, "
            "?, 1, ?, ?, ?, ?)",
            (
                job_id,
                command_id,
                harness.lease.saga_id,
                operation_id,
                now,
                now,
                "claim_00000000000000000000000000009009",
                harness.lease.owner,
                expiry,
                harness.lease.fence_token,
                canonical_json(policy.model_dump(mode="json")),
                digest,
                now,
            ),
        )


def _remove_claimed_recovery_job(harness: KernelHarness, claimed: ClaimedCommand) -> None:
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute(
            "DELETE FROM reconciliation_jobs WHERE operation_id = ?", (claimed.operation_id,)
        )
        connection.execute(
            "UPDATE outbox_commands SET state = 'runnable', claim_id = NULL, "
            "claim_owner = NULL, claim_expires_at = NULL WHERE command_id = ?",
            (claimed.command_id,),
        )


@pytest.mark.asyncio
async def test_should_durably_escalate_cancellation_without_leaking_text(
    tmp_path: Path,
) -> None:
    # Given
    harness, probe = _probe_harness(tmp_path, "block")
    await harness.dispatch_with_response_loss()
    reconciler = Reconciler(harness.store, harness.registry, clock=harness.clock)
    task = asyncio.create_task(reconciler.reconcile_one(harness.lease.owner))
    await _wait_for_lookup(probe)

    # When / Then
    task.cancel("private-cancellation-marker")
    with pytest.raises(asyncio.CancelledError):
        await task
    durable = harness.store.path.read_bytes()
    assert b"private-cancellation-marker" not in durable
    assert harness.store.load_snapshot(harness.lease.saga_id).status is SagaStatus.HUMAN_REQUIRED
    assert probe_counts(probe) == (0, 1)


async def _wait_for_lookup(probe: Path) -> None:
    async def wait() -> None:
        while probe_counts(probe)[1] == 0:  # noqa: ASYNC110
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=2)


@pytest.mark.asyncio
async def test_should_revalidate_authority_after_command_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    original = harness.store.load_outbox
    taken_over = False

    def load_then_take_over(command_id: str) -> object:
        nonlocal taken_over
        command = original(command_id)
        if not taken_over:
            taken_over = True
            harness.clock.advance(timedelta(minutes=6))
            LeaseService(harness.store).acquire(
                harness.lease.saga_id, "worker-b", timedelta(minutes=5)
            )
        return command

    monkeypatch.setattr(harness.store, "load_outbox", load_then_take_over)

    # When
    result = await Reconciler(harness.store, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    # Then
    assert result.failure is not None and result.failure.code == "authority_lost"
    assert harness.provider.reconcile_call_count == 0


@pytest.mark.asyncio
async def test_should_fail_closed_when_provider_lookup_times_out(tmp_path: Path) -> None:
    # Given
    harness, probe = _probe_harness(tmp_path, "block")
    await harness.dispatch_with_response_loss()

    # When
    result = await Reconciler(
        harness.store,
        harness.registry,
        clock=harness.clock,
        reconcile_timeout=timedelta(seconds=1),
    ).reconcile_one(harness.lease.owner)

    # Then
    assert result.failure is not None and result.failure.code == "human_required"
    assert harness.store.load_snapshot(harness.lease.saga_id).status is SagaStatus.HUMAN_REQUIRED
    assert probe_counts(probe) == (0, 1)


@pytest.mark.asyncio
async def test_should_escalate_insufficient_retention_without_lookup(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    definition = harness.registry.definition(TOOL_NAME)
    assert isinstance(definition, EffectToolDefinition)
    capabilities = definition.capabilities.model_copy(
        update={"reconciliation_supported": False, "idempotency_retention_seconds": 1}
    )
    registry = ToolRegistry((replace(definition, capabilities=capabilities),))

    # When
    result = await Reconciler(harness.store, registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    # Then
    assert result.failure is not None and result.failure.code == "human_required"
    assert harness.provider.reconcile_call_count == 0


def _unsupported_capabilities(retention_seconds: int) -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=retention_seconds,
        reconciliation_supported=False,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=False,
    )


@pytest.mark.asyncio
async def test_should_use_store_time_not_future_reconciler_clock_at_exact_boundary(
    tmp_path: Path,
) -> None:
    # Given
    capabilities = _unsupported_capabilities(3_930)
    harness = KernelHarness.create_with_capabilities(tmp_path, capabilities)
    await harness.dispatch_with_response_loss()
    future = FakeClock(harness.clock.now() + timedelta(days=1))

    # When
    result = await Reconciler(harness.store, harness.registry, clock=future).reconcile_one(
        harness.lease.owner
    )

    # Then
    assert result.decision is not None and result.decision.action == "retry_same_id"


@pytest.mark.asyncio
async def test_should_use_store_time_not_stale_reconciler_clock_after_expiry(
    tmp_path: Path,
) -> None:
    # Given
    capabilities = _unsupported_capabilities(3_930)
    harness = KernelHarness.create_with_capabilities(tmp_path, capabilities)
    await harness.dispatch_with_response_loss()
    stale = FakeClock(harness.clock.now())
    harness.clock.advance(timedelta(milliseconds=1))

    # When
    result = await Reconciler(harness.store, harness.registry, clock=stale).reconcile_one(
        harness.lease.owner
    )

    # Then
    assert result.failure is not None and result.failure.code == "human_required"


@pytest.mark.asyncio
async def test_should_recheck_retention_at_commit_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    capabilities = _unsupported_capabilities(3_930)
    harness = KernelHarness.create_with_capabilities(tmp_path, capabilities)
    await harness.dispatch_with_response_loss()
    commit = harness.store.commit_reconciliation
    advanced = False

    def advance_then_commit(
        job: ReconciliationJob,
        lease: Lease,
        batch: TransitionBatch,
        action: str,
        check_after: datetime | None,
    ) -> SagaSnapshot:
        nonlocal advanced
        if not advanced:
            advanced = True
            harness.clock.advance(timedelta(milliseconds=1))
        return commit(job, lease, batch, action, check_after)

    monkeypatch.setattr(harness.store, "commit_reconciliation", advance_then_commit)

    # When
    result = await _reconcile(harness)

    # Then
    assert result.failure is not None and result.failure.code == "human_required"
    assert harness.store.runnable_count(harness.lease.saga_id) == 0


@pytest.mark.asyncio
async def test_should_escalate_same_version_capability_drift(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    definition = harness.registry.definition(TOOL_NAME)
    assert isinstance(definition, EffectToolDefinition)
    changed = replace(definition, capabilities=_unsupported_capabilities(86_400))

    # When
    result = await Reconciler(
        harness.store, ToolRegistry((changed,)), clock=harness.clock
    ).reconcile_one(harness.lease.owner)

    # Then
    assert result.failure is not None and result.failure.code == "human_required"
    assert harness.provider.reconcile_call_count == 0


@pytest.mark.asyncio
async def test_should_preserve_capability_proof_through_backup_restore(tmp_path: Path) -> None:
    # Given
    capabilities = _unsupported_capabilities(3_930)
    harness = KernelHarness.create_with_capabilities(tmp_path, capabilities)
    await harness.dispatch_with_response_loss()
    backup = tmp_path / "proof-backup.db"
    restored_path = tmp_path / "proof-restored.db"
    harness.store.backup_to(backup)
    SQLiteKernelStore.restore_from(backup, restored_path)
    restored = SQLiteKernelStore.open(restored_path, clock=harness.clock)

    # When
    result = await Reconciler(restored, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    # Then
    assert result.decision is not None and result.decision.action == "retry_same_id"


@pytest.mark.asyncio
async def test_should_require_policy_on_direct_first_claim(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()

    # When / Then
    with pytest.raises(ValueError, match="policy"):
        harness.store.claim_reconciliation("worker-b", timedelta(minutes=1))


@pytest.mark.asyncio
async def test_should_reject_changed_policy_inside_claim_transaction(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    policy = RecoveryHorizon(
        maximum_retry_delay=timedelta(minutes=5),
        operator_response_window=timedelta(hours=1),
        clock_skew_allowance=timedelta(seconds=30),
    ).public_policy()
    first = harness.store.claim_reconciliation(
        "worker-b", timedelta(minutes=1), policy, sha256_json(policy.model_dump(mode="json"))
    )
    assert first is not None
    harness.store.release_reconciliation(first)
    harness.clock.advance(timedelta(minutes=5))
    changed = policy.model_copy(update={"maximum_retry_delay_microseconds": 1})

    # When / Then
    with pytest.raises(StoreConflict, match="policy"):
        harness.store.claim_reconciliation(
            "worker-b",
            timedelta(minutes=1),
            changed,
            sha256_json(changed.model_dump(mode="json")),
        )


@pytest.mark.asyncio
async def test_should_reject_missing_reconciliation_job_on_open(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute("DELETE FROM reconciliation_jobs")

    # When / Then
    with pytest.raises(StoreCorruption, match="cardinality"):
        SQLiteKernelStore.open(harness.store.path)


@pytest.mark.asyncio
async def test_should_reject_future_first_dispatch_anchor_on_open(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    job = harness.store.reconciliation_job(harness.operation_id)
    assert job is not None
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute(
            "UPDATE reconciliation_jobs SET first_dispatch_at = ?",
            ((job.first_dispatch_at + timedelta(days=1)).isoformat(),),
        )

    # When / Then
    with pytest.raises(StoreCorruption, match="identity"):
        SQLiteKernelStore.open(harness.store.path)


@pytest.mark.asyncio
async def test_should_reject_cross_saga_reconciliation_job_on_open(tmp_path: Path) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    first = harness.store.read_events(harness.lease.saga_id)[0]
    assert isinstance(first, SagaCreated)
    other = first.model_copy(
        update={"event_id": "evt_0000000000009009", "saga_id": "saga_0000000000009009"}
    )
    harness.store.create_saga(other)
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute("UPDATE reconciliation_jobs SET saga_id = ?", (other.saga_id,))

    # When / Then
    with pytest.raises(StoreCorruption, match="identity"):
        SQLiteKernelStore.open(harness.store.path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        StoreFailpoint.BEFORE_EVENT_INSERT,
        StoreFailpoint.AFTER_EVENT_INSERT,
        StoreFailpoint.BEFORE_PROJECTION_UPDATE,
        StoreFailpoint.AFTER_PROJECTION_UPDATE,
        StoreFailpoint.BEFORE_RECEIPT_INSERT,
        StoreFailpoint.AFTER_RECEIPT_INSERT,
        StoreFailpoint.BEFORE_RECONCILIATION_JOB_UPDATE,
        StoreFailpoint.AFTER_RECONCILIATION_JOB_UPDATE,
        StoreFailpoint.BEFORE_COMMIT,
        StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN,
    ],
)
async def test_should_retry_each_uncertain_reconciliation_commit_exactly(
    tmp_path: Path, point: StoreFailpoint
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    failpoint = _OneShotFailpoint(point)
    store = SQLiteKernelStore.open(harness.store.path, clock=harness.clock, failpoint=failpoint)

    # When
    await Reconciler(store, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    # Then
    events = store.read_events(harness.lease.saga_id)
    assert failpoint.fired
    assert sum(event.event_type == "reconciliation_recorded" for event in events) == 1
    assert harness.provider.reconcile_call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    [
        StoreFailpoint.BEFORE_RECONCILIATION_JOB_UPDATE,
        StoreFailpoint.AFTER_RECONCILIATION_JOB_UPDATE,
    ],
)
async def test_should_roll_back_unknown_disposition_when_job_creation_crashes(
    tmp_path: Path, point: StoreFailpoint
) -> None:
    # Given
    harness = KernelHarness.create(tmp_path)
    failpoint = _OneShotFailpoint(point)
    store = SQLiteKernelStore.open(harness.store.path, clock=harness.clock, failpoint=failpoint)
    harness.dispatcher = harness.dispatcher.__class__(store, harness.registry, clock=harness.clock)
    harness.provider.enable_response_loss_after_effect()

    # When / Then
    with pytest.raises(InjectedStoreFailure):
        await harness.dispatcher.dispatch_one(harness.lease.owner)
    assert failpoint.fired
    assert store.reconciliation_job(harness.operation_id) is None
    assert (
        store.load_snapshot(harness.lease.saga_id).operations[harness.operation_id].status
        is OperationStatus.DISPATCHED
    )
