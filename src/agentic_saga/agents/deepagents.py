from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from agentic_saga.contracts.actions import AgentProposal, ToolCall
from agentic_saga.contracts.common import JsonPayloadError, canonical_json, require_bounded_json
from agentic_saga.contracts.runtime import AgentDriver, SagaObservation, ToolDescriptor
from agentic_saga.manifest import SagaContext, SagaManifest, require_public_agent_payload

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

type ProposalCall = Callable[[str, str], Awaitable[object]]
_RECURSION_LIMIT = 8
_TOO_MANY_REQUESTS = 429
_SERVER_ERROR_RANGE = range(500, 600)
_AUTHORITY = """Agentic Saga authority:
Choose exactly one next proposal from the current public evidence and eligible descriptors.
Only the deterministic Saga kernel executes business tools or assigns terminal state.
Never invent a tool, receipt, approval, idempotency key, successful outcome, or private fact.
Return the strict proposal only, with a concise rationale rather than private chain-of-thought."""


class AgentFailureCategory(StrEnum):
    """Closed, secret-free reason for an adapter planning failure."""

    RATE_LIMIT_EXHAUSTED = "rate_limit_exhausted"
    SERVER_ERROR_EXHAUSTED = "server_error_exhausted"
    TRANSPORT_EXHAUSTED = "transport_exhausted"
    INVALID_RESPONSE = "invalid_response"
    INTERNAL = "internal"


class AgentPlanningError(RuntimeError):
    """Safe adapter error that never retains the provider exception."""

    def __init__(self, category: AgentFailureCategory) -> None:
        self.category = category
        super().__init__(category.value)


class AgentDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    proposal: AgentProposal


class _AgentGraph(Protocol):
    async def ainvoke(self, value: dict[str, object], config: dict[str, object]) -> object: ...


class _CreateDeepAgent(Protocol):
    def __call__(  # noqa: PLR0913 - mirrors the maintained third-party factory
        self,
        model: BaseChatModel,
        tools: Sequence[object],
        *,
        system_prompt: str,
        middleware: Sequence[object],
        subagents: Sequence[object],
        response_format: object,
        name: str,
    ) -> _AgentGraph: ...


class _ToolStrategy(Protocol):
    def __call__(self, schema: type[BaseModel]) -> object: ...


class _ModelCallLimit(Protocol):
    def __call__(self, *, run_limit: int, exit_behavior: Literal["error"]) -> object: ...


class _ProfileFactory(Protocol):
    def __call__(self, **values: object) -> object: ...


class _RegisterProfile(Protocol):
    def __call__(self, key: str, profile: object) -> None: ...


class _TracingContext(Protocol):
    def __call__(self, *, enabled: Literal[False]) -> AbstractContextManager[None]: ...


@dataclass(frozen=True)
class _DeepAgentDependencies:
    create_agent: _CreateDeepAgent
    tool_strategy: _ToolStrategy
    model_call_limit: _ModelCallLimit
    harness_profile: _ProfileFactory
    general_purpose_profile: _ProfileFactory
    register_profile: _RegisterProfile


@dataclass(frozen=True)
class DeepAgentsDriver(AgentDriver):
    """Planning-only adapter from Saga context to one strict proposal."""

    context: SagaContext
    proposal_call: ProposalCall
    provider_id: str = "injected"
    model_route: tuple[str, ...] = ("injected",)

    @classmethod
    def _from_model(
        cls,
        context: SagaContext,
        model: BaseChatModel,
        *,
        provider_id: str,
        model_route: tuple[str, ...],
    ) -> DeepAgentsDriver:
        """Package-internal bridge for the validated OpenRouter factory and offline tests."""
        call = _build_deep_agent_call(context, model, provider_id, model_route)
        return cls(context, call, provider_id, model_route)

    async def next_action(
        self,
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> AgentProposal:
        _require_catalog(self.context, available_tools)
        system = _system_context(self.context)
        turn = _turn_context(observation, available_tools)
        raw = await _call_model(self.proposal_call, system, turn)
        proposal = _validated_proposal(raw)
        require_public_agent_payload(proposal.model_dump(mode="json"))
        _require_current(proposal, observation, available_tools)
        return proposal


async def _call_model(call: ProposalCall, system: str, turn: str) -> object:
    try:
        return await call(system, turn)
    except Exception as error:
        category = _failure_category(error)
    raise AgentPlanningError(category) from None


def _validated_proposal(raw: object) -> AgentProposal:
    try:
        payload = _decision_payload(raw)
        require_bounded_json(payload)
        decision = AgentDecision.model_validate(payload, strict=True)
    except (JsonPayloadError, TypeError, ValidationError, ValueError):
        pass
    else:
        return decision.proposal
    raise AgentPlanningError(AgentFailureCategory.INVALID_RESPONSE) from None


def _decision_payload(raw: object) -> object:
    if type(raw) is AgentDecision:
        return raw.model_dump(mode="json")
    return raw


def _failure_category(error: Exception) -> AgentFailureCategory:
    status = _status_code(error)
    if status == _TOO_MANY_REQUESTS:
        return AgentFailureCategory.RATE_LIMIT_EXHAUSTED
    if status in _SERVER_ERROR_RANGE:
        return AgentFailureCategory.SERVER_ERROR_EXHAUSTED
    if isinstance(error, (ConnectionError, OSError, TimeoutError)):
        return AgentFailureCategory.TRANSPORT_EXHAUSTED
    return (
        AgentFailureCategory.INVALID_RESPONSE
        if isinstance(error, ValueError)
        else AgentFailureCategory.INTERNAL
    )


def _status_code(error: Exception) -> int | None:
    try:
        value = getattr(error, "status_code", None)
        if value is None:
            response = getattr(error, "response", None)
            value = getattr(response, "status_code", None)
    except Exception:
        return None
    return value if type(value) is int else None


@dataclass(frozen=True)
class _GraphProposalCall:
    graph: _AgentGraph
    system_context: str
    provider_id: str
    model_route: tuple[str, ...]

    async def __call__(self, system: str, turn: str) -> object:
        if system != self.system_context:
            raise ValueError("Saga Context changed after agent construction")
        value: dict[str, object] = {"messages": [{"role": "user", "content": turn}]}
        config: dict[str, object] = {
            "metadata": _metadata(self.provider_id, self.model_route),
            "recursion_limit": _RECURSION_LIMIT,
        }
        with _tracing_disabled():
            raw = await self.graph.ainvoke(value, config=config)
        return _structured_response(raw)


def _tracing_disabled() -> AbstractContextManager[None]:
    module = import_module("langsmith")
    context = cast(_TracingContext, module.tracing_context)
    return context(enabled=False)


def _structured_response(raw: object) -> object:
    if not isinstance(raw, Mapping) or "structured_response" not in raw:
        raise ValueError("Deep Agents returned no structured proposal")
    return raw["structured_response"]


def _build_deep_agent_call(
    context: SagaContext,
    model: BaseChatModel,
    provider_id: str,
    model_route: tuple[str, ...],
) -> ProposalCall:
    dependencies = _load_deep_agent_dependencies()
    system = _system_context(context)
    _disable_default_subagent(dependencies, _profile_key(provider_id, model_route))
    graph = _create_graph(dependencies, model, system)
    return _GraphProposalCall(graph, system, provider_id, model_route)


def _metadata(provider_id: str, model_route: tuple[str, ...]) -> dict[str, str]:
    return {"provider": provider_id, "model_route": ",".join(model_route)}


def _create_graph(
    dependencies: _DeepAgentDependencies, model: BaseChatModel, system: str
) -> _AgentGraph:
    limit = dependencies.model_call_limit(run_limit=1, exit_behavior="error")
    return dependencies.create_agent(
        model,
        (),
        system_prompt=system,
        middleware=(limit,),
        subagents=(),
        response_format=dependencies.tool_strategy(AgentDecision),
        name="agentic_saga_planner",
    )


def _disable_default_subagent(dependencies: _DeepAgentDependencies, profile_key: str) -> None:
    general = dependencies.general_purpose_profile(enabled=False)
    profile = dependencies.harness_profile(general_purpose_subagent=general)
    dependencies.register_profile(profile_key, profile)


def _profile_key(provider_id: str, model_route: tuple[str, ...]) -> str:
    invalid = not provider_id or ":" in provider_id or not model_route or ":" in model_route[0]
    if invalid:
        raise ValueError("provider/model identity cannot configure Deep Agents safely")
    return f"{provider_id}:{model_route[0]}"


def _load_deep_agent_dependencies() -> _DeepAgentDependencies:
    try:
        deepagents = import_module("deepagents")
        structured_output = import_module("langchain.agents.structured_output")
        middleware = import_module("langchain.agents.middleware")
    except ModuleNotFoundError:
        raise RuntimeError("install agentic-saga[agent] to use the agent adapter") from None
    return _DeepAgentDependencies(
        create_agent=cast(_CreateDeepAgent, deepagents.create_deep_agent),
        tool_strategy=cast(_ToolStrategy, structured_output.ToolStrategy),
        model_call_limit=cast(_ModelCallLimit, middleware.ModelCallLimitMiddleware),
        harness_profile=cast(_ProfileFactory, deepagents.HarnessProfile),
        general_purpose_profile=cast(_ProfileFactory, deepagents.GeneralPurposeSubagentProfile),
        register_profile=cast(_RegisterProfile, deepagents.register_harness_profile),
    )


def _turn_context(
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> str:
    payload = {
        "available_tools": [item.model_dump(mode="json") for item in available_tools],
        "observation": observation.model_dump(mode="json"),
    }
    require_public_agent_payload(payload)
    return canonical_json(payload).decode()


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
    names = frozenset(item.name for item in available_tools)
    wrong_tool = isinstance(proposal, ToolCall) and proposal.tool_name not in names
    if proposal.based_on_saga_seq != observation.saga_seq or wrong_tool:
        raise ValueError("agent returned an invalid proposal")


def _require_catalog(context: SagaContext, available_tools: Sequence[ToolDescriptor]) -> None:
    pinned = {item.name: item for item in context.tool_descriptors}
    names = [item.name for item in available_tools]
    if len(names) != len(set(names)):
        raise ValueError("available descriptor catalog differs from Saga Context")
    for descriptor in available_tools:
        _require_pinned_descriptor(pinned, descriptor)


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


__all__ = ["DeepAgentsDriver"]
