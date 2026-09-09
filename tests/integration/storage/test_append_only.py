from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest

from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import Direction
from agentic_saga.contracts.events import DispatchAbortedBeforeEntry
from agentic_saga.kernel.ports import (
    ClaimedCommand,
    InjectedStoreFailure,
    StoreConflict,
    StoreCorruption,
    StoreFailpoint,
    TransitionBatch,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.integration.storage.helpers import (
    OTHER_OPERATION_ID,
    RECORDED_AT,
    SAGA_ID,
    dispatch_batch,
    dispatch_started,
    effect_intent,
    outbox_command,
    outcome_batch,
    prepared_transition,
)


class RaisingFailpoint:
    def __init__(self, target: StoreFailpoint) -> None:
        self._target = target

    def hit(self, point: StoreFailpoint) -> None:
        if point is self._target:
            raise InjectedStoreFailure(point)


class FixedClaimIdFactory:
    def __call__(self) -> str:
        return f"claim_{'c' * 32}"


def _populated(path: Path, clock: FakeClock | None = None) -> SQLiteKernelStore:
    prepared = prepared_transition()
    store = SQLiteKernelStore.initialize(path, clock=clock or FakeClock(RECORDED_AT))
    store.create_saga(prepared.created)
    store.commit_transition(prepared.batch)
    return store


def _second_intent(snapshot: SagaSnapshot) -> TransitionBatch:
    event = effect_intent(operation_id=OTHER_OPERATION_ID, seq=snapshot.seq + 1)
    command = outbox_command(operation_id=OTHER_OPERATION_ID, command_id="cmd_0000000000000002")
    return TransitionBatch(
        transition_id="txn_0000000000000004",
        saga_id=SAGA_ID,
        expected_seq=snapshot.seq,
        expected_fence_token=0,
        events=(event,),
        projection=reduce_event(snapshot, event),
        outbox_commands=(command,),
    )


def _claim_and_dispatch(store: SQLiteKernelStore) -> tuple[ClaimedCommand, SagaSnapshot]:
    claimed = store.claim_outbox("worker", timedelta(seconds=30))
    if claimed is None:
        raise AssertionError("expected a due outbox command")
    dispatched = store.start_dispatch(claimed, dispatch_batch(store.load_snapshot(SAGA_ID)))
    return claimed, dispatched


def _abort_batch(snapshot: SagaSnapshot) -> TransitionBatch:
    fields = dispatch_started(snapshot.seq + 1).model_dump()
    event = DispatchAbortedBeforeEntry.model_validate(
        fields | {"event_type": "dispatch_aborted_before_entry"}
    )
    return TransitionBatch(
        transition_id="txn_0000000000000007",
        saga_id=SAGA_ID,
        expected_seq=snapshot.seq,
        expected_fence_token=0,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )


def _generic_lifecycle_batch(store: SQLiteKernelStore, lifecycle: str) -> TransitionBatch:
    snapshot = store.load_snapshot(SAGA_ID)
    if lifecycle == "dispatch_started":
        return dispatch_batch(snapshot)
    _, dispatched = _claim_and_dispatch(store)
    if lifecycle == "dispatch_aborted_before_entry":
        return _abort_batch(dispatched)
    return outcome_batch(dispatched)


def _finalized_outbox(
    path: Path, *, parked: bool = False
) -> tuple[SQLiteKernelStore, ClaimedCommand, TransitionBatch]:
    store = _populated(path)
    claimed, dispatched = _claim_and_dispatch(store)
    batch = outcome_batch(dispatched, unknown=parked)
    (store.park_outbox if parked else store.complete_outbox)(claimed, batch)
    return store, claimed, batch


def _tamper_historical_claim_fence(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'outbox_claim_fence_guard'"
        ).fetchone()
        if row is not None:
            connection.execute("DROP TRIGGER outbox_claim_fence_guard")
        connection.execute("UPDATE outbox_commands SET claim_fence_token = 1")
        if row is not None:
            connection.execute(str(row[0]))


@pytest.mark.parametrize(
    "statement", ["UPDATE ledger_events SET event_type = event_type", "DELETE FROM ledger_events"]
)
def test_should_forbid_ledger_update_and_delete(tmp_path: Path, statement: str) -> None:
    # Given
    path = tmp_path / "saga.db"
    _populated(path)

    # When / Then
    with (
        closing(sqlite3.connect(path)) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError, match="ledger_events is append-only"),
    ):
        connection.execute(statement)


def test_should_claim_due_command_with_owner_expiry_attempt_and_generation(tmp_path: Path) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")

    # When
    claimed = store.claim_outbox("worker-a", timedelta(seconds=30))

    # Then
    assert claimed is not None
    assert claimed.claim_owner == "worker-a"
    assert claimed.claim_expires_at == RECORDED_AT + timedelta(seconds=30)
    assert claimed.delivery_attempt == 1
    assert claimed.claim_generation == 1
    assert claimed.operation_id == claimed.envelope.operation_id
    assert claimed.tool_name == "charge_payment"
    assert claimed.definition_version == "checkout-v1"
    assert claimed.command_schema_version == "1.0"
    assert claimed.step_instance_id == "step_00000001"
    assert claimed.direction.value == "forward"
    assert claimed.semantic_generation == 0
    assert claimed.command == claimed.envelope.command
    assert claimed.command_hash == claimed.envelope.command_hash
    assert claimed.available_at == RECORDED_AT
    assert store.runnable_count(SAGA_ID) == 0


def test_should_use_injected_claim_identity_factory(tmp_path: Path) -> None:
    # Given
    try:
        store = SQLiteKernelStore.initialize(
            tmp_path / "saga.db",
            claim_id_factory=FixedClaimIdFactory(),
            clock=FakeClock(RECORDED_AT),
        )
    except TypeError:
        pytest.fail("store must accept an injected claim ID factory")
    prepared = prepared_transition()
    store.create_saga(prepared.created)
    store.commit_transition(prepared.batch)

    # When
    claimed = store.claim_outbox("worker-a", timedelta(seconds=30))

    # Then
    assert claimed is not None
    assert claimed.claim_id == f"claim_{'c' * 32}"


def test_should_not_claim_command_before_available_time(tmp_path: Path) -> None:
    # Given
    clock = FakeClock(RECORDED_AT - timedelta(milliseconds=1))
    store = _populated(tmp_path / "saga.db", clock)

    # When
    claimed = store.claim_outbox("worker-a", timedelta(seconds=30))

    # Then
    assert claimed is None
    assert store.runnable_count(SAGA_ID) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE outbox_commands SET claim_generation = 1 WHERE saga_id = ?",
        "UPDATE outbox_commands SET delivery_attempt = 1 WHERE saga_id = ?",
        "UPDATE outbox_commands SET state = 'claimed', "
        "claim_id = 'claim_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', claim_owner = 'worker', "
        "claim_expires_at = '2026-09-07T12:00:00+00:00' WHERE saga_id = ?",
        "UPDATE outbox_commands SET state = 'claimed', claim_generation = 1, "
        "delivery_attempt = 1 WHERE saga_id = ?",
        "UPDATE outbox_commands SET state = 'claimed', claim_owner = 'worker', "
        "claim_expires_at = '2026-09-07T12:00:00+00:00', claim_generation = 1, "
        "delivery_attempt = 1 WHERE saga_id = ?",
        "UPDATE outbox_commands SET state = 'claimed', "
        "claim_id = 'claim_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
        "claim_expires_at = '2026-09-07T12:00:00+00:00', claim_generation = 1, "
        "delivery_attempt = 1 WHERE saga_id = ?",
    ],
)
def test_should_pair_claim_state_with_generation_and_attempt(
    tmp_path: Path, statement: str
) -> None:
    # Given
    path = tmp_path / "saga.db"
    _populated(path)

    # When / Then
    with (
        closing(sqlite3.connect(path)) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(statement, (SAGA_ID,))


def test_should_allow_only_one_concurrent_claim(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    _populated(path)
    barrier = Barrier(2)

    # When
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda owner: _race_claim(path, barrier, owner), ("a", "b")))

    # Then
    assert sum(result is not None for result in results) == 1


def _race_claim(path: Path, barrier: Barrier, owner: str) -> ClaimedCommand | None:
    store = SQLiteKernelStore.open(path, clock=FakeClock(RECORDED_AT))
    barrier.wait()
    return store.claim_outbox(owner, timedelta(seconds=30))


def test_should_reclaim_expired_claim_without_attempt_inflation(tmp_path: Path) -> None:
    # Given
    clock = FakeClock(RECORDED_AT)
    store = _populated(tmp_path / "saga.db", clock)
    first = store.claim_outbox("worker-a", timedelta(seconds=30))
    assert first is not None

    # When
    clock.advance(timedelta(seconds=31))
    second = store.claim_outbox("worker-b", timedelta(seconds=30))

    # Then
    assert second is not None
    assert second.claim_id != first.claim_id
    assert second.claim_generation == 2
    assert second.delivery_attempt == 1


def test_should_reject_stale_claim_after_owner_aba(tmp_path: Path) -> None:
    # Given
    clock = FakeClock(RECORDED_AT)
    store = _populated(tmp_path / "saga.db", clock)
    first = store.claim_outbox("worker", timedelta(seconds=1))
    clock.advance(timedelta(seconds=2))
    second = store.claim_outbox("worker", timedelta(seconds=30))
    assert first is not None and second is not None
    dispatched = store.start_dispatch(second, dispatch_batch(store.load_snapshot(SAGA_ID)))
    batch = outcome_batch(dispatched)

    # When / Then
    with pytest.raises(StoreConflict, match="claim"):
        store.complete_outbox(first, batch)
    store.complete_outbox(second, batch)


def test_should_reject_completion_without_transition_evidence(tmp_path: Path) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    claimed = store.claim_outbox("worker", timedelta(seconds=30))
    assert claimed is not None

    # When / Then
    with pytest.raises(TypeError):
        store.complete_outbox(claimed)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "lifecycle",
    ["dispatch_started", "dispatch_aborted_before_entry", "effect_outcome_recorded"],
)
def test_should_reject_dispatch_lifecycle_through_generic_commit(
    tmp_path: Path, lifecycle: str
) -> None:
    store = _populated(tmp_path / "saga.db")
    batch = _generic_lifecycle_batch(store, lifecycle)
    before = store.read_events(SAGA_ID)
    snapshot = store.load_snapshot(SAGA_ID)
    state = store.outbox_state("cmd_0000000000000001")

    with pytest.raises(StoreConflict, match="specialized"):
        store.commit_transition(batch)

    assert store.read_events(SAGA_ID) == before
    assert store.load_snapshot(SAGA_ID) == snapshot
    assert store.outbox_state("cmd_0000000000000001") is state


def test_should_reject_completion_without_outcome_event(tmp_path: Path) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    claimed = store.claim_outbox("worker", timedelta(seconds=30))
    assert claimed is not None
    dispatch = dispatch_batch(store.load_snapshot(SAGA_ID))

    # When / Then
    with pytest.raises(StoreConflict, match="outcome"):
        store.complete_outbox(claimed, dispatch)
    assert store.load_snapshot(SAGA_ID).seq == 3


@pytest.mark.parametrize(
    "field",
    ["tool", "step", "direction", "generation", "hash", "attempt"],
)
def test_should_bind_disposition_evidence_to_claimed_envelope(tmp_path: Path, field: str) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    claimed, dispatched = _claim_and_dispatch(store)
    mutated = _mutated_claim(claimed, field)
    outcome = outcome_batch(dispatched)

    # When / Then
    with pytest.raises(StoreConflict, match="claimed operation"):
        store.complete_outbox(mutated, outcome)
    assert store.load_snapshot(SAGA_ID) == dispatched


def _mutated_claim(claimed: ClaimedCommand, field: str) -> ClaimedCommand:
    if field == "attempt":
        metadata = claimed.claim.model_copy(
            update={"delivery_attempt": claimed.delivery_attempt + 1}
        )
        return claimed.model_copy(update={"claim": metadata})
    names = {
        "tool": ("tool_name", "other_tool"),
        "step": ("step_instance_id", "step_00000002"),
        "direction": ("direction", Direction.COMPENSATION),
        "generation": ("semantic_generation", 1),
        "hash": ("command_hash", "f" * 64),
    }
    name, value = names[field]
    envelope = claimed.envelope.model_copy(update={name: value})
    return claimed.model_copy(update={"envelope": envelope})


@pytest.mark.parametrize(("disposition", "unknown"), [("complete", True), ("park", False)])
def test_should_require_disposition_matching_outcome_semantics(
    tmp_path: Path, disposition: str, unknown: bool
) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    claimed, dispatched = _claim_and_dispatch(store)
    batch = outcome_batch(dispatched, unknown=unknown)

    # When / Then
    with pytest.raises(StoreConflict, match="disposition"):
        if disposition == "complete":
            store.complete_outbox(claimed, batch)
        else:
            store.park_outbox(claimed, batch)
    assert store.load_snapshot(SAGA_ID) == dispatched


def test_should_reject_completion_with_unrelated_operation_batch(tmp_path: Path) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    second = store.commit_transition(_second_intent(store.load_snapshot(SAGA_ID)))
    claimed = store.claim_outbox("worker", timedelta(seconds=30))
    assert claimed is not None
    other = store.claim_outbox("worker", timedelta(seconds=30))
    assert other is not None
    dispatched = store.start_dispatch(
        other, dispatch_batch(second, OTHER_OPERATION_ID, "txn_0000000000000005")
    )
    unrelated = outcome_batch(
        dispatched,
        operation_id=OTHER_OPERATION_ID,
        transition_id="txn_0000000000000006",
    )

    # When / Then
    with pytest.raises(StoreConflict, match="claimed operation"):
        store.complete_outbox(claimed, unrelated)
    assert store.outbox_state(claimed.command_id).value == "claimed"
    assert store.load_snapshot(SAGA_ID) == dispatched


def test_should_reject_claim_with_envelope_from_another_operation(tmp_path: Path) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    second = store.commit_transition(_second_intent(store.load_snapshot(SAGA_ID)))
    claimed = store.claim_outbox("worker", timedelta(seconds=30))
    assert claimed is not None
    other = store.claim_outbox("worker", timedelta(seconds=30))
    assert other is not None
    dispatched = store.start_dispatch(
        other, dispatch_batch(second, OTHER_OPERATION_ID, "txn_0000000000000005")
    )
    envelope = outbox_command(operation_id=OTHER_OPERATION_ID, command_id=claimed.command_id)
    forged = ClaimedCommand(envelope=envelope, claim=claimed.claim)
    outcome = outcome_batch(
        dispatched, operation_id=OTHER_OPERATION_ID, transition_id="txn_0000000000000006"
    )
    events = store.read_events(SAGA_ID)

    # When / Then
    with pytest.raises(StoreConflict, match="durable claim"):
        store.complete_outbox(forged, outcome)
    assert store.load_snapshot(SAGA_ID) == dispatched
    assert store.read_events(SAGA_ID) == events
    assert store.outbox_state(claimed.command_id).value == "claimed"
    assert store.outbox_state("cmd_0000000000000002").value == "claimed"
    with closing(sqlite3.connect(store.path)) as connection, connection:
        receipt = connection.execute(
            "SELECT 1 FROM transition_receipts WHERE transition_id = ?",
            (outcome.transition_id,),
        ).fetchone()
    assert receipt is None


def test_should_reject_expired_claim_without_partial_outcome(tmp_path: Path) -> None:
    # Given
    clock = FakeClock(RECORDED_AT)
    store = _populated(tmp_path / "saga.db", clock)
    claimed = store.claim_outbox("worker", timedelta(seconds=1))
    assert claimed is not None
    dispatched = store.start_dispatch(claimed, dispatch_batch(store.load_snapshot(SAGA_ID)))
    outcome = outcome_batch(dispatched)
    clock.advance(timedelta(seconds=2))

    # When / Then
    with pytest.raises(StoreConflict, match="expired"):
        store.complete_outbox(claimed, outcome)
    assert store.outbox_state(claimed.command_id).value == "claimed"
    assert store.load_snapshot(SAGA_ID) == dispatched


@pytest.mark.parametrize("disposition", ["completed", "parked"])
def test_should_atomically_finish_claim_with_exact_owner_and_fence(
    tmp_path: Path, disposition: str
) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    claimed, dispatched = _claim_and_dispatch(store)
    batch = outcome_batch(dispatched, unknown=disposition == "parked")

    # When
    if disposition == "completed":
        result = store.complete_outbox(claimed, batch)
    else:
        result = store.park_outbox(claimed, batch)

    # Then
    assert store.outbox_state(claimed.command_id).value == disposition
    assert result == batch.projection


def test_should_reject_claim_completion_after_saga_fence_changes(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    store = _populated(path)
    claimed, dispatched = _claim_and_dispatch(store)
    batch = outcome_batch(dispatched)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE sagas SET fence_token = 1, lease_owner = ?, lease_expires_at = ? "
            "WHERE saga_id = ?",
            ("new-worker", "2026-09-07T12:00:00+00:00", SAGA_ID),
        )

    # When / Then
    with pytest.raises(StoreConflict, match="fence"):
        store.complete_outbox(claimed, batch)


def test_should_roll_back_event_projection_and_completion_together(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    base = _populated(path)
    claimed, dispatched = _claim_and_dispatch(base)
    batch = outcome_batch(dispatched)
    store = SQLiteKernelStore.open(
        path,
        failpoint=RaisingFailpoint(StoreFailpoint.AFTER_OUTBOX_UPDATE),
        clock=FakeClock(RECORDED_AT),
    )

    # When / Then
    with pytest.raises(InjectedStoreFailure):
        store.complete_outbox(claimed, batch)
    assert store.load_snapshot(SAGA_ID) == dispatched
    assert store.outbox_state(claimed.command_id).value == "claimed"


def test_should_retry_uncertain_atomic_completion_idempotently(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    base = _populated(path)
    claimed, dispatched = _claim_and_dispatch(base)
    batch = outcome_batch(dispatched)
    store = SQLiteKernelStore.open(
        path,
        failpoint=RaisingFailpoint(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN),
        clock=FakeClock(RECORDED_AT),
    )

    # When
    with pytest.raises(InjectedStoreFailure):
        store.complete_outbox(claimed, batch)
    retried = store.complete_outbox(claimed, batch)

    # Then
    assert retried == batch.projection
    assert store.outbox_state(claimed.command_id).value == "completed"
    assert len(store.read_events(SAGA_ID)) == 5


def test_should_retry_committed_completion_after_fence_takeover(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    base = _populated(path)
    claimed, dispatched = _claim_and_dispatch(base)
    batch = outcome_batch(dispatched)
    store = SQLiteKernelStore.open(
        path,
        failpoint=RaisingFailpoint(StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN),
        clock=FakeClock(RECORDED_AT),
    )
    with pytest.raises(InjectedStoreFailure):
        store.complete_outbox(claimed, batch)
    with closing(sqlite3.connect(path)) as connection, connection:
        before_receipts = connection.execute("SELECT COUNT(*) FROM transition_receipts").fetchone()
        connection.execute(
            "UPDATE sagas SET fence_token = 1, lease_owner = ?, lease_expires_at = ? "
            "WHERE saga_id = ?",
            ("new-worker", "2099-09-07T12:00:00+00:00", SAGA_ID),
        )
    before_events = store.read_events(SAGA_ID)

    # When
    retried = store.complete_outbox(claimed, batch)

    # Then
    with closing(sqlite3.connect(path)) as connection, connection:
        after_receipts = connection.execute("SELECT COUNT(*) FROM transition_receipts").fetchone()
    assert retried == batch.projection
    assert store.read_events(SAGA_ID) == before_events
    assert after_receipts == before_receipts
    assert store.outbox_state(claimed.command_id).value == "completed"


def test_should_block_historical_claim_fence_mutation_and_preserve_retry(
    tmp_path: Path,
) -> None:
    # Given
    path = tmp_path / "saga.db"
    store, claimed, batch = _finalized_outbox(path)
    events = store.read_events(SAGA_ID)

    # When / Then
    with (
        closing(sqlite3.connect(path)) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute("UPDATE outbox_commands SET claim_fence_token = 1")
    assert store.complete_outbox(claimed, batch) == batch.projection
    assert store.read_events(SAGA_ID) == events


@pytest.mark.parametrize("parked", [False, True])
def test_should_reject_historical_claim_fence_without_disposition_evidence(
    tmp_path: Path, *, parked: bool
) -> None:
    # Given
    path = tmp_path / "saga.db"
    _finalized_outbox(path, parked=parked)
    _tamper_historical_claim_fence(path)

    # When / Then
    with pytest.raises(StoreCorruption, match="claim fence"):
        SQLiteKernelStore.open(path)


def test_should_reject_historical_claim_fence_during_backup(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    destination = tmp_path / "backup.db"
    store, _, _ = _finalized_outbox(path)
    _tamper_historical_claim_fence(path)

    # When / Then
    with pytest.raises(StoreCorruption, match="claim fence"):
        store.backup_to(destination)
    assert not destination.exists()


def test_should_count_runnable_commands_for_requested_saga_only(tmp_path: Path) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")

    # When / Then
    assert store.runnable_count(SAGA_ID) == 1
    assert store.runnable_count("saga_0000000000000002") == 0
