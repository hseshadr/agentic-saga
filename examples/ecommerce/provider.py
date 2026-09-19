from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256

from pydantic import BaseModel, TypeAdapter
from temporalio.exceptions import ApplicationError

from agentic_saga.contracts.common import JsonObject, Reversibility
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    NoEffectConfirmed,
    ReconcileEffectConfirmed,
    ReconcileNoEffectConfirmed,
    ReconcileUnsupported,
    ReconciliationOutcome,
)
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ReadToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
)
from examples.ecommerce.domain import (
    CancelFulfillment,
    ChargePayment,
    InspectOrder,
    ProviderCount,
    ProviderState,
    RefundPayment,
    ReleaseInventory,
    ReserveInventory,
    ScenarioName,
    ScheduleFulfillment,
    StrictModel,
)

_JSON: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_COMPENSATIONS = frozenset({"cancel_fulfillment", "refund_payment", "release_inventory"})


class OrderVerification(StrictModel):
    verified: bool


@dataclass
class _Count:
    executes: int = 0
    effects: int = 0
    reconciliations: int = 0


class EcommerceProvider:
    """Small idempotent provider simulation with authoritative business rules."""

    def __init__(self, scenario: ScenarioName) -> None:
        self.scenario = scenario
        self.state = ProviderState()
        self.events: list[str] = []
        self._counts: dict[str, _Count] = {}
        self._receipts: dict[str, JsonObject] = {}

    async def execute(self, tool: str, command: BaseModel, context: EffectContext) -> EffectOutcome:
        self._record(tool, "execute")
        prior = self._receipts.get(context.operation_id)
        if prior is not None:
            return self._repeat_result(tool, prior)
        if not _command_allowed(tool, command, self.state):
            return NoEffectConfirmed(reason="authoritative business rule rejected command")
        receipt = _receipt(tool, context.operation_id)
        self._commit(tool, command, context.operation_id, receipt)
        return self._repeat_result(tool, receipt)

    async def read(self, tool: str, command: BaseModel) -> BaseModel:
        self._record(tool, "read")
        verified = command == InspectOrder(order_id=self.state.order_id)
        return OrderVerification(verified=verified and self.state.fulfillment == "scheduled")

    async def reconcile(
        self, tool: str, command: BaseModel, context: ReconcileContext
    ) -> ReconciliationOutcome:
        self._count(tool).reconciliations += 1
        self.events.append(f"reconcile:{tool}")
        if self.scenario is ScenarioName.COMPENSATION_FAILURE and tool == "refund_payment":
            return ReconcileUnsupported(reason="refund evidence is not authoritative")
        receipt = self._receipts.get(context.operation_id)
        if receipt is None or not _command_matches(tool, command, self.state):
            return ReconcileNoEffectConfirmed(reason="provider has no matching effect")
        return ReconcileEffectConfirmed(receipt=receipt)

    def counts(self) -> tuple[ProviderCount, ...]:
        return tuple(
            ProviderCount(name, item.executes, item.effects, item.reconciliations)
            for name, item in sorted(self._counts.items())
        )

    def compensation_order(self) -> tuple[str, ...]:
        return tuple(
            event.removeprefix("effect:")
            for event in self.events
            if event.removeprefix("effect:") in _COMPENSATIONS
        )

    def _repeat_result(self, tool: str, receipt: JsonObject) -> EffectOutcome:
        if _response_is_lost(self.scenario, tool):
            raise ApplicationError("provider response was lost", type="ResponseLost")
        return EffectConfirmed(receipt=receipt)

    def _commit(
        self, tool: str, command: BaseModel, operation_id: str, receipt: JsonObject
    ) -> None:
        self.state = _MUTATIONS[tool](self.state, command, self.scenario)
        self._receipts[operation_id] = receipt
        self._count(tool).effects += 1
        self.events.append(f"effect:{tool}")

    def _record(self, tool: str, event: str) -> None:
        self._count(tool).executes += 1
        self.events.append(f"{event}:{tool}")

    def _count(self, tool: str) -> _Count:
        return self._counts.setdefault(tool, _Count())


@dataclass(frozen=True)
class ProviderEffectAdapter[CommandT: BaseModel]:
    provider: EcommerceProvider
    tool: str

    async def execute(self, command: CommandT, context: EffectContext) -> EffectOutcome:
        return await self.provider.execute(self.tool, command, context)

    async def reconcile(
        self, command: CommandT, context: ReconcileContext
    ) -> ReconciliationOutcome:
        return await self.provider.reconcile(self.tool, command, context)


@dataclass(frozen=True)
class ProviderReadAdapter:
    provider: EcommerceProvider

    async def read(self, command: InspectOrder) -> OrderVerification:
        result = await self.provider.read("verify_order", command)
        return OrderVerification.model_validate(result)


@dataclass(frozen=True)
class _EffectSpec:
    name: str
    model: type[BaseModel]
    compensation: str | None
    description: str


def build_registry(provider: EcommerceProvider) -> ToolRegistry:
    effects = tuple(_effect_definition(provider, spec) for spec in _EFFECT_SPECS)
    verification = ReadToolDefinition(
        "verify_order",
        InspectOrder,
        OrderVerification,
        ProviderReadAdapter(provider),
        "Verify the authoritative final order state.",
    )
    return ToolRegistry((*effects, verification))


def _effect_definition(
    provider: EcommerceProvider, spec: _EffectSpec
) -> EffectToolDefinition[BaseModel]:
    return EffectToolDefinition(
        name=spec.name,
        definition_version="temporal-ecommerce-v1",
        command_schema_version="ecommerce-command-v1",
        input_model=spec.model,
        adapter=ProviderEffectAdapter(provider, spec.name),
        capabilities=_capabilities(spec.compensation),
        compensate_with=spec.compensation,
        description=spec.description,
    )


def _capabilities(compensation: str | None) -> ToolCapabilities:
    reversibility = Reversibility.SEMANTIC if compensation else Reversibility.IRREVERSIBLE
    return ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=reversibility,
        partial_effects_possible=False,
    )


def _receipt(tool: str, operation_id: str) -> JsonObject:
    digest = sha256(f"{tool}\0{operation_id}".encode()).hexdigest()
    return _JSON.validate_python({"receipt_ref": f"opaque://ecommerce/{digest}/v1"})


def _response_is_lost(scenario: ScenarioName, tool: str) -> bool:
    return (scenario, tool) in {
        (ScenarioName.LOST_RESPONSE, "charge_payment"),
        (ScenarioName.COMPENSATION_FAILURE, "refund_payment"),
    }


def _command_allowed(tool: str, command: BaseModel, state: ProviderState) -> bool:
    return _command_matches(tool, command, state) and _phase_allows(tool, state)


def _command_matches(tool: str, command: BaseModel, state: ProviderState) -> bool:
    expected = _expected_commands(state).get(tool)
    return expected is not None and command == expected


def _expected_commands(state: ProviderState) -> dict[str, BaseModel]:
    return {
        "reserve_inventory": _reserve_input(state),
        "charge_payment": _charge_input(state),
        "schedule_fulfillment": ScheduleFulfillment(order_id=state.order_id),
        "cancel_fulfillment": CancelFulfillment(order_id=state.order_id),
        "refund_payment": _refund_input(state),
        "release_inventory": _release_input(state),
    }


def _reserve_input(state: ProviderState) -> ReserveInventory:
    return ReserveInventory(
        order_id=state.order_id,
        sku=state.sku,
        quantity=state.order_quantity,
        expected_version=state.inventory_version,
    )


def _charge_input(state: ProviderState) -> ChargePayment:
    return ChargePayment(
        order_id=state.order_id,
        customer_id=state.customer_id,
        amount_minor=state.order_amount_minor,
        currency=state.currency,
    )


def _refund_input(state: ProviderState) -> RefundPayment:
    return RefundPayment(
        order_id=state.order_id,
        customer_id=state.customer_id,
        amount_minor=state.order_amount_minor,
        currency=state.currency,
    )


def _release_input(state: ProviderState) -> ReleaseInventory:
    return ReleaseInventory(
        order_id=state.order_id,
        sku=state.sku,
        quantity=state.order_quantity,
    )


def _phase_allows(tool: str, state: ProviderState) -> bool:
    check = _PHASE_CHECKS.get(tool)
    return check(state) if check is not None else False


def _can_reserve(state: ProviderState) -> bool:
    return (
        state.payment == "open" and state.reserved == 0 and state.available >= state.order_quantity
    )


def _can_charge(state: ProviderState) -> bool:
    return (
        state.customer_authorized
        and state.payment == "open"
        and state.reserved == state.order_quantity
    )


def _can_schedule(state: ProviderState) -> bool:
    return state.payment == "captured" and state.fulfillment == "pending"


def _can_cancel(state: ProviderState) -> bool:
    return state.fulfillment in {"scheduled", "rejected"}


def _can_refund(state: ProviderState) -> bool:
    return state.payment == "captured" and state.fulfillment == "cancelled"


def _can_release(state: ProviderState) -> bool:
    return state.payment == "refunded" and state.reserved == state.order_quantity


def _reserve(state: ProviderState, command: BaseModel, scenario: ScenarioName) -> ProviderState:
    del scenario
    reserve = ReserveInventory.model_validate(command)
    return state.model_copy(
        update={
            "available": state.available - reserve.quantity,
            "reserved": state.reserved + reserve.quantity,
            "inventory_version": state.inventory_version + 1,
        }
    )


def _charge(state: ProviderState, command: BaseModel, scenario: ScenarioName) -> ProviderState:
    del scenario
    charge = ChargePayment.model_validate(command)
    return state.model_copy(
        update={
            "payment": "captured",
            "captured_customer_id": charge.customer_id,
            "captured_amount_minor": charge.amount_minor,
            "captured_currency": charge.currency,
        }
    )


def _schedule(state: ProviderState, command: BaseModel, scenario: ScenarioName) -> ProviderState:
    ScheduleFulfillment.model_validate(command)
    failing = scenario in {ScenarioName.BUSINESS_FAILURE, ScenarioName.COMPENSATION_FAILURE}
    return state.model_copy(update={"fulfillment": "rejected" if failing else "scheduled"})


def _cancel(state: ProviderState, command: BaseModel, scenario: ScenarioName) -> ProviderState:
    del scenario
    CancelFulfillment.model_validate(command)
    return state.model_copy(update={"fulfillment": "cancelled"})


def _refund(state: ProviderState, command: BaseModel, scenario: ScenarioName) -> ProviderState:
    del scenario
    refund = RefundPayment.model_validate(command)
    return state.model_copy(
        update={"payment": "refunded", "refunded_amount_minor": refund.amount_minor}
    )


def _release(state: ProviderState, command: BaseModel, scenario: ScenarioName) -> ProviderState:
    del scenario
    release = ReleaseInventory.model_validate(command)
    return state.model_copy(
        update={
            "available": state.available + release.quantity,
            "reserved": state.reserved - release.quantity,
            "inventory_version": state.inventory_version + 1,
        }
    )


type _Mutation = Callable[[ProviderState, BaseModel, ScenarioName], ProviderState]
type _PhaseCheck = Callable[[ProviderState], bool]
_PHASE_CHECKS: dict[str, _PhaseCheck] = {
    "reserve_inventory": _can_reserve,
    "charge_payment": _can_charge,
    "schedule_fulfillment": _can_schedule,
    "cancel_fulfillment": _can_cancel,
    "refund_payment": _can_refund,
    "release_inventory": _can_release,
}
_MUTATIONS: dict[str, _Mutation] = {
    "reserve_inventory": _reserve,
    "charge_payment": _charge,
    "schedule_fulfillment": _schedule,
    "cancel_fulfillment": _cancel,
    "refund_payment": _refund,
    "release_inventory": _release,
}

_EFFECT_SPECS = (
    _EffectSpec("reserve_inventory", ReserveInventory, "release_inventory", "Reserve stock."),
    _EffectSpec("charge_payment", ChargePayment, "refund_payment", "Charge the customer."),
    _EffectSpec(
        "schedule_fulfillment",
        ScheduleFulfillment,
        "cancel_fulfillment",
        "Schedule shipment.",
    ),
    _EffectSpec("cancel_fulfillment", CancelFulfillment, None, "Cancel shipment."),
    _EffectSpec("refund_payment", RefundPayment, None, "Refund the customer."),
    _EffectSpec("release_inventory", ReleaseInventory, None, "Release reserved stock."),
)
