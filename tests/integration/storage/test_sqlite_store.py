from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

import agentic_saga.storage.sqlite as sqlite_storage
from agentic_saga.contracts.events import SagaCreated
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.kernel.ports import (
    InjectedStoreFailure,
    StaleFence,
    StoreConflict,
    StoreCorruption,
    StoreFailpoint,
    TransitionBatch,
)
from agentic_saga.kernel.reducer import InvalidTransition, SequenceGap, reduce_event
from agentic_saga.storage import SQLiteKernelStore
from tests.integration.storage.helpers import (
    OTHER_SAGA_ID,
    SAGA_ID,
    PreparedTransition,
    canonical_hash,
    effect_intent,
    prepared_transition,
    saga_created,
    saga_started,
)


class RaisingFailpoint:
    def __init__(self, target: StoreFailpoint) -> None:
        self._target = target

    def hit(self, point: StoreFailpoint) -> None:
        if point is self._target:
            raise InjectedStoreFailure(point)


def _initialized(path: Path, target: StoreFailpoint | None = None) -> SQLiteKernelStore:
    hook = None if target is None else RaisingFailpoint(target)
    return SQLiteKernelStore.initialize(path, failpoint=hook)


def _created_store(path: Path, prepared: PreparedTransition) -> SQLiteKernelStore:
    store = _initialized(path)
    store.create_saga(prepared.created)
    return store


def test_saga_created_fingerprint_round_trips_in_hashed_canonical_event(tmp_path: Path) -> None:
    # Given
    created = saga_created()
    store = _initialized(tmp_path / "saga.db")

    # When
    store.create_saga(created)
    events = store.read_events(created.saga_id)

    # Then read_events has verified the canonical bytes and ledger hash chain
    assert events == (created,)
    assert isinstance(events[0], SagaCreated)
    assert events[0].definition_fingerprint == "f" * 64


def test_schema_v2_rejects_created_event_without_definition_fingerprint(tmp_path: Path) -> None:
    # Given a valid v2 database whose creation bytes are missing the required identity
    created = saga_created()
    store = _initialized(tmp_path / "saga.db")
    store.create_saga(created)
    with closing(store._connection()) as connection, connection:
        row = connection.execute(
            "SELECT event_json FROM ledger_events WHERE event_id = ?", (created.event_id,)
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        del payload["definition_fingerprint"]
        connection.execute("DROP TRIGGER ledger_events_no_update")
        connection.execute(
            "UPDATE ledger_events SET event_json = ? WHERE event_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), created.event_id),
        )

    # When / Then
    with pytest.raises(StoreCorruption, match="stored ledger event is invalid"):
        store.read_events(created.saga_id)


@contextmanager
def _temporary_umask(value: int) -> Iterator[None]:
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


def _permission_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_should_atomically_append_event_projection_and_outbox(tmp_path: Path) -> None:
    # Given
    prepared = prepared_transition()
    store = _created_store(tmp_path / "saga.db", prepared)

    # When
    updated = store.commit_transition(prepared.batch)

    # Then
    assert updated.seq == 3
    assert [event.saga_seq for event in store.read_events(SAGA_ID)] == [1, 2, 3]
    assert store.runnable_count(SAGA_ID) == 1
    assert store.rebuild_and_verify(SAGA_ID) == updated


def test_should_configure_every_connection_for_durability(tmp_path: Path) -> None:
    # Given
    store = _initialized(tmp_path / "saga.db")

    # When
    settings = store.connection_settings()

    # Then
    assert settings.foreign_keys is True
    assert settings.journal_mode == "wal"
    assert settings.synchronous == 2
    assert settings.busy_timeout_ms == 5_000
    assert settings.schema_version == 2


def _configure_short_busy_timeout(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 1")


def test_should_translate_writer_contention_when_acquiring_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    prepared = prepared_transition()
    store = _created_store(tmp_path / "saga.db", prepared)
    monkeypatch.setattr(sqlite_storage, "_configure", _configure_short_busy_timeout)

    # When / Then
    with closing(sqlite3.connect(store.path, isolation_level=None)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreConflict, match="writer is busy; retry with bounded backoff"):
            store.acquire_lease(SAGA_ID, "worker-b", timedelta(seconds=30))


def test_should_create_database_and_sidecars_private_with_permissive_umask(
    tmp_path: Path,
) -> None:
    # Given
    path = tmp_path / "saga.db"

    # When
    with _temporary_umask(0), closing(_initialized(path)._connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SELECT COUNT(*) FROM sagas").fetchone()
        modes = tuple(_permission_bits(Path(f"{path}{suffix}")) for suffix in ("", "-wal", "-shm"))
        connection.rollback()

    # Then
    assert modes == (0o600, 0o600, 0o600)


def test_should_tighten_owned_database_permissions_before_open(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    _initialized(path)
    path.chmod(0o644)

    # When
    SQLiteKernelStore.open(path)

    # Then
    assert _permission_bits(path) == 0o600


def test_should_reject_initialize_through_dangling_symlink(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "target.db"
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)

    # When / Then
    with pytest.raises(StoreConflict):
        SQLiteKernelStore.initialize(alias)
    assert not target.exists()


def test_should_reject_open_through_database_symlink(tmp_path: Path) -> None:
    # Given
    target = tmp_path / "target.db"
    _initialized(target)
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)

    # When / Then
    with pytest.raises(StoreConflict):
        SQLiteKernelStore.open(alias)


def test_should_reject_preexisting_sqlite_sidecar_symlink(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    _initialized(path)
    victim = tmp_path / "victim"
    victim.write_bytes(b"unchanged")
    Path(f"{path}-wal").symlink_to(victim)

    # When / Then
    with pytest.raises(StoreConflict):
        SQLiteKernelStore.open(path)
    assert victim.read_bytes() == b"unchanged"


def test_should_tolerate_optional_sidecar_removed_during_secure_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "saga.db"
    _initialized(path)
    sidecar = Path(f"{path}-wal")
    sidecar.write_bytes(b"transient")
    real_open = os.open
    disappeared = False

    def disappear_once(
        file: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal disappeared
        if Path(os.fsdecode(file)) == sidecar and not disappeared:
            disappeared = True
            sidecar.unlink()
            raise FileNotFoundError(sidecar)
        if dir_fd is None:
            return real_open(file, flags, mode)
        return real_open(file, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", disappear_once)

    SQLiteKernelStore.open(path)

    assert disappeared is True


def test_should_reject_non_regular_database_leaf(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "pipe.db"
    os.mkfifo(path)

    # When / Then
    with pytest.raises(StoreConflict):
        SQLiteKernelStore.open(path)


def test_should_reject_database_creation_in_shared_writable_parent(tmp_path: Path) -> None:
    # Given
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o777)

    # When / Then
    with pytest.raises(StoreConflict):
        SQLiteKernelStore.initialize(parent / "saga.db")


def test_should_not_recreate_database_removed_after_secure_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    path = tmp_path / "saga.db"
    _initialized(path)
    real_connect = sqlite3.connect

    def remove_then_connect(
        database: str | Path,
        timeout: float = 5.0,
        isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = None,
        *,
        uri: bool = False,
    ) -> sqlite3.Connection:
        path.unlink()
        return real_connect(database, timeout=timeout, isolation_level=isolation_level, uri=uri)

    monkeypatch.setattr(sqlite3, "connect", remove_then_connect)

    # When / Then
    with pytest.raises(StoreCorruption):
        SQLiteKernelStore.open(path)
    assert not path.exists()


def test_should_enforce_foreign_keys_on_store_connections(tmp_path: Path) -> None:
    # Given
    store = _initialized(tmp_path / "saga.db")

    # When / Then
    with closing(store._connection()) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO ledger_events "
            "(event_id, saga_id, saga_seq, event_type, event_json, event_hash, prior_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "evt_0000000000000099",
                SAGA_ID,
                1,
                "saga_created",
                b"{}",
                "a" * 64,
                None,
            ),
        )


def test_should_reject_open_when_database_is_missing(tmp_path: Path) -> None:
    # Given / When / Then
    with pytest.raises(StoreConflict, match="does not exist"):
        SQLiteKernelStore.open(tmp_path / "missing.db")


def test_should_reject_initialize_over_existing_database(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    _initialized(path)

    # When / Then
    with pytest.raises(StoreConflict, match="already exists"):
        SQLiteKernelStore.initialize(path)


def test_should_reject_initialize_when_parent_is_missing(tmp_path: Path) -> None:
    # Given / When / Then
    with pytest.raises(StoreConflict, match="parent directory"):
        SQLiteKernelStore.initialize(tmp_path / "missing" / "saga.db")


@pytest.mark.parametrize("schema_version", [1, 99], ids=("pre-fingerprint", "unknown"))
def test_should_reject_open_when_schema_version_is_incompatible(
    tmp_path: Path, schema_version: int
) -> None:
    # Given
    path = tmp_path / "saga.db"
    _initialized(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {schema_version}")

    # When / Then
    with pytest.raises(StoreCorruption, match=f"schema version {schema_version}"):
        SQLiteKernelStore.open(path)


def test_should_create_saga_atomically_and_reject_duplicate(tmp_path: Path) -> None:
    # Given
    store = _initialized(tmp_path / "saga.db")

    # When
    snapshot = store.create_saga(saga_created())

    # Then
    assert snapshot.seq == 1
    with pytest.raises(StoreConflict, match="already exists"):
        store.create_saga(saga_created(event_id="evt_0000000000000009"))
    assert len(store.read_events(SAGA_ID)) == 1


def test_should_reject_invalid_first_event_without_writes(tmp_path: Path) -> None:
    # Given
    store = _initialized(tmp_path / "saga.db")

    # When / Then
    with pytest.raises(InvalidTransition, match="SagaCreated"):
        store.create_saga(saga_started())
    with pytest.raises(StoreConflict, match="not found"):
        store.load_snapshot(SAGA_ID)


def test_should_reject_nonbootstrap_fence_on_saga_creation(tmp_path: Path) -> None:
    # Given
    store = _initialized(tmp_path / "saga.db")
    event = saga_created().model_copy(update={"fence_token": 1})

    # When / Then
    with pytest.raises(StaleFence, match="bootstrap fence"):
        store.create_saga(event)
    with pytest.raises(StoreConflict, match="not found"):
        store.load_snapshot(SAGA_ID)


@pytest.mark.parametrize(
    ("expected_seq", "expected_fence", "error", "message"),
    [
        (0, 0, StoreConflict, "expected Saga sequence 0, found 1"),
        (1, 1, StaleFence, "expected fence 1, found 0"),
    ],
)
def test_should_reject_stale_compare_and_swap(
    tmp_path: Path,
    expected_seq: int,
    expected_fence: int,
    error: type[Exception],
    message: str,
) -> None:
    # Given
    prepared = prepared_transition()
    store = _created_store(tmp_path / "saga.db", prepared)
    batch = prepared.batch.model_copy(
        update={"expected_seq": expected_seq, "expected_fence_token": expected_fence}
    )

    # When / Then
    with pytest.raises(error, match=message):
        store.commit_transition(batch)
    assert store.load_snapshot(SAGA_ID).seq == 1


def test_should_bind_positive_fence_to_every_event(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    prepared = prepared_transition()
    store = _created_store(path, prepared)
    expiry = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE sagas SET fence_token = 1, lease_owner = ?, lease_expires_at = ? "
            "WHERE saga_id = ?",
            ("worker-a", expiry, SAGA_ID),
        )
    batch = prepared.batch.model_copy(update={"expected_fence_token": 1, "lease_owner": "worker-a"})

    # When / Then
    with pytest.raises(StaleFence, match="event fence"):
        store.commit_transition(batch)
    assert store.load_snapshot(SAGA_ID).seq == 1


@pytest.mark.parametrize(
    ("fence", "owner", "expiry"),
    [
        (0, "worker-a", "2026-09-07T12:00:00+00:00"),
        (1, "worker-a", None),
        (1, None, "2026-09-07T12:00:00+00:00"),
    ],
)
def test_should_pair_persisted_fence_with_complete_lease_metadata(
    tmp_path: Path, fence: int, owner: str | None, expiry: str | None
) -> None:
    # Given
    path = tmp_path / "saga.db"
    store = _initialized(path)
    store.create_saga(saga_created())

    # When / Then
    with (
        closing(sqlite3.connect(path)) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            "UPDATE sagas SET fence_token = ?, lease_owner = ?, lease_expires_at = ? "
            "WHERE saga_id = ?",
            (fence, owner, expiry, SAGA_ID),
        )


def test_should_preserve_positive_fence_after_lease_release(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    store = _initialized(path)
    store.create_saga(saga_created())

    # When
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE sagas SET fence_token = 1, lease_owner = NULL, lease_expires_at = NULL "
            "WHERE saga_id = ?",
            (SAGA_ID,),
        )

    # Then
    assert store.current_fence(SAGA_ID) == 1


@pytest.mark.parametrize("failure", ["owner", "expiry"])
def test_should_reject_noncurrent_positive_fence_lease(tmp_path: Path, failure: str) -> None:
    # Given
    path = tmp_path / "saga.db"
    prepared = prepared_transition()
    store = _created_store(path, prepared)
    expiry_delta = timedelta(hours=-1 if failure == "expiry" else 1)
    expiry = (datetime.now(tz=UTC) + expiry_delta).isoformat()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE sagas SET fence_token = 1, lease_owner = ?, lease_expires_at = ? "
            "WHERE saga_id = ?",
            ("worker-a", expiry, SAGA_ID),
        )
    owner = "worker-b" if failure == "owner" else "worker-a"
    batch = prepared.batch.model_copy(update={"expected_fence_token": 1, "lease_owner": owner})

    # When / Then
    with pytest.raises(StaleFence, match=r"lease owner|expired"):
        store.commit_transition(batch)


@pytest.mark.parametrize("mutation", ["gap", "wrong_saga", "wrong_definition", "projection"])
def test_should_reject_invalid_ordered_batch_without_writes(tmp_path: Path, mutation: str) -> None:
    # Given
    prepared = prepared_transition()
    store = _created_store(tmp_path / "saga.db", prepared)

    # When / Then
    with pytest.raises((InvalidTransition, SequenceGap, StoreConflict)):
        store.commit_transition(_mutated_batch(prepared, mutation))
    assert store.load_snapshot(SAGA_ID).seq == 1
    assert store.runnable_count(SAGA_ID) == 0


def _mutated_batch(prepared: PreparedTransition, mutation: str) -> TransitionBatch:
    if mutation == "gap":
        return prepared.batch.model_copy(update={"events": (effect_intent(seq=4),)})
    if mutation == "wrong_saga":
        return prepared.batch.model_copy(update={"events": (saga_started(OTHER_SAGA_ID),)})
    if mutation == "wrong_definition":
        event = saga_started().model_copy(update={"definition_version": "other-v1"})
        return prepared.batch.model_copy(update={"events": (event,)})
    projection = prepared.projection.model_copy(update={"status": SagaStatus.CREATED})
    return prepared.batch.model_copy(update={"projection": projection})


@pytest.mark.parametrize(
    "target",
    [
        StoreFailpoint.BEFORE_EVENT_INSERT,
        StoreFailpoint.AFTER_EVENT_INSERT,
        StoreFailpoint.BEFORE_OUTBOX_INSERT,
        StoreFailpoint.AFTER_OUTBOX_INSERT,
        StoreFailpoint.BEFORE_PROJECTION_UPDATE,
        StoreFailpoint.AFTER_PROJECTION_UPDATE,
        StoreFailpoint.BEFORE_RECEIPT_INSERT,
        StoreFailpoint.AFTER_RECEIPT_INSERT,
        StoreFailpoint.BEFORE_COMMIT,
    ],
)
def test_should_roll_back_every_transition_failpoint(
    tmp_path: Path, target: StoreFailpoint
) -> None:
    # Given
    prepared = prepared_transition()
    store = _initialized(tmp_path / "saga.db", target)
    store.create_saga(prepared.created)

    # When / Then
    with pytest.raises(InjectedStoreFailure):
        store.commit_transition(prepared.batch)
    assert store.load_snapshot(SAGA_ID).seq == 1
    assert store.runnable_count(SAGA_ID) == 0


def test_should_return_committed_receipt_when_retry_follows_uncertain_return(
    tmp_path: Path,
) -> None:
    # Given
    prepared = prepared_transition()
    path = tmp_path / "saga.db"
    store = _initialized(path, StoreFailpoint.AFTER_COMMIT_BEFORE_RETURN)
    store.create_saga(prepared.created)

    # When
    with pytest.raises(InjectedStoreFailure):
        store.commit_transition(prepared.batch)
    retried = SQLiteKernelStore.open(path).commit_transition(prepared.batch)

    # Then
    assert retried == prepared.projection
    assert len(store.read_events(SAGA_ID)) == 3
    assert store.runnable_count(SAGA_ID) == 1


def test_should_reject_transition_id_reuse_with_different_payload(tmp_path: Path) -> None:
    # Given
    prepared = prepared_transition()
    store = _created_store(tmp_path / "saga.db", prepared)
    store.commit_transition(prepared.batch)
    changed = prepared.batch.model_copy(update={"expected_fence_token": 1})

    # When / Then
    with pytest.raises(StoreConflict, match="transition identity"):
        store.commit_transition(changed)


def test_should_roll_back_duplicate_command_constraint(tmp_path: Path) -> None:
    # Given
    prepared = prepared_transition()
    store = _created_store(tmp_path / "saga.db", prepared)
    store.commit_transition(prepared.batch)
    batch = _duplicate_command_batch(store, prepared)

    # When / Then
    with pytest.raises(StoreConflict, match="constraint"):
        store.commit_transition(batch)
    assert store.load_snapshot(SAGA_ID) == prepared.projection


def test_should_reject_effect_intent_without_exactly_one_outbox_command(
    tmp_path: Path,
) -> None:
    # Given
    prepared = prepared_transition()
    store = _created_store(tmp_path / "saga.db", prepared)
    missing = prepared.batch.model_copy(update={"outbox_commands": ()})

    # When / Then
    with pytest.raises(StoreConflict, match=r"effect intent.*outbox"):
        store.commit_transition(missing)
    assert store.load_snapshot(SAGA_ID).seq == 1


@pytest.mark.parametrize("mutation", ["hash", "redaction", "tool"])
def test_should_reject_non_executable_outbox_envelope(tmp_path: Path, mutation: str) -> None:
    # Given
    prepared = prepared_transition()
    store = _created_store(tmp_path / "saga.db", prepared)

    # When / Then
    with pytest.raises(StoreConflict, match="outbox command"):
        store.commit_transition(_non_executable_batch(prepared, mutation))
    assert store.load_snapshot(SAGA_ID).seq == 1


def _non_executable_batch(prepared: PreparedTransition, mutation: str) -> TransitionBatch:
    if mutation == "tool":
        command = prepared.command.model_copy(update={"tool_name": "other"})
        return prepared.batch.model_copy(update={"outbox_commands": (command,)})
    value = {"credential_ref": "[REDACTED]"} if mutation == "redaction" else {"x": 1}
    digest = "f" * 64 if mutation == "hash" else canonical_hash(value)
    event = prepared.intent.model_copy(update={"redacted_command": value, "command_hash": digest})
    initial = reduce_event(reduce_event(None, prepared.created), prepared.started)
    command = prepared.command.model_copy(update={"command": value, "command_hash": digest})
    return prepared.batch.model_copy(
        update={
            "events": (prepared.started, event),
            "projection": reduce_event(initial, event),
            "outbox_commands": (command,),
        }
    )


def test_should_preserve_explicit_command_schema_version(tmp_path: Path) -> None:
    # Given
    prepared = prepared_transition()
    store = _created_store(tmp_path / "saga.db", prepared)
    command = prepared.command.model_copy(
        update={"definition_version": "charge-definition-v2", "command_schema_version": "charge-v2"}
    )

    # When
    store.commit_transition(prepared.batch.model_copy(update={"outbox_commands": (command,)}))
    claimed = store.claim_outbox("worker", timedelta(minutes=1))

    # Then
    assert claimed is not None
    assert claimed.definition_version == "charge-definition-v2"
    assert claimed.command_schema_version == "charge-v2"


@pytest.mark.parametrize(
    ("owner", "duration"),
    [
        ("", timedelta(seconds=1)),
        ("worker", timedelta(0)),
    ],
)
def test_should_reject_invalid_claim_boundaries(
    tmp_path: Path, owner: str, duration: timedelta
) -> None:
    # Given
    store = _initialized(tmp_path / "saga.db")

    # When / Then
    with pytest.raises(ValueError):
        store.claim_outbox(owner, duration)


def _duplicate_command_batch(
    store: SQLiteKernelStore, prepared: PreparedTransition
) -> TransitionBatch:
    event = effect_intent(operation_id=f"op_{'b' * 64}", seq=4)
    projection = reduce_event(store.load_snapshot(SAGA_ID), event)
    duplicate = prepared.command.model_copy(update={"operation_id": event.operation_id})
    return TransitionBatch(
        transition_id="txn_0000000000000002",
        saga_id=SAGA_ID,
        expected_seq=3,
        expected_fence_token=0,
        events=(event,),
        projection=projection,
        outbox_commands=(duplicate,),
    )
