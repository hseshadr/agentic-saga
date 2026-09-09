from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path

import pytest

import agentic_saga.storage.sqlite as sqlite_storage
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.kernel.ports import StoreConflict, StoreCorruption, TransitionBatch
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.storage import SQLiteKernelStore
from tests.integration.storage.helpers import SAGA_ID, prepared_transition

_RECEIPT_UPDATE_TRIGGER = """CREATE TRIGGER transition_receipts_no_update
BEFORE UPDATE ON transition_receipts
BEGIN
  SELECT RAISE(ABORT, 'transition_receipts is append-only');
END"""
_RECEIPT_DELETE_TRIGGER = """CREATE TRIGGER transition_receipts_no_delete
BEFORE DELETE ON transition_receipts
BEGIN
  SELECT RAISE(ABORT, 'transition_receipts is append-only');
END"""


def _populated(path: Path) -> SQLiteKernelStore:
    prepared = prepared_transition()
    store = SQLiteKernelStore.initialize(path)
    store.create_saga(prepared.created)
    store.commit_transition(prepared.batch)
    return store


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _permission_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _install_backup_race(monkeypatch: pytest.MonkeyPatch, action: Callable[[], object]) -> None:
    original = SQLiteKernelStore._backup_into

    def raced(store: SQLiteKernelStore, temporary: Path) -> tuple[object, ...]:
        marks = original(store, temporary)
        action()
        return marks

    monkeypatch.setattr(SQLiteKernelStore, "_backup_into", raced)


def _active_wal_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
    connection.execute("INSERT INTO sentinel VALUES ('original')")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("UPDATE sentinel SET value = 'in-flight'")
    return connection


@contextmanager
def _mutable_receipts(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER transition_receipts_no_update")
        connection.execute("DROP TRIGGER transition_receipts_no_delete")
        try:
            yield connection
        finally:
            connection.execute(_RECEIPT_UPDATE_TRIGGER)
            connection.execute(_RECEIPT_DELETE_TRIGGER)


def _tamper_receipt_boundary(path: Path) -> None:
    with _mutable_receipts(path) as connection:
        connection.execute(
            "UPDATE transition_receipts SET expected_seq = 2, resulting_seq = 3, "
            "payload_digest = ?",
            ("f" * 64,),
        )


def _tamper_receipt_projection(path: Path) -> None:
    prepared = prepared_transition()
    projection = prepared.projection.model_copy(update={"status": SagaStatus.CREATED})
    batch = prepared.batch.model_copy(update={"projection": projection})
    transition = _canonical_bytes(batch.model_dump(mode="json"))
    with _mutable_receipts(path) as connection:
        connection.execute(
            "UPDATE transition_receipts SET payload_digest = ?, projection_json = ?, "
            "transition_json = ?",
            (
                sha256(transition).hexdigest(),
                _canonical_bytes(projection.model_dump(mode="json")),
                transition,
            ),
        )


def _tamper_receipt_digest(path: Path) -> None:
    with _mutable_receipts(path) as connection:
        connection.execute("UPDATE transition_receipts SET payload_digest = ?", ("f" * 64,))


def _receipt_values(batch: TransitionBatch) -> tuple[object, ...]:
    transition = _canonical_bytes(batch.model_dump(mode="json"))
    projection = _canonical_bytes(batch.projection.model_dump(mode="json"))
    return (
        batch.transition_id,
        batch.saga_id,
        batch.expected_seq,
        batch.projection.seq,
        sha256(transition).hexdigest(),
        projection,
        transition,
    )


def _insert_receipt(connection: sqlite3.Connection, batch: TransitionBatch) -> None:
    connection.execute(
        "INSERT INTO transition_receipts "
        "(transition_id, saga_id, expected_seq, resulting_seq, payload_digest, projection_json, "
        "transition_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        _receipt_values(batch),
    )


def _started_only_batch() -> TransitionBatch:
    prepared = prepared_transition()
    created = reduce_event(None, prepared.created)
    projection = reduce_event(created, prepared.started)
    return TransitionBatch(
        transition_id="txn_0000000000000097",
        saga_id=SAGA_ID,
        expected_seq=1,
        expected_fence_token=0,
        events=(prepared.started,),
        projection=projection,
    )


def _overlapping_batch() -> TransitionBatch:
    prepared = prepared_transition()
    return TransitionBatch(
        transition_id="txn_0000000000000098",
        saga_id=SAGA_ID,
        expected_seq=2,
        expected_fence_token=0,
        events=(prepared.intent,),
        projection=prepared.projection,
        outbox_commands=(prepared.command,),
    )


def _replace_receipts(path: Path, batch: TransitionBatch | None) -> None:
    with _mutable_receipts(path) as connection:
        connection.execute("DELETE FROM transition_receipts")
        if batch is not None:
            _insert_receipt(connection, batch)


def _assert_receipt_rejected(store: SQLiteKernelStore, action: str, destination: Path) -> None:
    with pytest.raises(StoreCorruption, match=r"receipt.*coverage"):
        if action == "open":
            SQLiteKernelStore.open(store.path)
        elif action == "backup":
            store.backup_to(destination)
        else:
            SQLiteKernelStore.restore_from(store.path, destination)
    assert not destination.exists()


def test_should_publish_verified_online_backup_with_authoritative_projection(
    tmp_path: Path,
) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    expected = source.load_snapshot(SAGA_ID)
    destination = tmp_path / "backup.db"

    # When
    source.backup_to(destination)
    restored = SQLiteKernelStore.open(destination)

    # Then
    assert restored.integrity_check() == "ok"
    assert restored.foreign_key_check() == ()
    assert restored.rebuild_and_verify(SAGA_ID) == expected


def test_should_publish_private_backup_with_permissive_umask(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"

    # When
    previous = os.umask(0)
    try:
        source.backup_to(destination)
    finally:
        os.umask(previous)

    # Then
    assert _permission_bits(destination) == 0o600


def test_should_not_clobber_destination_created_during_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    _install_backup_race(monkeypatch, lambda: destination.write_bytes(b"competitor"))

    # When / Then
    with pytest.raises(StoreConflict):
        source.backup_to(destination)
    assert destination.read_bytes() == b"competitor"
    assert not tuple(tmp_path.glob(".backup.db.*.tmp*"))


def test_should_not_publish_over_destination_with_active_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    connections: list[sqlite3.Connection] = []
    inodes: list[int] = []

    def publish_competitor() -> None:
        connections.append(_active_wal_database(destination))
        inodes.append(destination.stat().st_ino)

    _install_backup_race(monkeypatch, publish_competitor)

    # When / Then
    try:
        with pytest.raises(StoreConflict):
            source.backup_to(destination)
        assert destination.stat().st_ino == inodes[0]
        assert connections[0].execute("SELECT value FROM sentinel").fetchone() == ("in-flight",)
        assert all(Path(f"{destination}{suffix}").exists() for suffix in ("-wal", "-shm"))
    finally:
        connections[0].rollback()
        connections[0].close()


def test_should_remove_new_backup_if_sidecar_appears_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    real_link = os.link

    def link_then_create_sidecar(
        source_path: Path, destination_path: Path, *, follow_symlinks: bool = True
    ) -> None:
        real_link(source_path, destination_path, follow_symlinks=follow_symlinks)
        Path(f"{destination}-wal").write_bytes(b"competitor")

    monkeypatch.setattr(os, "link", link_then_create_sidecar)

    # When / Then
    with pytest.raises(StoreConflict):
        source.backup_to(destination)
    assert not destination.exists()
    assert Path(f"{destination}-wal").read_bytes() == b"competitor"


def test_should_reject_destination_substituted_after_publication_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    real_link = os.link

    def substitute_destination(
        source_path: Path, destination_path: Path, *, follow_symlinks: bool = True
    ) -> None:
        real_link(source_path, destination_path, follow_symlinks=follow_symlinks)
        destination_path.unlink()
        destination_path.write_bytes(b"competitor")
        destination_path.chmod(0o644)

    monkeypatch.setattr(os, "link", substitute_destination)

    # When / Then
    with pytest.raises(StoreConflict):
        source.backup_to(destination)
    assert destination.read_bytes() == b"competitor"
    assert _permission_bits(destination) == 0o644


def test_should_reject_destination_substituted_after_descriptor_hardening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    real_fchmod = os.fchmod
    real_link = os.link
    published = False

    def mark_published(
        source_path: Path, destination_path: Path, *, follow_symlinks: bool = True
    ) -> None:
        nonlocal published
        real_link(source_path, destination_path, follow_symlinks=follow_symlinks)
        published = True

    def substitute_after_hardening(descriptor: int, mode: int) -> None:
        nonlocal published
        real_fchmod(descriptor, mode)
        if published and destination.exists():
            destination.unlink()
            destination.write_bytes(b"competitor")
            destination.chmod(0o644)
            published = False

    monkeypatch.setattr(os, "link", mark_published)
    monkeypatch.setattr(os, "fchmod", substitute_after_hardening)

    # When / Then
    with pytest.raises(StoreConflict):
        source.backup_to(destination)
    assert destination.read_bytes() == b"competitor"
    assert _permission_bits(destination) == 0o644


def test_should_reject_destination_sidecar_created_during_hardening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    sidecar = Path(f"{destination}-wal")
    real_fchmod = os.fchmod

    def create_sidecar_after_hardening(descriptor: int, mode: int) -> None:
        real_fchmod(descriptor, mode)
        if destination.exists() and not sidecar.exists():
            details = os.fstat(descriptor)
            if (details.st_dev, details.st_ino) == (
                destination.stat().st_dev,
                destination.stat().st_ino,
            ):
                sidecar.write_bytes(b"competitor")

    monkeypatch.setattr(os, "fchmod", create_sidecar_after_hardening)

    # When / Then
    with pytest.raises(StoreConflict):
        source.backup_to(destination)
    assert not destination.exists()
    assert sidecar.read_bytes() == b"competitor"


def test_should_preserve_substituted_temporary_leaf_and_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    original = SQLiteKernelStore._backup_into
    temporary_paths: list[Path] = []

    def substitute_temporary(store: SQLiteKernelStore, temporary: Path) -> tuple[object, ...]:
        original(store, temporary)
        temporary.unlink()
        temporary.write_bytes(b"competitor-main")
        Path(f"{temporary}-wal").write_bytes(b"competitor-sidecar")
        temporary_paths.append(temporary)
        raise StoreConflict("injected temporary substitution")

    monkeypatch.setattr(SQLiteKernelStore, "_backup_into", substitute_temporary)

    # When / Then
    with pytest.raises(StoreConflict, match="temporary substitution"):
        source.backup_to(destination)
    assert temporary_paths[0].read_bytes() == b"competitor-main"
    assert Path(f"{temporary_paths[0]}-wal").read_bytes() == b"competitor-sidecar"


def test_should_not_publish_backup_with_uncheckpointed_temporary_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    original = sqlite_storage._publish_new_backup

    def leave_sidecar(
        temporary: Path, target: Path, expected: sqlite_storage._FileIdentity
    ) -> None:
        Path(f"{temporary}-wal").write_bytes(b"uncheckpointed")
        original(temporary, target, expected)

    monkeypatch.setattr(sqlite_storage, "_publish_new_backup", leave_sidecar)

    # When / Then
    with pytest.raises(StoreConflict):
        source.backup_to(destination)
    assert not destination.exists()
    assert tuple(tmp_path.glob(".backup.db.*.tmp-wal"))


def test_should_reject_backup_destination_symlink(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    target = tmp_path / "target.db"
    target.write_bytes(b"unchanged")
    destination = tmp_path / "backup.db"
    destination.symlink_to(target)

    # When / Then
    with pytest.raises(StoreConflict):
        source.backup_to(destination)
    assert target.read_bytes() == b"unchanged"


def test_should_reject_dangling_backup_destination_symlink(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    target = tmp_path / "missing.db"
    destination = tmp_path / "backup.db"
    destination.symlink_to(target)

    # When / Then
    with pytest.raises(StoreConflict):
        source.backup_to(destination)
    assert not target.exists()


def test_should_reject_backup_destination_with_sidecar_symlink(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    victim = tmp_path / "victim"
    victim.write_bytes(b"unchanged")
    Path(f"{destination}-wal").symlink_to(victim)

    # When / Then
    with pytest.raises(StoreConflict):
        source.backup_to(destination)
    assert not destination.exists()
    assert victim.read_bytes() == b"unchanged"


def test_should_reject_restore_source_symlink(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    alias = tmp_path / "alias.db"
    alias.symlink_to(source.path)
    destination = tmp_path / "restored.db"

    # When / Then
    with pytest.raises(StoreConflict):
        SQLiteKernelStore.restore_from(alias, destination)
    assert not destination.exists()


def test_should_reject_restore_destination_symlink(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    target = tmp_path / "target.db"
    target.write_bytes(b"unchanged")
    destination = tmp_path / "restored.db"
    destination.symlink_to(target)

    # When / Then
    with pytest.raises(StoreConflict):
        SQLiteKernelStore.restore_from(source.path, destination)
    assert target.read_bytes() == b"unchanged"


def test_should_keep_backup_independent_from_later_source_changes(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    source.backup_to(destination)

    # When
    with closing(sqlite3.connect(source.path)) as connection, connection:
        connection.execute(
            "UPDATE sagas SET fence_token = 1, lease_owner = ?, lease_expires_at = ? "
            "WHERE saga_id = ?",
            ("worker-a", "2026-09-07T12:00:00+00:00", SAGA_ID),
        )

    # Then
    assert SQLiteKernelStore.open(destination).current_fence(SAGA_ID) == 0
    assert source.current_fence(SAGA_ID) == 1


def test_should_keep_existing_backup_destination_untouched(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    source.backup_to(destination)
    original = destination.read_bytes()
    inode = destination.stat().st_ino

    # When / Then
    with pytest.raises(StoreConflict, match="already exists"):
        source.backup_to(destination)
    assert destination.read_bytes() == original
    assert destination.stat().st_ino == inode


def test_should_reject_backup_over_source_database(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")

    # When / Then
    with pytest.raises(StoreConflict, match="source database"):
        source.backup_to(source.path)


def test_should_restore_verified_source_only_to_fresh_destination(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    backup = tmp_path / "backup.db"
    destination = tmp_path / "restored.db"
    source.backup_to(backup)

    # When
    restored = SQLiteKernelStore.restore_from(backup, destination)

    # Then
    assert restored.rebuild_and_verify(SAGA_ID) == source.load_snapshot(SAGA_ID)
    inode = destination.stat().st_ino
    with pytest.raises(StoreConflict, match="already exists"):
        SQLiteKernelStore.restore_from(backup, destination)
    assert destination.stat().st_ino == inode


def test_should_reject_corrupt_restore_source_without_publishing(tmp_path: Path) -> None:
    # Given
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    destination = tmp_path / "restored.db"

    # When / Then
    with pytest.raises(StoreCorruption):
        SQLiteKernelStore.restore_from(corrupt, destination)
    assert not destination.exists()


def test_should_reject_tampered_backup_on_open_and_restore(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    backup = tmp_path / "backup.db"
    source.backup_to(backup)
    with closing(sqlite3.connect(backup)) as connection, connection:
        connection.execute("DROP TRIGGER ledger_events_no_update")
        connection.execute(
            "UPDATE ledger_events SET event_hash = ? WHERE saga_id = ? AND saga_seq = 2",
            ("f" * 64, SAGA_ID),
        )

    # When / Then
    with pytest.raises(StoreCorruption, match="schema"):
        SQLiteKernelStore.open(backup)
    with pytest.raises(StoreCorruption, match="schema"):
        SQLiteKernelStore.restore_from(backup, tmp_path / "restored.db")


def test_should_reject_backup_with_tampered_outbox_command(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    backup = tmp_path / "backup.db"
    source.backup_to(backup)
    with closing(sqlite3.connect(backup)) as connection, connection:
        connection.execute(
            "UPDATE outbox_commands SET command_hash = ? WHERE saga_id = ?",
            ("f" * 64, SAGA_ID),
        )

    # When / Then
    with pytest.raises(StoreCorruption, match="outbox command"):
        SQLiteKernelStore.open(backup)


def test_should_reject_backup_missing_outbox_for_durable_intent(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    backup = tmp_path / "backup.db"
    source.backup_to(backup)
    with closing(sqlite3.connect(backup)) as connection, connection:
        connection.execute("DELETE FROM outbox_commands WHERE saga_id = ?", (SAGA_ID,))

    # When / Then
    with pytest.raises(StoreCorruption, match="outbox cardinality"):
        SQLiteKernelStore.open(backup)


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TRIGGER ledger_events_no_update",
        "DROP INDEX outbox_runnable_scan",
        "DROP TABLE transition_receipts",
    ],
)
def test_should_reject_database_missing_required_schema_object(
    tmp_path: Path, statement: str
) -> None:
    # Given
    path = tmp_path / "saga.db"
    store = _populated(path)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(statement)

    # When / Then
    with pytest.raises(StoreCorruption, match="schema"):
        SQLiteKernelStore.open(path)


def test_should_reject_required_trigger_with_altered_definition(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    _populated(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TRIGGER ledger_events_no_update")
        connection.execute(
            "CREATE TRIGGER ledger_events_no_update BEFORE UPDATE ON ledger_events "
            "BEGIN SELECT 1; END"
        )

    # When / Then
    with pytest.raises(StoreCorruption, match="schema"):
        SQLiteKernelStore.open(path)


def test_should_reject_completed_outbox_without_durable_outcome(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    _populated(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE outbox_commands SET state = 'completed', claim_id = ?, claim_owner = ?, "
            "claim_expires_at = ?, claim_generation = 1, delivery_attempt = 1 "
            "WHERE saga_id = ?",
            (
                f"claim_{'a' * 32}",
                "worker-a",
                "2026-09-07T12:00:00+00:00",
                SAGA_ID,
            ),
        )

    # When / Then
    with pytest.raises(StoreCorruption, match="outbox lifecycle"):
        SQLiteKernelStore.open(path)


@pytest.mark.parametrize(
    ("claim_id", "expiry"),
    [
        (f"claim_{'z' * 32}", "2026-09-07T12:00:00+00:00"),
        (f"claim_{'a' * 32}", "zzzzzzzzzzzzzzzzzzzz"),
    ],
)
def test_should_reject_malformed_claim_metadata_on_open(
    tmp_path: Path, claim_id: str, expiry: str
) -> None:
    # Given
    path = tmp_path / "saga.db"
    _populated(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE outbox_commands SET state = 'claimed', claim_id = ?, claim_owner = ?, "
            "claim_expires_at = ?, claim_generation = 1, delivery_attempt = 1 "
            "WHERE saga_id = ?",
            (claim_id, "worker-a", expiry, SAGA_ID),
        )

    # When / Then
    with pytest.raises(StoreCorruption):
        SQLiteKernelStore.open(path)


def test_should_reject_malformed_active_lease_on_open(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    _populated(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE sagas SET fence_token = 1, lease_owner = ?, lease_expires_at = ? "
            "WHERE saga_id = ?",
            ("worker-a", "zzzzzzzzzzzzzzzzzzzz", SAGA_ID),
        )

    # When / Then
    with pytest.raises(StoreCorruption):
        SQLiteKernelStore.open(path)


def test_should_reject_malformed_transition_receipt_on_open(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    _populated(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO transition_receipts "
            "(transition_id, saga_id, expected_seq, resulting_seq, payload_digest, "
            "projection_json, transition_json) SELECT ?, saga_id, 2, 3, ?, projection_json, ? "
            "FROM sagas WHERE saga_id = ?",
            ("txn_0000000000000099", "f" * 64, b"{}", SAGA_ID),
        )

    # When / Then
    with pytest.raises(StoreCorruption, match="receipt"):
        SQLiteKernelStore.open(path)


def test_should_reject_receipt_projection_that_disagrees_with_ledger(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "saga.db"
    _populated(path)
    _tamper_receipt_projection(path)

    # When / Then
    with pytest.raises(StoreCorruption, match="receipt"):
        SQLiteKernelStore.open(path)


def test_should_reject_backup_when_source_receipt_lacks_durable_proof(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    destination = tmp_path / "backup.db"
    _tamper_receipt_boundary(source.path)

    # When / Then
    with pytest.raises(StoreCorruption, match="receipt"):
        source.backup_to(destination)
    assert not destination.exists()


def test_should_reject_restore_when_receipt_lacks_durable_proof(tmp_path: Path) -> None:
    # Given
    source = _populated(tmp_path / "source.db")
    backup = tmp_path / "backup.db"
    destination = tmp_path / "restored.db"
    source.backup_to(backup)
    _tamper_receipt_digest(backup)

    # When / Then
    with pytest.raises(StoreCorruption, match="receipt"):
        SQLiteKernelStore.restore_from(backup, destination)
    assert not destination.exists()


def test_should_reject_duplicate_receipt_boundary_in_database(tmp_path: Path) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    duplicate = prepared_transition().batch.model_copy(
        update={"transition_id": "txn_0000000000000099"}
    )

    # When / Then
    with closing(sqlite3.connect(store.path)) as connection, connection:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            _insert_receipt(connection, duplicate)
        count = connection.execute("SELECT COUNT(*) FROM transition_receipts").fetchone()
    assert count == (1,)


@pytest.mark.parametrize("action", ["open", "backup", "restore"])
def test_should_reject_overlapping_receipt_coverage(tmp_path: Path, action: str) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    with closing(sqlite3.connect(store.path)) as connection, connection:
        _insert_receipt(connection, _overlapping_batch())

    # When / Then
    _assert_receipt_rejected(store, action, tmp_path / "destination.db")


@pytest.mark.parametrize("action", ["open", "backup", "restore"])
def test_should_reject_gapped_receipt_coverage(tmp_path: Path, action: str) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    _replace_receipts(store.path, _started_only_batch())

    # When / Then
    _assert_receipt_rejected(store, action, tmp_path / "destination.db")


@pytest.mark.parametrize("action", ["open", "backup", "restore"])
def test_should_reject_missing_receipt_coverage(tmp_path: Path, action: str) -> None:
    # Given
    store = _populated(tmp_path / "saga.db")
    _replace_receipts(store.path, None)

    # When / Then
    _assert_receipt_rejected(store, action, tmp_path / "destination.db")
