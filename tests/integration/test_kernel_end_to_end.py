from pathlib import Path

import pytest

from agentic_saga.contracts.common import Direction
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.kernel.failpoints import DurabilityPoint
from agentic_saga.kernel.ports import StoreFailpoint
from agentic_saga.storage import SQLiteKernelStore
from tests.support.durable_tool import DurableFakeTool
from tests.support.kernel_harness import REPAIR_TOOL_NAME, TOOL_NAME, CrashKernelHarness


class _NoOpCrash:
    def hit(self, point: DurabilityPoint | StoreFailpoint) -> None:
        del point


def _initialize_harness(directory: Path) -> CrashKernelHarness:
    DurableFakeTool.initialize(directory / "forward.db", TOOL_NAME)
    DurableFakeTool.initialize(directory / "repair.db", REPAIR_TOOL_NAME)
    return CrashKernelHarness.initialize(directory, _NoOpCrash())


def _assert_reopened(harness: CrashKernelHarness, backup: Path) -> None:
    harness.store.backup_to(backup)
    restored = SQLiteKernelStore.restore_from(backup, backup.with_name("restored.db"))
    snapshot = restored.load_snapshot(harness.lease.saga_id)
    assert restored.rebuild_and_verify(snapshot.saga_id) == snapshot
    assert snapshot.status is SagaStatus.COMPENSATED_VERIFIED


@pytest.mark.asyncio
async def test_generic_effect_compensates_and_reopens_with_verified_evidence(
    tmp_path: Path,
) -> None:
    harness = _initialize_harness(tmp_path)
    harness.ensure_forward_intent()
    await harness.settle(Direction.FORWARD)
    harness.ensure_compensation_started()
    harness.ensure_compensation_intent()
    await harness.settle(Direction.COMPENSATION)
    harness.finish(SagaStatus.COMPENSATED_VERIFIED)
    operation = harness.operation(Direction.FORWARD)
    assert operation is not None
    assert harness.forward_provider.effect_count(operation.operation_id) == 1
    _assert_reopened(harness, tmp_path / "backup.db")
