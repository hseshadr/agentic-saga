from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentic_saga.contracts.actions import Finish
from agentic_saga.contracts.common import Direction, sha256_json
from agentic_saga.contracts.events import (
    AgentTurnReserved,
    EffectIntentRecorded,
    InvariantEvaluated,
    LedgerEvent,
    ProposalRejected,
    TerminalDenied,
)
from agentic_saga.contracts.runtime import (
    ExecutionBudget,
    SagaGoal,
    SagaStatus,
    TerminalRequirement,
)
from agentic_saga.execution.runtime import _saga_id
from agentic_saga.kernel.failpoints import DurabilityPoint
from agentic_saga.kernel.invariants import (
    InvariantEvidence,
    InvariantResult,
    TerminalGate,
)
from agentic_saga.kernel.ports import Lease, StoreFailpoint, UnwindQuiescence
from agentic_saga.kernel.reducer import rebuild_projection
from agentic_saga.kernel.runtime import EventMetadataFactory, SagaKernel, StableIdFactory
from agentic_saga.kernel.state import SagaSnapshot
from tests.support.durable_tool import DurableFakeTool, DurableToolCall, DurableToolEffect
from tests.support.kernel_harness import REPAIR_TOOL_NAME, TOOL_NAME, CrashKernelHarness
from tests.unit.execution.test_runtime_loop import (
    _agent,
    _effect_registry,
    _harness,
    _wait_for_agent_call,
)


class TerminalBlocker(StrEnum):
    UNKNOWN = "unknown"
    RUNNABLE = "runnable"
    CLAIMED = "claimed"
    ABSENT = "absent"
    STALE = "stale"
    FAILING = "failing"


_PROOF_BLOCKERS: Final[frozenset[TerminalBlocker]] = frozenset(
    {TerminalBlocker.ABSENT, TerminalBlocker.STALE, TerminalBlocker.FAILING}
)
type OutboxFingerprint = tuple[str | int | bytes | None, ...]
_OUTBOX_FINGERPRINT_SQL: Final = (
    "SELECT command_id, saga_id, operation_id, tool_name, definition_version, "
    "command_schema_version, step_instance_id, direction, semantic_generation, command_json, "
    "command_hash, capabilities_json, capability_digest, state, available_at, claim_id, "
    "claim_owner, claim_expires_at, claim_generation, claim_fence_token, delivery_attempt, "
    "suspended_at_seq FROM outbox_commands WHERE saga_id = ? ORDER BY command_id"
)
_SUFFIX_MUTATIONS: Final[tuple[tuple[int, str, object], ...]] = (
    (0, "actor", "mutated"),
    (0, "trace_id", "trace_" + "f" * 64),
    (0, "recorded_at", datetime(2026, 9, 7, tzinfo=UTC)),
    (0, "evidence_digest", "f" * 64),
    (1, "proposal_hash", "f" * 64),
    (1, "reason_code", "invalid_invariant_evidence"),
)


@dataclass(frozen=True)
class TerminalAuthority:
    runnable: int
    quiescence: UnwindQuiescence
    outbox: tuple[OutboxFingerprint, ...]
    calls: tuple[tuple[DurableToolCall, ...], tuple[DurableToolCall, ...]]
    effects: tuple[tuple[DurableToolEffect, ...], tuple[DurableToolEffect, ...]]


@dataclass(frozen=True)
class TerminalAttempt:
    accepted: bool
    before_events: tuple[LedgerEvent, ...]
    before_snapshot: SagaSnapshot
    after_events: tuple[LedgerEvent, ...]
    before_authority: TerminalAuthority
    after_authority: TerminalAuthority
    after_snapshot: SagaSnapshot
    blocker: TerminalBlocker


@dataclass(frozen=True)
class Reservation:
    index: int
    elapsed_ms: int
    tokens: int


@dataclass(frozen=True)
class BudgetEvidence:
    original: tuple[Reservation, ...]
    reopened: tuple[Reservation, ...]
    tool_calls: int


class _NoOpCrash:
    def hit(self, point: DurabilityPoint | StoreFailpoint) -> None:
        del point


@dataclass(frozen=True)
class _Proof:
    blocker: TerminalBlocker

    def evaluate(
        self, saga_id: str, snapshot: SagaSnapshot, target_status: SagaStatus
    ) -> InvariantEvidence:
        if self.blocker is TerminalBlocker.ABSENT:
            raise RuntimeError("proof unavailable")
        return InvariantEvidence(
            saga_id=saga_id,
            definition_version=snapshot.definition_version,
            evaluated_at_seq=snapshot.seq - int(self.blocker is TerminalBlocker.STALE),
            target_status=target_status,
            invariant_version="property-v1",
            results=(_proof_result(self.blocker),),
        )


def _proof_result(blocker: TerminalBlocker) -> InvariantResult:
    return InvariantResult(
        rule_id="property_terminal",
        passed=blocker is not TerminalBlocker.FAILING,
        inputs={},
        explanation="property proof",
    )


def _terminal_attempt(blocker: TerminalBlocker) -> TerminalAttempt:
    with TemporaryDirectory(prefix="agentic-saga-terminal-") as directory:
        harness = _terminal_harness(Path(directory))
        _prepare(harness, blocker)
        return _run_terminal_attempt(harness, blocker)


def _run_terminal_attempt(harness: CrashKernelHarness, blocker: TerminalBlocker) -> TerminalAttempt:
    before_events = harness.store.read_events(harness.lease.saga_id)
    before_authority = _authority(harness)
    result = _terminal_kernel(harness, blocker).assign_terminal(
        harness.lease.saga_id, _finish(len(before_events)), harness.lease
    )
    return TerminalAttempt(
        result.accepted,
        before_events,
        rebuild_projection(before_events),
        harness.store.read_events(harness.lease.saga_id),
        before_authority,
        _authority(harness),
        harness.store.load_snapshot(harness.lease.saga_id),
        blocker,
    )


def _terminal_harness(directory: Path) -> CrashKernelHarness:
    DurableFakeTool.initialize(directory / "forward.db", TOOL_NAME)
    DurableFakeTool.initialize(directory / "repair.db", REPAIR_TOOL_NAME)
    return CrashKernelHarness.initialize(directory, _NoOpCrash())


def _prepare(harness: CrashKernelHarness, blocker: TerminalBlocker) -> None:
    if blocker is TerminalBlocker.UNKNOWN:
        harness.ensure_forward_intent()
        harness.forward_provider.enable_response_loss_after_effect()
        asyncio.run(harness.dispatcher.dispatch_saga(harness.lease.saga_id, harness.lease.owner))
        return
    harness.ensure_forward_intent()
    if blocker in _PROOF_BLOCKERS:
        asyncio.run(harness.settle(Direction.FORWARD))
    if blocker is TerminalBlocker.CLAIMED:
        claimed = harness.store.claim_outbox_for_saga(
            harness.lease.saga_id, "property-claim", timedelta(minutes=1)
        )
        assert claimed is not None


def _terminal_kernel(harness: CrashKernelHarness, blocker: TerminalBlocker) -> SagaKernel:
    return SagaKernel(
        store=harness.store,
        policy=harness.kernel.policy,
        registry=harness.registry,
        policy_context_provider=harness.contexts,
        event_metadata_factory=EventMetadataFactory(harness.clock),
        id_factory=StableIdFactory(namespace=b"task-17-terminal"),
        terminal_gate=_terminal_gate(),
        invariant_evidence_provider=_Proof(blocker),
    )


def _terminal_gate() -> TerminalGate:
    targets = (
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    )
    requirement = TerminalRequirement(
        invariant_version="property-v1", required_rule_ids=("property_terminal",)
    )
    return TerminalGate({target: requirement for target in targets})


def _finish(based_on_saga_seq: int) -> Finish:
    return Finish.model_validate(
        {
            "proposal_id": "proposal_00008090",
            "based_on_saga_seq": based_on_saga_seq,
            "rationale": "Attempt terminal completion.",
            "target_status": "succeeded_verified",
        }
    )


def _authority(harness: CrashKernelHarness) -> TerminalAuthority:
    lease = Lease.model_validate(harness.lease.model_dump())
    return TerminalAuthority(
        harness.store.runnable_count(harness.lease.saga_id),
        harness.store.inspect_unwind_quiescence(harness.lease.saga_id, lease),
        _outbox_fingerprint(harness),
        (harness.forward_provider.calls, harness.repair_provider.calls),
        (harness.forward_provider.effects, harness.repair_provider.effects),
    )


def _outbox_fingerprint(harness: CrashKernelHarness) -> tuple[OutboxFingerprint, ...]:
    with closing(sqlite3.connect(harness.store.path)) as connection, connection:
        rows = connection.execute(_OUTBOX_FINGERPRINT_SQL, (harness.lease.saga_id,)).fetchall()
    return tuple(cast(OutboxFingerprint, row) for row in rows)


def _assert_terminal_denied(attempt: TerminalAttempt) -> None:
    suffix = attempt.after_events[len(attempt.before_events) :]
    assert not attempt.accepted
    assert attempt.after_events[: len(attempt.before_events)] == attempt.before_events
    assert tuple(event.event_type for event in suffix) == _expected_suffix(attempt.blocker)
    _assert_terminal_fields(attempt, suffix)
    assert attempt.after_snapshot == rebuild_projection(attempt.after_events)
    assert _same_authority(attempt.after_authority, attempt.before_authority)


def _expected_suffix(blocker: TerminalBlocker) -> tuple[str, ...]:
    if blocker is TerminalBlocker.UNKNOWN:
        return ("proposal_rejected",)
    if blocker in {TerminalBlocker.ABSENT, TerminalBlocker.STALE}:
        return ("terminal_denied",)
    return "invariant_evaluated", "terminal_denied"


def _assert_terminal_fields(attempt: TerminalAttempt, suffix: tuple[LedgerEvent, ...]) -> None:
    proposal_digest = sha256_json(_finish(attempt.before_snapshot.seq).model_dump(mode="json"))
    _assert_suffix_identity(attempt, suffix)
    if attempt.blocker is TerminalBlocker.UNKNOWN:
        _assert_rejection(suffix[0], proposal_digest)
        return
    _assert_denial(suffix[-1], attempt.blocker, proposal_digest)
    if isinstance(suffix[0], InvariantEvaluated):
        _assert_evaluation(suffix[0], attempt)


def _assert_suffix_identity(attempt: TerminalAttempt, suffix: tuple[LedgerEvent, ...]) -> None:
    previous = attempt.before_events[-1]
    ids = StableIdFactory(namespace=b"task-17-terminal")
    transition = ids.transition_id(previous.saga_id, "proposal_00008090")
    for offset, event in enumerate(suffix, start=1):
        assert event == event.model_copy(
            update=_expected_metadata(previous, ids, transition, offset)
        )


def _expected_metadata(
    previous: LedgerEvent, ids: StableIdFactory, transition: str, offset: int
) -> dict[str, object]:
    return {
        "event_id": ids.event_id(transition, offset - 1),
        "saga_id": previous.saga_id,
        "saga_seq": previous.saga_seq + offset,
        "schema_version": "1.0",
        "definition_version": previous.definition_version,
        "fence_token": previous.fence_token,
        "actor": "kernel",
        "trace_id": ids.trace_id(transition),
        "recorded_at": previous.recorded_at,
    }


def _assert_rejection(event: LedgerEvent, proposal_digest: str) -> None:
    assert isinstance(event, ProposalRejected)
    assert event.proposal_id == "proposal_00008090"
    assert event.proposal_hash == proposal_digest
    assert event.reason_code == "saga_phase_denied"


def _assert_denial(event: LedgerEvent, blocker: TerminalBlocker, proposal_digest: str) -> None:
    expected = (
        "invariant_provider_error"
        if blocker is TerminalBlocker.ABSENT
        else ("terminal_gate_denied", "invalid_invariant_evidence")[
            blocker is TerminalBlocker.STALE
        ]
    )
    assert isinstance(event, TerminalDenied)
    assert event.proposal_id == "proposal_00008090"
    assert event.proposal_hash == proposal_digest
    assert event.target_status == SagaStatus.SUCCEEDED_VERIFIED.value
    assert event.reason_code == expected


def _assert_evaluation(event: InvariantEvaluated, attempt: TerminalAttempt) -> None:
    evidence = _Proof(attempt.blocker).evaluate(
        attempt.before_snapshot.saga_id, attempt.before_snapshot, SagaStatus.SUCCEEDED_VERIFIED
    )
    assert event.evaluated_at_seq == attempt.before_events[-1].saga_seq
    assert event.target_status == SagaStatus.SUCCEEDED_VERIFIED.value
    assert event.invariant_version == "property-v1"
    assert event.evidence_digest == sha256_json(evidence.model_dump(mode="json"))
    assert event.results == {"property_terminal": attempt.blocker is not TerminalBlocker.FAILING}
    assert event.all_passed is (attempt.blocker is not TerminalBlocker.FAILING)


def _same_authority(after: TerminalAuthority, before: TerminalAuthority) -> bool:
    normalized = after.quiescence.model_copy(update={"saga_seq": before.quiescence.saga_seq})
    return replace(after, quiescence=normalized) == before


def _mutated_suffix_attempt(index: int, field: str, value: object) -> TerminalAttempt:
    attempt = _terminal_attempt(TerminalBlocker.RUNNABLE)
    position = len(attempt.before_events) + index
    event = attempt.after_events[position].model_copy(update={field: value})
    events = (*attempt.after_events[:position], event, *attempt.after_events[position + 1 :])
    return replace(attempt, after_events=events, after_snapshot=rebuild_projection(events))


def _budget(turns: int) -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=turns,
        tool_call_limit=turns,
        elapsed_ms_limit=1_200,
        token_limit=120,
    )


def _reservations(events: tuple[LedgerEvent, ...]) -> tuple[Reservation, ...]:
    return tuple(
        Reservation(
            event.turn_index,
            event.reserved_elapsed_ms,
            event.reserved_tokens,
        )
        for event in events
        if isinstance(event, AgentTurnReserved)
    )


def _expected(turns: int) -> tuple[Reservation, ...]:
    values = {1: (1_200, 120), 2: (600, 60), 3: (400, 40), 4: (300, 30)}
    elapsed, tokens = values[turns]
    return tuple(Reservation(index, elapsed, tokens) for index in range(1, turns + 1))


def _prefixes(values: tuple[Reservation, ...]) -> tuple[tuple[int, int], ...]:
    elapsed = tokens = 0
    result: list[tuple[int, int]] = []
    for item in values:
        elapsed, tokens = elapsed + item.elapsed_ms, tokens + item.tokens
        result.append((elapsed, tokens))
    return tuple(result)


def _budget_evidence(turns: int) -> BudgetEvidence:
    with TemporaryDirectory(prefix="agentic-saga-budget-") as directory:
        return _run_budget(Path(directory), turns)


def _run_budget(directory: Path, turns: int) -> BudgetEvidence:
    provider = DurableFakeTool.initialize(directory / "provider.db", "mutate_generic")
    harness = _harness(directory, _effect_registry(provider), execution_budget=_budget(turns))
    goal = SagaGoal(goal_id=f"goal_property_{turns}", text="Reserve safely.", context={})
    agent = _agent(["effect"] * (turns - 1) + ["escalate"], tool_name="mutate_generic")
    result = asyncio.run(
        harness.runtime.start(definition=harness.definition, goal=goal, agent=agent)
    )
    events = harness.store.read_events(result.saga_id)
    reopened = harness.store.open(harness.store.path, clock=harness.clock)
    return BudgetEvidence(
        _reservations(events),
        _reservations(reopened.read_events(result.saga_id)),
        _tool_calls(events),
    )


def _tool_calls(events: tuple[LedgerEvent, ...]) -> int:
    return sum(isinstance(event, EffectIntentRecorded) for event in events)


async def _unresolved() -> tuple[tuple[Reservation, ...], tuple[Reservation, ...]]:
    with TemporaryDirectory(prefix="agentic-saga-resume-") as directory:
        harness = _harness(Path(directory), execution_budget=_budget(1))
        goal = SagaGoal(goal_id="goal_property_unresolved", text="Resume safely.", context={})
        agent = _agent([], mode="block")
        active = asyncio.create_task(
            harness.runtime.start(definition=harness.definition, goal=goal, agent=agent)
        )
        await _wait_for_agent_call(agent)
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
        before = _reservations(harness.store.read_events(_saga_id(goal)))
        result = await harness.runtime.resume(saga_id=_saga_id(goal), agent=_agent([]))
        return before, _reservations(harness.store.read_events(result.saga_id))


@given(blocker=st.sampled_from(tuple(TerminalBlocker)))
@settings(deadline=None)
def test_terminal_denial_has_exact_blocker_suffix(blocker: TerminalBlocker) -> None:
    # Catches mutation: allowing terminal denial to append unrelated durable authority.
    _assert_terminal_denied(_terminal_attempt(blocker))


@pytest.mark.parametrize(("index", "field", "value"), _SUFFIX_MUTATIONS)
def test_terminal_oracle_rejects_mutated_suffix(index: int, field: str, value: object) -> None:
    # Catches mutation: accepting changed terminal evidence after a real denial transition.
    with pytest.raises(AssertionError):
        _assert_terminal_denied(_mutated_suffix_attempt(index, field, value))


def test_terminal_authority_observes_every_claim_field() -> None:
    # Catches mutation: changing a claimed outbox owner without changing terminal authority.
    with TemporaryDirectory(prefix="agentic-saga-claim-fingerprint-") as directory:
        harness = _terminal_harness(Path(directory))
        _prepare(harness, TerminalBlocker.CLAIMED)
        before = _authority(harness)
        with closing(sqlite3.connect(harness.store.path)) as connection, connection:
            connection.execute(
                "UPDATE outbox_commands SET claim_owner = 'mutated-owner' WHERE state = 'claimed'"
            )
        assert _authority(harness) != before


@given(turns=st.integers(min_value=1, max_value=4))
@settings(deadline=None)
def test_budget_reservations_are_exact_across_reopen(turns: int) -> None:
    # Catches mutation: dropping or zeroing a durable reservation component.
    evidence = _budget_evidence(turns)
    assert evidence.original == evidence.reopened == _expected(turns)
    assert _prefixes(evidence.original) == _prefixes(_expected(turns))
    assert evidence.tool_calls == turns - 1


def test_unresolved_reserved_turn_is_not_reentered_after_reopen() -> None:
    # Catches mutation: treating a cancelled durable reservation as free runtime capacity.
    assert asyncio.run(_unresolved()) == (_expected(1), _expected(1))
