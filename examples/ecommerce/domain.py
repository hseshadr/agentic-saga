from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentic_saga.contracts.runtime import SagaResult
from agentic_saga.contracts.trace import RunTrace, TraceEvent


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ScenarioName(StrEnum):
    HAPPY_PATH = "happy-path"
    BUSINESS_FAILURE = "business-failure"
    LOST_RESPONSE = "lost-response"
    COMPENSATION_FAILURE = "compensation-failure"


class CheckInventory(StrictModel):
    sku: str
    quantity: int = Field(strict=True, gt=0)


class InventoryView(StrictModel):
    sku: str
    warehouse_id: str
    available: int = Field(strict=True, ge=0)
    reserved: int = Field(strict=True, ge=0)
    version: int = Field(strict=True, ge=1)


class InspectOrder(StrictModel):
    order_id: str


class OrderView(StrictModel):
    order_id: str
    payment: Literal["open", "captured", "refunded"]
    fulfillment: Literal["pending", "scheduled", "rejected", "cancelled"]


class ReserveInventory(StrictModel):
    order_id: str
    sku: str
    quantity: int = Field(strict=True, gt=0)
    expected_version: int = Field(default=1, strict=True, ge=1)


class ReleaseInventory(StrictModel):
    order_id: str
    sku: str
    quantity: int = Field(strict=True, gt=0)


class ChargePayment(StrictModel):
    order_id: str
    customer_id: str
    amount_minor: int = Field(strict=True, gt=0)
    currency: str


class RefundPayment(StrictModel):
    order_id: str
    customer_id: str
    amount_minor: int = Field(strict=True, gt=0)
    currency: str


class ScheduleFulfillment(StrictModel):
    order_id: str


class CancelFulfillment(StrictModel):
    order_id: str


class ProviderState(StrictModel):
    order_id: str = "order_demo_001"
    customer_id: str = "customer_demo_001"
    sku: str = "sku_travel_pack"
    order_amount_minor: int = Field(default=7900, strict=True, gt=0)
    currency: str = "USD"
    customer_authorized: bool = True
    order_quantity: int = Field(default=1, strict=True, gt=0)
    available: int = 2
    reserved: int = 0
    warehouse_id: str = "primary"
    alternate_warehouse_id: str | None = None
    alternate_available: int = 0
    inventory_version: int = Field(default=1, strict=True, ge=1)
    payment: Literal["open", "captured", "refunded"] = "open"
    fulfillment: Literal["pending", "scheduled", "rejected", "cancelled"] = "pending"
    reject_fulfillment: bool = False
    captured_customer_id: str | None = None
    captured_amount_minor: int | None = Field(default=None, strict=True, gt=0)
    captured_currency: str | None = None
    refunded_amount_minor: int | None = Field(default=None, strict=True, gt=0)


class ProviderFault(StrictModel):
    tool_name: Literal[
        "check_inventory",
        "reserve_inventory",
        "charge_payment",
        "schedule_fulfillment",
        "cancel_fulfillment",
        "refund_payment",
        "release_inventory",
    ]
    mode: Literal[
        "lost_pending",
        "lost_conflict",
        "transient_read",
        "restock",
        "alternate",
        "stale_read",
    ]
    count: int = Field(default=1, strict=True, ge=1, le=5)
    quantity: int = Field(default=0, strict=True, ge=0, le=100)

    @model_validator(mode="after")
    def require_restock_quantity(self) -> ProviderFault:
        if (self.mode == "restock") != (self.quantity > 0):
            raise ValueError("only restock faults require a positive quantity")
        return self


class CapabilityOverride(StrictModel):
    tool_name: Literal[
        "reserve_inventory",
        "charge_payment",
        "schedule_fulfillment",
        "cancel_fulfillment",
        "refund_payment",
        "release_inventory",
    ]
    idempotency_retention_seconds: int = Field(strict=True, gt=0)


@dataclass(frozen=True)
class ProviderCount:
    tool: str
    executes: int
    effects: int
    reconciliations: int


@dataclass(frozen=True)
class EscalationPacket:
    saga_id: str
    reason_code: str
    last_sequence: int
    unresolved_operation_ids: tuple[str, ...]
    recommended_action: str


@dataclass(frozen=True)
class DemoRun:
    scenario: ScenarioName
    result: SagaResult
    trace: RunTrace
    proposals: tuple[str, ...]
    compensation_tools: tuple[str, ...]
    counts: tuple[ProviderCount, ...]
    restarted: bool
    escalation: EscalationPacket | None

    @property
    def timeline_kinds(self) -> tuple[str, ...]:
        return tuple(item.event_type for item in self.trace.events)

    def count(self, tool: str) -> ProviderCount:
        match = next((item for item in self.counts if item.tool == tool), None)
        if match is None:
            return ProviderCount(tool, 0, 0, 0)
        return match

    def evidence_order_is_valid(self) -> bool:
        return _evidence_order_is_valid(self.trace.events)


def _evidence_order_is_valid(events: tuple[TraceEvent, ...]) -> bool:
    return _effects_are_paired(events) and _proof_follows_effects(events)


def _proof_follows_effects(events: tuple[TraceEvent, ...]) -> bool:
    outcomes = _event_sequences(events, "effect_outcome_recorded")
    if not outcomes:
        return False
    proof = _first_sequence(events, "invariant_evaluated")
    terminal = _first_sequence(events, "terminal_assigned")
    return max(outcomes) < proof < terminal


def _event_sequences(events: tuple[TraceEvent, ...], event_type: str) -> tuple[int, ...]:
    return tuple(item.saga_seq for item in events if item.event_type == event_type)


def _first_sequence(events: tuple[TraceEvent, ...], event_type: str) -> int:
    return next(item.saga_seq for item in events if item.event_type == event_type)


def _effects_are_paired(events: tuple[TraceEvent, ...]) -> bool:
    intents = _operation_sequences(events, "intent_recorded")
    outcomes = _operation_sequences(events, "effect_outcome_recorded")
    intent_map = dict(intents)
    outcome_map = dict(outcomes)
    unique = len(intents) == len(intent_map) and len(outcomes) == len(outcome_map)
    return (
        unique
        and intent_map.keys() == outcome_map.keys()
        and all(intent_map[key] < outcome_map[key] for key in intent_map)
    )


def _operation_sequences(
    events: tuple[TraceEvent, ...], event_fragment: str
) -> tuple[tuple[str, int], ...]:
    return tuple(
        (item.operation_id, item.saga_seq)
        for item in events
        if event_fragment in item.event_type and item.operation_id is not None
    )
