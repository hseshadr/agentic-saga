from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from agentic_saga.contracts.common import Direction, JsonObject, canonical_json
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.kernel.failpoints import DurabilityPoint
from agentic_saga.kernel.ports import StoreFailpoint
from agentic_saga.storage import SQLiteKernelStore
from tests.support.kernel_harness import SAGA_ID, CrashKernelHarness

_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


class _Scenario(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    mode: Literal["succeed", "compensate", "opaque"]


@dataclass(frozen=True)
class _ExitFailpoint:
    target: DurabilityPoint | None

    @classmethod
    def from_environment(cls) -> _ExitFailpoint:
        value = os.environ.get("AGENTIC_SAGA_FAILPOINT")
        return cls(None if value is None else DurabilityPoint(value))

    def hit(self, point: DurabilityPoint | StoreFailpoint) -> None:
        if point is self.target:
            os._exit(91)


def _read_scenario(directory: Path) -> _Scenario:
    payload = (directory / "scenario.json").read_bytes()
    scenario = _Scenario.model_validate_json(payload, strict=True)
    if payload != canonical_json(scenario.model_dump(mode="json")):
        raise ValueError("crash scenario must use canonical JSON")
    return scenario


def _harness(directory: Path, failpoint: _ExitFailpoint) -> CrashKernelHarness:
    if (directory / "saga.db").exists():
        return CrashKernelHarness.open(directory, failpoint)
    return CrashKernelHarness.initialize(directory, failpoint)


async def _run(harness: CrashKernelHarness, scenario: _Scenario) -> None:
    harness.ensure_forward_intent()
    await harness.settle(Direction.FORWARD)
    if scenario.mode == "compensate":
        await _compensate(harness)
    elif scenario.mode == "succeed":
        harness.finish(SagaStatus.SUCCEEDED_VERIFIED)


async def _compensate(harness: CrashKernelHarness) -> None:
    harness.ensure_compensation_started()
    harness.ensure_compensation_intent()
    await harness.settle(Direction.COMPENSATION)
    harness.finish(SagaStatus.COMPENSATED_VERIFIED)


def _effect_count(harness: CrashKernelHarness, direction: Direction) -> int:
    operation = harness.operation(direction)
    if operation is None:
        return 0
    provider = harness.forward_provider
    if direction is Direction.COMPENSATION:
        provider = harness.repair_provider
    return provider.effect_count(operation.operation_id)


def _integrity(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection, connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    return "missing" if row is None else str(row[0])


def _backup_integrity(harness: CrashKernelHarness, directory: Path) -> str:
    backup = directory / "backup.db"
    restored = directory / "restored.db"
    harness.store.backup_to(backup)
    opened = SQLiteKernelStore.restore_from(backup, restored)
    opened.rebuild_and_verify(SAGA_ID)
    return opened.integrity_check()


def _copy_database(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source)) as original,
        closing(sqlite3.connect(destination)) as backup,
        original,
        backup,
    ):
        original.backup(backup)


def _logical_dump(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as connection, connection:
        return tuple(connection.iterdump())


def _provider_backup_integrity(source: Path, destination: Path) -> str:
    _copy_database(source, destination)
    if _integrity(destination) != "ok":
        return "invalid"
    return "ok" if _logical_dump(source) == _logical_dump(destination) else "mismatch"


def _evidence(harness: CrashKernelHarness, directory: Path, status: SagaStatus) -> JsonObject:
    values = {"status": status.value} | _execution_evidence(harness)
    return _JSON_OBJECT_ADAPTER.validate_python(values | _storage_evidence(harness, directory))


def _execution_evidence(harness: CrashKernelHarness) -> dict[str, object]:
    forward = _effect_count(harness, Direction.FORWARD)
    compensation = _effect_count(harness, Direction.COMPENSATION)
    duplicates = max(0, forward - 1) + max(0, compensation - 1)
    return {
        "duplicate_business_effects": duplicates,
        "forward_effect_count": forward,
        "compensation_effect_count": compensation,
        "forward_execute_calls": harness.forward_provider.execute_call_count,
        "forward_reconcile_calls": harness.forward_provider.reconcile_call_count,
        "compensation_execute_calls": harness.repair_provider.execute_call_count,
        "compensation_reconcile_calls": harness.repair_provider.reconcile_call_count,
        "recovery_worker_pid": os.getpid(),
    }


def _storage_evidence(harness: CrashKernelHarness, directory: Path) -> dict[str, object]:
    events = harness.store.read_events(SAGA_ID)
    projection = harness.store.rebuild_and_verify(SAGA_ID)
    current = harness.store.load_snapshot(SAGA_ID)
    durable = {
        "integrity": _integrity(harness.store.path),
        "backup_integrity": _backup_integrity(harness, directory),
        "projection_matches_replay": projection == current,
        "event_seqs": [event.saga_seq for event in events],
    }
    return durable | _provider_storage_evidence(harness, directory)


def _provider_storage_evidence(harness: CrashKernelHarness, directory: Path) -> dict[str, object]:
    forward = harness.forward_provider.path
    compensation = harness.repair_provider.path
    return {
        "forward_provider_integrity": _integrity(forward),
        "compensation_provider_integrity": _integrity(compensation),
        "forward_provider_backup_integrity": _provider_backup_integrity(
            forward, directory / "forward-backup.db"
        ),
        "compensation_provider_backup_integrity": _provider_backup_integrity(
            compensation, directory / "repair-backup.db"
        ),
    }


def main() -> None:
    directory = Path(sys.argv[1]).absolute()
    scenario = _read_scenario(directory)
    failpoint = _ExitFailpoint.from_environment()
    if failpoint.target is not None:
        (directory / "initial-worker.pid").write_text(str(os.getpid()), encoding="ascii")
    harness = _harness(directory, failpoint)
    asyncio.run(_run(harness, scenario))
    status = harness.store.load_snapshot(SAGA_ID).status
    (directory / "result.json").write_bytes(canonical_json(_evidence(harness, directory, status)))


if __name__ == "__main__":
    main()
