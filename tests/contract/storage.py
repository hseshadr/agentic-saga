from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agentic_saga.kernel.ports import KernelStore, LeaseLost, StoreConflict
from tests.integration.storage.helpers import SAGA_ID, prepared_transition


class StorageContract:
    def make_store(self, path: Path) -> KernelStore:
        raise NotImplementedError

    def reopen_store(self, path: Path) -> KernelStore:
        raise NotImplementedError

    def restore_store(self, source: Path, destination: Path) -> KernelStore:
        raise NotImplementedError

    def test_atomic_transition_rebuilds_exact_projection(self, tmp_path: Path) -> None:
        prepared = prepared_transition()
        store = self.make_store(tmp_path / "saga.db")
        store.create_saga(prepared.created)

        updated = store.commit_transition(prepared.batch)

        assert store.rebuild_and_verify(SAGA_ID) == updated
        assert store.runnable_count(SAGA_ID) == 1

    def test_compare_and_swap_rejects_stale_writer_without_mutation(self, tmp_path: Path) -> None:
        prepared = prepared_transition()
        store = self.make_store(tmp_path / "saga.db")
        before = store.create_saga(prepared.created)
        stale = prepared.batch.model_copy(update={"expected_seq": 0})

        with pytest.raises(StoreConflict):
            store.commit_transition(stale)

        assert store.load_snapshot(SAGA_ID) == before
        assert store.read_events(SAGA_ID) == (prepared.created,)

    def test_reopen_and_backup_preserve_verified_state(self, tmp_path: Path) -> None:
        prepared = prepared_transition()
        path, backup = tmp_path / "saga.db", tmp_path / "backup.db"
        store = self.make_store(path)
        store.create_saga(prepared.created)
        expected = store.commit_transition(prepared.batch)

        store.backup_to(backup)
        reopened = self.reopen_store(backup)

        assert reopened.rebuild_and_verify(SAGA_ID) == expected
        assert reopened.runnable_count(SAGA_ID) == 1

    def test_restore_to_new_destination_preserves_projection(self, tmp_path: Path) -> None:
        prepared = prepared_transition()
        source, backup, destination = _storage_paths(tmp_path)
        store = self.make_store(source)
        store.create_saga(prepared.created)
        expected = store.commit_transition(prepared.batch)

        store.backup_to(backup)
        self.restore_store(backup, destination)
        reopened = self.reopen_store(destination)

        assert reopened.load_snapshot(SAGA_ID) == expected
        assert reopened.rebuild_and_verify(SAGA_ID) == expected

    def test_lease_fence_is_monotonic_and_stale_owner_is_rejected(self, tmp_path: Path) -> None:
        prepared = prepared_transition()
        store = self.make_store(tmp_path / "saga.db")
        store.create_saga(prepared.created)
        first = store.acquire_lease(SAGA_ID, "worker-a", timedelta(minutes=1))
        store.release_lease(first)

        second = store.acquire_lease(SAGA_ID, "worker-b", timedelta(minutes=1))

        assert second.fence_token > first.fence_token
        with pytest.raises(LeaseLost):
            store.release_lease(first)


def _storage_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    return tmp_path / "source.db", tmp_path / "backup.db", tmp_path / "restored.db"


__all__ = ["StorageContract"]
