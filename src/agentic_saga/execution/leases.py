from __future__ import annotations

from datetime import timedelta

from agentic_saga.contracts.common import SagaId
from agentic_saga.kernel.ports import Lease as _Lease
from agentic_saga.kernel.ports import LeaseStorage


class LeaseService:
    """Maps durable store lease identities onto the execution boundary."""

    def __init__(self, store: LeaseStorage) -> None:
        self._store = store

    def acquire(self, saga_id: SagaId, owner: str, duration: timedelta) -> _Lease:
        return self._store.acquire_lease(saga_id, owner, duration)

    def renew(self, lease: _Lease, duration: timedelta) -> _Lease:
        return self._store.renew_lease(lease, duration)

    def release(self, lease: _Lease) -> None:
        self._store.release_lease(lease)


__all__ = ["LeaseService"]
