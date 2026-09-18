from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import cast

import pydantic_deep as pydantic_deep_package  # type: ignore[import-untyped]
import pytest
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings

from agentic_saga import SagaContext, SagaManifest
from agentic_saga.agents import DeepAgentsDriver
from agentic_saga.agents import deepagents as adapter_module
from agentic_saga.agents.deepagents import (
    AgentDecision,
    AgentFailureCategory,
    AgentPlanningError,
    native_proposal_tool_names,
)
from agentic_saga.contracts.actions import BeginCompensation, Escalate, Finish, ToolCall
from agentic_saga.contracts.common import canonical_json
from agentic_saga.contracts.runtime import (
    ControlProposalCapabilities,
    ExecutionBudget,
    SagaGoal,
    SagaObservation,
    SagaStatus,
    ToolDescriptor,
)

type ProposalCall = Callable[[str, str], Awaitable[object]]


class _TextUnlessToolRequiredModel(TestModel):
    observed_settings: ModelSettings | None = None

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self.observed_settings = model_settings
        if model_settings and model_settings.get("tool_choice") == "required":
            self.call_tools = ["inspect"]
            self.custom_output_text = None
        return await super().request(messages, model_settings, model_request_parameters)


class _TextThenNativeToolModel(TestModel):
    requests: int = 0

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self.requests += 1
        if self.requests == 1:
            return ModelResponse(parts=[TextPart(content="I would inspect first.")])
        return ModelResponse(parts=[ToolCallPart("inspect", {}, "corrected-inspect")])


class _InvalidToolThenTextThenToolModel(TestModel):
    requests: int = 0

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        del messages, model_settings, model_request_parameters
        self.requests += 1
        if self.requests == 1:
            return ModelResponse(parts=[ToolCallPart("unknown", {}, "unknown")])
        if self.requests == 2:
            return ModelResponse(parts=[TextPart(content="I would inspect first.")])
        return ModelResponse(parts=[ToolCallPart("inspect", {}, "too-late")])


class _ContextTrackingModel(TestModel):
    enters: int = 0
    exits: int = 0

    async def __aenter__(self) -> _ContextTrackingModel:
        self.enters += 1
        await super().__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        self.exits += 1
        return await super().__aexit__(exc_type, exc_val, exc_tb)


def _budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=4,
        tool_call_limit=4,
        elapsed_ms_limit=20_000,
        token_limit=2_000,
    )


def _descriptor(name: str) -> ToolDescriptor:
    return ToolDescriptor.model_validate(
        {
            "name": name,
            "kind": "read",
            "description": "Inspect authoritative public state.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "reversibility": None,
        }
    )


def _manifest(tool_names: tuple[str, ...]) -> SagaManifest:
    return SagaManifest.model_validate(
        {
            "schema_version": "1.0",
            "name": "generic_booking",
            "version": "1.0",
            "objective": "Complete the request or restore a valid state.",
            "instructions": ["Choose one safe next action from current evidence."],
            "success_criteria": ["Registered success checks pass."],
            "autonomy": {"mode": "guarded", "instructions": ["Escalate if uncertain."]},
            "budgets": _budget().model_dump(),
            "tools": {"catalog_sha256": "0" * 64, "allowed": tool_names},
            "checks": {
                "policy": [],
                "success": ["success"],
                "compensation": ["compensated"],
                "clean_abort": ["clean"],
            },
            "escalation": {
                "conditions": ["Evidence remains ambiguous."],
                "instructions": ["Present public evidence to an operator."],
            },
        }
    )


def _context(*tool_names: str) -> SagaContext:
    descriptors = tuple(_descriptor(name) for name in tool_names)
    manifest = _manifest(tool_names)
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "tools": [item.model_dump(mode="json") for item in descriptors],
    }
    agent_context = canonical_json(payload).decode()
    return SagaContext(
        manifest=manifest,
        agent_context=agent_context,
        tool_descriptors=descriptors,
        budget=_budget(),
    )


def _observation(
    controls: ControlProposalCapabilities | None = None,
) -> SagaObservation:
    return SagaObservation(
        saga_id="saga_0123456789abcdef",
        saga_seq=3,
        state=SagaStatus.RUNNING,
        goal=SagaGoal(goal_id="goal_1", text="Complete the public request.", context={}),
        last_action=None,
        projection={"status": "pending"},
        remaining_budget=_budget(),
        proposal_controls=controls
        or ControlProposalCapabilities(
            finish_targets=("succeeded_verified", "aborted_clean"),
            escalate_to_human=True,
        ),
    )


def _tool_call(tool_name: str = "inspect") -> dict[str, object]:
    return {
        "proposal": {
            "kind": "tool_call",
            "tool_name": tool_name,
            "arguments": {},
            "rationale": "Inspect current evidence.",
        }
    }


def _assert_tool_proposal(proposal: object, tool_name: str = "inspect", seq: int = 3) -> None:
    assert isinstance(proposal, ToolCall)
    assert proposal.tool_name == tool_name
    assert proposal.arguments == {}
    assert proposal.based_on_saga_seq == seq
    assert proposal.proposal_id.startswith("proposal_")


def _finish() -> dict[str, object]:
    return {
        "proposal": {
            "kind": "finish",
            "rationale": "All registered checks can now be evaluated.",
            "target_status": "succeeded_verified",
        }
    }


def _escalate() -> dict[str, object]:
    return {
        "proposal": {
            "kind": "escalate",
            "reason_code": "ambiguous_evidence",
            "rationale": "A human must resolve conflicting public evidence.",
        }
    }


def _begin_compensation() -> dict[str, object]:
    return {
        "proposal": {
            "kind": "begin_compensation",
            "reason_code": "forward_goal_unreachable",
            "rationale": "Use the kernel-owned compensation frontier.",
        }
    }


@pytest.mark.asyncio
async def test_should_return_strict_tool_call_without_executing_business_tool() -> None:
    # Given
    calls: list[tuple[str, str]] = []

    async def propose(system_context: str, turn_context: str) -> object:
        calls.append((system_context, turn_context))
        return _tool_call()

    driver = DeepAgentsDriver(_context("inspect"), propose)

    # When
    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    # Then
    _assert_tool_proposal(proposal)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_should_bind_deterministic_protocol_envelope_to_model_intent() -> None:
    async def inspect(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return _tool_call()

    async def reserve(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return _tool_call("reserve")

    context = _context("inspect", "reserve")
    observation = _observation()
    first = await DeepAgentsDriver(context, inspect).next_action(
        observation, (_descriptor("inspect"),)
    )
    repeated = await DeepAgentsDriver(context, inspect).next_action(
        observation, (_descriptor("inspect"),)
    )
    changed = await DeepAgentsDriver(context, reserve).next_action(
        observation, (_descriptor("reserve"),)
    )
    later = await DeepAgentsDriver(context, reserve).next_action(
        observation.model_copy(update={"saga_seq": 4}), (_descriptor("reserve"),)
    )

    assert first.proposal_id == repeated.proposal_id == changed.proposal_id
    assert later.proposal_id != first.proposal_id
    assert first.based_on_saga_seq == 3
    assert later.based_on_saga_seq == 4


@pytest.mark.asyncio
async def test_should_reject_model_forged_protocol_envelope() -> None:
    raw = _tool_call()
    intent = cast(dict[str, object], raw["proposal"])
    intent["proposal_id"] = "proposal_forged01"
    intent["based_on_saga_seq"] = 3

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    with pytest.raises(AgentPlanningError, match="invalid_response"):
        await DeepAgentsDriver(_context("inspect"), propose).next_action(
            _observation(), (_descriptor("inspect"),)
        )


def test_model_schema_should_omit_kernel_owned_envelope_fields() -> None:
    schema = json.dumps(AgentDecision.model_json_schema(), sort_keys=True)

    assert "proposal_id" not in schema
    assert "based_on_saga_seq" not in schema


def test_model_schema_should_require_discriminator_for_every_intent_variant() -> None:
    definitions = AgentDecision.model_json_schema()["$defs"]
    intent_variants = [
        definition
        for definition in definitions.values()
        if "kind" in definition.get("properties", {})
    ]

    assert len(intent_variants) == 4
    assert all("kind" in definition.get("required", []) for definition in intent_variants)


@pytest.mark.asyncio
async def test_should_render_manifest_observation_and_current_eligible_catalog() -> None:
    # Given
    calls: list[tuple[str, str]] = []

    async def propose(system_context: str, turn_context: str) -> object:
        calls.append((system_context, turn_context))
        return _tool_call()

    context = _context("inspect", "reserve")
    driver = DeepAgentsDriver(context, propose)

    # When
    await driver.next_action(_observation(), (_descriptor("inspect"),))

    # Then
    system_context, turn_context = calls[0]
    rendered = json.loads(turn_context)
    assert context.agent_context in system_context
    assert "Only the deterministic Saga kernel executes business tools" in system_context
    assert rendered["observation"]["saga_seq"] == 3
    assert [item["name"] for item in rendered["available_tools"]] == ["inspect"]


@pytest.mark.asyncio
async def test_system_context_should_teach_generic_compensation_decisions() -> None:
    calls: list[str] = []

    async def propose(system_context: str, turn_context: str) -> object:
        del turn_context
        calls.append(system_context)
        return _tool_call()

    await DeepAgentsDriver(_context("inspect"), propose).next_action(
        _observation(), (_descriptor("inspect"),)
    )

    system_context = calls[0]
    assert "Call begin_compensation" in system_context
    assert "Call finish_saga" in system_context
    assert "compensated_verified" in system_context
    assert "aborted_clean" in system_context
    assert "kernel derives rollback order" in system_context
    assert "runtime before another turn" in system_context
    assert "currently advertised compensation tool" in system_context
    assert "confirmed forward effect is success evidence, not failure" in system_context
    assert (
        "Never begin compensation from stale or missing evidence or speculation" in system_context
    )
    assert "Refresh facts needed for the next forward step" in system_context
    assert "proves the forward goal cannot safely complete" in system_context
    assert "last_action.event_type=terminal_denied" in system_context
    assert "failed forward invariant" in system_context
    assert "call begin_compensation immediately" in system_context
    assert "freshness=fresh" in system_context
    assert "never duplicate an already-satisfied effect" in system_context
    assert "never repeat the rejected effect" in system_context
    assert "explicitly requires human escalation" in system_context
    assert "reconciliation read" not in system_context
    assert "exact advertised native tool" in system_context


@pytest.mark.asyncio
async def test_system_context_should_teach_rejection_refresh_and_budget_decisions() -> None:
    calls: list[str] = []

    async def propose(system_context: str, turn_context: str) -> object:
        del turn_context
        calls.append(system_context)
        return _tool_call()

    await DeepAgentsDriver(_context("inspect"), propose).next_action(
        _observation(), (_descriptor("inspect"),)
    )

    system_context = calls[0]
    assert "last_action.event_type=proposal_rejected" in system_context
    assert "never repeat the rejected effect" in system_context
    assert "policy_constraints.rejection_refresh_tool" in system_context
    assert "call that exact eligible read before any retry" in system_context
    assert "Before the first mutation" in system_context
    assert "remaining_budget.turn_limit" in system_context
    assert "evidence, effects, and terminal proof" in system_context
    assert "finish aborted_clean before creating partial work" in system_context


@pytest.mark.asyncio
async def test_should_reject_hallucinated_tool_before_kernel_effect_entry() -> None:
    # Given
    raw = _tool_call("invented_tool")

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    driver = DeepAgentsDriver(_context("inspect"), propose)

    # When / Then
    with pytest.raises(ValueError, match="invalid proposal"):
        await driver.next_action(_observation(), (_descriptor("inspect"),))


@pytest.mark.asyncio
async def test_should_reject_descriptor_drift_before_calling_model() -> None:
    # Given
    called = False

    async def propose(system_context: str, turn_context: str) -> object:
        nonlocal called
        del system_context, turn_context
        called = True
        return _tool_call()

    changed = _descriptor("inspect").model_copy(update={"description": "Changed at runtime."})
    driver = DeepAgentsDriver(_context("inspect"), propose)

    # When / Then
    with pytest.raises(ValueError, match="descriptor catalog"):
        await driver.next_action(_observation(), (changed,))
    assert called is False


@pytest.mark.asyncio
async def test_should_reject_duplicate_descriptor_before_calling_model() -> None:
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        raise AssertionError("model must not be called")

    descriptor = _descriptor("inspect")
    driver = DeepAgentsDriver(_context("inspect"), propose)
    with pytest.raises(ValueError, match="descriptor catalog"):
        await driver.next_action(_observation(), (descriptor, descriptor))


@pytest.mark.asyncio
async def test_should_reject_private_observation_before_calling_model() -> None:
    # Given
    called = False

    async def propose(system_context: str, turn_context: str) -> object:
        nonlocal called
        del system_context, turn_context
        called = True
        return _tool_call()

    private = _observation().model_copy(update={"projection": {"api_key": "private-value"}})
    driver = DeepAgentsDriver(_context("inspect"), propose)

    # When / Then
    with pytest.raises(ValueError, match="private material"):
        await driver.next_action(private, (_descriptor("inspect"),))
    assert called is False


@pytest.mark.asyncio
async def test_should_reject_high_confidence_credential_in_observation() -> None:
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        raise AssertionError("model must not be called")

    credential = "sk_" + "live_" + "abcdefghijklmnopqrstuv"
    goal = SagaGoal(
        goal_id="goal_1",
        text="Complete the public request.",
        context={"note": credential},
    )
    observation = _observation().model_copy(update={"goal": goal})
    driver = DeepAgentsDriver(_context("inspect"), propose)
    with pytest.raises(ValueError, match="private material") as captured:
        await driver.next_action(observation, (_descriptor("inspect"),))
    assert credential not in str(captured.value)


@pytest.mark.asyncio
async def test_should_reject_forged_agent_context_before_calling_model() -> None:
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        raise AssertionError("model must not be called")

    forged = _context("inspect").model_copy(update={"agent_context": "{}"})
    driver = DeepAgentsDriver(forged, propose)
    with pytest.raises(ValueError, match="authoritative"):
        await driver.next_action(_observation(), (_descriptor("inspect"),))


@pytest.mark.asyncio
async def test_should_reject_private_descriptor_in_direct_context() -> None:
    called = False

    async def propose(system_context: str, turn_context: str) -> object:
        nonlocal called
        del system_context, turn_context
        called = True
        return _tool_call()

    context = _context("inspect")
    private = context.tool_descriptors[0].model_copy(
        update={"policy_constraints": {"api_key": "private-provider-value"}}
    )
    payload = {
        "manifest": context.manifest.model_dump(mode="json"),
        "tools": [private.model_dump(mode="json")],
    }
    unsafe = context.model_copy(
        update={"agent_context": canonical_json(payload).decode(), "tool_descriptors": (private,)}
    )
    with pytest.raises(ValueError, match="private material"):
        await DeepAgentsDriver(unsafe, propose).next_action(_observation(), (private,))
    assert called is False


@pytest.mark.asyncio
async def test_should_reject_high_confidence_credential_in_direct_descriptor() -> None:
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        raise AssertionError("model must not be called")

    context = _context("inspect")
    credential = "sk_" + "live_" + "abcdefghijklmnopqrstuv"
    private = context.tool_descriptors[0].model_copy(
        update={"description": f"Use {credential} for inspection."}
    )
    payload = {
        "manifest": context.manifest.model_dump(mode="json"),
        "tools": [private.model_dump(mode="json")],
    }
    unsafe = context.model_copy(
        update={"agent_context": canonical_json(payload).decode(), "tool_descriptors": (private,)}
    )
    with pytest.raises(ValueError, match="private material") as captured:
        await DeepAgentsDriver(unsafe, propose).next_action(_observation(), (private,))
    assert credential not in str(captured.value)


@pytest.mark.asyncio
async def test_should_reject_private_manifest_in_direct_context() -> None:
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        raise AssertionError("model must not be called")

    context = _context("inspect")
    manifest = context.manifest.model_copy(update={"objective": "Bearer private-provider-value"})
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "tools": [item.model_dump(mode="json") for item in context.tool_descriptors],
    }
    unsafe = context.model_copy(
        update={"agent_context": canonical_json(payload).decode(), "manifest": manifest}
    )
    with pytest.raises(ValueError, match="invalid or private manifest") as captured:
        await DeepAgentsDriver(unsafe, propose).next_action(
            _observation(), (_descriptor("inspect"),)
        )
    assert "private-provider-value" not in str(captured.value)


@pytest.mark.asyncio
async def test_should_replace_raw_provider_error_with_safe_failure() -> None:
    private_error = "provider failed with private authorization material"

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        raise RuntimeError(private_error)

    with pytest.raises(AgentPlanningError, match="internal") as captured:
        await DeepAgentsDriver(_context("inspect"), propose).next_action(
            _observation(), (_descriptor("inspect"),)
        )
    _assert_safe_error(captured.value, AgentFailureCategory.INTERNAL, private_error)


@pytest.mark.asyncio
async def test_should_replace_malformed_secret_response_with_safe_failure() -> None:
    raw = {"proposal": {"authorization": "Bearer private-provider-value"}}

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    with pytest.raises(AgentPlanningError, match="invalid_response") as captured:
        await DeepAgentsDriver(_context("inspect"), propose).next_action(
            _observation(), (_descriptor("inspect"),)
        )
    _assert_safe_error(captured.value, AgentFailureCategory.INVALID_RESPONSE, "private-provider")


def _assert_safe_error(
    error: AgentPlanningError, category: AgentFailureCategory, private: str
) -> None:
    assert error.category is category
    assert private not in f"{error!s}{error!r}"
    assert private not in repr(error.args)
    assert private not in repr(vars(error))
    assert error.__cause__ is None
    assert error.__context__ is None


class _ProviderError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class _ProviderResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _NestedProviderError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        self.response = _ProviderResponse(status_code)
        super().__init__(message)


class _HostileProviderError(RuntimeError):
    @property
    def status_code(self) -> int:
        raise RuntimeError("private-status-property")

    @property
    def response(self) -> object:
        raise RuntimeError("private-response-property")


class _HostileValue:
    def __repr__(self) -> str:
        return "private-provider-object"


class _HostileDecision(AgentDecision):
    def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError("private-model-dump")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "category"),
    [
        (ConnectionError("private URL"), AgentFailureCategory.TRANSPORT_EXHAUSTED),
        (
            UnexpectedModelBehavior("private malformed tool call"),
            AgentFailureCategory.INVALID_RESPONSE,
        ),
        (_ProviderError(400, "private request"), AgentFailureCategory.REQUEST_REJECTED),
        (_ProviderError(499, "private request"), AgentFailureCategory.REQUEST_REJECTED),
        (
            _NestedProviderError(401, "private response"),
            AgentFailureCategory.REQUEST_REJECTED,
        ),
        (_ProviderError(429, "private body"), AgentFailureCategory.RATE_LIMIT_EXHAUSTED),
        (
            _NestedProviderError(429, "private response"),
            AgentFailureCategory.RATE_LIMIT_EXHAUSTED,
        ),
        (_ProviderError(503, "private headers"), AgentFailureCategory.SERVER_ERROR_EXHAUSTED),
        (
            _NestedProviderError(502, "private response"),
            AgentFailureCategory.SERVER_ERROR_EXHAUSTED,
        ),
    ],
)
async def test_should_preserve_only_safe_provider_failure_category(
    error: Exception, category: AgentFailureCategory
) -> None:
    private = str(error)

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        raise error

    driver = DeepAgentsDriver(_context("inspect"), propose)
    with pytest.raises(AgentPlanningError, match=category.value) as captured:
        await driver.next_action(_observation(), (_descriptor("inspect"),))
    _assert_safe_error(captured.value, category, private)


@pytest.mark.asyncio
async def test_should_categorize_provider_error_when_status_properties_raise() -> None:
    # Given
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        raise _HostileProviderError("private-provider-body")

    # When / Then
    with pytest.raises(AgentPlanningError) as captured:
        await DeepAgentsDriver(_context("inspect"), propose).next_action(
            _observation(), (_descriptor("inspect"),)
        )
    _assert_safe_error(captured.value, AgentFailureCategory.INTERNAL, "private-")


@pytest.mark.asyncio
async def test_should_sanitize_deep_provider_response_before_pydantic_recurses() -> None:
    # Given
    root: dict[str, object] = {}
    current = root
    for _ in range(1_500):
        child: dict[str, object] = {}
        current["next"] = child
        current = child
    raw = _tool_call()
    proposal = cast(dict[str, object], raw["proposal"])
    proposal["arguments"] = root

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    # When / Then
    with pytest.raises(AgentPlanningError) as captured:
        await DeepAgentsDriver(_context("inspect"), propose).next_action(
            _observation(), (_descriptor("inspect"),)
        )
    _assert_safe_error(captured.value, AgentFailureCategory.INVALID_RESPONSE, "next")


@pytest.mark.asyncio
async def test_should_sanitize_non_json_provider_response_before_pydantic() -> None:
    # Given
    raw = _tool_call()
    proposal = cast(dict[str, object], raw["proposal"])
    proposal["arguments"] = {"value": _HostileValue()}

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    # When / Then
    with pytest.raises(AgentPlanningError) as captured:
        await DeepAgentsDriver(_context("inspect"), propose).next_action(
            _observation(), (_descriptor("inspect"),)
        )
    _assert_safe_error(
        captured.value, AgentFailureCategory.INVALID_RESPONSE, "private-provider-object"
    )


@pytest.mark.asyncio
async def test_should_not_call_overridden_dump_on_agent_decision_subclass() -> None:
    # Given
    decision = AgentDecision.model_validate(_tool_call(), strict=True)
    raw = _HostileDecision.model_construct(proposal=decision.proposal)

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    # When / Then
    with pytest.raises(AgentPlanningError) as captured:
        await DeepAgentsDriver(_context("inspect"), propose).next_action(
            _observation(), (_descriptor("inspect"),)
        )
    _assert_safe_error(captured.value, AgentFailureCategory.INVALID_RESPONSE, "private-model-dump")


@pytest.mark.asyncio
async def test_should_reject_private_material_in_valid_provider_proposal() -> None:
    credential = "Bearer " + "private-provider-value"
    raw = _tool_call()
    proposal = cast(dict[str, object], raw["proposal"])
    proposal["arguments"] = {"authorization": credential}

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    driver = DeepAgentsDriver(_context("inspect"), propose)
    with pytest.raises(ValueError, match="private material") as captured:
        await driver.next_action(_observation(), (_descriptor("inspect"),))
    assert credential not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "proposal_type", "controls"),
    [
        (
            _finish(),
            Finish,
            ControlProposalCapabilities(finish_targets=("succeeded_verified",)),
        ),
        (
            _begin_compensation(),
            BeginCompensation,
            ControlProposalCapabilities(begin_compensation=True),
        ),
        (_escalate(), Escalate, ControlProposalCapabilities(escalate_to_human=True)),
    ],
)
async def test_should_accept_each_non_effect_proposal(
    raw: dict[str, object],
    proposal_type: type[Finish] | type[BeginCompensation] | type[Escalate],
    controls: ControlProposalCapabilities,
) -> None:
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    proposal = await DeepAgentsDriver(_context("inspect"), propose).next_action(
        _observation(controls), (_descriptor("inspect"),)
    )
    assert isinstance(proposal, proposal_type)


@pytest.mark.asyncio
async def test_should_reject_model_supplied_stale_sequence() -> None:
    raw = _tool_call()
    intent = cast(dict[str, object], raw["proposal"])
    intent["based_on_saga_seq"] = 2

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    driver = DeepAgentsDriver(_context("inspect"), propose)
    with pytest.raises(AgentPlanningError, match="invalid_response"):
        await driver.next_action(_observation(), (_descriptor("inspect"),))


@pytest.mark.asyncio
async def test_should_render_dynamic_eligible_catalog_each_turn() -> None:
    catalogs: list[list[str]] = []

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context
        rendered = json.loads(turn_context)
        catalogs.append([item["name"] for item in rendered["available_tools"]])
        return _tool_call(catalogs[-1][0])

    driver = DeepAgentsDriver(_context("inspect", "reserve"), propose)
    await driver.next_action(_observation(), (_descriptor("inspect"),))
    await driver.next_action(_observation(), (_descriptor("reserve"),))
    assert catalogs == [["inspect"], ["reserve"]]


@pytest.mark.asyncio
async def test_should_accept_public_runtime_policy_constraints() -> None:
    calls: list[str] = []

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context
        calls.append(turn_context)
        return _tool_call()

    current = _descriptor("inspect").model_copy(
        update={"policy_constraints": {"sequence_bound": True}}
    )
    await DeepAgentsDriver(_context("inspect"), propose).next_action(_observation(), (current,))
    rendered = json.loads(calls[0])
    assert rendered["available_tools"][0]["policy_constraints"] == {"sequence_bound": True}


@pytest.mark.asyncio
async def test_should_reject_private_runtime_policy_constraints() -> None:
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        raise AssertionError("model must not be called")

    current = _descriptor("inspect").model_copy(
        update={"policy_constraints": {"api_key": "private-provider-value"}}
    )
    driver = DeepAgentsDriver(_context("inspect"), propose)
    with pytest.raises(ValueError, match="private material"):
        await driver.next_action(_observation(), (current,))


@pytest.mark.asyncio
async def test_should_render_identical_inputs_deterministically() -> None:
    calls: list[tuple[str, str]] = []

    async def propose(system_context: str, turn_context: str) -> object:
        calls.append((system_context, turn_context))
        return _tool_call()

    driver = DeepAgentsDriver(_context("inspect"), propose)
    await driver.next_action(_observation(), (_descriptor("inspect"),))
    await driver.next_action(_observation(), (_descriptor("inspect"),))
    assert calls[0] == calls[1]


@pytest.mark.asyncio
async def test_should_cooperate_with_kernel_timeout_cancellation() -> None:
    cancelled = asyncio.Event()

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    driver = DeepAgentsDriver(_context("inspect"), propose)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await driver.next_action(_observation(), (_descriptor("inspect"),))
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_pydantic_deep_should_call_one_native_eligible_proposal_tool() -> None:
    model = TestModel(call_tools=["inspect"])
    driver = DeepAgentsDriver._from_model(
        _context("inspect"),
        model,
        provider_id="test",
        model_route=("test/native-tools",),
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    _assert_tool_proposal(proposal)
    parameters = model.last_model_request_parameters
    assert parameters is not None
    assert [tool.name for tool in parameters.function_tools] == [
        "inspect",
        "finish_saga",
        "escalate_to_human",
    ]


@pytest.mark.asyncio
async def test_pydantic_deep_should_require_a_native_tool_call_instead_of_text() -> None:
    model = _TextUnlessToolRequiredModel(call_tools=[], custom_output_text="I would inspect first.")
    driver = DeepAgentsDriver._from_model(
        _context("inspect"),
        model,
        provider_id="test",
        model_route=("test/native-tools",),
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    _assert_tool_proposal(proposal)
    assert model.observed_settings is not None
    assert model.observed_settings["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_should_correct_free_text_once_into_a_native_deferred_tool() -> None:
    model = _TextThenNativeToolModel(call_tools=[])
    driver = DeepAgentsDriver._from_model(
        _context("inspect"),
        model,
        provider_id="test",
        model_route=("test/native-tools",),
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    _assert_tool_proposal(proposal)
    assert model.requests == 2


@pytest.mark.asyncio
async def test_should_never_exceed_two_model_requests_across_retry_categories() -> None:
    model = _InvalidToolThenTextThenToolModel()
    driver = DeepAgentsDriver._from_model(
        _context("inspect"), model, provider_id="test", model_route=("test/native-tools",)
    )

    with pytest.raises(AgentPlanningError):
        await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert model.requests == 2


def test_should_keep_arbitrary_prebuilt_model_construction_internal() -> None:
    assert not hasattr(DeepAgentsDriver, "from_model")


@pytest.mark.asyncio
async def test_should_expose_only_eligible_business_and_control_proposal_tools() -> None:
    model = TestModel(call_tools=["reserve"])
    driver = DeepAgentsDriver._from_model(
        _context("inspect", "reserve"),
        model,
        provider_id="test",
        model_route=("test/native-tools",),
    )
    proposal = await driver.next_action(
        _observation(),
        (_descriptor("reserve"),),
    )

    _assert_tool_proposal(proposal, "reserve")
    parameters = model.last_model_request_parameters
    assert parameters is not None
    assert [tool.name for tool in parameters.function_tools] == [
        "reserve",
        "finish_saga",
        "escalate_to_human",
    ]


@pytest.mark.asyncio
async def test_should_scope_model_resources_to_each_durable_agent_turn() -> None:
    model = _ContextTrackingModel(call_tools=["inspect"])
    driver = DeepAgentsDriver._from_model(
        _context("inspect"),
        model,
        provider_id="test",
        model_route=("test/native-tools",),
    )

    await driver.next_action(_observation(), (_descriptor("inspect"),))
    await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert (model.enters, model.exits) == (2, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "proposal_type"),
    [
        ("finish_saga", Finish),
        ("begin_compensation", BeginCompensation),
        ("escalate_to_human", Escalate),
    ],
)
async def test_should_model_control_decisions_as_native_proposal_tools(
    tool_name: str,
    proposal_type: type[Finish] | type[BeginCompensation] | type[Escalate],
) -> None:
    controls = ControlProposalCapabilities(
        finish_targets=("succeeded_verified", "aborted_clean"),
        begin_compensation=True,
        escalate_to_human=True,
    )
    model = TestModel(call_tools=[tool_name])
    driver = DeepAgentsDriver._from_model(
        _context("inspect"),
        model,
        provider_id="test",
        model_route=("test/native-tools",),
    )

    proposal = await driver.next_action(_observation(controls), (_descriptor("inspect"),))

    assert isinstance(proposal, proposal_type)
    assert proposal.based_on_saga_seq == 3
    assert proposal.proposal_id.startswith("proposal_")


@pytest.mark.asyncio
async def test_should_remove_begin_compensation_after_compensation_starts() -> None:
    model = TestModel(call_tools=["inspect"])
    driver = DeepAgentsDriver._from_model(
        _context("inspect"),
        model,
        provider_id="test",
        model_route=("test/native-tools",),
    )
    controls = ControlProposalCapabilities(
        finish_targets=("compensated_verified",),
        escalate_to_human=True,
    )
    observation = _observation(controls).model_copy(update={"state": SagaStatus.COMPENSATING})

    await driver.next_action(observation, (_descriptor("inspect"),))

    parameters = model.last_model_request_parameters
    assert parameters is not None
    assert [tool.name for tool in parameters.function_tools] == [
        "inspect",
        "finish_saga",
        "escalate_to_human",
    ]


def test_should_report_the_exact_state_dependent_native_tool_allowlist() -> None:
    running = native_proposal_tool_names(_observation(), (_descriptor("inspect"),))
    compensable = native_proposal_tool_names(
        _observation(
            ControlProposalCapabilities(
                finish_targets=("succeeded_verified", "aborted_clean"),
                begin_compensation=True,
                escalate_to_human=True,
            )
        ),
        (_descriptor("inspect"),),
    )
    controls = ControlProposalCapabilities(
        finish_targets=("compensated_verified",),
        escalate_to_human=True,
    )
    compensating = native_proposal_tool_names(
        _observation(controls).model_copy(update={"state": SagaStatus.COMPENSATING}),
        (_descriptor("refund"),),
    )

    assert running == (
        "inspect",
        "finish_saga",
        "escalate_to_human",
    )
    assert compensable == (
        "inspect",
        "finish_saga",
        "begin_compensation",
        "escalate_to_human",
    )
    assert compensating == ("refund", "finish_saga", "escalate_to_human")


@pytest.mark.asyncio
async def test_should_restrict_finish_schema_to_kernel_advertised_targets() -> None:
    controls = ControlProposalCapabilities(finish_targets=("compensated_verified",))
    observation = _observation(controls).model_copy(update={"state": SagaStatus.COMPENSATING})
    model = TestModel(call_tools=["finish_saga"])
    driver = DeepAgentsDriver._from_model(
        _context("inspect"),
        model,
        provider_id="test",
        model_route=("test/native-tools",),
    )

    await driver.next_action(observation, ())

    parameters = model.last_model_request_parameters
    assert parameters is not None
    finish = next(tool for tool in parameters.function_tools if tool.name == "finish_saga")
    terminal = finish.parameters_json_schema["properties"]["target_status"]
    assert terminal["enum"] == ["compensated_verified"]


@pytest.mark.asyncio
async def test_should_reject_multiple_native_proposal_calls_without_executing_effects() -> None:
    model = TestModel(call_tools=["inspect", "finish_saga"])
    driver = DeepAgentsDriver._from_model(
        _context("inspect"),
        model,
        provider_id="test",
        model_route=("test/native-tools",),
    )

    with pytest.raises(AgentPlanningError, match="invalid_response"):
        await driver.next_action(_observation(), (_descriptor("inspect"),))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "controls"),
    [
        (_begin_compensation(), ControlProposalCapabilities()),
        (_escalate(), ControlProposalCapabilities()),
        (
            _finish(),
            ControlProposalCapabilities(finish_targets=("compensated_verified",)),
        ),
    ],
)
async def test_should_reject_a_control_outside_the_kernel_advertised_surface(
    raw: dict[str, object],
    controls: ControlProposalCapabilities,
) -> None:
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    driver = DeepAgentsDriver(_context("inspect"), propose)

    with pytest.raises(ValueError, match="invalid proposal"):
        await driver.next_action(_observation(controls), (_descriptor("inspect"),))


@pytest.mark.asyncio
async def test_should_reject_model_defined_compensation_policy_reason() -> None:
    raw = _begin_compensation()
    intent = cast(dict[str, object], raw["proposal"])
    intent["reason_code"] = "payment_failed"

    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    controls = ControlProposalCapabilities(begin_compensation=True)
    driver = DeepAgentsDriver(_context("inspect"), propose)

    with pytest.raises(AgentPlanningError, match="invalid_response"):
        await driver.next_action(_observation(controls), (_descriptor("inspect"),))


def test_should_strip_every_unneeded_pydantic_deep_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CreatedAgent:
        instrument: object = True

        def output_validator(self, func: Callable[[object], object]) -> object:
            captured["output_validator"] = func
            return func

    created = CreatedAgent()

    def create_agent(**kwargs: object) -> object:
        captured.update(kwargs)
        return created

    monkeypatch.setattr(pydantic_deep_package, "create_deep_agent", create_agent)
    DeepAgentsDriver._from_model(
        _context("inspect"),
        TestModel(call_tools=["inspect"]),
        provider_id="test",
        model_route=("test/native-tools",),
    )

    disabled = (
        "include_todo",
        "include_filesystem",
        "include_subagents",
        "include_skills",
        "include_builtin_subagents",
        "include_plan",
        "include_memory",
        "include_teams",
        "include_monitoring",
        "include_improve",
        "include_liteparse",
        "include_checkpoints",
        "include_history_archive",
        "context_manager",
        "context_discovery",
        "patch_tool_calls",
        "stuck_loop_detection",
        "web_search",
        "web_fetch",
        "thinking",
        "cost_tracking",
        "forking",
        "tool_search",
    )
    assert all(captured[name] is False for name in disabled)
    assert captured["eviction_token_limit"] is None
    assert captured["history_processors"] == ()
    assert captured["tools"] == ()
    assert captured["toolsets"] == ()
    capabilities = cast(tuple[object, ...], captured["capabilities"])
    assert [type(item).__name__ for item in capabilities] == ["RequireToolCall"]
    model_settings = cast(dict[str, object], captured["model_settings"])
    assert "parallel_tool_calls" not in model_settings
    assert model_settings["temperature"] == 0
    assert model_settings["openrouter_cache_instructions"] is False
    assert model_settings["openrouter_cache_tool_definitions"] is False
    assert model_settings["openrouter_cache_messages"] is False
    assert captured["retries"] == 1
    assert callable(captured["output_validator"])
    assert "instrument" not in captured
    assert created.instrument is False


def test_should_disable_ambient_pydantic_ai_instrumentation() -> None:
    PydanticAgent.instrument_all(True)
    try:
        driver = DeepAgentsDriver._from_model(
            _context("inspect"),
            TestModel(call_tools=["inspect"]),
            provider_id="test",
            model_route=("test/native-tools",),
        )
    finally:
        PydanticAgent.instrument_all(False)

    native = cast(adapter_module._NativeProposalCall, driver.proposal_call)
    assert native.agent.instrument is False


def test_should_fail_safely_when_deep_agents_extra_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> object:
        del name
        raise ModuleNotFoundError("raw dependency internals")

    monkeypatch.setattr(adapter_module, "import_module", missing)
    with pytest.raises(RuntimeError, match=r"install agentic-saga\[agent\]") as captured:
        adapter_module._load_pydantic_dependencies()
    assert "raw dependency internals" not in str(captured.value)
    assert captured.value.__cause__ is None
