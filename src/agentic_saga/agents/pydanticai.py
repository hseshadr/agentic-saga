from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from types import MappingProxyType, ModuleType
from typing import Annotated, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError

from agentic_saga.contracts.actions import (
    AgentProposal,
    Finish,
    ToolCall,
)
from agentic_saga.contracts.common import (
    JsonObject,
    JsonPayloadError,
    canonical_json,
    require_bounded_json,
    sha256_json,
    thaw_json_object,
)
from agentic_saga.contracts.runtime import AgentDriver, SagaObservation, ToolDescriptor
from agentic_saga.manifest import SagaContext, SagaManifest, require_public_agent_payload

type ProposalCall = Callable[[str, str], Awaitable[object]]
type _BoundedName = Annotated[str, StringConstraints(min_length=1, max_length=200)]
type _Rationale = Annotated[str, StringConstraints(min_length=1, max_length=500)]
type _TerminalStatus = Literal["succeeded_verified"]

_TOO_MANY_REQUESTS = 429
_CLIENT_ERROR_RANGE = range(400, 500)
_SERVER_ERROR_RANGE = range(500, 600)
_PROPOSAL_ID_DOMAIN = "agentic-saga:native-tool-proposal:v1"
_TOOLSET_ID = "agentic-saga-eligible-proposals"
_FINISH_TOOL = "finish_saga"
_CONTROL_TOOLS = frozenset({_FINISH_TOOL})
_FINISH_DESCRIPTION = "Propose verified success to the deterministic Temporal workflow."
_NATIVE_DEFERRED_CALL_LIMIT = 1
_MODEL_RESULT_RETRIES = 1
_AGENT_PROPOSAL: TypeAdapter[AgentProposal] = TypeAdapter(AgentProposal)
_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_AUTHORITY = "\n".join(
    (
        "Agentic Saga authority:",
        "Call exactly one advertised native proposal tool from current public evidence.",
        "Only the deterministic Temporal workflow executes business tools or assigns state.",
        "Never invent a tool, receipt, approval, idempotency key, outcome, or private fact.",
        "The adapter assigns proposal identity and binds the current Saga sequence.",
        "Reuse read_evidence only when freshness=fresh; refresh affected stale facts.",
        "Tool calls express concise business intent, never private reasoning.",
        "",
        "Decision examples; apply the pattern to the current domain:",
        "- If required facts are absent, call the exact eligible read that proves them.",
        "- Use current evidence to never duplicate an already-satisfied effect.",
        (
            "- If last_action.event_type=proposal_rejected, never repeat the rejected effect. "
            "Use policy_constraints.rejection_refresh_tool and call that exact eligible read "
            "before any retry."
        ),
        (
            "- Before the first mutation, ensure remaining_budget.turn_limit covers the "
            "evidence, effects, and terminal proof; otherwise avoid creating partial work."
        ),
        (
            "- A confirmed forward effect is success evidence. Refresh facts needed for the "
            "next currently eligible business choice."
        ),
        (
            "- Call finish_saga with succeeded_verified only when it is advertised and current "
            "checks prove the goal."
        ),
        (
            "- An ordinary failed proof triggers workflow-owned compensation; unresolved "
            "evidence triggers verified human handling."
        ),
        (
            "- The deterministic Temporal workflow owns reconciliation, reverse compensation, "
            "and entry into HUMAN_REQUIRED."
        ),
        "- Unknown outcomes are reconciled by the Temporal workflow before another agent turn.",
        (
            "- Select only an exact advertised native tool; an exact advertised action is "
            "available, so never claim that a matching action is unavailable or copy an "
            "unavailable example name."
        ),
    )
)
_AGENT_OPTIONS: Mapping[str, object] = MappingProxyType(
    {
        "model_settings": {
            "temperature": 0,
            "openrouter_cache_instructions": False,
            "openrouter_cache_tool_definitions": False,
            "openrouter_cache_messages": False,
        },
        "retries": _MODEL_RESULT_RETRIES,
    }
)


class AgentFailureCategory(StrEnum):
    """Closed, secret-free reason for an adapter planning failure."""

    RATE_LIMIT_EXHAUSTED = "rate_limit_exhausted"
    REQUEST_REJECTED = "request_rejected"
    SERVER_ERROR_EXHAUSTED = "server_error_exhausted"
    TRANSPORT_EXHAUSTED = "transport_exhausted"
    INVALID_RESPONSE = "invalid_response"
    INTERNAL = "internal"


class AgentPlanningError(RuntimeError):
    """Safe adapter error that never retains the provider exception."""

    def __init__(self, category: AgentFailureCategory) -> None:
        self.category = category
        super().__init__(category.value)


class _IntentBase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class _ToolCallIntent(_IntentBase):
    kind: Literal["tool_call"]
    tool_name: _BoundedName
    arguments: JsonObject
    rationale: _Rationale


class _FinishIntent(_IntentBase):
    kind: Literal["finish"]
    rationale: _Rationale
    target_status: _TerminalStatus


type _AgentIntent = Annotated[
    _ToolCallIntent | _FinishIntent,
    Field(discriminator="kind"),
]


class AgentDecision(BaseModel):
    """Legacy injected-call seam; provider-backed drivers use native tools."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    proposal: _AgentIntent


class _FinishArguments(_IntentBase):
    rationale: _Rationale
    target_status: _TerminalStatus


class _RunResult(Protocol):
    output: object


class _DeferredCall(Protocol):
    tool_name: str

    def args_as_dict(self) -> dict[str, object]: ...


class _DeferredRequests(Protocol):
    calls: list[_DeferredCall]
    approvals: list[object]


class _PydanticAgent(Protocol):
    instrument: object

    async def __aenter__(self) -> _PydanticAgent: ...

    async def __aexit__(self, *args: object) -> bool | None: ...

    async def run(
        self,
        prompt: str,
        *,
        toolsets: Sequence[object],
        usage_limits: object,
    ) -> _RunResult: ...

    def output_validator(self, func: Callable[[object], object]) -> object: ...


class _CreateAgent(Protocol):
    def __call__(self, **values: object) -> _PydanticAgent: ...


class _ExternalToolsetFactory(Protocol):
    def __call__(self, tools: list[object], *, id: str) -> object: ...


class _ToolDefinitionFactory(Protocol):
    def __call__(self, **values: object) -> object: ...


class _UsageLimitsFactory(Protocol):
    def __call__(self, *, request_limit: int) -> object: ...


@dataclass(frozen=True)
class _PydanticDependencies:
    create_agent: _CreateAgent
    external_toolset: _ExternalToolsetFactory
    tool_definition: _ToolDefinitionFactory
    deferred_requests_type: type[object]
    model_retry: Callable[[str], Exception]
    usage_limits: _UsageLimitsFactory
    required_tool_capability: object


@dataclass(frozen=True)
class _NativeProposalCall:
    agent: _PydanticAgent
    dependencies: _PydanticDependencies
    system_context: str

    async def propose(
        self,
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> AgentProposal:
        toolset = _proposal_toolset(self.dependencies, observation, available_tools)
        async with self.agent:
            result = await self.agent.run(
                _turn_context(observation, available_tools),
                toolsets=(toolset,),
                usage_limits=_usage_limits(self.dependencies),
            )
        return _proposal_from_output(result.output, observation, available_tools, self.dependencies)


@dataclass(frozen=True)
class PydanticAIDriver(AgentDriver):
    """Translate one native Pydantic AI tool call into one workflow proposal."""

    context: SagaContext
    proposal_call: ProposalCall | _NativeProposalCall
    provider_id: str = "injected"
    model_route: tuple[str, ...] = ("injected",)

    @classmethod
    def _from_model(
        cls,
        context: SagaContext,
        model: object,
        *,
        provider_id: str,
        model_route: tuple[str, ...],
    ) -> PydanticAIDriver:
        call = _build_pydantic_call(context, model)
        return cls(context, call, provider_id, model_route)

    async def next_action(
        self,
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> AgentProposal:
        _require_catalog(self.context, available_tools)
        require_public_agent_payload(_turn_payload(observation, available_tools))
        proposal = await self._propose(observation, available_tools)
        require_public_agent_payload(proposal.model_dump(mode="json"))
        _require_current(proposal, observation, available_tools)
        return proposal

    async def _propose(
        self,
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> AgentProposal:
        if isinstance(self.proposal_call, _NativeProposalCall):
            return await _call_native(self.proposal_call, observation, available_tools)
        system = _system_context(self.context)
        turn = _turn_context(observation, available_tools)
        raw = await _call_model(self.proposal_call, system, turn)
        return _validated_proposal(raw, observation)


async def _call_native(
    call: _NativeProposalCall,
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> AgentProposal:
    try:
        return await call.propose(observation, available_tools)
    except AgentPlanningError as error:
        raise error from None
    except Exception as error:
        category = _failure_category(error)
    raise AgentPlanningError(category) from None


async def _call_model(call: ProposalCall, system: str, turn: str) -> object:
    try:
        return await call(system, turn)
    except Exception as error:
        category = _failure_category(error)
    raise AgentPlanningError(category) from None


def _proposal_from_output(
    raw: object,
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
    dependencies: _PydanticDependencies,
) -> AgentProposal:
    try:
        intent = _intent_from_output(raw, available_tools, dependencies)
        proposal = _bind_proposal(intent, observation)
    except (AttributeError, JsonPayloadError, TypeError, ValidationError, ValueError):
        pass
    else:
        return proposal
    raise AgentPlanningError(AgentFailureCategory.INVALID_RESPONSE) from None


def _intent_from_output(
    raw: object,
    available_tools: Sequence[ToolDescriptor],
    dependencies: _PydanticDependencies,
) -> _AgentIntent:
    if not isinstance(raw, dependencies.deferred_requests_type):
        raise TypeError("agent did not return a deferred proposal")
    requests = cast(_DeferredRequests, raw)
    calls = requests.calls
    approvals = requests.approvals
    if not isinstance(calls, list) or approvals or len(calls) != _NATIVE_DEFERRED_CALL_LIMIT:
        raise ValueError("agent must return exactly one proposal call")
    return _intent_from_call(calls[0], available_tools)


def _intent_from_call(
    call: _DeferredCall, available_tools: Sequence[ToolDescriptor]
) -> _AgentIntent:
    name = call.tool_name
    arguments = _call_arguments(call)
    eligible = frozenset(item.name for item in available_tools)
    if name in eligible:
        return _ToolCallIntent(
            kind="tool_call",
            tool_name=name,
            arguments=arguments,
            rationale=f"Selected eligible Saga proposal tool {name}.",
        )
    return _control_intent(name, arguments)


def _call_arguments(call: _DeferredCall) -> JsonObject:
    arguments = call.args_as_dict()
    require_bounded_json(arguments)
    return _JSON_OBJECT.validate_python(arguments, strict=True)


def _control_intent(name: object, arguments: JsonObject) -> _AgentIntent:
    if name != _FINISH_TOOL:
        raise ValueError("agent selected an unavailable proposal tool")
    finish = _FinishArguments.model_validate(thaw_json_object(arguments), strict=True)
    return _FinishIntent(kind="finish", **finish.model_dump())


def _validated_proposal(raw: object, observation: SagaObservation) -> AgentProposal:
    try:
        payload = _decision_payload(raw)
        require_bounded_json(payload)
        decision = AgentDecision.model_validate(payload, strict=True)
    except (JsonPayloadError, TypeError, ValidationError, ValueError):
        pass
    else:
        return _bind_proposal(decision.proposal, observation)
    raise AgentPlanningError(AgentFailureCategory.INVALID_RESPONSE) from None


def _bind_proposal(intent: _AgentIntent, observation: SagaObservation) -> AgentProposal:
    payload = intent.model_dump(mode="json")
    payload["proposal_id"] = _proposal_id(observation)
    payload["based_on_saga_seq"] = observation.saga_seq
    return _AGENT_PROPOSAL.validate_python(payload, strict=True)


def _proposal_id(observation: SagaObservation) -> str:
    payload = {
        "domain": _PROPOSAL_ID_DOMAIN,
        "saga_id": observation.saga_id,
        "saga_seq": observation.saga_seq,
    }
    return f"proposal_{sha256_json(payload)}"


def _decision_payload(raw: object) -> object:
    if type(raw) is AgentDecision:
        return raw.model_dump(mode="json")
    return raw


def _build_pydantic_call(context: SagaContext, model: object) -> _NativeProposalCall:
    dependencies = _load_pydantic_dependencies()
    system = _system_context(context)
    agent = _create_agent(dependencies, model, system)
    return _NativeProposalCall(agent, dependencies, system)


def _create_agent(
    dependencies: _PydanticDependencies,
    model: object,
    system: str,
) -> _PydanticAgent:
    values = dict(_AGENT_OPTIONS)
    values["model"] = model
    values["instructions"] = system
    values["output_type"] = [str, dependencies.deferred_requests_type]
    values["capabilities"] = (dependencies.required_tool_capability,)
    agent = dependencies.create_agent(**values)
    agent.output_validator(_deferred_output_validator(dependencies))
    agent.instrument = False
    return agent


def native_model_request_limit() -> Literal[2]:
    """Return the hard maximum provider requests within one durable agent turn."""

    return 2


def _usage_limits(dependencies: _PydanticDependencies) -> object:
    return dependencies.usage_limits(request_limit=native_model_request_limit())


def _deferred_output_validator(
    dependencies: _PydanticDependencies,
) -> Callable[[object], object]:
    def validate(output: object) -> object:
        if isinstance(output, dependencies.deferred_requests_type):
            return output
        raise dependencies.model_retry("Call exactly one advertised native proposal tool.")

    return validate


def _proposal_toolset(
    dependencies: _PydanticDependencies,
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> object:
    definitions = [_business_definition(dependencies, item) for item in available_tools]
    definitions.extend(_control_definitions(dependencies, observation))
    return dependencies.external_toolset(definitions, id=_TOOLSET_ID)


def native_proposal_tool_names(
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> tuple[str, ...]:
    """Report the exact provider-visible proposal tools for one Saga turn."""

    names = tuple(item.name for item in available_tools)
    if len(names) != len(set(names)) or _CONTROL_TOOLS.intersection(names):
        raise ValueError("eligible Saga tools conflict with native control tools")
    return (*names, *_control_tool_names(observation))


def _business_definition(
    dependencies: _PydanticDependencies,
    descriptor: ToolDescriptor,
) -> object:
    description = f"{descriptor.description} Proposal only; the Temporal workflow executes it."
    return dependencies.tool_definition(
        name=descriptor.name,
        description=description,
        parameters_json_schema=thaw_json_object(descriptor.input_schema),
        strict=True,
        sequential=True,
    )


def _control_definitions(
    dependencies: _PydanticDependencies,
    observation: SagaObservation,
) -> list[object]:
    if not _control_tool_names(observation):
        return []
    return [_finish_definition(dependencies)]


def _control_tool_names(observation: SagaObservation) -> tuple[str, ...]:
    if observation.finish_allowed:
        return (_FINISH_TOOL,)
    return ()


def _finish_definition(
    dependencies: _PydanticDependencies,
) -> object:
    return dependencies.tool_definition(
        name=_FINISH_TOOL,
        description=_FINISH_DESCRIPTION,
        parameters_json_schema=_finish_schema(),
        strict=True,
        sequential=True,
    )


def _finish_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "rationale": {"type": "string", "minLength": 1, "maxLength": 500},
            "target_status": {
                "type": "string",
                "enum": ["succeeded_verified"],
            },
        },
        "required": ["rationale", "target_status"],
        "additionalProperties": False,
    }


def _load_pydantic_dependencies() -> _PydanticDependencies:
    ai, toolsets, tools = _import_pydantic_modules()
    return _PydanticDependencies(
        create_agent=cast(_CreateAgent, ai.Agent),
        external_toolset=cast(_ExternalToolsetFactory, toolsets.ExternalToolset),
        tool_definition=cast(_ToolDefinitionFactory, tools.ToolDefinition),
        deferred_requests_type=cast(type[object], ai.DeferredToolRequests),
        model_retry=cast(Callable[[str], Exception], ai.ModelRetry),
        usage_limits=cast(_UsageLimitsFactory, ai.UsageLimits),
        required_tool_capability=_required_tool_capability(),
    )


def _import_pydantic_modules() -> tuple[ModuleType, ModuleType, ModuleType]:
    try:
        ai = import_module("pydantic_ai")
        toolsets = import_module("pydantic_ai.toolsets")
        tools = import_module("pydantic_ai.tools")
    except ModuleNotFoundError:
        raise RuntimeError("install agentic-saga[agent] to use the agent adapter") from None
    return ai, toolsets, tools


def _required_tool_capability() -> object:
    module = import_module("agentic_saga.agents._required_tool")
    return module.RequireToolCall()


def _failure_category(error: Exception) -> AgentFailureCategory:
    http_category = _http_failure_category(_status_code(error))
    if http_category is not None:
        return http_category
    if isinstance(error, (ConnectionError, OSError, TimeoutError)):
        return AgentFailureCategory.TRANSPORT_EXHAUSTED
    if isinstance(error, ValueError) or _is_invalid_model_response(error):
        return AgentFailureCategory.INVALID_RESPONSE
    return AgentFailureCategory.INTERNAL


def _is_invalid_model_response(error: Exception) -> bool:
    try:
        exceptions = import_module("pydantic_ai.exceptions")
    except ModuleNotFoundError:
        return False
    error_type = getattr(exceptions, "UnexpectedModelBehavior", None)
    return isinstance(error_type, type) and isinstance(error, error_type)


def _http_failure_category(status: int | None) -> AgentFailureCategory | None:
    if status == _TOO_MANY_REQUESTS:
        return AgentFailureCategory.RATE_LIMIT_EXHAUSTED
    if status in _CLIENT_ERROR_RANGE:
        return AgentFailureCategory.REQUEST_REJECTED
    if status in _SERVER_ERROR_RANGE:
        return AgentFailureCategory.SERVER_ERROR_EXHAUSTED
    return None


def _status_code(error: Exception) -> int | None:
    try:
        value = getattr(error, "status_code", None)
        if value is None:
            response = getattr(error, "response", None)
            value = getattr(response, "status_code", None)
    except Exception:
        return None
    return value if type(value) is int else None


def _turn_context(
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> str:
    return canonical_json(_turn_payload(observation, available_tools)).decode()


def _turn_payload(
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> dict[str, object]:
    return {
        "available_tools": [item.model_dump(mode="json") for item in available_tools],
        "observation": observation.model_dump(mode="json"),
    }


def _system_context(context: SagaContext) -> str:
    payload = _context_payload(context)
    expected = canonical_json(payload).decode()
    if context.agent_context != expected:
        raise ValueError("Saga Context is not authoritative")
    _require_valid_manifest(context.manifest)
    require_public_agent_payload(payload)
    return f"{context.agent_context}\n\n{_AUTHORITY}"


def _context_payload(context: SagaContext) -> dict[str, object]:
    return {
        "manifest": context.manifest.model_dump(mode="json"),
        "tools": [item.model_dump(mode="json") for item in context.tool_descriptors],
    }


def _require_valid_manifest(manifest: SagaManifest) -> None:
    try:
        SagaManifest.model_validate(manifest.model_dump(mode="json"), strict=True)
    except ValidationError:
        raise ValueError("Saga Context contains invalid or private manifest material") from None


def _require_current(
    proposal: AgentProposal,
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> None:
    if _proposal_is_current(proposal, observation, available_tools):
        return
    raise ValueError("agent returned an invalid proposal")


def _proposal_is_current(
    proposal: AgentProposal,
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> bool:
    current_sequence = proposal.based_on_saga_seq == observation.saga_seq
    eligible_tool = not isinstance(proposal, ToolCall) or proposal.tool_name in {
        item.name for item in available_tools
    }
    return current_sequence and eligible_tool and _control_is_current(proposal, observation)


def _control_is_current(proposal: AgentProposal, observation: SagaObservation) -> bool:
    if isinstance(proposal, Finish):
        return proposal.target_status == "succeeded_verified" and observation.finish_allowed
    return isinstance(proposal, ToolCall)


def _require_catalog(context: SagaContext, available_tools: Sequence[ToolDescriptor]) -> None:
    pinned = {item.name: item for item in context.tool_descriptors}
    names = [item.name for item in available_tools]
    _require_available_names(names)
    for descriptor in available_tools:
        _require_pinned_descriptor(pinned, descriptor)


def _require_available_names(names: list[str]) -> None:
    if len(names) != len(set(names)):
        raise ValueError("available descriptor catalog differs from Saga Context")
    if _CONTROL_TOOLS.intersection(names):
        raise ValueError("available descriptor catalog differs from Saga Context")


def _require_pinned_descriptor(
    pinned: Mapping[str, ToolDescriptor], descriptor: ToolDescriptor
) -> None:
    expected = pinned.get(descriptor.name)
    if expected is None or not _same_tool_contract(expected, descriptor):
        raise ValueError("available descriptor catalog differs from Saga Context")


def _same_tool_contract(expected: ToolDescriptor, current: ToolDescriptor) -> bool:
    return (
        expected.name,
        expected.kind,
        expected.description,
        expected.input_schema,
        expected.reversibility,
    ) == (
        current.name,
        current.kind,
        current.description,
        current.input_schema,
        current.reversibility,
    )


__all__ = ["PydanticAIDriver", "native_proposal_tool_names"]
