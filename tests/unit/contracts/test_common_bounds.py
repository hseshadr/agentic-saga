from __future__ import annotations

import tracemalloc

import pytest

from agentic_saga.contracts.actions import ToolCall
from agentic_saga.contracts.common import JsonLimit, JsonPayloadError
from agentic_saga.contracts.outcomes import EffectConfirmed


def _tool_call(arguments: object) -> dict[str, object]:
    return {
        "kind": "tool_call",
        "proposal_id": "proposal_12345678",
        "tool_name": "inspect",
        "arguments": arguments,
        "based_on_saga_seq": 1,
        "rationale": "Inspect current evidence.",
    }


def _deep_object(depth: int) -> dict[str, object]:
    root: dict[str, object] = {}
    current = root
    for _ in range(depth):
        child: dict[str, object] = {}
        current["next"] = child
        current = child
    return root


def _assert_sanitized(error: JsonPayloadError, private: str) -> None:
    assert private not in repr(error.args)
    assert private not in repr(vars(error))
    assert error.__cause__ is None
    assert error.__context__ is None


def test_should_reject_json_container_width_before_freezing() -> None:
    # Given
    private = "private-width-value"
    arguments = {f"key_{index}": private for index in range(257)}

    # When / Then
    with pytest.raises(JsonPayloadError) as captured:
        ToolCall.model_validate(_tool_call(arguments))
    assert captured.value.limit is JsonLimit.CONTAINER_ITEMS
    _assert_sanitized(captured.value, private)


def test_should_reject_json_string_bytes_before_freezing() -> None:
    # Given
    private = "sensitive" * 2_049

    # When / Then
    with pytest.raises(JsonPayloadError) as captured:
        ToolCall.model_validate(_tool_call({"note": private}))
    assert captured.value.limit is JsonLimit.STRING_BYTES
    _assert_sanitized(captured.value, private)


def test_should_reject_huge_character_count_without_encoding_copy() -> None:
    # Given
    private = "s" * 10_000_000

    # When
    tracemalloc.start()
    with pytest.raises(JsonPayloadError) as captured:
        ToolCall.model_validate(_tool_call({"note": private}))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Then
    assert captured.value.limit is JsonLimit.STRING_BYTES
    assert peak < 1_000_000


def test_should_reject_total_json_bytes_before_contract_acceptance() -> None:
    # Given
    private = "z" * 14_000
    arguments = {f"part_{index}": private for index in range(5)}

    # When / Then
    with pytest.raises(JsonPayloadError) as captured:
        ToolCall.model_validate(_tool_call(arguments))
    assert captured.value.limit is JsonLimit.ENCODED_BYTES
    _assert_sanitized(captured.value, private)


def test_should_reject_total_json_nodes_before_contract_acceptance() -> None:
    # Given
    arguments = [{f"key_{group}_{index}": index for index in range(250)} for group in range(9)]

    # When / Then
    with pytest.raises(JsonPayloadError) as captured:
        ToolCall.model_validate(_tool_call({"items": arguments}))
    assert captured.value.limit is JsonLimit.NODES
    _assert_sanitized(captured.value, "key_0_0")


def test_should_bound_memory_while_rejecting_large_serialized_json() -> None:
    # Given
    value = "x" * 16_000
    mappings = [{f"key_{group}_{index}": value for index in range(256)} for group in range(7)]

    # When
    tracemalloc.start()
    with pytest.raises(JsonPayloadError) as captured:
        ToolCall.model_validate(_tool_call({"items": mappings}))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Then
    assert captured.value.limit is JsonLimit.ENCODED_BYTES
    assert peak < 2_000_000


def test_should_reject_extreme_receipt_depth_without_recursion_error() -> None:
    # Given
    receipt = _deep_object(1_500)

    # When / Then
    with pytest.raises(JsonPayloadError) as captured:
        EffectConfirmed.model_validate({"receipt": receipt})
    assert captured.value.limit is JsonLimit.DEPTH
    _assert_sanitized(captured.value, "next")
