from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
