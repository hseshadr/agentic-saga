from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from agentic_saga.contracts.clock import Clock
from agentic_saga.contracts.common import JsonObject, canonical_json, thaw_json_object
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    NoEffectConfirmed,
    ReconcileConflict,
    ReconcileEffectConfirmed,
    ReconcileNoEffectConfirmed,
    ReconcilePending,
    ReconciliationOutcome,
)
from agentic_saga.contracts.tools import EffectContext, ReconcileContext
from examples.ecommerce.domain import (
    InventoryView,
    OrderView,
    ProviderCount,
    ProviderFault,
    ProviderState,
    ScenarioName,
)

_JSON: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_EFFECT_TOOLS = (
    "reserve_inventory",
    "charge_payment",
    "schedule_fulfillment",
    "cancel_fulfillment",
    "refund_payment",
    "release_inventory",
)


class FaultMode(StrEnum):
    LOST_PENDING = "lost_pending"
    LOST_CONFLICT = "lost_conflict"


class ResponseLost(RuntimeError):
    """Raised after a durable provider effect when its response is lost."""


class ProviderFenceRejected(RuntimeError):
    """Raised when a stale Saga worker attempts an external effect."""


class BusinessEffectRejected(ValueError):
    """Raised when authoritative business state rejects an effect."""


type _Mutation = Callable[[ProviderState, JsonObject], ProviderState]


@dataclass(frozen=True)
class _Executed:
    outcome: EffectOutcome
    response_lost: bool


@dataclass(frozen=True)
class EcommerceProvider:
    path: Path
    clock: Clock

    @classmethod
    def initialize(cls, path: Path, clock: Clock, scenario: ScenarioName) -> EcommerceProvider:
        state = ProviderState(reject_fulfillment=_rejects_fulfillment(scenario))
        fault = _scenario_fault(scenario)
        faults = () if fault is None else (fault,)
        return cls.initialize_fixture(path, clock, state, faults)

    @classmethod
    def initialize_fixture(
        cls,
        path: Path,
        clock: Clock,
        state: ProviderState,
        faults: tuple[ProviderFault, ...] = (),
    ) -> EcommerceProvider:
        path.parent.mkdir(parents=True, exist_ok=True)
        provider = cls(path, clock)
        provider._create_schema()
        provider._seed(state, faults)
        return provider

    @classmethod
    def open(cls, path: Path, clock: Clock) -> EcommerceProvider:
        if not path.is_file():
            raise FileNotFoundError(path)
        return cls(path, clock)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            _configure(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _create_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)

    def _seed(self, state: ProviderState, faults: tuple[ProviderFault, ...]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT INTO provider_state VALUES (1, ?)", (_encode(state),))
            connection.executemany(
                "INSERT INTO faults VALUES (?, ?, ?, ?)",
                ((fault.tool_name, fault.mode, fault.count, fault.quantity) for fault in faults),
            )

    async def execute(self, tool: str, command: BaseModel, context: EffectContext) -> EffectOutcome:
        return await asyncio.to_thread(self._execute_sync, tool, command, context)

    def _execute_sync(self, tool: str, command: BaseModel, context: EffectContext) -> EffectOutcome:
        payload = _JSON.validate_python(command.model_dump(mode="json"))
        with closing(self._connect()) as connection, connection:
            executed = self._execute_transaction(connection, tool, payload, context)
        if executed.response_lost:
            raise ResponseLost("provider response was not received")
        return executed.outcome

    def _execute_transaction(
        self,
        connection: sqlite3.Connection,
        tool: str,
        command: JsonObject,
        context: EffectContext,
    ) -> _Executed:
        _increment(connection, tool, "execute")
        prior = _prior_receipt(connection, context.operation_id)
        if prior is not None:
            return _Executed(EffectConfirmed(receipt=prior), False)
        _require_fence(connection, _resource(command), context.fence_token)
        outcome = _apply_effect(connection, tool, command, context.operation_id)
        lost = _consume_response_loss(connection, tool)
        return _Executed(outcome, lost)

    async def reconcile(
        self, tool: str, command: BaseModel, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command
        return await asyncio.to_thread(self._reconcile_sync, tool, context)

    def _reconcile_sync(self, tool: str, context: ReconcileContext) -> ReconciliationOutcome:
        with closing(self._connect()) as connection, connection:
            attempt = _increment(connection, tool, "reconcile")
            fault = _fault_mode(connection, tool)
            receipt = _prior_receipt(connection, context.operation_id)
        return _reconciliation(fault, attempt, receipt, self.clock)

    async def read(self, tool: str, command: BaseModel) -> JsonObject:
        payload = _JSON.validate_python(command.model_dump(mode="json"))
        return await asyncio.to_thread(self._read_sync, tool, payload)

    def _read_sync(self, tool: str, command: JsonObject) -> JsonObject:
        with closing(self._connect()) as connection, connection:
            state, unavailable = _prepare_inventory_read(connection, tool, _load_state(connection))
        if unavailable:
            raise BusinessEffectRejected("inventory read is temporarily unavailable")
        readers: dict[str, Callable[[ProviderState, JsonObject], BaseModel]] = {
            "check_inventory": _inventory_view,
            "inspect_order": _order_view,
        }
        result = readers[tool](state, command)
        return _JSON.validate_python(result.model_dump(mode="json"))

    def snapshot(self) -> ProviderState:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT payload FROM provider_state WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("provider state is unavailable")
        return ProviderState.model_validate_json(row["payload"], strict=True)

    def counts(self) -> tuple[ProviderCount, ...]:
        with closing(self._connect()) as connection, connection:
            return tuple(_tool_count(connection, tool) for tool in _EFFECT_TOOLS)


@dataclass(frozen=True)
class ProviderEffectAdapter[CommandT: BaseModel]:
    provider: EcommerceProvider
    tool: str

    def definition_identity(self) -> JsonObject:
        return _JSON.validate_python(
            {"adapter_version": "ecommerce-effect-v1", "tool": self.tool}, strict=True
        )

    async def execute(self, command: CommandT, context: EffectContext) -> EffectOutcome:
        return await self.provider.execute(self.tool, command, context)

    async def reconcile(
        self, command: CommandT, context: ReconcileContext
    ) -> ReconciliationOutcome:
        return await self.provider.reconcile(self.tool, command, context)


@dataclass(frozen=True)
class ProviderReadAdapter[CommandT: BaseModel, ResultT: BaseModel]:
    provider: EcommerceProvider
    tool: str
    result_model: type[ResultT]

    def definition_identity(self) -> JsonObject:
        return _JSON.validate_python(
            {"adapter_version": "ecommerce-read-v1", "tool": self.tool}, strict=True
        )

    async def read(self, command: CommandT) -> ResultT:
        raw = await self.provider.read(self.tool, command)
        return self.result_model.model_validate(thaw_json_object(raw), strict=True)


def _encode(state: ProviderState) -> bytes:
    return state.model_dump_json().encode()


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")


def _rejects_fulfillment(scenario: ScenarioName) -> bool:
    return scenario in {ScenarioName.BUSINESS_FAILURE, ScenarioName.COMPENSATION_FAILURE}


def _scenario_fault(scenario: ScenarioName) -> ProviderFault | None:
    faults = {
        ScenarioName.LOST_RESPONSE: ProviderFault(tool_name="charge_payment", mode="lost_pending"),
        ScenarioName.COMPENSATION_FAILURE: ProviderFault(
            tool_name="refund_payment", mode="lost_conflict"
        ),
    }
    return faults.get(scenario)


def _resource(command: JsonObject) -> str:
    value = command.get("order_id", command.get("sku"))
    if not isinstance(value, str):
        raise BusinessEffectRejected("effect has no stable resource")
    return value


def _require_fence(connection: sqlite3.Connection, resource: str, token: int) -> None:
    row = connection.execute(
        "SELECT fence_token FROM fences WHERE resource_id = ?", (resource,)
    ).fetchone()
    if row is not None and token < row["fence_token"]:
        raise ProviderFenceRejected("stale provider fence")
    connection.execute(
        "INSERT INTO fences VALUES (?, ?) ON CONFLICT(resource_id) DO UPDATE SET fence_token = ?",
        (resource, token, token),
    )


def _apply_effect(
    connection: sqlite3.Connection, tool: str, command: JsonObject, operation_id: str
) -> EffectOutcome:
    state = _load_state(connection)
    try:
        updated = _MUTATIONS[tool](state, command)
    except BusinessEffectRejected:
        return NoEffectConfirmed(reason="authoritative business state rejected the effect")
    receipt = _receipt(tool, operation_id)
    _store_effect(connection, tool, operation_id, updated, receipt)
    return EffectConfirmed(receipt=receipt)


def _load_state(connection: sqlite3.Connection) -> ProviderState:
    row = connection.execute("SELECT payload FROM provider_state WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("provider state is unavailable")
    return ProviderState.model_validate_json(row["payload"], strict=True)


def _store_effect(
    connection: sqlite3.Connection,
    tool: str,
    operation_id: str,
    state: ProviderState,
    receipt: JsonObject,
) -> None:
    connection.execute("UPDATE provider_state SET payload = ? WHERE id = 1", (_encode(state),))
    connection.execute(
        "INSERT INTO effects VALUES (?, ?, ?)", (operation_id, tool, _encode_json(receipt))
    )
    _increment(connection, tool, "effect")


def _receipt(tool: str, operation_id: str) -> JsonObject:
    digest = sha256(f"{tool}\0{operation_id}".encode()).hexdigest()
    return _JSON.validate_python({"receipt_ref": f"opaque://ecommerce/{digest}/v1"})


def _encode_json(value: JsonObject) -> bytes:
    return canonical_json(value)


def _prior_receipt(connection: sqlite3.Connection, operation_id: str) -> JsonObject | None:
    row = connection.execute(
        "SELECT receipt FROM effects WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if row is None:
        return None
    return _JSON.validate_json(row["receipt"], strict=True)


def _increment(connection: sqlite3.Connection, tool: str, phase: str) -> int:
    connection.execute(
        "INSERT INTO calls VALUES (?, ?, 1) ON CONFLICT(tool, phase) DO UPDATE SET count=count+1",
        (tool, phase),
    )
    row = connection.execute(
        "SELECT count FROM calls WHERE tool = ? AND phase = ?", (tool, phase)
    ).fetchone()
    if row is None:
        raise RuntimeError("provider call count was not recorded")
    return int(row["count"])


def _consume_response_loss(connection: sqlite3.Connection, tool: str) -> bool:
    mode = _fault_mode(connection, tool)
    return mode is not None and _fault_step(connection, tool, mode.value) is not None


def _fault_mode(connection: sqlite3.Connection, tool: str) -> FaultMode | None:
    row = connection.execute(
        "SELECT mode FROM faults WHERE tool = ? AND mode IN (?, ?)",
        (tool, FaultMode.LOST_PENDING.value, FaultMode.LOST_CONFLICT.value),
    ).fetchone()
    return None if row is None else FaultMode(row["mode"])


def _fault_step(connection: sqlite3.Connection, tool: str, mode: str) -> tuple[int, int] | None:
    row = connection.execute(
        "SELECT remaining, quantity FROM faults WHERE tool = ? AND mode = ?", (tool, mode)
    ).fetchone()
    if row is None or row["remaining"] == 0:
        return None
    remaining, quantity = int(row["remaining"]), int(row["quantity"])
    connection.execute(
        "UPDATE faults SET remaining = ? WHERE tool = ? AND mode = ?",
        (remaining - 1, tool, mode),
    )
    return remaining, quantity


def _reconciliation(
    fault: FaultMode | None,
    attempt: int,
    receipt: JsonObject | None,
    clock: Clock,
) -> ReconciliationOutcome:
    if fault is FaultMode.LOST_CONFLICT:
        return ReconcileConflict(reason="provider evidence cannot verify the refund")
    if fault is FaultMode.LOST_PENDING and attempt == 1:
        return _pending_reconciliation(clock)
    if receipt is None:
        return ReconcileNoEffectConfirmed(reason="no provider effect exists")
    return ReconcileEffectConfirmed(receipt=receipt)


def _pending_reconciliation(clock: Clock) -> ReconcilePending:
    return ReconcilePending(
        correlation="opaque://ecommerce/pending123/v1",
        check_after=clock.now() + timedelta(seconds=1),
    )


def _inventory_view(state: ProviderState, command: JsonObject) -> InventoryView:
    if command.get("sku") != state.sku:
        raise BusinessEffectRejected("unknown inventory item")
    return InventoryView(
        sku=state.sku,
        warehouse_id=state.warehouse_id,
        available=state.available,
        reserved=state.reserved,
        version=state.inventory_version,
    )


def _order_view(state: ProviderState, command: JsonObject) -> OrderView:
    if command.get("order_id") != state.order_id:
        raise BusinessEffectRejected("unknown order")
    return OrderView(order_id=state.order_id, payment=state.payment, fulfillment=state.fulfillment)


def _reserve(state: ProviderState, command: JsonObject) -> ProviderState:
    quantity = _quantity(command)
    if command.get("sku") != state.sku or quantity > state.available:
        raise BusinessEffectRejected("inventory is unavailable")
    return state.model_copy(
        update={
            "available": state.available - quantity,
            "reserved": state.reserved + quantity,
            "inventory_version": state.inventory_version + 1,
        }
    )


def _release(state: ProviderState, command: JsonObject) -> ProviderState:
    quantity = _quantity(command)
    if command.get("sku") != state.sku or quantity > state.reserved:
        raise BusinessEffectRejected("reservation is unavailable")
    return state.model_copy(
        update={
            "available": state.available + quantity,
            "reserved": state.reserved - quantity,
            "inventory_version": state.inventory_version + 1,
        }
    )


def _prepare_inventory_read(
    connection: sqlite3.Connection, tool: str, state: ProviderState
) -> tuple[ProviderState, bool]:
    if tool != "check_inventory":
        return state, False
    _increment(connection, tool, "read")
    current = _apply_inventory_change(connection, state)
    if _fault_step(connection, tool, "transient_read") is not None:
        return current, True
    if _fault_step(connection, tool, "stale_read") is not None:
        current = current.model_copy(update={"inventory_version": current.inventory_version - 1})
    return current, False


def _apply_inventory_change(connection: sqlite3.Connection, state: ProviderState) -> ProviderState:
    restock = _fault_step(connection, "check_inventory", "restock")
    alternate = _fault_step(connection, "check_inventory", "alternate")
    if restock is None and alternate is None:
        return state
    update = _inventory_change(state, restock, alternate)
    updated = state.model_copy(update=update)
    connection.execute("UPDATE provider_state SET payload = ? WHERE id = 1", (_encode(updated),))
    return updated


def _inventory_change(
    state: ProviderState,
    restock: tuple[int, int] | None,
    alternate: tuple[int, int] | None,
) -> dict[str, object]:
    if alternate is not None and alternate[0] == 1:
        return {
            "warehouse_id": state.alternate_warehouse_id,
            "available": state.alternate_available,
            "inventory_version": state.inventory_version + 1,
        }
    quantity = 0 if restock is None or restock[0] != 1 else restock[1]
    return {
        "available": state.available + quantity,
        "inventory_version": state.inventory_version + 1,
    }


def _charge(state: ProviderState, command: JsonObject) -> ProviderState:
    _require_payment_terms(state, command)
    if state.payment != "open":
        raise BusinessEffectRejected("payment is not open")
    return state.model_copy(update=_captured_payment(command))


def _refund(state: ProviderState, command: JsonObject) -> ProviderState:
    _require_refund_terms(state, command)
    if state.payment != "captured":
        raise BusinessEffectRejected("payment is not captured")
    return state.model_copy(
        update={"payment": "refunded", "refunded_amount_minor": command["amount_minor"]}
    )


def _require_payment_terms(state: ProviderState, command: JsonObject) -> None:
    expected = (state.order_id, state.customer_id, state.order_amount_minor, state.currency)
    if not state.customer_authorized or _payment_terms(command) != expected:
        raise BusinessEffectRejected("payment terms are not authorized")


def _require_refund_terms(state: ProviderState, command: JsonObject) -> None:
    expected = (
        state.order_id,
        state.captured_customer_id,
        state.captured_amount_minor,
        state.captured_currency,
    )
    if _payment_terms(command) != expected:
        raise BusinessEffectRejected("refund does not match the captured payment")


def _payment_terms(command: JsonObject) -> tuple[object, ...]:
    keys = ("order_id", "customer_id", "amount_minor", "currency")
    return tuple(command.get(key) for key in keys)


def _captured_payment(command: JsonObject) -> dict[str, object]:
    return {
        "payment": "captured",
        "captured_customer_id": command["customer_id"],
        "captured_amount_minor": command["amount_minor"],
        "captured_currency": command["currency"],
    }


def _schedule(state: ProviderState, command: JsonObject) -> ProviderState:
    _require_order(state, command)
    status = "rejected" if state.reject_fulfillment else "scheduled"
    return state.model_copy(update={"fulfillment": status})


def _cancel(state: ProviderState, command: JsonObject) -> ProviderState:
    _require_order(state, command)
    if state.fulfillment not in {"scheduled", "rejected"}:
        raise BusinessEffectRejected("fulfillment is not cancellable")
    return state.model_copy(update={"fulfillment": "cancelled"})


def _require_order(state: ProviderState, command: JsonObject) -> None:
    if command.get("order_id") != state.order_id:
        raise BusinessEffectRejected("unknown order")


def _quantity(command: JsonObject) -> int:
    value = command.get("quantity")
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessEffectRejected("quantity is invalid")
    return value


_MUTATIONS: dict[str, _Mutation] = {
    "reserve_inventory": _reserve,
    "release_inventory": _release,
    "charge_payment": _charge,
    "refund_payment": _refund,
    "schedule_fulfillment": _schedule,
    "cancel_fulfillment": _cancel,
}


def _tool_count(connection: sqlite3.Connection, tool: str) -> ProviderCount:
    values = tuple(_count(connection, tool, phase) for phase in ("execute", "effect", "reconcile"))
    return ProviderCount(tool, *values)


def _count(connection: sqlite3.Connection, tool: str, phase: str) -> int:
    row = connection.execute(
        "SELECT count FROM calls WHERE tool = ? AND phase = ?", (tool, phase)
    ).fetchone()
    return 0 if row is None else int(row["count"])


_SCHEMA = """
CREATE TABLE provider_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload BLOB NOT NULL
);
CREATE TABLE effects (
    operation_id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    receipt BLOB NOT NULL
);
CREATE TABLE calls (
    tool TEXT NOT NULL,
    phase TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (tool, phase)
);
CREATE TABLE faults (
    tool TEXT NOT NULL,
    mode TEXT NOT NULL,
    remaining INTEGER NOT NULL CHECK (remaining >= 0),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    PRIMARY KEY (tool, mode)
);
CREATE TABLE fences (
    resource_id TEXT PRIMARY KEY,
    fence_token INTEGER NOT NULL
);
"""
