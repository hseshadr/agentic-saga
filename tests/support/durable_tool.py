from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import cast

from pydantic import BaseModel, TypeAdapter

from agentic_saga.contracts.common import JsonObject, canonical_json, sha256_json, thaw_json_object
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    NoEffectConfirmed,
    PartialEffectConfirmed,
    ReconcileConflict,
    ReconcileEffectConfirmed,
    ReconcileNoEffectConfirmed,
    ReconcilePending,
    ReconcileUnsupported,
    ReconciliationOutcome,
)
from agentic_saga.contracts.redaction import RedactionPolicy, redact_json
from agentic_saga.contracts.tools import EffectContext, ReconcileContext
from agentic_saga.kernel.failpoints import (
    DurabilityFailpoint,
    DurabilityPoint,
    NoOpDurabilityFailpoint,
)

_REDACTION = RedactionPolicy()
_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_RECEIPTS_ADAPTER: TypeAdapter[tuple[JsonObject, ...]] = TypeAdapter(tuple[JsonObject, ...])
_MUTABLE_STATE = frozenset(
    {
        "lose_response",
        "reconciliation_mode",
        "pending_check_after",
        "fault_mode",
        "compensation_failed",
        "block_released",
        "fencing_supported",
    }
)


class DurableToolIdentityConflict(RuntimeError):
    """Raised when one provider identity is reused for different command bytes."""


class DurableResponseLost(RuntimeError):
    """Test-only transport loss raised after a durable provider effect."""


class DurableStaleFence(RuntimeError):
    """Raised when a provider resource observes an obsolete fence."""


class DurableToolFault(StrEnum):
    NONE = "none"
    NO_EFFECT_FAILURE = "no_effect_failure"
    PARTIAL_EFFECT = "partial_effect"
    EFFECT_THEN_LOSE_RESPONSE = "effect_then_lose_response"
    BLOCK_AFTER_EFFECT = "block_after_effect"
    COMPENSATION_FAILURE_ONCE = "compensation_failure_once"


@dataclass(frozen=True)
class DurableToolCall:
    tool_name: str
    saga_id: str
    operation_id: str
    command_hash: str
    resource_ref: str
    receipt_ref: str | None
    fence_token: int
    delivery_attempt: int
    forward_receipts: tuple[JsonObject, ...]


@dataclass(frozen=True, order=True)
class DurableToolEffect:
    tool_name: str
    operation_id: str
    command_hash: str
    receipt_ref: str


def _durable_call(raw: object) -> DurableToolCall:
    row = cast(tuple[str, str, str, str, str, str | None, int, int, bytes], raw)
    receipts = _RECEIPTS_ADAPTER.validate_json(row[8], strict=True)
    return DurableToolCall(*row[:8], receipts)


def _call_values(
    tool_name: str, context: EffectContext, command_hash: str, resource_id: str
) -> tuple[str, str, str, str, str, None, int, int, bytes]:
    receipts = canonical_json([thaw_json_object(item) for item in context.forward_receipts])
    return (
        tool_name,
        context.saga_id,
        context.operation_id,
        command_hash,
        resource_id,
        None,
        context.fence_token,
        context.delivery_attempt,
        receipts,
    )


@contextmanager
def _immediate(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 5000")


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    _configure(connection)
    return connection


def _initialize_schema(connection: sqlite3.Connection, tool_name: str) -> None:
    _initialize_effects(connection)
    _initialize_tool_state(connection, tool_name)
    _initialize_calls(connection)
    _initialize_resource_fences(connection)
    _initialize_resource_inventory(connection)


def _initialize_effects(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE effects (tool_name TEXT NOT NULL, operation_id TEXT NOT NULL, "
        "command_hash TEXT NOT NULL, receipt_ref TEXT NOT NULL, fence_token INTEGER NOT NULL, "
        "PRIMARY KEY (tool_name, operation_id))"
    )


def _initialize_tool_state(connection: sqlite3.Connection, tool_name: str) -> None:
    connection.execute(
        "CREATE TABLE tool_state (tool_name TEXT PRIMARY KEY, execute_calls INTEGER NOT NULL, "
        "reconcile_calls INTEGER NOT NULL, lose_response INTEGER NOT NULL, "
        "reconciliation_mode TEXT NOT NULL, pending_check_after TEXT, fault_mode TEXT NOT NULL, "
        "compensation_failed INTEGER NOT NULL, block_released INTEGER NOT NULL, "
        "fencing_supported INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO tool_state VALUES (?, 0, 0, 0, 'reconcilable', NULL, 'none', 0, 0, 1)",
        (tool_name,),
    )


def _initialize_resource_fences(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE resource_fences (tool_name TEXT NOT NULL, resource_id TEXT NOT NULL, "
        "highest_fence INTEGER NOT NULL, PRIMARY KEY (tool_name, resource_id))"
    )


def _initialize_resource_inventory(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE resource_inventory (resource_id TEXT PRIMARY KEY, version INTEGER NOT NULL, "
        "remaining INTEGER NOT NULL)"
    )


def _initialize_calls(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE calls (call_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "tool_name TEXT NOT NULL, saga_id TEXT NOT NULL, operation_id TEXT NOT NULL, "
        "command_hash TEXT NOT NULL, resource_ref TEXT NOT NULL, receipt_ref TEXT, "
        "fence_token INTEGER NOT NULL, "
        "delivery_attempt INTEGER NOT NULL, "
        "forward_receipts_json BLOB NOT NULL)"
    )


def _state_query(column: str) -> str | None:
    queries = {
        "execute_calls": "SELECT execute_calls FROM tool_state WHERE tool_name = ?",
        "reconcile_calls": "SELECT reconcile_calls FROM tool_state WHERE tool_name = ?",
        "lose_response": "SELECT lose_response FROM tool_state WHERE tool_name = ?",
        "reconciliation_mode": "SELECT reconciliation_mode FROM tool_state WHERE tool_name = ?",
        "pending_check_after": "SELECT pending_check_after FROM tool_state WHERE tool_name = ?",
        "fault_mode": "SELECT fault_mode FROM tool_state WHERE tool_name = ?",
        "compensation_failed": "SELECT compensation_failed FROM tool_state WHERE tool_name = ?",
        "block_released": "SELECT block_released FROM tool_state WHERE tool_name = ?",
        "fencing_supported": "SELECT fencing_supported FROM tool_state WHERE tool_name = ?",
    }
    return queries.get(column)


def _receipt_ref(tool_name: str, operation_id: str) -> str:
    material = f"{tool_name}\0{operation_id}".encode()
    return f"opaque://durable-fake/{sha256(material).hexdigest()}/v1"


def _receipt(reference: str) -> EffectConfirmed:
    return EffectConfirmed(receipt={"provider_receipt": reference})


def _public_command_hash(command: BaseModel) -> str:
    public_command = redact_json(command.model_dump(mode="json"), _REDACTION)
    if not isinstance(public_command, dict):
        raise ValueError("fake-tool command must serialize to a JSON object")
    return sha256_json(public_command)


def _resource_id(command: BaseModel, context: EffectContext) -> str:
    raw = command.model_dump(mode="json")
    resource_id = raw.get("resource_id") if isinstance(raw, dict) else None
    if isinstance(resource_id, str):
        if redact_json(resource_id, _REDACTION) != resource_id:
            raise ValueError("provider resource reference must be public")
        return resource_id
    return context.saga_id


def _expected_resource_version(command: BaseModel) -> int | None:
    raw = command.model_dump(mode="json")
    value = raw.get("expected_provider_version") if isinstance(raw, dict) else None
    return value if isinstance(value, int) else None


def _enforce_resource_fence(
    connection: sqlite3.Connection, tool_name: str, resource_id: str, fence_token: int
) -> None:
    row = connection.execute(
        "SELECT highest_fence FROM resource_fences WHERE tool_name = ? AND resource_id = ?",
        (tool_name, resource_id),
    ).fetchone()
    if row is not None and int(row[0]) > fence_token:
        raise DurableStaleFence("provider resource fence is stale")
    connection.execute(
        "INSERT INTO resource_fences VALUES (?, ?, ?) ON CONFLICT(tool_name, resource_id) "
        "DO UPDATE SET highest_fence = MAX(highest_fence, excluded.highest_fence)",
        (tool_name, resource_id, fence_token),
    )


def _reserve_inventory(connection: sqlite3.Connection, resource_id: str, version: int) -> bool:
    cursor = connection.execute(
        "UPDATE resource_inventory SET remaining = remaining - 1, version = version + 1 "
        "WHERE resource_id = ? AND version = ? AND remaining > 0",
        (resource_id, version),
    )
    return cursor.rowcount == 1


def _version_allows(
    connection: sqlite3.Connection, resource_id: str, expected_version: int | None
) -> bool:
    return expected_version is None or _reserve_inventory(connection, resource_id, expected_version)


def _enforce_fence_if_supported(
    connection: sqlite3.Connection,
    tool_name: str,
    resource_id: str,
    fence_token: int,
    supported: bool,
) -> None:
    if supported:
        _enforce_resource_fence(connection, tool_name, resource_id, fence_token)


class DurableFakeTool:
    """SQLite-backed fake whose durable uniqueness models provider deduplication."""

    def __init__(
        self, path: Path, tool_name: str, failpoint: DurabilityFailpoint | None = None
    ) -> None:
        self._path = path.absolute()
        self._tool_name = tool_name
        self._failpoint = failpoint or NoOpDurabilityFailpoint()
        self._effect_entered: asyncio.Event | None = None
        self._effect_release: asyncio.Event | None = None

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python(
            {"adapter_version": "durable-fake-v1", "tool_name": self._tool_name}, strict=True
        )

    @classmethod
    def initialize(
        cls, path: Path, tool_name: str, failpoint: DurabilityFailpoint | None = None
    ) -> DurableFakeTool:
        target = path.absolute()
        with closing(_connect(target)) as connection:
            _initialize_schema(connection, tool_name)
        return cls(target, tool_name, failpoint)

    @classmethod
    def open(
        cls, path: Path, tool_name: str, failpoint: DurabilityFailpoint | None = None
    ) -> DurableFakeTool:
        return cls(path, tool_name, failpoint)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def calls(self) -> tuple[DurableToolCall, ...]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT tool_name, saga_id, operation_id, command_hash, resource_ref, "
                "receipt_ref, fence_token, delivery_attempt, forward_receipts_json "
                "FROM calls WHERE tool_name = ? "
                "ORDER BY call_id",
                (self._tool_name,),
            ).fetchall()
        return tuple(_durable_call(row) for row in rows)

    @property
    def effects(self) -> tuple[DurableToolEffect, ...]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT tool_name, operation_id, command_hash, receipt_ref FROM effects "
                "WHERE tool_name = ? ORDER BY operation_id",
                (self._tool_name,),
            ).fetchall()
        return tuple(DurableToolEffect(*cast(tuple[str, str, str, str], row)) for row in rows)

    @property
    def accepted_fences(self) -> tuple[int, ...]:
        with closing(_connect(self._path)) as connection:
            rows = connection.execute(
                "SELECT fence_token FROM effects WHERE tool_name = ? ORDER BY rowid",
                (self._tool_name,),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def blocking_barrier(self) -> tuple[asyncio.Event, asyncio.Event]:
        entered = asyncio.Event()
        released = asyncio.Event()
        self._effect_entered = entered
        self._effect_release = released
        return entered, released

    async def execute(self, command: BaseModel, context: EffectContext) -> EffectOutcome:
        command_hash = _public_command_hash(command)
        resource_id = _resource_id(command, context)
        call_id = self._record_attempt(context, command_hash, resource_id)
        fault = DurableToolFault(str(self._state_value("fault_mode")))
        self._raise_pre_effect_failure(fault, context)
        await self._wait_at_effect_barrier()
        expected_version = _expected_resource_version(command)
        reference = self._execute_once(
            context, command_hash, resource_id, self.fencing_supported, expected_version
        )
        if reference is None:
            return NoEffectConfirmed(reason="provider resource version conflict")
        self._effect_checkpoint(context)
        self._record_call(call_id, reference)
        return await self._post_effect_outcome(fault, context, reference)

    async def _wait_at_effect_barrier(self) -> None:
        entered = self._effect_entered
        released = self._effect_release
        if entered is None or released is None:
            return
        self._effect_entered = None
        self._effect_release = None
        entered.set()
        await released.wait()

    def _effect_checkpoint(self, context: EffectContext) -> None:
        point = DurabilityPoint.AFTER_PROVIDER_EFFECT
        if context.forward_receipts:
            point = DurabilityPoint.AFTER_COMPENSATION_EFFECT
        self._failpoint.hit(point)

    def _raise_pre_effect_failure(self, fault: DurableToolFault, context: EffectContext) -> None:
        if fault is DurableToolFault.NO_EFFECT_FAILURE:
            raise RuntimeError("durable fake failed before effect")
        if self._should_fail_compensation(fault, context):
            raise RuntimeError("transient compensation failure")
        self._raise_transient_failure()

    def _raise_transient_failure(self) -> None:
        if self._state_value("reconciliation_mode") != "transient_once":
            return
        self._set_state("reconciliation_mode", "reconcilable")
        raise RuntimeError("transient fake failure before effect")

    async def _post_effect_outcome(
        self, fault: DurableToolFault, context: EffectContext, reference: str
    ) -> EffectOutcome:
        if self._response_is_lost(fault):
            raise DurableResponseLost("durable provider response unavailable")
        if fault is DurableToolFault.BLOCK_AFTER_EFFECT:
            await self._wait_for_release()
        if fault is DurableToolFault.PARTIAL_EFFECT:
            return PartialEffectConfirmed(receipts=self.partial_receipts(context.operation_id))
        return _receipt(reference)

    async def reconcile(
        self, command: BaseModel, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command
        self._increment("reconcile_calls")
        return self._reconciliation_outcome(context)

    @property
    def execute_call_count(self) -> int:
        return self._int_state_value("execute_calls")

    @property
    def reconcile_call_count(self) -> int:
        return self._int_state_value("reconcile_calls")

    @property
    def reconciliation_enabled(self) -> bool:
        return self._state_value("reconciliation_mode") != "unsupported"

    @property
    def deduplication_enabled(self) -> bool:
        return self._state_value("reconciliation_mode") != "unsupported"

    @property
    def fencing_supported(self) -> bool:
        return self._int_state_value("fencing_supported") == 1

    def enable_response_loss_after_effect(self) -> None:
        self._set_state("lose_response", 1)

    def disable_response_loss(self) -> None:
        self._set_state("lose_response", 0)

    def disable_reconciliation_and_deduplication(self) -> None:
        self._set_state("reconciliation_mode", "unsupported")

    def set_fencing_supported(self, enabled: bool) -> None:
        self._set_state("fencing_supported", int(enabled))

    def set_resource_inventory(self, resource_id: str, remaining: int) -> None:
        with closing(_connect(self._path)) as connection, _immediate(connection):
            connection.execute(
                "INSERT INTO resource_inventory VALUES (?, 0, ?) ON CONFLICT(resource_id) "
                "DO UPDATE SET version = 0, remaining = excluded.remaining",
                (resource_id, remaining),
            )

    def set_reconciliation_pending(self, check_after: datetime) -> None:
        if check_after.utcoffset() != UTC.utcoffset(check_after):
            raise ValueError("pending time must use UTC")
        self._set_state("reconciliation_mode", "pending")
        self._set_state("pending_check_after", check_after.isoformat())

    def set_reconciliation_conflict(self) -> None:
        self._set_state("reconciliation_mode", "conflict")

    def set_reconciliation_no_effect(self) -> None:
        self._set_state("reconciliation_mode", "no_effect")

    def set_reconciliation_confirmed(self) -> None:
        self._set_state("reconciliation_mode", "reconcilable")

    def fail_transiently_once(self) -> None:
        self._set_state("reconciliation_mode", "transient_once")

    def set_fault(self, fault: DurableToolFault) -> None:
        self._set_state("fault_mode", fault.value)
        self._set_state("compensation_failed", 0)
        self._set_state("block_released", 0)

    def release_blocked_effect(self) -> None:
        self._set_state("block_released", 1)

    def partial_receipts(self, operation_id: str) -> tuple[JsonObject, ...]:
        prefix = _receipt_ref(self._tool_name, operation_id)
        raw = ({"provider_receipt": f"{prefix}/part/1"}, {"provider_receipt": f"{prefix}/part/2"})
        return _RECEIPTS_ADAPTER.validate_python(raw, strict=True)

    def effect_count(self, operation_id: str) -> int:
        with closing(_connect(self._path)) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM effects WHERE tool_name = ? AND operation_id = ?",
                (self._tool_name, operation_id),
            ).fetchone()
        return 0 if row is None else int(row[0])

    def durable_bytes(self) -> bytes:
        files = sorted(self._path.parent.glob(f"{self._path.name}*"))
        return b"".join(path.read_bytes() for path in files if path.is_file())

    def _execute_once(
        self,
        context: EffectContext,
        command_hash: str,
        resource_id: str,
        fenced: bool,
        expected_version: int | None,
    ) -> str | None:
        with closing(_connect(self._path)) as connection, _immediate(connection):
            _enforce_fence_if_supported(
                connection, self._tool_name, resource_id, context.fence_token, fenced
            )
            stored = self._stored_effect(connection, context.operation_id)
            if stored is not None:
                return self._deduplicated(stored, command_hash)
            if not _version_allows(connection, resource_id, expected_version):
                return None
            reference = _receipt_ref(self._tool_name, context.operation_id)
            self._insert_effect(
                connection, context.operation_id, command_hash, reference, context.fence_token
            )
            return reference

    def _should_fail_compensation(self, fault: DurableToolFault, context: EffectContext) -> bool:
        if fault is not DurableToolFault.COMPENSATION_FAILURE_ONCE:
            return False
        if not context.forward_receipts:
            return False
        return self._consume_compensation_failure()

    def _consume_compensation_failure(self) -> bool:
        with closing(_connect(self._path)) as connection, _immediate(connection):
            row = connection.execute(
                "SELECT compensation_failed FROM tool_state WHERE tool_name = ?",
                (self._tool_name,),
            ).fetchone()
            if row is None or int(row[0]) != 0:
                return False
            connection.execute(
                "UPDATE tool_state SET compensation_failed = 1 WHERE tool_name = ?",
                (self._tool_name,),
            )
        return True

    def _response_is_lost(self, fault: DurableToolFault) -> bool:
        return fault is DurableToolFault.EFFECT_THEN_LOSE_RESPONSE or (
            self._state_value("lose_response") == 1
        )

    async def _wait_for_release(self) -> None:
        while self._state_value("block_released") != 1:  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    def _stored_effect(
        self, connection: sqlite3.Connection, operation_id: str
    ) -> tuple[str, str] | None:
        row = connection.execute(
            "SELECT command_hash, receipt_ref FROM effects "
            "WHERE tool_name = ? AND operation_id = ?",
            (self._tool_name, operation_id),
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    @staticmethod
    def _deduplicated(stored: tuple[str, str], command_hash: str) -> str:
        if stored[0] != command_hash:
            raise DurableToolIdentityConflict("provider operation identity conflict")
        return stored[1]

    def _insert_effect(
        self,
        connection: sqlite3.Connection,
        operation_id: str,
        command_hash: str,
        reference: str,
        fence_token: int,
    ) -> None:
        connection.execute(
            "INSERT INTO effects (tool_name, operation_id, command_hash, receipt_ref, fence_token) "
            "VALUES (?, ?, ?, ?, ?)",
            (self._tool_name, operation_id, command_hash, reference, fence_token),
        )

    def _record_attempt(self, context: EffectContext, command_hash: str, resource_id: str) -> int:
        values = _call_values(self._tool_name, context, command_hash, resource_id)
        with closing(_connect(self._path)) as connection, _immediate(connection):
            connection.execute(
                "UPDATE tool_state SET execute_calls = execute_calls + 1 WHERE tool_name = ?",
                (self._tool_name,),
            )
            cursor = connection.execute(
                "INSERT INTO calls (tool_name, saga_id, operation_id, command_hash, "
                "resource_ref, receipt_ref, fence_token, delivery_attempt, "
                "forward_receipts_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return cast(int, cursor.lastrowid)

    def _record_call(self, call_id: int, receipt_ref: str) -> None:
        with closing(_connect(self._path)) as connection, _immediate(connection):
            connection.execute(
                "UPDATE calls SET receipt_ref = ? WHERE call_id = ?",
                (receipt_ref, call_id),
            )

    def _find_receipt(self, operation_id: str) -> str | None:
        with closing(_connect(self._path)) as connection:
            stored = self._stored_effect(connection, operation_id)
        return None if stored is None else stored[1]

    def _state_value(self, column: str) -> object:
        query = _state_query(column)
        if query is None:
            raise ValueError("unsupported fake-tool state column")
        with closing(_connect(self._path)) as connection:
            row = connection.execute(query, (self._tool_name,)).fetchone()
        if row is None:
            raise RuntimeError("fake-tool state is missing")
        return row[0]

    def _set_state(self, column: str, value: object) -> None:
        if column not in _MUTABLE_STATE:
            raise ValueError("unsupported fake-tool state column")
        with closing(_connect(self._path)) as connection, _immediate(connection):
            connection.execute(
                f"UPDATE tool_state SET {column} = ? WHERE tool_name = ?",  # noqa: S608
                (value, self._tool_name),
            )

    def _increment(self, column: str) -> None:
        if column not in {"execute_calls", "reconcile_calls"}:
            raise ValueError("unsupported fake-tool counter")
        with closing(_connect(self._path)) as connection, _immediate(connection):
            connection.execute(
                f"UPDATE tool_state SET {column} = {column} + 1 WHERE tool_name = ?",  # noqa: S608
                (self._tool_name,),
            )

    def _int_state_value(self, column: str) -> int:
        value = self._state_value(column)
        if not isinstance(value, int):
            raise RuntimeError("fake-tool counter is not an integer")
        return value

    def _reconciliation_outcome(self, context: ReconcileContext) -> ReconciliationOutcome:
        mode = str(self._state_value("reconciliation_mode"))
        if mode == "pending":
            return self._pending_outcome(context)
        fixed = _fixed_reconciliation_outcome(mode)
        if fixed is not None:
            return fixed
        reference = self._find_receipt(context.operation_id)
        if reference is None:
            return ReconcileNoEffectConfirmed(reason="provider confirmed no effect")
        return ReconcileEffectConfirmed(receipt={"provider_receipt": reference})

    def _pending_outcome(self, context: ReconcileContext) -> ReconciliationOutcome:
        raw = self._state_value("pending_check_after")
        if not isinstance(raw, str):
            return ReconcileConflict(reason="missing pending time")
        return ReconcilePending(
            correlation=context.correlation,
            check_after=datetime.fromisoformat(raw),
        )


def _fixed_reconciliation_outcome(mode: str) -> ReconciliationOutcome | None:
    outcomes: dict[str, ReconciliationOutcome] = {
        "conflict": ReconcileConflict(reason="provider evidence conflict"),
        "unsupported": ReconcileUnsupported(reason="provider lookup unsupported"),
        "no_effect": ReconcileNoEffectConfirmed(reason="provider confirmed no effect"),
    }
    return outcomes.get(mode)


def public_receipt(outcome: EffectOutcome) -> JsonObject:
    if not isinstance(outcome, EffectConfirmed):
        raise AssertionError("expected confirmed fake-tool outcome")
    return outcome.receipt


class ReceiptCheckingDurableFakeTool(DurableFakeTool):
    def __init__(
        self, path: Path, tool_name: str, expected_receipts: tuple[JsonObject, ...]
    ) -> None:
        super().__init__(path, tool_name)
        self._expected_receipts = expected_receipts

    async def reconcile(
        self, command: BaseModel, context: ReconcileContext
    ) -> ReconciliationOutcome:
        if context.forward_receipts != self._expected_receipts:
            return ReconcileConflict(reason="forward receipt mismatch")
        return await super().reconcile(command, context)


__all__ = [
    "DurableFakeTool",
    "DurableResponseLost",
    "DurableStaleFence",
    "DurableToolCall",
    "DurableToolFault",
    "DurableToolIdentityConflict",
    "ReceiptCheckingDurableFakeTool",
    "public_receipt",
]
