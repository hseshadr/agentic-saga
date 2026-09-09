from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from agentic_saga.contracts.actions import Finish, HumanDecision
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.events import (
    HumanResolutionRecorded,
    InvariantEvaluated,
    TerminalAssigned,
    TerminalDenied,
)
from agentic_saga.contracts.runtime import SagaStatus, TerminalRequirement
from agentic_saga.execution.leases import LeaseService
from agentic_saga.kernel.invariants import (
    AcceptedResidual,
    InvariantEvidence,
    InvariantResult,
    TerminalGate,
    VerifiedHumanResolution,
    build_invariant_event_fields,
)
from agentic_saga.kernel.policy import human_resolution_digest
from agentic_saga.kernel.ports import (
    Lease,
    LeaseLost,
    StoreConflict,
    TransitionBatch,
)
from agentic_saga.kernel.runtime import (
    InvariantEvidenceProvider,
    SagaKernel,
)
from agentic_saga.kernel.state import SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.integration.kernel.test_proposal_to_outbox import (
    NOW,
    SAGA_ID,
    Contexts,
    compensation_proposal,
    escalation,
    event_batch,
    prepare_compensation,
    settle_forward,
)
from tests.integration.kernel.test_proposal_to_outbox import kernel as proposal_kernel
from tests.integration.kernel.test_proposal_to_outbox import proposal as tool_proposal


@dataclass
class Evidence(InvariantEvidenceProvider):
    passed: bool = True
    sequence_offset: int = 0
    human_resolution: VerifiedHumanResolution | None = None

    def evaluate(
        self, saga_id: str, snapshot: SagaSnapshot, target_status: SagaStatus
    ) -> InvariantEvidence:
        return InvariantEvidence(
            saga_id=saga_id,
            definition_version=snapshot.definition_version,
            evaluated_at_seq=snapshot.seq + self.sequence_offset,
            target_status=target_status,
            invariant_version="invariants-v1",
            results=(invariant_result(self.passed),),
            human_resolution=self.human_resolution,
        )


def invariant_result(passed: bool) -> InvariantResult:
    return InvariantResult(
        rule_id="settled",
        passed=passed,
        inputs={"source": "authoritative"},
        explanation="Authoritative state was evaluated.",
    )


class FailingProvider(InvariantEvidenceProvider):
    def evaluate(
        self, saga_id: str, snapshot: SagaSnapshot, target_status: SagaStatus
    ) -> InvariantEvidence:
        del saga_id, snapshot, target_status
        raise RuntimeError("provider-secret-must-not-persist")


def gate() -> TerminalGate:
    requirement = TerminalRequirement(
        invariant_version="invariants-v1", required_rule_ids=("settled",)
    )
    return TerminalGate(
        {
            status: requirement
            for status in (
                SagaStatus.SUCCEEDED_VERIFIED,
                SagaStatus.COMPENSATED_VERIFIED,
                SagaStatus.ABORTED_CLEAN,
                SagaStatus.RESOLVED_WITH_EXCEPTION,
            )
        }
    )


def terminal_kernel(path: Path, evidence: Evidence) -> tuple[SagaKernel, SQLiteKernelStore, Lease]:
    runtime, store, lease, _ = proposal_kernel(
        path, terminal_gate=gate(), invariant_provider=evidence
    )
    return runtime, store, lease


def finish(
    seq: int = 2,
    target: SagaStatus = SagaStatus.SUCCEEDED_VERIFIED,
    proposal_id: str = "proposal_00007101",
) -> Finish:
    return Finish.model_validate(
        {
            "proposal_id": proposal_id,
            "based_on_saga_seq": seq,
            "rationale": "The authoritative invariants should decide.",
            "target_status": target.value,
        }
    )


def test_finish_rechecks_authoritative_invariants_at_current_sequence(tmp_path: Path) -> None:
    runtime, store, lease = terminal_kernel(tmp_path / "saga.db", Evidence())

    result = runtime.submit_proposal(SAGA_ID, finish(), lease)

    assert result.accepted is True
    assert store.load_snapshot(SAGA_ID).status is SagaStatus.SUCCEEDED_VERIFIED
    events = store.read_events(SAGA_ID)
    assert isinstance(events[-2], InvariantEvaluated)
    assert events[-2].evaluated_at_seq == 2


def test_finish_with_failing_proof_records_nonterminal_denial(tmp_path: Path) -> None:
    runtime, store, lease = terminal_kernel(tmp_path / "saga.db", Evidence(passed=False))

    result = runtime.submit_proposal(SAGA_ID, finish(), lease)

    assert result.accepted is False
    assert store.load_snapshot(SAGA_ID).status is SagaStatus.RUNNING
    assert isinstance(store.read_events(SAGA_ID)[-1], TerminalDenied)


def test_finish_with_stale_proof_never_fabricates_invariant_event(tmp_path: Path) -> None:
    runtime, store, lease = terminal_kernel(tmp_path / "saga.db", Evidence(sequence_offset=-1))

    result = runtime.submit_proposal(SAGA_ID, finish(), lease)

    assert result.code == "invalid_invariant_evidence"
    assert not any(isinstance(event, InvariantEvaluated) for event in store.read_events(SAGA_ID))


def test_direct_terminal_assignment_is_idempotent(tmp_path: Path) -> None:
    runtime, store, lease = terminal_kernel(tmp_path / "saga.db", Evidence())
    proposal = finish()

    first = runtime.assign_terminal(SAGA_ID, proposal, lease)
    second = runtime.assign_terminal(SAGA_ID, proposal, lease)

    assert second == first
    assert len(store.read_events(SAGA_ID)) == 4


def test_direct_terminal_assignment_rejects_raw_nonterminal_status(tmp_path: Path) -> None:
    runtime, store, lease = terminal_kernel(tmp_path / "saga.db", Evidence())
    invalid = cast(Finish, SagaStatus.RUNNING)

    result = runtime.assign_terminal(SAGA_ID, invalid, lease)

    assert result.code == "invalid_terminal_proposal"
    assert store.load_snapshot(SAGA_ID).status is SagaStatus.RUNNING
    assert len(store.read_events(SAGA_ID)) == 2


def test_invariant_provider_exception_records_safe_denial(tmp_path: Path) -> None:
    runtime, store, lease, _ = proposal_kernel(
        tmp_path / "saga.db",
        terminal_gate=gate(),
        invariant_provider=FailingProvider(),
    )

    result = runtime.submit_proposal(SAGA_ID, finish(), lease)

    assert result.code == "invariant_provider_error"
    assert b"provider-secret-must-not-persist" not in store.path.read_bytes()
    assert isinstance(store.read_events(SAGA_ID)[-1], TerminalDenied)


def test_new_proposal_against_terminal_saga_is_non_mutating_denial(tmp_path: Path) -> None:
    runtime, store, lease = terminal_kernel(tmp_path / "saga.db", Evidence())
    runtime.submit_proposal(SAGA_ID, finish(), lease)
    before = store.read_events(SAGA_ID)
    new_call = tool_proposal(
        proposal_id="proposal_00007102",
        based_on_saga_seq=4,
    )

    result = runtime.submit_proposal(SAGA_ID, new_call, lease)

    assert result.accepted is False
    assert result.code == "immutable_saga"
    assert store.read_events(SAGA_ID) == before


def compensated_saga(path: Path) -> tuple[SagaKernel, SQLiteKernelStore, Lease]:
    contexts = Contexts()
    runtime, store, lease, _ = proposal_kernel(
        path, contexts=contexts, terminal_gate=gate(), invariant_provider=Evidence()
    )
    prepare_compensation(runtime, store, lease, contexts)
    runtime.submit_proposal(SAGA_ID, compensation_proposal(), lease)
    settle_forward(store, lease, 7007)
    return runtime, store, lease


def test_compensated_finish_uses_fresh_target_bound_proof(tmp_path: Path) -> None:
    runtime, store, lease = compensated_saga(tmp_path / "saga.db")
    proposal = finish(9, SagaStatus.COMPENSATED_VERIFIED)

    result = runtime.assign_terminal(SAGA_ID, proposal, lease)

    assert result.accepted is True
    assert store.load_snapshot(SAGA_ID).status is SagaStatus.COMPENSATED_VERIFIED


def verified_resolution(operation_id: str, seq: int) -> VerifiedHumanResolution:
    residual = AcceptedResidual(
        operation_id=operation_id,
        reason="The confirmed effect is explicitly accepted.",
        evidence={"provider_state": "confirmed"},
    )
    return VerifiedHumanResolution(
        decision_id="decision_00007101",
        actor="authorized-operator",
        proposal_hash=resolution_decision(seq - 1).proposal_hash,
        reason="The residual effect is accepted.",
        accepted_residuals=(residual,),
        saga_id=SAGA_ID,
        resolved_at_seq=seq,
        verification_result=True,
    )


def resolution_decision(sequence: int) -> HumanDecision:
    decision = HumanDecision(
        decision_id="decision_00007101",
        saga_id=SAGA_ID,
        based_on_saga_seq=sequence,
        action="approve",
        proposal_hash="0" * 64,
        actor="authorized-operator",
        issued_at=NOW,
        auth_proof="verified-terminal-proof",
    )
    return decision.model_copy(update={"proposal_hash": human_resolution_digest(decision)})


def verify_resolution(decision: HumanDecision, snapshot: SagaSnapshot) -> bool:
    del snapshot
    return decision.auth_proof == "verified-terminal-proof"


def human_resolution_event(
    snapshot: SagaSnapshot, lease: Lease, decision: HumanDecision
) -> HumanResolutionRecorded:
    return HumanResolutionRecorded(
        event_id="evt_0000000000007199",
        saga_id=SAGA_ID,
        saga_seq=snapshot.seq + 1,
        definition_version=snapshot.definition_version,
        fence_token=lease.fence_token,
        actor="authorized-operator",
        trace_id="trace_0000000000007199",
        recorded_at=NOW,
        decision_id=decision.decision_id,
        proposal_hash=decision.proposal_hash,
        verification_result=True,
        action=decision.action,
    )


def prepare_human_exception(runtime: SagaKernel, store: SQLiteKernelStore, lease: Lease) -> str:
    accepted = runtime.submit_proposal(SAGA_ID, tool_proposal(), lease)
    assert accepted.operation_id is not None
    settle_forward(store, lease)
    runtime.submit_proposal(SAGA_ID, escalation(5), lease)
    snapshot = store.load_snapshot(SAGA_ID)
    decision = resolution_decision(snapshot.seq)
    event = human_resolution_event(snapshot, lease, decision)
    batch = event_batch(snapshot, lease, event, "txn_0000000000007199")
    authenticated = SQLiteKernelStore.open(
        store.path,
        clock=FakeClock(NOW),
        human_resolution_verifier=verify_resolution,
    )
    authenticated.commit_human_resolution(batch, decision)
    return accepted.operation_id


def test_human_required_finish_requires_verified_residual_resolution(tmp_path: Path) -> None:
    evidence = Evidence()
    runtime, store, lease, _ = proposal_kernel(
        tmp_path / "saga.db", terminal_gate=gate(), invariant_provider=evidence
    )
    operation_id = prepare_human_exception(runtime, store, lease)
    evidence.human_resolution = verified_resolution(operation_id, 7)

    result = runtime.assign_terminal(SAGA_ID, finish(7, SagaStatus.RESOLVED_WITH_EXCEPTION), lease)

    assert result.accepted is True
    assert store.load_snapshot(SAGA_ID).status is SagaStatus.RESOLVED_WITH_EXCEPTION


def terminal_metadata(snapshot: SagaSnapshot, lease: Lease, seq: int) -> dict[str, object]:
    return {
        "event_id": f"evt_{seq:016d}",
        "saga_id": SAGA_ID,
        "saga_seq": seq,
        "definition_version": snapshot.definition_version,
        "fence_token": lease.fence_token,
        "actor": "kernel",
        "trace_id": "trace_0000000000007188",
        "recorded_at": NOW,
    }


def attempted_terminal_events(
    snapshot: SagaSnapshot, lease: Lease
) -> tuple[InvariantEvaluated, TerminalAssigned]:
    evidence = Evidence().evaluate(SAGA_ID, snapshot, SagaStatus.SUCCEEDED_VERIFIED)
    fields = build_invariant_event_fields(evidence).model_dump()
    proof = InvariantEvaluated.model_validate(terminal_metadata(snapshot, lease, 4) | fields)
    assigned = TerminalAssigned.model_validate(
        terminal_metadata(snapshot, lease, 5) | {"status": "succeeded_verified"}
    )
    return proof, assigned


def attempted_terminal_batch(store: SQLiteKernelStore, lease: Lease) -> TransitionBatch:
    snapshot = store.load_snapshot(SAGA_ID)
    events = attempted_terminal_events(snapshot, lease)
    projection = snapshot.model_copy(
        update={"seq": snapshot.seq + 2, "status": SagaStatus.SUCCEEDED_VERIFIED}
    )
    return TransitionBatch(
        transition_id="txn_0000000000007188",
        saga_id=SAGA_ID,
        expected_seq=snapshot.seq,
        expected_fence_token=lease.fence_token,
        lease_owner=lease.owner,
        events=events,
        projection=projection,
    )


@pytest.mark.parametrize("claimed", [False, True])
def test_store_rejects_terminal_commit_with_runnable_or_claimed_work(
    tmp_path: Path, claimed: bool
) -> None:
    runtime, store, lease, _ = proposal_kernel(tmp_path / "saga.db")
    runtime.submit_proposal(SAGA_ID, tool_proposal(), lease)
    if claimed:
        assert store.claim_outbox("dispatcher", timedelta(minutes=1)) is not None
    with pytest.raises(StoreConflict, match="active outbox work"):
        store.commit_terminal(attempted_terminal_batch(store, lease), "d" * 64)
    assert len(store.read_events(SAGA_ID)) == 3


def test_terminal_exact_retry_requires_current_lease(tmp_path: Path) -> None:
    runtime, store, lease = terminal_kernel(tmp_path / "saga.db", Evidence())
    proposal = finish()
    runtime.assign_terminal(SAGA_ID, proposal, lease)
    LeaseService(store).release(lease)
    LeaseService(store).acquire(SAGA_ID, "worker-b", timedelta(minutes=5))

    with pytest.raises(LeaseLost):
        runtime.assign_terminal(SAGA_ID, proposal, lease)
