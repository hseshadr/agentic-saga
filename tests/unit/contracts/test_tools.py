from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentic_saga.contracts.actions import AuthorizedToolCall
from agentic_saga.contracts.common import Direction, Reversibility
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    ReconcileEffectConfirmed,
    ReconciliationOutcome,
)
from agentic_saga.contracts.tools import (
    DuplicateToolError,
    EffectAdapter,
    EffectContext,
    EffectToolDefinition,
    InvalidToolSchemaError,
    ReadAdapter,
    ReadToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
    UnknownToolError,
    UnsupportedToolDefinitionError,
)

SAGA_ID = "saga_0123456789abcdef"
STEP_ID = "step_01234567"
OPERATION_ID = f"op_{'a' * 64}"


class ChargeCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    amount_minor: int = Field(gt=0)
    currency: Literal["USD"]


class InventoryQuery(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    sku: str


class InventoryResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    available: int = Field(ge=0)


class NonStrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int


class ExtraIgnoringSchema(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    value: int


class MutableSchema(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    value: int


class ChargeAdapter:
    async def execute(self, command: ChargeCommand, context: EffectContext) -> EffectOutcome:
        return EffectConfirmed(receipt={"operation_id": context.operation_id})

    async def reconcile(
        self, command: ChargeCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        return ReconcileEffectConfirmed(receipt={"correlation": context.correlation})


class InventoryAdapter:
    async def read(self, command: InventoryQuery) -> InventoryResult:
        return InventoryResult(available=len(command.sku))


class GenericEffectAdapter:
    async def execute(self, command: BaseModel, context: EffectContext) -> EffectOutcome:
        return EffectConfirmed(receipt={})

    async def reconcile(
        self, command: BaseModel, context: ReconcileContext
    ) -> ReconciliationOutcome:
        return ReconcileEffectConfirmed(receipt={})


class GenericReadAdapter:
    async def read(self, command: BaseModel) -> BaseModel:
        return command


class StructuralDefinition:
    name = "structural_tool"
    input_model = ChargeCommand


def effect_capabilities() -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=False,
    )


def test_effect_definition_uses_trusted_adapter_but_proof_is_data_only() -> None:
    adapter = ChargeAdapter()
    capabilities = ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=True,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=False,
    )

    definition = EffectToolDefinition(
        "charge_payment",
        "charge-payment-v1",
        "charge-command-v1",
        ChargeCommand,
        adapter,
        capabilities,
        "refund_payment",
    )

    assert definition.adapter is adapter
    assert "factory" not in str(capabilities.model_dump(mode="json"))


def charge_definition() -> EffectToolDefinition[ChargeCommand]:
    return EffectToolDefinition(
        name="charge_payment",
        definition_version="charge-payment-v1",
        command_schema_version="charge-command-v1",
        input_model=ChargeCommand,
        adapter=ChargeAdapter(),
        capabilities=effect_capabilities(),
        compensate_with="refund_payment",
    )


def inventory_definition() -> ReadToolDefinition[InventoryQuery, InventoryResult]:
    return ReadToolDefinition(
        name="check_inventory",
        input_model=InventoryQuery,
        result_model=InventoryResult,
        adapter=InventoryAdapter(),
    )


def effect_definition_with(model: type[BaseModel]) -> EffectToolDefinition[BaseModel]:
    return EffectToolDefinition(
        name="unsafe_effect",
        definition_version="unsafe-effect-v1",
        command_schema_version="unsafe-command-v1",
        input_model=model,
        adapter=GenericEffectAdapter(),
        capabilities=effect_capabilities(),
        compensate_with=None,
    )


def read_definition_with(
    input_model: type[BaseModel], result_model: type[BaseModel]
) -> ReadToolDefinition[BaseModel, BaseModel]:
    return ReadToolDefinition(
        name="unsafe_read",
        input_model=input_model,
        result_model=result_model,
        adapter=GenericReadAdapter(),
    )


def effect_context() -> EffectContext:
    return EffectContext(
        saga_id=SAGA_ID,
        step_instance_id=STEP_ID,
        operation_id=OPERATION_ID,
        fence_token=7,
        delivery_attempt=2,
    )


def test_should_convert_strict_command_before_authorization() -> None:
    # Given
    registry = ToolRegistry((charge_definition(),))

    # When
    command = registry.validate_command(
        "charge_payment", {"amount_minor": 14_900, "currency": "USD"}
    )
    assert isinstance(command, ChargeCommand)
    authorized = AuthorizedToolCall[ChargeCommand](
        operation_id=OPERATION_ID,
        step_instance_id=STEP_ID,
        direction=Direction.FORWARD,
        semantic_generation=0,
        command=command,
    )

    # Then
    assert isinstance(authorized.command, ChargeCommand)


def test_should_reject_extra_field_before_command_authorization() -> None:
    # Given
    registry = ToolRegistry((charge_definition(),))

    # When / Then
    with pytest.raises(ValidationError):
        registry.validate_command(
            "charge_payment",
            {"amount_minor": 14_900, "currency": "USD", "admin": True},
        )


def test_should_reject_coercion_before_command_authorization() -> None:
    # Given
    registry = ToolRegistry((charge_definition(),))

    # When / Then
    with pytest.raises(ValidationError):
        registry.validate_command("charge_payment", {"amount_minor": "14900", "currency": "USD"})


def test_should_raise_domain_error_when_tool_is_unknown() -> None:
    # Given
    registry = ToolRegistry(())

    # When / Then
    with pytest.raises(UnknownToolError, match="wire_money"):
        registry.definition("wire_money")


def test_should_register_effect_definition_when_name_is_unique() -> None:
    # Given
    registry = ToolRegistry(())
    definition = charge_definition()

    # When
    registry.register_effect(definition)

    # Then
    assert registry.definition("charge_payment") is definition


@pytest.mark.parametrize(
    ("schema", "requirement"),
    [
        (NonStrictSchema, "strict=True"),
        (ExtraIgnoringSchema, "extra='forbid'"),
        (MutableSchema, "frozen=True"),
    ],
)
def test_should_reject_lax_effect_input_schema(schema: type[BaseModel], requirement: str) -> None:
    # Given / When
    with pytest.raises(InvalidToolSchemaError) as caught:
        effect_definition_with(schema)

    # Then
    assert str(caught.value) == (
        f"tool 'unsafe_effect' effect input schema must configure {requirement}"
    )


@pytest.mark.parametrize(
    ("schema", "requirement"),
    [
        (NonStrictSchema, "strict=True"),
        (ExtraIgnoringSchema, "extra='forbid'"),
        (MutableSchema, "frozen=True"),
    ],
)
def test_should_reject_lax_read_input_schema(schema: type[BaseModel], requirement: str) -> None:
    # Given / When
    with pytest.raises(InvalidToolSchemaError) as caught:
        read_definition_with(schema, InventoryResult)

    # Then
    assert str(caught.value) == f"tool 'unsafe_read' read input schema must configure {requirement}"


@pytest.mark.parametrize(
    ("schema", "requirement"),
    [
        (NonStrictSchema, "strict=True"),
        (ExtraIgnoringSchema, "extra='forbid'"),
        (MutableSchema, "frozen=True"),
    ],
)
def test_should_reject_lax_read_result_schema(schema: type[BaseModel], requirement: str) -> None:
    # Given / When
    with pytest.raises(InvalidToolSchemaError) as caught:
        read_definition_with(InventoryQuery, schema)

    # Then
    assert str(caught.value) == (
        f"tool 'unsafe_read' read result schema must configure {requirement}"
    )


def test_should_accept_safe_effect_input_schema() -> None:
    # Given / When
    definition = charge_definition()

    # Then
    assert definition.input_model is ChargeCommand
    assert isinstance(definition.adapter, ChargeAdapter)


def test_should_accept_safe_read_input_and_result_schemas() -> None:
    # Given / When
    definition = inventory_definition()

    # Then
    assert definition.input_model is InventoryQuery
    assert definition.result_model is InventoryResult


def test_should_reject_structural_definition_from_registry_constructor() -> None:
    # Given
    impostor = StructuralDefinition()

    # When / Then
    with pytest.raises(UnsupportedToolDefinitionError, match="StructuralDefinition"):
        ToolRegistry((impostor,))


def test_should_reject_duplicate_name_across_tool_kinds() -> None:
    # Given
    registry = ToolRegistry((charge_definition(),))
    duplicate = ReadToolDefinition(
        name="charge_payment",
        input_model=InventoryQuery,
        result_model=InventoryResult,
        adapter=InventoryAdapter(),
    )

    # When / Then
    with pytest.raises(DuplicateToolError, match="charge_payment"):
        registry.register_read(duplicate)


def test_should_keep_read_and_effect_definitions_separate() -> None:
    # Given
    read = inventory_definition()
    effect = charge_definition()
    registry = ToolRegistry((read, effect))

    # When
    registered_read = registry.definition("check_inventory")
    registered_effect = registry.definition("charge_payment")

    # Then
    assert isinstance(registered_read, ReadToolDefinition)
    assert not hasattr(registered_read, "capabilities")
    assert isinstance(registered_effect, EffectToolDefinition)
    assert registered_effect.compensate_with == "refund_payment"


def test_should_prevent_capability_assignment() -> None:
    # Given
    capabilities = effect_capabilities()

    # When / Then
    with pytest.raises(ValidationError):
        capabilities.fencing_supported = False


def test_should_reject_coercive_capability_value() -> None:
    # Given
    raw = effect_capabilities().model_dump()

    # When / Then
    with pytest.raises(ValidationError):
        ToolCapabilities.model_validate(raw | {"fencing_supported": "true"})


def test_should_reject_extra_capability_field() -> None:
    # Given
    raw = effect_capabilities().model_dump()

    # When / Then
    with pytest.raises(ValidationError):
        ToolCapabilities.model_validate(raw | {"reconcile_with": "find_payment"})


def test_should_reject_nonpositive_idempotency_retention() -> None:
    # Given
    raw = effect_capabilities().model_dump()

    # When / Then
    with pytest.raises(ValidationError):
        ToolCapabilities.model_validate(raw | {"idempotency_retention_seconds": 0})


def test_should_prevent_definition_assignment() -> None:
    # Given
    definition = charge_definition()

    # When / Then
    with pytest.raises(AttributeError):
        definition.compensate_with = None  # type: ignore[misc]


@pytest.mark.asyncio
async def test_should_match_effect_execution_protocol_signature() -> None:
    # Given
    adapter = ChargeAdapter()
    context = effect_context()

    # When
    executed = await adapter.execute(ChargeCommand(amount_minor=1, currency="USD"), context)

    # Then
    assert isinstance(adapter, EffectAdapter)
    assert executed.kind == "effect_confirmed"


@pytest.mark.asyncio
async def test_should_match_effect_reconciliation_protocol_signature() -> None:
    # Given
    adapter = ChargeAdapter()
    context = effect_context()
    reconcile = ReconcileContext(
        **context.model_dump(), correlation="opaque://provider/request00000007/v1"
    )

    # When
    reconciled = await adapter.reconcile(ChargeCommand(amount_minor=1, currency="USD"), reconcile)

    # Then
    assert isinstance(adapter, EffectAdapter)
    assert reconciled.kind == "reconcile_effect_confirmed"


@pytest.mark.asyncio
async def test_should_match_read_adapter_protocol_signature() -> None:
    # Given
    adapter = InventoryAdapter()

    # When
    result = await adapter.read(InventoryQuery(sku="sku_1"))

    # Then
    assert isinstance(adapter, ReadAdapter)
    assert result.available == 5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("saga_id", "saga_invalid"),
        ("step_instance_id", "step_bad"),
        ("operation_id", "op_invalid"),
        ("fence_token", 0),
    ],
)
def test_should_reject_invalid_identity_or_fence_in_effect_context(
    field: str, value: str | int
) -> None:
    # Given
    raw = effect_context().model_dump() | {field: value}

    # When / Then
    with pytest.raises(ValidationError):
        EffectContext.model_validate(raw)


@pytest.mark.parametrize("attempt", [0, -1, "2"])
def test_should_reject_invalid_delivery_attempt(attempt: int | str) -> None:
    # Given
    raw = effect_context().model_dump() | {"delivery_attempt": attempt}

    # When / Then
    with pytest.raises(ValidationError):
        EffectContext.model_validate(raw)


def test_should_reject_missing_reconcile_correlation() -> None:
    # Given
    raw = effect_context().model_dump()

    # When / Then
    with pytest.raises(ValidationError):
        ReconcileContext.model_validate(raw)


def test_should_reject_extra_reconcile_context_field() -> None:
    # Given
    raw = effect_context().model_dump()

    # When / Then
    with pytest.raises(ValidationError):
        ReconcileContext.model_validate(raw | {"correlation": "request-1", "token": "secret"})


@pytest.mark.parametrize("correlation", ["", "x" * 501])
def test_should_reject_reconcile_correlation_outside_bounds(correlation: str) -> None:
    # Given
    raw = effect_context().model_dump() | {"correlation": correlation}

    # When / Then
    with pytest.raises(ValidationError):
        ReconcileContext.model_validate(raw)


def test_should_prevent_context_assignment() -> None:
    # Given
    context = effect_context()

    # When / Then
    with pytest.raises(ValidationError):
        context.delivery_attempt = 3
