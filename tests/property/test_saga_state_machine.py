from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from agentic_saga.contracts.common import Direction
from agentic_saga.contracts.events import EffectIntentRecorded, EffectOutcomeRecorded, LedgerEvent
from agentic_saga.contracts.tools import EffectContext
from agentic_saga.execution import LeaseService
from agentic_saga.kernel.reducer import rebuild_projection
from agentic_saga.kernel.state import ObligationStatus, OperationStatus, SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.support.kernel_harness import STEP_ID, KernelHarness


@settings(max_examples=25, stateful_step_count=25, deadline=None)
class SagaRuleMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.directory = TemporaryDirectory(prefix="agentic-saga-property-")
        self.harness = KernelHarness.create_reversible(Path(self.directory.name))
        self.previous = self._events()

    def teardown(self) -> None:
        self.directory.cleanup()

    @precondition(lambda machine: machine._status() is OperationStatus.INTENT_DURABLE)
    @rule()
    def dispatch(self) -> None:
        # Catches mutation: executing before a durable intent or duplicating its effect.
        harness = self.harness
        asyncio.run(harness.dispatcher.dispatch_saga(harness.lease.saga_id, harness.lease.owner))
        self._assert_durable_step()

    @precondition(lambda machine: machine._status() is OperationStatus.INTENT_DURABLE)
    @rule()
    def lose_response(self) -> None:
        # Catches mutation: treating a lost provider response as a confirmed outcome.
        asyncio.run(self.harness.dispatch_with_response_loss())
        self._assert_durable_step()

    @precondition(lambda machine: machine._status() is OperationStatus.OUTCOME_UNKNOWN)
    @rule()
    def reconcile(self) -> None:
        # Catches mutation: blindly re-executing unknown work instead of reconciling it.
        asyncio.run(self.harness.restart_and_reconcile())
        self._assert_durable_step()

    @precondition(lambda machine: machine._status() is OperationStatus.INTENT_DURABLE)
    @rule()
    def take_over_and_reject_stale_dispatch(self) -> None:
        # Catches mutation: allowing an in-flight old fence to append or duplicate an effect.
        asyncio.run(_reject_inflight_stale_dispatch(self.harness))
        self._assert_durable_step()

    @rule()
    def reopen(self) -> None:
        # Catches mutation: reopening SQLite with a projection different from its ledger.
        self._assert_durable_step()

    @invariant()
    def outcomes_follow_intents(self) -> None:
        # Catches mutation: recording an effect outcome before its matching durable intent.
        intents, outcomes = _effect_events(self._events())
        assert all(_has_prior_intent(outcome, intents) for outcome in outcomes)

    @invariant()
    def confirmed_work_has_durable_disposition(self) -> None:
        # Catches mutation: dropping the unresolved obligation for confirmed reversible work.
        confirmed, unresolved = _confirmed_and_unresolved(self._snapshot())
        assert confirmed <= unresolved

    def _status(self) -> OperationStatus:
        return self._snapshot().operations[self.harness.operation_id].status

    def _events(self) -> tuple[LedgerEvent, ...]:
        return self.harness.store.read_events(self.harness.lease.saga_id)

    def _snapshot(self) -> SagaSnapshot:
        return self.harness.store.load_snapshot(self.harness.lease.saga_id)

    def _assert_durable_step(self) -> None:
        events = self._events()
        assert events[: len(self.previous)] == self.previous
        assert self._snapshot() == rebuild_projection(events)
        reopened = SQLiteKernelStore.open(self.harness.store.path, clock=self.harness.clock)
        assert reopened.read_events(self.harness.lease.saga_id) == events
        assert reopened.load_snapshot(self.harness.lease.saga_id) == rebuild_projection(events)
        self.previous = events


async def _reject_inflight_stale_dispatch(harness: KernelHarness) -> None:
    entered, release = harness.provider.blocking_barrier()
    old = asyncio.create_task(
        harness.dispatcher.dispatch_saga(harness.lease.saga_id, harness.lease.owner)
    )
    await entered.wait()
    harness.clock.advance(timedelta(minutes=6))
    harness.lease = LeaseService(harness.store).acquire(
        harness.lease.saga_id, "property-takeover", timedelta(minutes=5)
    )
    await _accept_new_fence(harness)
    before = _authority(harness)
    release.set()
    stale = await old
    assert stale.failure is not None and stale.failure.code == "authority_lost"
    assert _authority(harness) == before


async def _accept_new_fence(harness: KernelHarness) -> None:
    context = EffectContext(
        saga_id=harness.lease.saga_id,
        step_instance_id=STEP_ID,
        operation_id=harness.operation_id,
        fence_token=harness.lease.fence_token,
        delivery_attempt=2,
    )
    await harness.provider.execute(harness.command, context)


def _authority(harness: KernelHarness) -> tuple[SagaSnapshot, tuple[LedgerEvent, ...], int]:
    return (
        harness.store.load_snapshot(harness.lease.saga_id),
        harness.store.read_events(harness.lease.saga_id),
        harness.provider.effect_count(harness.operation_id),
    )


def _effect_events(
    events: tuple[LedgerEvent, ...],
) -> tuple[tuple[EffectIntentRecorded, ...], tuple[EffectOutcomeRecorded, ...]]:
    intents = tuple(event for event in events if isinstance(event, EffectIntentRecorded))
    outcomes = tuple(event for event in events if isinstance(event, EffectOutcomeRecorded))
    return intents, outcomes


def _has_prior_intent(
    outcome: EffectOutcomeRecorded, intents: tuple[EffectIntentRecorded, ...]
) -> bool:
    return any(
        intent.operation_id == outcome.operation_id and intent.saga_seq < outcome.saga_seq
        for intent in intents
    )


def _confirmed_and_unresolved(snapshot: SagaSnapshot) -> tuple[frozenset[str], frozenset[str]]:
    confirmed = frozenset(
        operation.operation_id
        for operation in snapshot.operations.values()
        if operation.direction is Direction.FORWARD
        and operation.status is OperationStatus.EFFECT_CONFIRMED
    )
    states = {ObligationStatus.ARMED, ObligationStatus.ELIGIBLE, ObligationStatus.IN_PROGRESS}
    unresolved = frozenset(
        item.forward_operation_id for item in snapshot.obligations.values() if item.status in states
    )
    return confirmed, unresolved


def test_saga_rule_machine() -> None:
    # Catches mutation: violating append-only replay safety across real runtime transitions.
    SagaRuleMachine.TestCase().runTest()
