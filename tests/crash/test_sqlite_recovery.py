from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentic_saga.contracts.common import Direction
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.kernel.failpoints import DurabilityPoint
from agentic_saga.kernel.ports import StoreFailpoint
from agentic_saga.storage import SQLiteKernelStore
from tests.crash.test_crash_matrix import _prepare, _recover
from tests.support.kernel_harness import CrashKernelHarness


class _StagedCrash(RuntimeError):
    pass


class _NoOpCrash:
    def hit(self, point: DurabilityPoint | StoreFailpoint) -> None:
        del point


@dataclass
class _StagedProbe:
    target: DurabilityPoint
    points: list[str] = field(default_factory=list)

    def hit(self, point: DurabilityPoint | StoreFailpoint) -> None:
        self.points.append(point.value)
        if point is self.target:
            raise _StagedCrash


def _assert_staged(probe: _StagedProbe) -> None:
    receipt = probe.points.index(StoreFailpoint.AFTER_RECEIPT_INSERT.value)
    semantic = probe.points.index(probe.target.value)
    assert receipt < semantic


def test_recovered_database_reopens_with_integrity_and_verified_projection(tmp_path: Path) -> None:
    evidence = _recover(tmp_path, "after_provider_effect")
    original = SQLiteKernelStore.open(tmp_path / "saga.db")
    restored = SQLiteKernelStore.open(tmp_path / "restored.db")
    snapshot = restored.load_snapshot("saga_0000000000008001")

    assert evidence.status == SagaStatus.SUCCEEDED_VERIFIED.value
    assert restored.integrity_check() == "ok"
    assert restored.rebuild_and_verify(snapshot.saga_id) == snapshot
    assert snapshot == original.load_snapshot(snapshot.saga_id)


def test_before_intent_crash_occurs_after_staged_writes_and_rolls_back(tmp_path: Path) -> None:
    _prepare(tmp_path, DurabilityPoint.BEFORE_INTENT_COMMIT.value)
    probe = _StagedProbe(DurabilityPoint.BEFORE_INTENT_COMMIT)
    harness = CrashKernelHarness.initialize(tmp_path, probe)

    with pytest.raises(_StagedCrash):
        harness.ensure_forward_intent()

    _assert_staged(probe)
    assert harness.store.load_snapshot(harness.lease.saga_id).seq == 2


@pytest.mark.asyncio
async def test_before_terminal_crash_rolls_back_staged_proof_and_terminal(
    tmp_path: Path,
) -> None:
    _prepare(tmp_path, DurabilityPoint.BEFORE_TERMINAL_COMMIT.value)
    probe = _StagedProbe(DurabilityPoint.BEFORE_TERMINAL_COMMIT)
    harness = CrashKernelHarness.initialize(tmp_path, probe)
    harness.ensure_forward_intent()
    await harness.settle(Direction.FORWARD)
    probe.points.clear()

    with pytest.raises(_StagedCrash):
        harness.finish(SagaStatus.SUCCEEDED_VERIFIED)

    _assert_staged(probe)
    assert harness.store.load_snapshot(harness.lease.saga_id).status is SagaStatus.RUNNING


@pytest.mark.asyncio
async def test_terminal_evidence_fails_when_provider_state_disagrees(tmp_path: Path) -> None:
    _prepare(tmp_path, DurabilityPoint.AFTER_OUTCOME_COMMIT.value)
    harness = CrashKernelHarness.initialize(tmp_path, _NoOpCrash())
    harness.ensure_forward_intent()
    await harness.settle(Direction.FORWARD)
    with closing(sqlite3.connect(harness.forward_provider.path)) as connection, connection:
        connection.execute("DELETE FROM effects")

    with pytest.raises(AssertionError, match="terminal transition failed"):
        harness.finish(SagaStatus.SUCCEEDED_VERIFIED)


@pytest.mark.asyncio
async def test_terminal_evidence_rejects_provider_identity_mismatch(tmp_path: Path) -> None:
    _prepare(tmp_path, DurabilityPoint.AFTER_OUTCOME_COMMIT.value)
    harness = CrashKernelHarness.initialize(tmp_path, _NoOpCrash())
    harness.ensure_forward_intent()
    await harness.settle(Direction.FORWARD)
    with closing(sqlite3.connect(harness.forward_provider.path)) as connection, connection:
        connection.execute(
            "UPDATE effects SET command_hash = ?, receipt_ref = ?",
            ("0" * 64, "opaque://forged/receipt/v1"),
        )

    with pytest.raises(AssertionError, match="terminal transition failed"):
        harness.finish(SagaStatus.SUCCEEDED_VERIFIED)
