from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

import agentic_saga.contracts.actions as action_contracts
from agentic_saga.contracts.actions import (
    AgentProposal,
    AuthorizedToolCall,
    Finish,
    HumanDecision,
    ToolCall,
)
from agentic_saga.contracts.common import (
    Direction,
    FenceToken,
    JsonObject,
    JsonValue,
    OperationId,
    Reversibility,
    SagaId,
    StepInstanceId,
    thaw_json_object,
)


def test_agent_controls_are_not_public_contracts() -> None:
    assert not hasattr(action_contracts, "BeginCompensation")
    assert not hasattr(action_contracts, "Escalate")


class ExampleCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    amount_minor: int


class NestedCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    item: ExampleCommand
    labels: tuple[str, ...]


def valid_tool_call() -> dict[str, object]:
    return {
        "kind": "tool_call",
        "proposal_id": "proposal_00000001",
        "tool_name": "charge_payment",
        "arguments": {"amount_minor": 14900},
        "based_on_saga_seq": 3,
        "rationale": "Payment is required before fulfillment.",
    }


def valid_human_decision() -> dict[str, object]:
    return {
        "decision_id": "decision_01",
        "saga_id": "saga_0123456789abcdef",
        "based_on_saga_seq": 3,
        "action": "approve",
        "proposal_hash": "a" * 64,
        "actor": "operator@example.test",
        "issued_at": datetime(2026, 9, 6, tzinfo=UTC),
        "auth_proof": "opaque-proof",
    }


@pytest.mark.parametrize(
    ("adapter", "value"),
    [
        (TypeAdapter(SagaId), b"saga_0123456789abcdef"),
        (TypeAdapter(StepInstanceId), b"step_01234567"),
        (TypeAdapter(OperationId), f"op_{'a' * 64}".encode()),
    ],
)
def test_should_reject_bytes_when_validating_public_id_alias(
    adapter: TypeAdapter[str], value: bytes
) -> None:
    # Given / When / Then
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


@pytest.mark.parametrize(
    "value",
    [
        {"items": ("sku_1",)},
        {"items": {"sku_1"}},
        {b"items": ["sku_1"]},
        {"items": b"sku_1"},
        [("items", ["sku_1"])],
    ],
)
def test_should_reject_coercive_container_when_validating_json_object(value: object) -> None:
    # Given
    adapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)

    # When / Then
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


@pytest.mark.parametrize("value", [("sku_1",), {"sku_1"}, b"sku_1", Decimal("1.2")])
def test_should_reject_coercive_value_when_validating_json_value(value: object) -> None:
    # Given
    adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

    # When / Then
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


def test_should_preserve_boolean_and_integer_when_validating_json_object() -> None:
    # Given
    adapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)

    # When
    value = adapter.validate_python({"approved": True, "attempt": 1})

    # Then
    assert type(value["approved"]) is bool
    assert type(value["attempt"]) is int


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_should_reject_non_finite_float_when_validating_json_value(value: float) -> None:
    # Given
    adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

    # When / Then
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_should_reject_nested_non_finite_float_when_validating_tool_call(value: float) -> None:
    # Given
    payload = valid_tool_call() | {"arguments": {"amount": value}}

    # When / Then
    with pytest.raises(ValidationError):
        ToolCall.model_validate(payload)


def test_should_reject_agent_idempotency_key_when_validating_tool_call() -> None:
    # Given
    payload = valid_tool_call() | {"idempotency_key": "attacker-choice"}

    # When / Then
    with pytest.raises(ValidationError):
        ToolCall.model_validate(payload)


def test_should_preserve_concrete_command_when_authorized_call_is_erased() -> None:
    # Given
    command = NestedCommand(item=ExampleCommand(amount_minor=14900), labels=("priority",))
    authorized = AuthorizedToolCall[BaseModel](
        operation_id=f"op_{'a' * 64}",
        step_instance_id="step_01234567",
        direction=Direction.FORWARD,
        semantic_generation=0,
        command=command,
    )

    # When
    dumped = authorized.model_dump(mode="json")

    # Then
    assert dumped["command"] == {
        "item": {"amount_minor": 14900},
        "labels": ["priority"],
    }
    assert '"amount_minor":14900' in authorized.model_dump_json()


def test_should_keep_json_value_type_when_validating_tool_arguments() -> None:
    # Given
    payload = valid_tool_call()

    # When
    proposal = ToolCall.model_validate(payload)

    # Then
    assert proposal.arguments == {"amount_minor": 14900}
    assert isinstance(proposal.arguments["amount_minor"], int)


def test_should_recursively_freeze_and_serialize_json_objects() -> None:
    # Given
    source: dict[str, object] = {"z": [{"count": 1}], "a": True}
    adapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)

    # When
    value = adapter.validate_python(source)
    source_items = cast(list[object], source["z"])
    cast(dict[str, object], source_items[0])["count"] = 2

    # Then
    nested = cast(tuple[JsonObject, ...], value["z"])
    assert tuple(value) == ("a", "z")
    assert nested[0]["count"] == 1
    assert adapter.dump_python(value, mode="json") == {"a": True, "z": [{"count": 1}]}
    assert adapter.validate_json(adapter.dump_json(value)) == value


def test_should_thaw_nested_json_into_fresh_exact_containers() -> None:
    # Given
    adapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
    frozen = adapter.validate_python({"items": [{"sku": "sku_1"}]})

    # When
    thawed = thaw_json_object(frozen)

    # Then
    assert type(thawed) is dict
    assert type(thawed["items"]) is list
    items = cast(list[object], thawed["items"])
    assert type(items[0]) is dict
    assert thawed == {"items": [{"sku": "sku_1"}]}


def test_should_reject_mutation_at_every_json_depth() -> None:
    # Given
    adapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
    value = adapter.validate_python({"items": [{"sku": "sku_1"}]})
    items = cast(tuple[JsonObject, ...], value["items"])

    # When / Then
    with pytest.raises(TypeError):
        value["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        items[0]["sku"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        items[0] = value  # type: ignore[index]


def test_should_reject_sequence_string_when_validating_tool_call() -> None:
    # Given
    payload = valid_tool_call() | {"based_on_saga_seq": "3"}

    # When / Then
    with pytest.raises(ValidationError):
        ToolCall.model_validate(payload)


def test_should_reject_negative_sequence_when_validating_tool_call() -> None:
    # Given
    payload = valid_tool_call() | {"based_on_saga_seq": -1}

    # When / Then
    with pytest.raises(ValidationError):
        ToolCall.model_validate(payload)


@pytest.mark.parametrize("tool_name", ["", "x" * 201])
def test_should_reject_tool_name_when_outside_bounds(tool_name: str) -> None:
    # Given
    payload = valid_tool_call() | {"tool_name": tool_name}

    # When / Then
    with pytest.raises(ValidationError):
        ToolCall.model_validate(payload)


@pytest.mark.parametrize("rationale", ["", "x" * 501])
def test_should_reject_rationale_when_outside_bounds(rationale: str) -> None:
    # Given
    payload = valid_tool_call() | {"rationale": rationale}

    # When / Then
    with pytest.raises(ValidationError):
        ToolCall.model_validate(payload)


def test_should_reject_tuple_when_json_array_is_required() -> None:
    # Given
    payload = valid_tool_call() | {"arguments": {"items": ("sku_1",)}}

    # When / Then
    with pytest.raises(ValidationError):
        ToolCall.model_validate(payload)


def test_should_reject_extra_field_when_validating_finish() -> None:
    # Given
    payload = {
        "kind": "finish",
        "proposal_id": "proposal_00000002",
        "based_on_saga_seq": 3,
        "rationale": "All required invariants appear satisfied.",
        "target_status": "succeeded",
    }

    # When / Then
    with pytest.raises(ValidationError):
        Finish.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (valid_tool_call(), ToolCall),
        (
            {
                "kind": "finish",
                "proposal_id": "proposal_00000002",
                "based_on_saga_seq": 3,
                "rationale": "The goal is verified.",
                "target_status": "succeeded_verified",
            },
            Finish,
        ),
    ],
)
def test_should_select_variant_when_validating_agent_proposal(
    payload: dict[str, object], expected_type: type[BaseModel]
) -> None:
    # Given
    adapter: TypeAdapter[AgentProposal] = TypeAdapter(AgentProposal)

    # When
    proposal = adapter.validate_python(payload)

    # Then
    assert isinstance(proposal, expected_type)


def test_should_reject_unknown_kind_when_validating_agent_proposal() -> None:
    # Given
    payload = valid_tool_call() | {"kind": "run_shell"}

    # When / Then
    with pytest.raises(ValidationError):
        TypeAdapter(AgentProposal).validate_python(payload)


def test_should_reject_invalid_operation_id_when_authorizing_command() -> None:
    # Given
    command = ExampleCommand(amount_minor=14900)

    # When / Then
    with pytest.raises(ValidationError):
        AuthorizedToolCall(
            operation_id="op_bad",
            step_instance_id="step_01234567",
            direction=Direction.FORWARD,
            semantic_generation=0,
            command=command,
        )


def test_should_reject_invalid_step_id_when_authorizing_command() -> None:
    # Given
    command = ExampleCommand(amount_minor=14900)

    # When / Then
    with pytest.raises(ValidationError):
        AuthorizedToolCall(
            operation_id=f"op_{'a' * 64}",
            step_instance_id="step_bad!",
            direction=Direction.FORWARD,
            semantic_generation=0,
            command=command,
        )


def test_should_reject_negative_generation_when_authorizing_command() -> None:
    # Given
    command = ExampleCommand(amount_minor=14900)

    # When / Then
    with pytest.raises(ValidationError):
        AuthorizedToolCall(
            operation_id=f"op_{'a' * 64}",
            step_instance_id="step_01234567",
            direction=Direction.FORWARD,
            semantic_generation=-1,
            command=command,
        )


def test_should_reject_direction_string_when_authorizing_command() -> None:
    # Given
    payload = {
        "operation_id": f"op_{'a' * 64}",
        "step_instance_id": "step_01234567",
        "direction": "forward",
        "semantic_generation": 0,
        "command": {"amount_minor": 14900},
    }

    # When / Then
    with pytest.raises(ValidationError):
        AuthorizedToolCall[ExampleCommand].model_validate(payload)


def test_should_reject_zero_when_validating_fence_token() -> None:
    # Given
    adapter: TypeAdapter[FenceToken] = TypeAdapter(FenceToken)

    # When / Then
    with pytest.raises(ValidationError):
        adapter.validate_python(0)


def test_should_reject_string_when_validating_fence_token() -> None:
    # Given
    adapter: TypeAdapter[FenceToken] = TypeAdapter(FenceToken)

    # When / Then
    with pytest.raises(ValidationError):
        adapter.validate_python("1")


def test_should_expose_expected_values_when_reading_reversibility() -> None:
    # Given / When
    values = {item.value for item in Reversibility}

    # Then
    assert values == {"exact", "semantic", "irreversible"}


def test_should_reject_naive_time_when_validating_human_decision() -> None:
    # Given
    payload = valid_human_decision() | {"issued_at": datetime(2026, 9, 6)}

    # When / Then
    with pytest.raises(ValidationError):
        HumanDecision.model_validate(payload)


def test_should_reject_invalid_saga_id_when_validating_human_decision() -> None:
    # Given
    payload = valid_human_decision() | {"saga_id": "saga_bad"}

    # When / Then
    with pytest.raises(ValidationError):
        HumanDecision.model_validate(payload)


def test_should_reject_extra_field_when_validating_human_decision() -> None:
    # Given
    payload = valid_human_decision() | {"credential": "reusable-secret"}

    # When / Then
    with pytest.raises(ValidationError):
        HumanDecision.model_validate(payload)


def test_should_prevent_assignment_when_proposal_is_frozen() -> None:
    # Given
    proposal = ToolCall.model_validate(valid_tool_call())

    # When / Then
    with pytest.raises(ValidationError):
        proposal.tool_name = "wire_money"
