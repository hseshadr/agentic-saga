from pathlib import Path

from agentic_saga.storage import SQLiteKernelStore
from tests.contract.storage import StorageContract


class TestSQLiteBackend(StorageContract):
    def make_store(self, path: Path) -> SQLiteKernelStore:
        return SQLiteKernelStore.initialize(path)

    def reopen_store(self, path: Path) -> SQLiteKernelStore:
        return SQLiteKernelStore.open(path)

    def restore_store(self, source: Path, destination: Path) -> SQLiteKernelStore:
        return SQLiteKernelStore.restore_from(source, destination)
