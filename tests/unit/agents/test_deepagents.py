from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

import deepagents as deepagents_package
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from agentic_saga import SagaContext, SagaManifest
from agentic_saga.agents import DeepAgentsDriver
from agentic_saga.agents import deepagents as adapter_module
from agentic_saga.agents.deepagents import AgentDecision, AgentFailureCategory, AgentPlanningError
from agentic_saga.contracts.actions import BeginCompensation, Escalate, Finish, ToolCall
from agentic_saga.contracts.common import canonical_json
from agentic_saga.contracts.runtime import (
    ExecutionBudget,
    SagaGoal,
    SagaObservation,
    SagaStatus,
    ToolDescriptor,
)

type ProposalCall = Callable[[str, str], Awaitable[object]]


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


def _observation() -> SagaObservation:
    return SagaObservation(
        saga_id="saga_0123456789abcdef",
        saga_seq=3,
        state=SagaStatus.RUNNING,
        goal=SagaGoal(goal_id="goal_1", text="Complete the public request.", context={}),
        last_action=None,
        projection={"status": "pending"},
        remaining_budget=_budget(),
    )


def _tool_call(tool_name: str = "inspect", saga_seq: int = 3) -> dict[str, object]:
    return {
        "proposal": {
            "kind": "tool_call",
            "proposal_id": "proposal_12345678",
            "tool_name": tool_name,
            "arguments": {},
            "based_on_saga_seq": saga_seq,
            "rationale": "Inspect current evidence.",
        }
    }


def _finish() -> dict[str, object]:
    return {
        "proposal": {
            "kind": "finish",
            "proposal_id": "proposal_12345678",
            "based_on_saga_seq": 3,
            "rationale": "All registered checks can now be evaluated.",
            "target_status": "succeeded_verified",
        }
    }


def _escalate() -> dict[str, object]:
    return {
        "proposal": {
            "kind": "escalate",
            "proposal_id": "proposal_12345678",
            "based_on_saga_seq": 3,
            "reason_code": "ambiguous_evidence",
            "rationale": "A human must resolve conflicting public evidence.",
        }
    }


def _begin_compensation() -> dict[str, object]:
    return {
        "proposal": {
            "kind": "begin_compensation",
            "proposal_id": "proposal_12345678",
            "based_on_saga_seq": 3,
            "reason_code": "goal_unreachable",
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
    assert proposal == ToolCall.model_validate(_tool_call()["proposal"])
    assert len(calls) == 1


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
        (_ProviderError(429, "private body"), AgentFailureCategory.RATE_LIMIT_EXHAUSTED),
        (_ProviderError(503, "private headers"), AgentFailureCategory.SERVER_ERROR_EXHAUSTED),
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
    proposal = ToolCall.model_validate(_tool_call()["proposal"])
    raw = _HostileDecision.model_construct(proposal=proposal)

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
    ("raw", "proposal_type"),
    [
        (_finish(), Finish),
        (_begin_compensation(), BeginCompensation),
        (_escalate(), Escalate),
    ],
)
async def test_should_accept_each_non_effect_proposal(
    raw: dict[str, object],
    proposal_type: type[Finish] | type[BeginCompensation] | type[Escalate],
) -> None:
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return raw

    proposal = await DeepAgentsDriver(_context("inspect"), propose).next_action(
        _observation(), (_descriptor("inspect"),)
    )
    assert isinstance(proposal, proposal_type)


@pytest.mark.asyncio
async def test_should_reject_stale_proposal_sequence() -> None:
    async def propose(system_context: str, turn_context: str) -> object:
        del system_context, turn_context
        return _tool_call(saga_seq=2)

    driver = DeepAgentsDriver(_context("inspect"), propose)
    with pytest.raises(ValueError, match="invalid proposal"):
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


class _RecordingGraph:
    def __init__(self) -> None:
        self.config: dict[str, object] = {}

    async def ainvoke(self, value: dict[str, object], config: dict[str, object]) -> object:
        del value
        self.config = config
        return {"structured_response": _tool_call()}


class StructuredFakeChatModel(FakeMessagesListChatModel):
    model: str = "fake/structured"


def _fake_model(monkeypatch: pytest.MonkeyPatch, exposed: list[str]) -> StructuredFakeChatModel:
    def bind_tools(
        model: BaseChatModel, tools: Sequence[object], **kwargs: object
    ) -> BaseChatModel:
        del kwargs
        exposed.extend(_named_tools(tools))
        return model

    monkeypatch.setattr(StructuredFakeChatModel, "bind_tools", bind_tools)
    message = AIMessage.model_validate(
        {
            "content": "",
            "tool_calls": [
                {"name": "AgentDecision", "args": _tool_call(), "id": "call_1", "type": "tool_call"}
            ],
        }
    )
    return StructuredFakeChatModel(responses=[message, AIMessage(content="unexpected")])


def _named_tools(tools: Sequence[object]) -> list[str]:
    names = [getattr(item, "name", None) for item in tools]
    return [name for name in names if isinstance(name, str)]


@pytest.mark.asyncio
async def test_real_deep_agent_graph_should_return_fake_structured_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposed: list[str] = []
    model = _fake_model(monkeypatch, exposed)
    driver = DeepAgentsDriver._from_model(
        _context("inspect"),
        model,
        provider_id="structuredfakechatmodel",
        model_route=("fake/structured",),
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert proposal == ToolCall.model_validate(_tool_call()["proposal"])
    assert "AgentDecision" in exposed
    assert "inspect" not in exposed
    assert "task" not in exposed
    assert model.i == 1


def test_should_keep_arbitrary_prebuilt_model_construction_internal() -> None:
    assert not hasattr(DeepAgentsDriver, "from_model")


@pytest.mark.asyncio
async def test_should_pass_no_business_tools_to_deep_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _RecordingGraph()
    captured_tools: Sequence[object] | None = None
    profile_keys: list[str] = []

    def create_agent(model: object, tools: Sequence[object], **kwargs: object) -> _RecordingGraph:
        nonlocal captured_tools
        del model, kwargs
        captured_tools = tools
        return graph

    monkeypatch.setattr(deepagents_package, "create_deep_agent", create_agent)
    monkeypatch.setattr(
        deepagents_package,
        "register_harness_profile",
        lambda key, profile: profile_keys.append(key),
    )
    model = _fake_model(monkeypatch, [])
    driver = DeepAgentsDriver._from_model(
        _context("inspect"),
        model,
        provider_id="structuredfakechatmodel",
        model_route=("fake/structured",),
    )
    await driver.next_action(_observation(), (_descriptor("inspect"),))
    assert captured_tools == ()
    assert profile_keys == ["structuredfakechatmodel:fake/structured"]
    assert graph.config["recursion_limit"] == 8


def test_should_fail_safely_when_deep_agents_extra_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> object:
        del name
        raise ModuleNotFoundError("raw dependency internals")

    monkeypatch.setattr(adapter_module, "import_module", missing)
    with pytest.raises(RuntimeError, match=r"install agentic-saga\[agent\]") as captured:
        adapter_module._load_deep_agent_dependencies()
    assert "raw dependency internals" not in str(captured.value)
    assert captured.value.__cause__ is None
