from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from agentic_saga.contracts.common import canonical_json
from agentic_saga.contracts.events import (
    DispatchStarted,
    EffectOutcomeRecorded,
    ReconciliationRecorded,
)
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.kernel.state import SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.support.durable_tool import DurableFakeTool
from tests.support.kernel_harness import REPAIR_TOOL_NAME, TOOL_NAME

_FAILPOINTS = (
    "before_intent_commit",
    "after_intent_commit",
    "after_dispatch_record",
    "after_provider_effect",
    "after_outcome_commit",
    "after_compensation_intent",
    "after_compensation_effect",
    "before_terminal_commit",
)
_COMPENSATION_POINTS = frozenset({"after_compensation_intent", "after_compensation_effect"})
type _ScenarioMode = Literal["succeed", "compensate", "opaque"]


@dataclass(frozen=True)
class _CutExpectation:
    seq: int
    status: str
    operations: tuple[tuple[str, str], ...]
    outbox: tuple[str, ...]
    forward_calls: int
    forward_effects: int
    repair_calls: int = 0
    repair_effects: int = 0


_CUTS = {
    "before_intent_commit": _CutExpectation(2, "running", (), (), 0, 0),
    "after_intent_commit": _CutExpectation(
        3, "running", (("forward", "intent_durable"),), ("runnable",), 0, 0
    ),
    "after_dispatch_record": _CutExpectation(
        4, "running", (("forward", "dispatched"),), ("claimed",), 0, 0
    ),
    "after_provider_effect": _CutExpectation(
        4, "running", (("forward", "dispatched"),), ("claimed",), 1, 1
    ),
    "after_outcome_commit": _CutExpectation(
        5, "running", (("forward", "effect_confirmed"),), ("completed",), 1, 1
    ),
    "after_compensation_intent": _CutExpectation(
        7,
        "compensating",
        (("compensation", "intent_durable"), ("forward", "effect_confirmed")),
        ("completed", "runnable"),
        1,
        1,
    ),
    "after_compensation_effect": _CutExpectation(
        8,
        "compensating",
        (("compensation", "dispatched"), ("forward", "effect_confirmed")),
        ("claimed", "completed"),
        1,
        1,
        1,
        1,
    ),
    "before_terminal_commit": _CutExpectation(
        5, "running", (("forward", "effect_confirmed"),), ("completed",), 1, 1
    ),
}


class CrashEvidence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    status: Literal["succeeded_verified", "compensated_verified", "human_required"]
    duplicate_business_effects: int
    forward_effect_count: int
    compensation_effect_count: int
    forward_execute_calls: int
    forward_reconcile_calls: int
    compensation_execute_calls: int
    compensation_reconcile_calls: int
    recovery_worker_pid: int
    integrity: Literal["ok"]
    backup_integrity: Literal["ok"]
    forward_provider_integrity: Literal["ok"]
    compensation_provider_integrity: Literal["ok"]
    forward_provider_backup_integrity: Literal["ok"]
    compensation_provider_backup_integrity: Literal["ok"]
    projection_matches_replay: bool
    event_seqs: tuple[int, ...]


def _scenario(point: str, requested: _ScenarioMode | None = None) -> bytes:
    mode = requested or ("compensate" if point in _COMPENSATION_POINTS else "succeed")
    return canonical_json({"mode": mode})


def _worker(directory: Path, point: str | None) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    if point is None:
        env.pop("AGENTIC_SAGA_FAILPOINT", None)
    else:
        env["AGENTIC_SAGA_FAILPOINT"] = point
    command = (sys.executable, "-m", "tests.crash.worker", str(directory))
    return subprocess.run(command, env=env, capture_output=True, timeout=30, check=False)


def _recover(directory: Path, point: str, mode: _ScenarioMode | None = None) -> CrashEvidence:
    _prepare(directory, point, mode)
    crashed = _worker(directory, point)
    assert crashed.returncode == 91, crashed.stderr.decode(errors="replace")
    assert not (directory / "result.json").exists()
    _assert_cut_state(directory, _CUTS[point])
    recovered = _worker(directory, None)
    assert recovered.returncode == 0, recovered.stderr.decode(errors="replace")
    evidence = CrashEvidence.model_validate_json(
        (directory / "result.json").read_bytes(), strict=True
    )
    assert evidence.recovery_worker_pid != int((directory / "initial-worker.pid").read_text())
    return evidence


def _prepare(directory: Path, point: str, mode: _ScenarioMode | None = None) -> None:
    (directory / "scenario.json").write_bytes(_scenario(point, mode))
    forward = DurableFakeTool.initialize(directory / "forward.db", TOOL_NAME)
    if mode == "opaque":
        forward.disable_reconciliation_and_deduplication()
    DurableFakeTool.initialize(directory / "repair.db", REPAIR_TOOL_NAME)


def _assert_cut_state(directory: Path, expected: _CutExpectation) -> None:
    store = SQLiteKernelStore.open(directory / "saga.db")
    snapshot = store.load_snapshot("saga_0000000000008001")
    operations = tuple(
        sorted((item.direction.value, item.status.value) for item in snapshot.operations.values())
    )
    assert (snapshot.seq, snapshot.status.value, operations) == (
        expected.seq,
        expected.status,
        expected.operations,
    )
    assert _outbox_states(store.path) == expected.outbox
    _assert_provider_cut(directory, snapshot, expected)


def _outbox_states(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as connection, connection:
        rows = connection.execute("SELECT state FROM outbox_commands ORDER BY state").fetchall()
    return tuple(str(row[0]) for row in rows)


def _assert_provider_cut(
    directory: Path, snapshot: SagaSnapshot, expected: _CutExpectation
) -> None:
    forward = DurableFakeTool.open(directory / "forward.db", TOOL_NAME)
    repair = DurableFakeTool.open(directory / "repair.db", REPAIR_TOOL_NAME)
    effects = _direction_effects(snapshot, forward, repair)
    assert (forward.execute_call_count, effects[0]) == (
        expected.forward_calls,
        expected.forward_effects,
    )
    assert (repair.execute_call_count, effects[1]) == (
        expected.repair_calls,
        expected.repair_effects,
    )


def _direction_effects(
    snapshot: SagaSnapshot, forward: DurableFakeTool, repair: DurableFakeTool
) -> tuple[int, int]:
    forward_ids = _operation_ids(snapshot, "forward")
    repair_ids = _operation_ids(snapshot, "compensation")
    return sum(forward.effect_count(item) for item in forward_ids), sum(
        repair.effect_count(item) for item in repair_ids
    )


def _operation_ids(snapshot: SagaSnapshot, direction: str) -> tuple[str, ...]:
    return tuple(
        item.operation_id
        for item in snapshot.operations.values()
        if item.direction.value == direction
    )


@pytest.mark.parametrize("point", _FAILPOINTS)
def test_restart_converges_without_duplicate_business_effects(tmp_path: Path, point: str) -> None:
    evidence = _recover(tmp_path, point)
    expected = (
        SagaStatus.COMPENSATED_VERIFIED.value
        if point in _COMPENSATION_POINTS
        else SagaStatus.SUCCEEDED_VERIFIED.value
    )
    assert evidence.status == expected
    assert evidence.duplicate_business_effects == 0
    assert evidence.forward_effect_count == 1
    assert evidence.compensation_effect_count == int(point in _COMPENSATION_POINTS)
    assert evidence.forward_execute_calls == 1
    assert evidence.forward_reconcile_calls == int(point in _reconciled_forward_points())
    assert evidence.compensation_execute_calls == int(point in _COMPENSATION_POINTS)
    assert evidence.compensation_reconcile_calls == int(point == "after_compensation_effect")


def _reconciled_forward_points() -> frozenset[str]:
    return frozenset({"after_dispatch_record", "after_provider_effect"})


def test_opaque_lost_response_escalates_without_blind_redelivery(tmp_path: Path) -> None:
    evidence = _recover(tmp_path, "after_provider_effect", "opaque")
    assert evidence.status == SagaStatus.HUMAN_REQUIRED.value
    assert evidence.forward_effect_count == 1
    assert evidence.forward_execute_calls == 1
    assert evidence.forward_reconcile_calls == 0
    assert evidence.duplicate_business_effects == 0


@pytest.mark.parametrize("point", _FAILPOINTS)
def test_restart_preserves_sqlite_ledger_and_projection(tmp_path: Path, point: str) -> None:
    evidence = _recover(tmp_path, point)
    store = SQLiteKernelStore.open(tmp_path / "saga.db")
    snapshot = store.load_snapshot("saga_0000000000008001")
    assert evidence.integrity == "ok"
    assert evidence.backup_integrity == "ok"
    assert (
        evidence.projection_matches_replay
        and store.rebuild_and_verify(snapshot.saga_id) == snapshot
    )
    assert evidence.event_seqs == tuple(range(1, len(evidence.event_seqs) + 1))
    _assert_recovery_path(store, point)


def _assert_recovery_path(store: SQLiteKernelStore, point: str) -> None:
    events = store.read_events("saga_0000000000008001")
    if point not in _reconciled_forward_points():
        return
    names = tuple(type(event) for event in events)
    assert names.count(ReconciliationRecorded) == 1
    assert names.count(EffectOutcomeRecorded) >= 1
    expected_dispatches = 2 if point == "after_dispatch_record" else 1
    assert names.count(DispatchStarted) == expected_dispatches
    if point == "after_dispatch_record":
        _assert_reconciliation_precedes_retry(names)


def _assert_reconciliation_precedes_retry(names: tuple[type[object], ...]) -> None:
    reconciliation = names.index(ReconciliationRecorded)
    dispatches = tuple(index for index, name in enumerate(names) if name is DispatchStarted)
    assert reconciliation < dispatches[1]
