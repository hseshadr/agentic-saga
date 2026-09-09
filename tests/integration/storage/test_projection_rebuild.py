from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest

from agentic_saga.contracts.events import RecoveryPlanRequired
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.kernel.ports import StoreCorruption, TransitionBatch
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.integration.storage.helpers import (
    SAGA_ID,
    prepared_transition,
    saga_created,
    saga_started,
)

_DOMAIN = b"agentic-saga-ledger-v1"


@dataclass(frozen=True)
class LedgerProof:
    saga_seq: int
    event_bytes: bytes
    event_hash: str
    prior_hash: str | None


def _length_delimited(value: bytes) -> bytes:
    return len(value).to_bytes(8, "big") + value


def _event_hash(seq: int, prior_hash: str | None, event_bytes: bytes) -> str:
    parts = (_DOMAIN, SAGA_ID.encode(), str(seq).encode(), (prior_hash or "").encode(), event_bytes)
    return sha256(b"".join(_length_delimited(part) for part in parts)).hexdigest()


def _proofs(path: Path) -> tuple[LedgerProof, ...]:
    with closing(sqlite3.connect(path)) as connection, connection:
        rows = connection.execute(
            "SELECT saga_seq, event_json, event_hash, prior_hash "
            "FROM ledger_events WHERE saga_id = ? ORDER BY saga_seq",
            (SAGA_ID,),
        ).fetchall()
    return tuple(LedgerProof(int(row[0]), bytes(row[1]), str(row[2]), row[3]) for row in rows)


def _populated(path: Path) -> SQLiteKernelStore:
    prepared = prepared_transition()
    store = SQLiteKernelStore.initialize(path)
    store.create_saga(prepared.created)
    store.commit_transition(prepared.batch)
    return store


def test_should_hash_exact_canonical_event_bytes_and_prior_hash(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    _populated(path)

    # When
    proofs = _proofs(path)

    # Then
    assert [proof.prior_hash for proof in proofs] == [
        None,
        proofs[0].event_hash,
        proofs[1].event_hash,
    ]
    assert [proof.event_hash for proof in proofs] == [
        _event_hash(proof.saga_seq, proof.prior_hash, proof.event_bytes) for proof in proofs
    ]
    assert all(
        proof.event_bytes
        == json.dumps(
            json.loads(proof.event_bytes),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        for proof in proofs
    )


def test_should_detect_tampered_event_hash_chain(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    store = _populated(path)
    proof = _proofs(path)[1]
    _tamper_event(path, proof.event_bytes.replace(b'"actor":"kernel"', b'"actor":"attacker"'))

    # When / Then
    with pytest.raises(StoreCorruption, match="hash"):
        store.rebuild_and_verify(SAGA_ID)


def test_should_detect_tampered_prior_hash(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    store = _populated(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER ledger_events_no_update")
        connection.execute(
            "UPDATE ledger_events SET prior_hash = ? WHERE saga_id = ? AND saga_seq = 3",
            ("f" * 64, SAGA_ID),
        )

    # When / Then
    with pytest.raises(StoreCorruption, match="prior hash"):
        store.rebuild_and_verify(SAGA_ID)


def _tamper_event(path: Path, payload: bytes) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER ledger_events_no_update")
        connection.execute(
            "UPDATE ledger_events SET event_json = ? WHERE saga_id = ? AND saga_seq = 2",
            (payload, SAGA_ID),
        )


def test_should_reject_noncanonical_event_bytes_even_with_matching_hash(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    store = _populated(path)
    proof = _proofs(path)[1]
    noncanonical = proof.event_bytes.replace(b'"actor":"kernel"', b'"actor": "kernel"')
    digest = _event_hash(proof.saga_seq, proof.prior_hash, noncanonical)
    _replace_event_bytes_and_hash(path, noncanonical, digest)

    # When / Then
    with pytest.raises(StoreCorruption, match="canonical"):
        store.rebuild_and_verify(SAGA_ID)


def _replace_event_bytes_and_hash(path: Path, payload: bytes, digest: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER ledger_events_no_update")
        connection.execute(
            "UPDATE ledger_events SET event_json = ?, event_hash = ? "
            "WHERE saga_id = ? AND saga_seq = 2",
            (payload, digest, SAGA_ID),
        )


def test_should_detect_redundant_row_and_payload_mismatch(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    store = _populated(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER ledger_events_no_update")
        connection.execute(
            "UPDATE ledger_events SET event_type = ? WHERE saga_id = ? AND saga_seq = 2",
            ("human_required", SAGA_ID),
        )

    # When / Then
    with pytest.raises(StoreCorruption, match="row does not match"):
        store.rebuild_and_verify(SAGA_ID)


def test_should_detect_projection_that_does_not_match_replay(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    store = _populated(path)
    snapshot = store.load_snapshot(SAGA_ID).model_copy(update={"status": SagaStatus.CREATED})
    encoded = json.dumps(
        snapshot.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE sagas SET projection_json = ?, status = ? WHERE saga_id = ?",
            (encoded, SagaStatus.CREATED.value, SAGA_ID),
        )

    # When / Then
    with pytest.raises(StoreCorruption, match="projection does not match"):
        store.rebuild_and_verify(SAGA_ID)


def test_should_refuse_to_extend_tampered_current_projection(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    store = SQLiteKernelStore.initialize(path)
    current = store.create_saga(saga_created())
    tampered = current.model_copy(update={"pending_approval": True})
    encoded = json.dumps(
        tampered.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE sagas SET projection_json = ? WHERE saga_id = ?", (encoded, SAGA_ID)
        )
    event = saga_started()
    batch = TransitionBatch(
        transition_id="txn_0000000000000009",
        saga_id=SAGA_ID,
        expected_seq=1,
        expected_fence_token=0,
        events=(event,),
        projection=reduce_event(tampered, event),
    )

    # When / Then
    with pytest.raises(StoreCorruption, match="current projection"):
        store.commit_transition(batch)


def test_should_verify_one_stable_snapshot_during_healthy_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = tmp_path / "saga.db"
    reader = _populated(path)
    writer = SQLiteKernelStore.open(path)
    expected = reader.load_snapshot(SAGA_ID)
    barrier = Barrier(2)
    original = reader._load_saga_row

    def interleaved_projection(connection: sqlite3.Connection, saga_id: str) -> object:
        barrier.wait()
        barrier.wait()
        return original(connection, saga_id)

    monkeypatch.setattr(reader, "_load_saga_row", interleaved_projection)

    # When
    with ThreadPoolExecutor(max_workers=1) as executor:
        committed = executor.submit(_commit_at_barrier, writer, barrier)
        rebuilt = reader.rebuild_and_verify(SAGA_ID)

    # Then
    assert rebuilt == expected
    assert committed.result().seq == expected.seq + 1


def _commit_at_barrier(store: SQLiteKernelStore, barrier: Barrier) -> SagaSnapshot:
    barrier.wait()
    snapshot = store.load_snapshot(SAGA_ID)
    updated = store.commit_transition(_healthy_batch(snapshot))
    barrier.wait()
    return updated


def _healthy_batch(snapshot: SagaSnapshot) -> TransitionBatch:
    event = RecoveryPlanRequired(
        event_id="evt_0000000000000004",
        saga_id=SAGA_ID,
        saga_seq=snapshot.seq + 1,
        definition_version="checkout-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000000001",
        recorded_at=prepared_transition().created.recorded_at,
        reason_code="healthy_concurrent_commit",
    )
    return TransitionBatch(
        transition_id="txn_0000000000000002",
        saga_id=SAGA_ID,
        expected_seq=snapshot.seq,
        expected_fence_token=0,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )
