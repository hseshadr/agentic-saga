from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Thread

import pytest

from agentic_saga.contracts.clock import Clock, FakeClock
from agentic_saga.contracts.events import SagaCreated, SagaStarted
from agentic_saga.execution.leases import LeaseService
from agentic_saga.kernel.ports import (
    InjectedStoreFailure,
    Lease,
    LeaseLost,
    LeaseUnavailable,
    StaleFence,
    StoreConflict,
    StoreCorruption,
    StoreFailpoint,
    TransitionBatch,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.storage import SQLiteKernelStore

SAGA_ID = "saga_0000000000000001"
RECORDED_AT = datetime(2026, 9, 6, 12, tzinfo=UTC)


@dataclass(frozen=True)
class _LeaseFixture:
    store: SQLiteKernelStore
    clock: FakeClock
    leases: LeaseService


class _RaisingFailpoint:
    def __init__(self, target: StoreFailpoint) -> None:
        self._target = target

    def hit(self, point: StoreFailpoint) -> None:
        if point is self._target:
            raise InjectedStoreFailure(point)


@dataclass(frozen=True)
class _StaticClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _fixture(
    path: Path,
    failpoint: StoreFailpoint | None = None,
    initial: datetime = RECORDED_AT,
) -> _LeaseFixture:
    clock = FakeClock(initial)
    hook = None if failpoint is None else _RaisingFailpoint(failpoint)
    store = SQLiteKernelStore.initialize(path, clock=clock, failpoint=hook)
    store.create_saga(_created())
    return _LeaseFixture(store, clock, LeaseService(store))


def _store_with_clock(path: Path, clock: Clock) -> SQLiteKernelStore:
    store = SQLiteKernelStore.initialize(path, clock=clock)
    store.create_saga(_created())
    return store


def _created() -> SagaCreated:
    return SagaCreated(
        event_id="evt_0000000000000001",
        saga_id=SAGA_ID,
        saga_seq=1,
        definition_version="checkout-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000000001",
        recorded_at=RECORDED_AT,
        definition_name="checkout",
        definition_fingerprint="f" * 64,
        redacted_goal={"order_id": "order-1"},
    )


def _transition(
    store: SQLiteKernelStore, lease: Lease, transition_id: str = "txn_0000000000000001"
) -> TransitionBatch:
    before = store.load_snapshot(SAGA_ID)
    event = SagaStarted(
        event_id="evt_0000000000000002",
        saga_id=SAGA_ID,
        saga_seq=2,
        definition_version="checkout-v1",
        fence_token=lease.fence_token,
        actor="kernel",
        trace_id="trace_0000000000000001",
        recorded_at=RECORDED_AT + timedelta(seconds=1),
    )
    return TransitionBatch(
        transition_id=transition_id,
        saga_id=SAGA_ID,
        expected_seq=before.seq,
        expected_fence_token=lease.fence_token,
        lease_owner=lease.owner,
        events=(event,),
        projection=reduce_event(before, event),
    )


def test_should_renew_same_owner_without_changing_fence(tmp_path: Path) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db")
    first = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    fixture.clock.advance(timedelta(seconds=10))

    # When
    renewed = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))

    # Then
    assert first.fence_token == 1
    assert renewed.fence_token == first.fence_token
    assert renewed.expires_at == RECORDED_AT + timedelta(seconds=40)


def test_should_reject_lease_expiry_outside_utc() -> None:
    # Given
    non_utc = datetime(2026, 9, 6, 13, tzinfo=timezone(timedelta(hours=1)))

    # When / Then
    with pytest.raises(ValueError, match="expires_at must use UTC"):
        Lease(
            saga_id=SAGA_ID,
            owner="worker-a",
            fence_token=1,
            expires_at=non_utc,
        )


def test_should_keep_existing_expiry_when_same_owner_reacquires_with_short_duration(
    tmp_path: Path,
) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db")
    first = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    fixture.clock.advance(timedelta(seconds=10))

    # When
    renewed = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=5))

    # Then
    assert renewed.fence_token == first.fence_token
    assert renewed.expires_at == first.expires_at


def test_should_allow_exactly_one_distinct_owner_to_acquire_simultaneously(tmp_path: Path) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db")
    barrier = Barrier(2)
    outcomes: list[Lease | LeaseUnavailable] = []

    def acquire(owner: str) -> None:
        barrier.wait()
        try:
            outcomes.append(fixture.leases.acquire(SAGA_ID, owner, timedelta(seconds=30)))
        except LeaseUnavailable as error:
            outcomes.append(error)

    # When
    workers = (
        Thread(target=acquire, args=("worker-a",)),
        Thread(target=acquire, args=("worker-b",)),
    )
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    # Then
    assert sum(isinstance(outcome, Lease) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, LeaseUnavailable) for outcome in outcomes) == 1


def test_should_fence_stale_owner_after_exact_expiry(tmp_path: Path) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db")
    first = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    fixture.clock.advance(timedelta(seconds=30))
    second = fixture.leases.acquire(SAGA_ID, "worker-b", timedelta(seconds=30))

    # When / Then
    assert second.fence_token > first.fence_token
    with pytest.raises(LeaseLost):
        fixture.leases.renew(first, timedelta(seconds=30))
    with pytest.raises(LeaseLost):
        fixture.leases.release(first)
    with pytest.raises(StaleFence):
        fixture.store.commit_transition(_transition(fixture.store, first))


def test_should_use_the_store_clock_to_reject_transition_at_exact_expiry(tmp_path: Path) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db", initial=datetime(2099, 1, 1, tzinfo=UTC))
    lease = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    fixture.clock.advance(timedelta(seconds=30))

    # When / Then
    with pytest.raises(StaleFence, match="expired"):
        fixture.store.commit_transition(_transition(fixture.store, lease))


def test_should_return_exact_durable_receipt_after_takeover_but_reject_new_stale_transition(
    tmp_path: Path,
) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db")
    first = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    batch = _transition(fixture.store, first)
    committed = fixture.store.commit_transition(batch)
    fixture.clock.advance(timedelta(seconds=30))
    fixture.leases.acquire(SAGA_ID, "worker-b", timedelta(seconds=30))

    # When / Then
    events_before_retry = len(fixture.store.read_events(SAGA_ID))
    assert fixture.store.commit_transition(batch) == committed
    assert len(fixture.store.read_events(SAGA_ID)) == events_before_retry
    changed_event = batch.events[0].model_copy(update={"actor": "other-worker"})
    changed_batch = batch.model_copy(update={"events": (changed_event,)})
    with pytest.raises(StoreConflict, match="transition identity"):
        fixture.store.commit_transition(changed_batch)
    with pytest.raises(StaleFence):
        fixture.store.commit_transition(
            batch.model_copy(update={"transition_id": "txn_0000000000000002"})
        )


def test_should_not_revive_expired_lease_on_renewal(tmp_path: Path) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db")
    lease = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    fixture.clock.advance(timedelta(seconds=30))

    # When / Then
    with pytest.raises(LeaseLost):
        fixture.leases.renew(lease, timedelta(seconds=30))
    replacement = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    assert replacement.fence_token > lease.fence_token


def test_should_mint_new_fence_after_release_to_prevent_aba(tmp_path: Path) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db")
    first = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    fixture.leases.release(first)

    # When
    second = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))

    # Then
    assert second.fence_token > first.fence_token


def test_should_preserve_active_lease_and_fence_when_store_reopens(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    fixture = _fixture(path)
    lease = fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))

    # When
    reopened = SQLiteKernelStore.open(path, clock=fixture.clock)

    # Then
    assert (
        LeaseService(reopened).renew(lease, timedelta(seconds=30)).fence_token == lease.fence_token
    )


def test_should_roll_back_all_lease_fields_when_write_failpoint_fires(tmp_path: Path) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db", StoreFailpoint.AFTER_LEASE_UPDATE)

    # When / Then
    with pytest.raises(InjectedStoreFailure):
        fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    assert fixture.store.current_fence(SAGA_ID) == 0
    assert fixture.store.lease_state(SAGA_ID) is None


def test_should_reject_fence_decrement_even_when_sql_bypasses_service(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    fixture = _fixture(path)
    fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))

    # When / Then
    with (
        closing(sqlite3.connect(path)) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError, match="fence"),
    ):
        connection.execute("UPDATE sagas SET fence_token = 0 WHERE saga_id = ?", (SAGA_ID,))


def test_should_reject_fence_token_overflow_without_mutating_lease(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    fixture = _fixture(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE sagas SET fence_token = ? WHERE saga_id = ?",
            (9_223_372_036_854_775_807, SAGA_ID),
        )

    # When / Then
    with pytest.raises(LeaseUnavailable, match="exhausted"):
        fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    assert fixture.store.current_fence(SAGA_ID) == 9_223_372_036_854_775_807
    assert fixture.store.lease_state(SAGA_ID) is None


def test_should_reject_unrepresentable_or_overlong_lease_duration(tmp_path: Path) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db")

    # When / Then
    with pytest.raises(ValueError, match="millisecond"):
        fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(microseconds=1))
    with pytest.raises(ValueError, match="bounded"):
        fixture.leases.acquire(SAGA_ID, "worker-a", timedelta(days=2))


@pytest.mark.parametrize("owner", ("", "x" * 201))
def test_should_reject_invalid_owner_without_writing_lease(tmp_path: Path, owner: str) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db")

    # When / Then
    with pytest.raises(ValueError, match="owner"):
        fixture.leases.acquire(SAGA_ID, owner, timedelta(seconds=30))
    assert fixture.store.current_fence(SAGA_ID) == 0
    assert fixture.store.lease_state(SAGA_ID) is None


def test_should_reject_unknown_saga_acquisition_with_typed_store_conflict(tmp_path: Path) -> None:
    # Given
    fixture = _fixture(tmp_path / "saga.db")
    unknown_saga = "saga_0000000000000002"

    # When / Then
    with pytest.raises(StoreConflict, match="Saga not found"):
        fixture.leases.acquire(unknown_saga, "worker-a", timedelta(seconds=30))
    with pytest.raises(StoreConflict, match="Saga not found"):
        fixture.store.lease_state(unknown_saga)
    assert fixture.store.current_fence(SAGA_ID) == 0
    assert fixture.store.lease_state(SAGA_ID) is None


@pytest.mark.parametrize(
    ("clock", "message"),
    (
        (_StaticClock(datetime(2026, 9, 6, 12, tzinfo=timezone(timedelta(hours=1)))), "UTC"),
        (_StaticClock(RECORDED_AT + timedelta(microseconds=1)), "millisecond"),
    ),
)
def test_should_fail_closed_for_invalid_injected_store_clock(
    tmp_path: Path, clock: Clock, message: str
) -> None:
    # Given
    store = _store_with_clock(tmp_path / "saga.db", clock)

    # When / Then
    with pytest.raises(StoreCorruption, match=message):
        LeaseService(store).acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    assert store.current_fence(SAGA_ID) == 0
    assert store.lease_state(SAGA_ID) is None
