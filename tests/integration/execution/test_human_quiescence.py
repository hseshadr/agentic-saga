from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel

from agentic_saga.contracts.actions import Escalate, HumanDecision
from agentic_saga.contracts.events import HumanRequired, HumanResolutionRecorded, LedgerEvent
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.tools import EffectToolDefinition, ToolRegistry
from agentic_saga.execution import Dispatcher, Reconciler
from agentic_saga.execution.leases import LeaseService
from agentic_saga.execution.unwind import (
    EmergencyUnwinder,
    QuiescenceVerifier,
    UnwindAction,
    UnwindTrigger,
)
from agentic_saga.kernel.policy import human_resolution_digest
from agentic_saga.kernel.ports import (
    HumanResolutionAuthenticationFailed,
    InjectedStoreFailure,
    Lease,
    LeaseLost,
    ReconciliationJob,
    ReconciliationJobState,
    StoreConflict,
    StoreCorruption,
    StoreFailpoint,
    TransitionBatch,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.support.kernel_harness import KernelHarness

_RECEIPT_UPDATE_TRIGGER = """CREATE TRIGGER transition_receipts_no_update
BEFORE UPDATE ON transition_receipts
BEGIN
  SELECT RAISE(ABORT, 'transition_receipts is append-only');
END"""


@dataclass
class OneShotFailpoint:
    target: StoreFailpoint
    fired: bool = False

    def hit(self, point: StoreFailpoint) -> None:
        if point is self.target and not self.fired:
            self.fired = True
            raise InjectedStoreFailure(point)


@dataclass
class CountingHumanVerifier:
    calls: int = 0

    def __call__(self, decision: HumanDecision, snapshot: SagaSnapshot) -> bool:
        del snapshot
        self.calls += 1
        return decision.auth_proof == "verified-human-proof"


@pytest.mark.asyncio
async def test_quiescence_rejects_runnable_and_parked_forward_work(tmp_path: Path) -> None:
    runnable_path = tmp_path / "runnable"
    parked_path = tmp_path / "parked"
    runnable_path.mkdir()
    parked_path.mkdir()
    runnable = KernelHarness.create(runnable_path)
    runnable_proof = QuiescenceVerifier(runnable.store).verify(
        runnable.lease.saga_id, runnable.lease, runnable.registry
    )

    parked = KernelHarness.create(parked_path)
    await parked.dispatch_with_response_loss()
    parked_proof = QuiescenceVerifier(parked.store).verify(
        parked.lease.saga_id, parked.lease, parked.registry
    )

    assert runnable_proof.safe is False
    assert runnable_proof.forward_outbox_blockers == (runnable.operation_id,)
    assert parked_proof.safe is False
    assert parked_proof.forward_outbox_blockers == (parked.operation_id,)
    assert parked_proof.reconciliation_blockers == (parked.operation_id,)


@pytest.mark.asyncio
async def test_quiescence_accepts_only_settled_forward_work_under_current_lease(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create(tmp_path)
    await harness.dispatcher.dispatch_one(harness.lease.owner)

    proof = QuiescenceVerifier(harness.store).verify(
        harness.lease.saga_id, harness.lease, harness.registry
    )

    assert proof.safe is True
    assert proof.saga_seq == harness.store.load_snapshot(harness.lease.saga_id).seq
    assert proof.fence_token == harness.lease.fence_token


def test_quiescence_rejects_stale_lease_after_takeover(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    harness.clock.advance(timedelta(minutes=6))
    LeaseService(harness.store).acquire(harness.lease.saga_id, "worker-b", timedelta(minutes=5))

    with pytest.raises(LeaseLost):
        QuiescenceVerifier(harness.store).verify(
            harness.lease.saga_id, harness.lease, harness.registry
        )


@pytest.mark.asyncio
async def test_irreversible_unwind_is_durable_and_restart_is_quiescent(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    await harness.dispatcher.dispatch_one(harness.lease.owner)
    calls_before = (harness.provider.execute_call_count, harness.provider.reconcile_call_count)

    decision = EmergencyUnwinder(harness.store, clock=harness.clock).execute(
        harness.lease.saga_id,
        harness.registry,
        UnwindTrigger.AGENT_UNAVAILABLE,
        harness.lease,
    )
    reopened = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)
    dispatch = await Dispatcher(reopened, harness.registry, clock=harness.clock).dispatch_one(
        harness.lease.owner
    )
    reconciliation = await Reconciler(
        reopened, harness.registry, clock=harness.clock
    ).reconcile_one(harness.lease.owner)

    assert decision.action is UnwindAction.HUMAN_REQUIRED
    assert reopened.load_snapshot(harness.lease.saga_id).status is SagaStatus.HUMAN_REQUIRED
    assert dispatch.failure is not None and dispatch.failure.code == "no_work"
    assert reconciliation.failure is not None and reconciliation.failure.code == "no_work"
    assert calls_before == (
        harness.provider.execute_call_count,
        harness.provider.reconcile_call_count,
    )


@pytest.mark.asyncio
async def test_compensation_starts_only_after_durable_quiescence(tmp_path: Path) -> None:
    harness = KernelHarness.create_reversible(tmp_path)
    await harness.dispatcher.dispatch_one(harness.lease.owner)
    calls_before = harness.provider.execute_call_count

    decision = EmergencyUnwinder(harness.store, clock=harness.clock).execute(
        harness.lease.saga_id,
        harness.registry,
        UnwindTrigger.INVALID_PROPOSAL_LIMIT,
        harness.lease,
    )

    assert decision.action is UnwindAction.COMPENSATE
    assert harness.store.load_snapshot(harness.lease.saga_id).status is SagaStatus.COMPENSATING
    assert harness.provider.execute_call_count == calls_before


@pytest.mark.asyncio
async def test_unknown_unwind_never_calls_or_compensates_blindly(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    calls_before = (harness.provider.execute_call_count, harness.provider.reconcile_call_count)

    decision = EmergencyUnwinder(harness.store, clock=harness.clock).execute(
        harness.lease.saga_id,
        harness.registry,
        UnwindTrigger.BUDGET_EXHAUSTED,
        harness.lease,
    )

    assert decision.action is UnwindAction.RECONCILE
    assert calls_before == (
        harness.provider.execute_call_count,
        harness.provider.reconcile_call_count,
    )


@pytest.mark.asyncio
async def test_human_transition_parks_reconciliation_job_and_survives_open(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create(tmp_path)
    await harness.dispatch_with_response_loss()
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    proposal = Escalate(
        proposal_id="proposal_00011001",
        based_on_saga_seq=snapshot.seq,
        reason_code="operator_intervention",
        rationale="Stop autonomous work without rewriting unknown evidence.",
    )

    result = harness.kernel.submit_proposal(harness.lease.saga_id, proposal, harness.lease)
    reopened = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)
    backup = tmp_path / "human-backup.db"
    restored_path = tmp_path / "human-restored.db"
    reopened.backup_to(backup)
    restored = SQLiteKernelStore.restore_from(backup, restored_path)
    job = restored.reconciliation_job(harness.operation_id)
    reconciled = await Reconciler(restored, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    assert result.accepted
    assert restored.load_snapshot(harness.lease.saga_id).status is SagaStatus.HUMAN_REQUIRED
    assert job is not None and job.state is ReconciliationJobState.HUMAN_REQUIRED
    assert reconciled.failure is not None and reconciled.failure.code == "no_work"


@pytest.mark.asyncio
async def test_human_unwind_recovers_commit_ack_loss_exactly_once(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    await harness.dispatcher.dispatch_one(harness.lease.owner)
    failpoint = OneShotFailpoint(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
    store = SQLiteKernelStore.open(harness.store.path, clock=harness.clock, failpoint=failpoint)

    decision = EmergencyUnwinder(store, clock=harness.clock).execute(
        harness.lease.saga_id,
        harness.registry,
        UnwindTrigger.AGENT_UNAVAILABLE,
        harness.lease,
    )
    events = store.read_events(harness.lease.saga_id)

    assert decision.action is UnwindAction.HUMAN_REQUIRED
    assert sum(event.event_type == "human_required" for event in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "point",
    (
        StoreFailpoint.BEFORE_EVENT_INSERT,
        StoreFailpoint.AFTER_EVENT_INSERT,
        StoreFailpoint.BEFORE_PROJECTION_UPDATE,
        StoreFailpoint.AFTER_PROJECTION_UPDATE,
        StoreFailpoint.BEFORE_COMMIT,
    ),
)
async def test_human_unwind_rolls_back_every_precommit_failure(
    tmp_path: Path, point: StoreFailpoint
) -> None:
    harness = KernelHarness.create(tmp_path)
    await harness.dispatcher.dispatch_one(harness.lease.owner)
    store = SQLiteKernelStore.open(
        harness.store.path,
        clock=harness.clock,
        failpoint=OneShotFailpoint(point),
    )

    with pytest.raises(InjectedStoreFailure):
        EmergencyUnwinder(store, clock=harness.clock).execute(
            harness.lease.saga_id,
            harness.registry,
            UnwindTrigger.AGENT_UNAVAILABLE,
            harness.lease,
        )
    reopened = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)

    assert reopened.load_snapshot(harness.lease.saga_id).status is SagaStatus.RUNNING
    events = reopened.read_events(harness.lease.saga_id)
    assert all(event.event_type != "human_required" for event in events)


@pytest.mark.asyncio
async def test_human_decision_is_authenticated_sequence_bound_single_use_and_private(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    await harness.dispatcher.dispatch_one(harness.lease.owner)
    EmergencyUnwinder(harness.store, clock=harness.clock).execute(
        harness.lease.saga_id,
        harness.registry,
        UnwindTrigger.AGENT_UNAVAILABLE,
        harness.lease,
    )
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    decision = _signed_decision(harness, snapshot.seq)

    accepted = harness.kernel.apply_human_decision(harness.lease.saga_id, decision, harness.lease)
    repeated = harness.kernel.apply_human_decision(harness.lease.saga_id, decision, harness.lease)
    backup = tmp_path / "resolved-backup.db"
    restored_path = tmp_path / "resolved-restored.db"
    harness.store.backup_to(backup)
    restored = SQLiteKernelStore.restore_from(backup, restored_path)
    durable = harness.store.path.read_bytes()

    assert accepted.accepted is True
    assert repeated.accepted is False and repeated.code == "decision_already_used"
    assert restored.rebuild_and_verify(harness.lease.saga_id).seq == accepted.saga_seq
    assert decision.auth_proof.encode() not in durable
    assert sha256(decision.auth_proof.encode()).hexdigest().encode() not in durable


@pytest.mark.parametrize("generic_method", ("commit_transition", "commit_proposal"))
def test_generic_store_commit_cannot_forge_human_resolution(
    tmp_path: Path, generic_method: str
) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    paused = _pause_queued_command(harness)
    decision = _signed_decision(harness, paused.seq)
    batch = _resolution_batch(harness, paused, decision)

    with pytest.raises(StoreConflict, match="specialized authenticated"):
        if generic_method == "commit_transition":
            harness.store.commit_transition(batch)
        else:
            harness.store.commit_proposal(batch, "0" * 64)

    durable = harness.store.load_snapshot(harness.lease.saga_id)
    assert durable.pending_approval is True
    assert durable.consumed_approval_ids == ()
    assert harness.store.runnable_count(harness.lease.saga_id) == 0


@pytest.mark.parametrize("changed_field", ("decision_id", "sequence", "action", "digest"))
def test_specialized_human_resolution_binds_exact_authenticated_decision(
    tmp_path: Path, changed_field: str
) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    paused = _pause_queued_command(harness)
    decision = _signed_decision(harness, paused.seq)
    batch = _changed_resolution_batch(harness, paused, decision, changed_field)

    with pytest.raises(StoreConflict):
        harness.store.commit_human_resolution(batch, decision)

    assert harness.store.load_snapshot(harness.lease.saga_id).pending_approval is True


def test_specialized_resolution_rejects_noncanonical_decision_digest_before_verifier(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    paused = _pause_queued_command(harness)
    valid = _signed_decision(harness, paused.seq)
    changed = valid.model_copy(update={"proposal_hash": "1" * 64})
    batch = _resolution_batch(harness, paused, valid)
    verifier = CountingHumanVerifier()
    store = SQLiteKernelStore.open(
        harness.store.path,
        clock=harness.clock,
        human_resolution_verifier=verifier,
    )

    with pytest.raises(StoreConflict, match="canonical public digest"):
        store.commit_human_resolution(batch, changed)
    backup = tmp_path / "invalid-resolution-digest-backup.db"
    restored_path = tmp_path / "invalid-resolution-digest-restored.db"
    store.backup_to(backup)
    restored = SQLiteKernelStore.restore_from(backup, restored_path)

    assert verifier.calls == 0
    assert store.lookup_transition_receipt(batch.transition_id) is None
    assert restored.rebuild_and_verify(harness.lease.saga_id) == paused
    assert restored.runnable_count(harness.lease.saga_id) == 0


def test_store_owned_verifier_denies_direct_resolution_on_default_handle(tmp_path: Path) -> None:
    harness = KernelHarness.create(tmp_path)
    paused = _pause_queued_command(harness)
    decision = _signed_decision(harness, paused.seq)
    batch = _resolution_batch(harness, paused, decision)

    with pytest.raises(HumanResolutionAuthenticationFailed):
        harness.store.commit_human_resolution(batch, decision)
    with pytest.raises(TypeError):
        harness.store.commit_human_resolution(  # type: ignore[call-arg]
            batch, decision, CountingHumanVerifier()
        )

    durable = harness.store.load_snapshot(harness.lease.saga_id)
    assert durable.pending_approval is True and durable.consumed_approval_ids == ()


def test_resolution_verifier_runs_once_across_ack_loss_retry(tmp_path: Path) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    paused = _pause_queued_command(harness)
    decision = _signed_decision(harness, paused.seq)
    batch = _resolution_batch(harness, paused, decision)
    verifier = CountingHumanVerifier()
    failpoint = OneShotFailpoint(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
    store = SQLiteKernelStore.open(
        harness.store.path,
        clock=harness.clock,
        failpoint=failpoint,
        human_resolution_verifier=verifier,
    )

    with pytest.raises(InjectedStoreFailure):
        store.commit_human_resolution(batch, decision)
    resolved = store.commit_human_resolution(batch, decision)

    assert resolved.pending_approval is False
    assert verifier.calls == 1
    receipt = store.lookup_transition_receipt(batch.transition_id)
    assert receipt is not None and receipt.request_digest == human_resolution_digest(decision)
    resolution = receipt.events[0]
    assert isinstance(resolution, HumanResolutionRecorded)
    assert resolution.proposal_hash == receipt.request_digest
    durable = store.path.read_bytes()
    assert decision.auth_proof.encode() not in durable
    assert sha256(decision.auth_proof.encode()).hexdigest().encode() not in durable


def test_reopened_store_defaults_to_deny_and_requires_trusted_verifier(tmp_path: Path) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    paused = _pause_queued_command(harness)
    decision = _signed_decision(harness, paused.seq)
    batch = _resolution_batch(harness, paused, decision)
    denied = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)

    with pytest.raises(HumanResolutionAuthenticationFailed):
        denied.commit_human_resolution(batch, decision)
    configured = SQLiteKernelStore.open(
        harness.store.path,
        clock=harness.clock,
        human_resolution_verifier=CountingHumanVerifier(),
    )

    assert configured.commit_human_resolution(batch, decision).pending_approval is False


def test_open_rejects_human_resolution_receipt_digest_divergence(tmp_path: Path) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    paused = _pause_queued_command(harness)
    decision = _signed_decision(harness, paused.seq)
    batch = _resolution_batch(harness, paused, decision)
    harness.store.commit_human_resolution(batch, decision)
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        connection.execute("DROP TRIGGER transition_receipts_no_update")
        connection.execute(
            "UPDATE transition_receipts SET request_digest = ? WHERE transition_id = ?",
            ("1" * 64, batch.transition_id),
        )
        connection.execute(_RECEIPT_UPDATE_TRIGGER)

    with pytest.raises(StoreCorruption, match="human resolution receipt digest"):
        SQLiteKernelStore.open(harness.store.path, clock=harness.clock)


@pytest.mark.asyncio
async def test_stale_or_invalid_human_decision_never_resumes(tmp_path: Path) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    await harness.dispatcher.dispatch_one(harness.lease.owner)
    EmergencyUnwinder(harness.store, clock=harness.clock).execute(
        harness.lease.saga_id,
        harness.registry,
        UnwindTrigger.AGENT_UNAVAILABLE,
        harness.lease,
    )
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    stale = _signed_decision(harness, snapshot.seq - 1)
    invalid = _signed_decision(harness, snapshot.seq).model_copy(update={"auth_proof": "invalid"})

    stale_result = harness.kernel.apply_human_decision(harness.lease.saga_id, stale, harness.lease)
    invalid_result = harness.kernel.apply_human_decision(
        harness.lease.saga_id, invalid, harness.lease
    )

    assert stale_result.code == "stale_human_decision"
    assert invalid_result.code == "human_decision_verification_failed"
    assert harness.store.load_snapshot(harness.lease.saga_id).pending_approval is True


@pytest.mark.asyncio
async def test_authenticated_approval_resumes_exact_suspended_command(tmp_path: Path) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    proposal = Escalate(
        proposal_id="proposal_00011002",
        based_on_saga_seq=snapshot.seq,
        reason_code="operator_intervention",
        rationale="Pause the queued effect for an operator.",
    )
    harness.kernel.submit_proposal(harness.lease.saga_id, proposal, harness.lease)
    paused = harness.store.load_snapshot(harness.lease.saga_id)

    result = harness.kernel.apply_human_decision(
        harness.lease.saga_id, _signed_decision(harness, paused.seq), harness.lease
    )
    dispatched = await harness.dispatcher.dispatch_one(harness.lease.owner)

    assert result.accepted
    assert dispatched.failure is None
    assert harness.provider.execute_call_count == 1


@pytest.mark.asyncio
async def test_authenticated_reconcile_resumes_only_parked_lookup(tmp_path: Path) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    await harness.dispatch_with_response_loss()
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    proposal = Escalate(
        proposal_id="proposal_00011003",
        based_on_saga_seq=snapshot.seq,
        reason_code="operator_intervention",
        rationale="Pause autonomous reconciliation for an operator.",
    )
    harness.kernel.submit_proposal(harness.lease.saga_id, proposal, harness.lease)
    paused = harness.store.load_snapshot(harness.lease.saga_id)

    result = harness.kernel.apply_human_decision(
        harness.lease.saga_id,
        _signed_decision(harness, paused.seq, action="reconcile"),
        harness.lease,
    )
    reconciled = await Reconciler(
        harness.store, harness.registry, clock=harness.clock
    ).reconcile_one(harness.lease.owner)

    assert result.accepted
    assert reconciled.failure is None
    assert harness.provider.execute_call_count == 1


@pytest.mark.asyncio
async def test_mixed_human_state_requires_reconcile_then_fresh_approval(tmp_path: Path) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    harness.add_runnable_operation()
    await harness.dispatch_with_response_loss()
    paused = _pause_unknown_lookup(harness)
    first = _signed_decision(harness, paused.seq, action="reconcile")

    resumed = harness.kernel.apply_human_decision(harness.lease.saga_id, first, harness.lease)

    assert resumed.accepted is True
    assert harness.store.runnable_count(harness.lease.saga_id) == 0
    assert harness.provider.execute_call_count == 1

    reconciled = await Reconciler(
        harness.store, harness.registry, clock=harness.clock
    ).reconcile_one(harness.lease.owner)
    pending = harness.store.load_snapshot(harness.lease.saga_id)

    assert reconciled.failure is None
    assert pending.status is SagaStatus.HUMAN_REQUIRED
    assert pending.pending_approval is True
    assert pending.seq == paused.seq + 3
    assert harness.store.runnable_count(harness.lease.saga_id) == 0
    assert harness.provider.execute_call_count == 1

    reopened = SQLiteKernelStore.open(harness.store.path, clock=harness.clock)
    backup = tmp_path / "mixed-human-backup.db"
    restored_path = tmp_path / "mixed-human-restored.db"
    reopened.backup_to(backup)
    restored = SQLiteKernelStore.restore_from(backup, restored_path)
    calls_before = (harness.provider.execute_call_count, harness.provider.reconcile_call_count)
    dispatch = await Dispatcher(restored, harness.registry, clock=harness.clock).dispatch_one(
        harness.lease.owner
    )
    lookup = await Reconciler(restored, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    assert reopened.rebuild_and_verify(harness.lease.saga_id) == pending
    assert restored.rebuild_and_verify(harness.lease.saga_id) == pending
    assert dispatch.failure is not None and dispatch.failure.code == "no_work"
    assert lookup.failure is not None and lookup.failure.code == "no_work"
    assert calls_before == (
        harness.provider.execute_call_count,
        harness.provider.reconcile_call_count,
    )

    reused = harness.kernel.apply_human_decision(harness.lease.saga_id, first, harness.lease)
    wrong = _signed_decision(
        harness,
        pending.seq,
        action="reconcile",
        decision_id="decision_00011002",
    )
    rejected = harness.kernel.apply_human_decision(harness.lease.saga_id, wrong, harness.lease)
    second = _signed_decision(
        harness,
        pending.seq,
        decision_id="decision_00011002",
    )
    approved = harness.kernel.apply_human_decision(harness.lease.saga_id, second, harness.lease)
    final = harness.store.load_snapshot(harness.lease.saga_id)

    assert approved.accepted is True
    assert reused.accepted is False and reused.code == "decision_already_used"
    assert rejected.accepted is False and rejected.code == "human_decision_action_inapplicable"
    assert harness.store.runnable_count(harness.lease.saga_id) == 1
    assert first.decision_id in pending.consumed_approval_ids
    assert second.decision_id in final.consumed_approval_ids


@pytest.mark.asyncio
async def test_mixed_human_reconciliation_recovers_ack_loss_exactly_once(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    harness.add_runnable_operation()
    await harness.dispatch_with_response_loss()
    paused = _pause_unknown_lookup(harness)
    decision = _signed_decision(harness, paused.seq, action="reconcile")
    assert harness.kernel.apply_human_decision(
        harness.lease.saga_id, decision, harness.lease
    ).accepted
    failpoint = OneShotFailpoint(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
    store = SQLiteKernelStore.open(harness.store.path, clock=harness.clock, failpoint=failpoint)

    result = await Reconciler(store, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )
    events = store.read_events(harness.lease.saga_id)

    assert result.failure is None
    assert failpoint.fired
    assert sum(event.event_type == "reconciliation_recorded" for event in events) == 1
    assert sum(event.event_type == "human_required" for event in events) == 2
    assert harness.provider.reconcile_call_count == 1
    assert store.runnable_count(harness.lease.saga_id) == 0


@pytest.mark.asyncio
async def test_mixed_no_effect_requeue_stays_suspended_until_fresh_approval(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    harness.add_runnable_operation()
    await harness.dispatch_with_response_loss()
    paused = _pause_unknown_lookup(harness)
    decision = _signed_decision(harness, paused.seq, action="reconcile")
    assert harness.kernel.apply_human_decision(
        harness.lease.saga_id, decision, harness.lease
    ).accepted
    harness.provider.set_reconciliation_no_effect()

    result = await Reconciler(harness.store, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )
    pending = harness.store.load_snapshot(harness.lease.saga_id)
    job = harness.store.reconciliation_job(harness.operation_id)

    assert result.decision is not None and result.decision.action == "retry_same_id"
    assert pending.status is SagaStatus.HUMAN_REQUIRED and pending.pending_approval
    assert job is not None and job.state is ReconciliationJobState.REQUEUED
    assert harness.store.runnable_count(harness.lease.saga_id) == 0
    assert harness.provider.execute_call_count == 1

    approved = harness.kernel.apply_human_decision(
        harness.lease.saga_id,
        _signed_decision(harness, pending.seq, decision_id="decision_00011003"),
        harness.lease,
    )

    assert approved.accepted
    assert harness.store.runnable_count(harness.lease.saga_id) == 2


@pytest.mark.asyncio
async def test_mixed_reconciliation_rejects_forged_followup_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    harness.add_runnable_operation()
    await harness.dispatch_with_response_loss()
    paused = _pause_unknown_lookup(harness)
    decision = _signed_decision(harness, paused.seq, action="reconcile")
    assert harness.kernel.apply_human_decision(
        harness.lease.saga_id, decision, harness.lease
    ).accepted
    commit = harness.store.commit_reconciliation

    def commit_with_forged_reason(
        job: ReconciliationJob,
        lease: Lease,
        batch: TransitionBatch,
        action: str,
        check_after: datetime | None,
    ) -> SagaSnapshot:
        events = _replace_human_reason(batch.events)
        projection = _replay_batch(harness.store.load_snapshot(batch.saga_id), events)
        changed = batch.model_copy(update={"events": events, "projection": projection})
        return commit(job, lease, changed, action, check_after)

    monkeypatch.setattr(harness.store, "commit_reconciliation", commit_with_forged_reason)
    result = await Reconciler(harness.store, harness.registry, clock=harness.clock).reconcile_one(
        harness.lease.owner
    )

    assert result.failure is not None and result.failure.code == "authority_lost"
    assert (
        harness.store.load_snapshot(harness.lease.saga_id).status is SagaStatus.RECONCILING_UNKNOWN
    )
    assert harness.store.runnable_count(harness.lease.saga_id) == 0


def test_inapplicable_reconcile_does_not_consume_or_brick_approval(tmp_path: Path) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    paused = _pause_queued_command(harness)

    wrong = harness.kernel.apply_human_decision(
        harness.lease.saga_id,
        _signed_decision(harness, paused.seq, action="reconcile"),
        harness.lease,
    )
    corrected = harness.kernel.apply_human_decision(
        harness.lease.saga_id,
        _signed_decision(harness, paused.seq, action="approve"),
        harness.lease,
    )

    assert wrong.accepted is False and wrong.code == "human_decision_action_inapplicable"
    assert corrected.accepted is True
    assert harness.store.runnable_count(harness.lease.saga_id) == 1


@pytest.mark.asyncio
async def test_inapplicable_approval_does_not_consume_or_brick_reconciliation(
    tmp_path: Path,
) -> None:
    harness = KernelHarness.create_human_enabled(tmp_path)
    await harness.dispatch_with_response_loss()
    paused = _pause_unknown_lookup(harness)

    wrong = harness.kernel.apply_human_decision(
        harness.lease.saga_id, _signed_decision(harness, paused.seq), harness.lease
    )
    corrected = harness.kernel.apply_human_decision(
        harness.lease.saga_id,
        _signed_decision(harness, paused.seq, action="reconcile"),
        harness.lease,
    )

    assert wrong.accepted is False and wrong.code == "human_decision_action_inapplicable"
    assert corrected.accepted is True
    job = harness.store.reconciliation_job(harness.operation_id)
    assert job is not None and job.state is ReconciliationJobState.DUE


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("missing", "definition", "schema", "capability"))
async def test_historical_tool_evidence_failure_is_durable_human_after_backup(
    tmp_path: Path, fault: str
) -> None:
    harness = KernelHarness.create_reversible(tmp_path)
    await harness.dispatcher.dispatch_one(harness.lease.owner)
    incompatible = _incompatible_registry(harness.registry, fault)

    decision = EmergencyUnwinder(harness.store, clock=harness.clock).execute(
        harness.lease.saga_id,
        incompatible,
        UnwindTrigger.AGENT_UNAVAILABLE,
        harness.lease,
    )
    backup = tmp_path / f"{fault}-backup.db"
    restored_path = tmp_path / f"{fault}-restored.db"
    harness.store.backup_to(backup)
    restored = SQLiteKernelStore.restore_from(backup, restored_path)

    assert decision.action is UnwindAction.HUMAN_REQUIRED
    assert decision.reason_codes == ("historical_tool_evidence_unavailable",)
    assert decision.blocker_operation_ids == (harness.operation_id,)
    assert restored.rebuild_and_verify(harness.lease.saga_id).status is SagaStatus.HUMAN_REQUIRED


def _signed_decision(
    harness: KernelHarness,
    sequence: int,
    *,
    action: Literal["approve", "reject", "reconcile"] = "approve",
    decision_id: str = "decision_00011001",
) -> HumanDecision:
    decision = HumanDecision(
        decision_id=decision_id,
        saga_id=harness.lease.saga_id,
        based_on_saga_seq=sequence,
        action=action,
        proposal_hash="0" * 64,
        actor="authorized-operator",
        issued_at=harness.clock.now(),
        auth_proof="verified-human-proof",
    )
    return decision.model_copy(update={"proposal_hash": human_resolution_digest(decision)})


def _replace_human_reason(events: tuple[LedgerEvent, ...]) -> tuple[LedgerEvent, ...]:
    return tuple(
        event.model_copy(update={"reason_code": "forged_followup"})
        if isinstance(event, HumanRequired)
        else event
        for event in events
    )


def _replay_batch(snapshot: SagaSnapshot, events: tuple[LedgerEvent, ...]) -> SagaSnapshot:
    projected = snapshot
    for event in events:
        projected = reduce_event(projected, event)
    return projected


def _pause_queued_command(harness: KernelHarness) -> SagaSnapshot:
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    proposal = Escalate(
        proposal_id="proposal_00011004",
        based_on_saga_seq=snapshot.seq,
        reason_code="operator_intervention",
        rationale="Pause queued work for exact human resolution.",
    )
    harness.kernel.submit_proposal(harness.lease.saga_id, proposal, harness.lease)
    return harness.store.load_snapshot(harness.lease.saga_id)


def _resolution_batch(
    harness: KernelHarness, snapshot: SagaSnapshot, decision: HumanDecision
) -> TransitionBatch:
    event = HumanResolutionRecorded(
        event_id="evt_forgedhumanresolution01",
        saga_id=snapshot.saga_id,
        saga_seq=snapshot.seq + 1,
        definition_version=snapshot.definition_version,
        fence_token=harness.lease.fence_token,
        actor=decision.actor,
        trace_id="trace_forgedhumanresolution01",
        recorded_at=harness.clock.now(),
        decision_id=decision.decision_id,
        proposal_hash=decision.proposal_hash,
        verification_result=True,
        action=decision.action,
    )
    return TransitionBatch(
        transition_id="txn_forgedhumanresolution01",
        saga_id=snapshot.saga_id,
        expected_seq=snapshot.seq,
        expected_fence_token=harness.lease.fence_token,
        lease_owner=harness.lease.owner,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )


def _changed_resolution_batch(
    harness: KernelHarness,
    snapshot: SagaSnapshot,
    decision: HumanDecision,
    field: str,
) -> TransitionBatch:
    batch = _resolution_batch(harness, snapshot, decision)
    if field == "sequence":
        return batch.model_copy(update={"expected_seq": snapshot.seq + 1})
    event_field = "proposal_hash" if field == "digest" else field
    value: object = "1" * 64 if field == "digest" else f"different-{field}"
    if field == "action":
        value = "reconcile"
    event = batch.events[0].model_copy(update={event_field: value})
    return batch.model_copy(update={"events": (event,)})


def _pause_unknown_lookup(harness: KernelHarness) -> SagaSnapshot:
    snapshot = harness.store.load_snapshot(harness.lease.saga_id)
    proposal = Escalate(
        proposal_id="proposal_00011005",
        based_on_saga_seq=snapshot.seq,
        reason_code="operator_intervention",
        rationale="Pause unknown provider work for exact human resolution.",
    )
    harness.kernel.submit_proposal(harness.lease.saga_id, proposal, harness.lease)
    return harness.store.load_snapshot(harness.lease.saga_id)


def _incompatible_registry(registry: ToolRegistry, fault: str) -> ToolRegistry:
    definitions = registry.effect_definitions()
    if fault == "missing":
        return ToolRegistry()
    forward, repair = definitions
    changed = _changed_definition(forward, fault)
    return ToolRegistry((changed, repair))


def _changed_definition(
    definition: EffectToolDefinition[BaseModel], fault: str
) -> EffectToolDefinition[BaseModel]:
    if fault == "definition":
        return replace(definition, definition_version="future-definition-v2")
    if fault == "schema":
        return replace(definition, command_schema_version="future-command-v2")
    capabilities = definition.capabilities.model_copy(
        update={"idempotency_retention_seconds": 86_401}
    )
    return replace(definition, capabilities=capabilities)
