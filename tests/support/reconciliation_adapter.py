from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from pydantic import BaseModel, TypeAdapter

from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import JsonObject, thaw_json_object
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    OutcomeUnknown,
    ReconcileEffectConfirmed,
    ReconciliationOutcome,
)
from agentic_saga.contracts.tools import EffectContext, ReconcileContext
from agentic_saga.execution.leases import LeaseService
from agentic_saga.storage import SQLiteKernelStore

_OUTCOME: TypeAdapter[ReconciliationOutcome] = TypeAdapter(ReconciliationOutcome)
_EFFECT_OUTCOME: TypeAdapter[EffectOutcome] = TypeAdapter(EffectOutcome)
_SECRET_ENV = "AGENTIC_SAGA_PROVIDER_TEST_SECRET"  # noqa: S105


def initialize_probe(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE calls (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "execute_calls INTEGER NOT NULL, reconcile_calls INTEGER NOT NULL, "
            "entered INTEGER NOT NULL, released INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO calls VALUES (1, 0, 0, 0, 0)")
        connection.commit()


def probe_counts(path: Path) -> tuple[int, int]:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT execute_calls, reconcile_calls FROM calls WHERE id = 1"
        ).fetchone()
    if row is None:
        raise RuntimeError("provider probe state is missing")
    return int(row[0]), int(row[1])


def probe_entered(path: Path) -> bool:
    return _probe_flag(path, "entered")


def release_probe(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE calls SET released = 1 WHERE id = 1")
        connection.commit()


def _probe_flag(path: Path, column: str) -> bool:
    query = {"entered": "SELECT entered FROM calls WHERE id = 1"}.get(column)
    if query is None:
        raise ValueError("unsupported probe flag")
    return _probe_flag_value(path, query)


def _probe_flag_value(path: Path, query: str) -> bool:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(query).fetchone()
    return row is not None and int(row[0]) == 1


class ProbeAdapter:
    def __init__(self, config: JsonObject) -> None:
        self._config = config

    def definition_identity(self) -> JsonObject:
        return TypeAdapter(JsonObject).validate_python(
            {"adapter_version": "reconciliation-probe-v1", "mode": self._required("mode")},
            strict=True,
        )

    async def execute(self, command: BaseModel, context: EffectContext) -> EffectOutcome:
        del command
        self._increment("execute_calls")
        mode = self._required("mode")
        if mode == "block":
            return await self._wait_for_release()
        if mode == "takeover":
            self._take_over(context)
        return self._effect_outcome(mode, context)

    async def reconcile(
        self, command: BaseModel, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command
        self._increment("reconcile_calls")
        mode = self._required("mode")
        if mode == "block":
            return await self._block_reconciliation()
        if mode == "takeover":
            self._take_over(context)
        return self._outcome(mode)

    async def _block_reconciliation(self) -> ReconciliationOutcome:
        self._mark_entered()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def _wait_for_release(self) -> EffectOutcome:
        self._mark_entered()
        while not self._is_released():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        return EffectConfirmed(receipt={"provider_receipt": "opaque://provider/probe0001/v1"})

    def _mark_entered(self) -> None:
        self._update_probe("UPDATE calls SET entered = 1 WHERE id = 1")

    def _is_released(self) -> bool:
        path = Path(self._required("probe_path"))
        return _probe_flag_value(path, "SELECT released FROM calls WHERE id = 1")

    def _update_probe(self, query: str) -> None:
        path = Path(self._required("probe_path"))
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(query)
            connection.commit()

    def _take_over(self, context: EffectContext | ReconcileContext) -> None:
        now = datetime.fromisoformat(self._required("now")) + timedelta(minutes=5)
        store = SQLiteKernelStore.open(Path(self._required("kernel_path")), clock=FakeClock(now))
        LeaseService(store).acquire(context.saga_id, "worker-b", timedelta(minutes=5))

    def _outcome(self, mode: str) -> ReconciliationOutcome:
        if mode == "secret_receipt":
            return ReconcileEffectConfirmed(receipt={"api_key": os.environ[_SECRET_ENV]})
        if mode == "exception":
            raise RuntimeError(os.environ[_SECRET_ENV])
        raw = self._config.get("outcome")
        if mode == "malformed":
            malformed = {"kind": "invalid", "secret": os.environ[_SECRET_ENV]}
            return cast(ReconciliationOutcome, malformed)
        return _OUTCOME.validate_python(thaw_json_object(cast(JsonObject, raw)), strict=True)

    def _effect_outcome(self, mode: str, context: EffectContext) -> EffectOutcome:
        if mode == "unknown":
            reference = context.operation_id.removeprefix("op_")
            return OutcomeUnknown(correlation=f"opaque://runtime/{reference}/v1")
        return self._known_effect_outcome(mode)

    def _known_effect_outcome(self, mode: str) -> EffectOutcome:
        outcomes: dict[str, EffectOutcome] = {
            "secret_receipt": EffectConfirmed(receipt={"api_key": os.environ[_SECRET_ENV]}),
            "default": EffectConfirmed(
                receipt={"provider_receipt": "opaque://provider/probe0001/v1"}
            ),
        }
        if mode == "exception":
            raise RuntimeError(os.environ[_SECRET_ENV])
        if mode == "malformed":
            return cast(EffectOutcome, {"kind": "invalid", "secret": os.environ[_SECRET_ENV]})
        if mode == "configured":
            return self._configured_effect_outcome()
        return outcomes.get(mode, outcomes["default"])

    def _configured_effect_outcome(self) -> EffectOutcome:
        raw = cast(JsonObject, self._config.get("effect_outcome"))
        return _EFFECT_OUTCOME.validate_python(thaw_json_object(raw), strict=True)

    def _increment(self, column: str) -> None:
        path = self._config.get("probe_path")
        if not isinstance(path, str):
            return
        queries = {
            "execute_calls": "UPDATE calls SET execute_calls = execute_calls + 1 WHERE id = 1",
            "reconcile_calls": (
                "UPDATE calls SET reconcile_calls = reconcile_calls + 1 WHERE id = 1"
            ),
        }
        query = queries[column]
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(query)
            connection.commit()

    def _required(self, key: str) -> str:
        value = self._config.get(key)
        if not isinstance(value, str):
            raise ValueError("probe adapter config is invalid")
        return value


__all__ = [
    "ProbeAdapter",
    "initialize_probe",
    "probe_counts",
    "probe_entered",
    "release_probe",
]
