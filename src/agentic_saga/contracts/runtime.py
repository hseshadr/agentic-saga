from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from agentic_saga.contracts.actions import AgentProposal
from agentic_saga.contracts.common import JsonObject, Reversibility, SagaId, thaw_json_object
from agentic_saga.contracts.redaction import RedactionPolicy, redact_json
from agentic_saga.contracts.tools import EffectToolDefinition, ReadToolDefinition

type _BoundedName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _BoundedText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2_000)]
type _ReasonCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,99}$")]
type _RuleId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _Version = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type TerminalStatus = Literal[
    "succeeded_verified",
    "compensated_verified",
    "aborted_clean",
    "resolved_with_exception",
]
_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


class SagaStatus(StrEnum):
    """Enumerate durable lifecycle states, including verified terminal outcomes."""

    CREATED = "created"
    RUNNING = "running"
    RECOVERY_PLAN_REQUIRED = "recovery_plan_required"
    RETRY_WAIT = "retry_wait"
    RECONCILING_UNKNOWN = "reconciling_unknown"
    COMPENSATING = "compensating"
    HUMAN_REQUIRED = "human_required"
    SUCCEEDED_VERIFIED = "succeeded_verified"
    COMPENSATED_VERIFIED = "compensated_verified"
    ABORTED_CLEAN = "aborted_clean"
    RESOLVED_WITH_EXCEPTION = "resolved_with_exception"


class ExecutionBudget(BaseModel):
    """Fixed limits supplied by the pinned Saga definition."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    turn_limit: int = Field(strict=True, ge=0)
    tool_call_limit: int = Field(strict=True, ge=0)
    elapsed_ms_limit: int = Field(strict=True, ge=0)
    token_limit: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def require_planning_capacity(self) -> Self:
        if self.elapsed_ms_limit < self.turn_limit:
            raise ValueError("elapsed_ms_limit must permit every configured turn")
        if self.token_limit < self.turn_limit:
            raise ValueError("token_limit must permit every configured turn")
        return self


class TerminalRequirement(BaseModel):
    """Name the invariant version and rules required for one terminal status."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    invariant_version: _Version
    required_rule_ids: tuple[_RuleId, ...] = Field(min_length=1)

    @field_validator("required_rule_ids")
    @classmethod
    def require_unique_rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("required invariant rule IDs must be unique")
        return value


class SagaGoal(BaseModel):
    """Public, secret-rejecting objective and context presented to the agent."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    goal_id: _BoundedName
    text: _BoundedText
    context: JsonObject

    @field_validator("text")
    @classmethod
    def require_public_text(cls, value: str) -> str:
        if redact_json(value, RedactionPolicy()) != value:
            raise ValueError("goal text contains private material")
        return value

    @field_validator("context")
    @classmethod
    def require_public_context(cls, value: JsonObject) -> JsonObject:
        public = thaw_json_object(value)
        if redact_json(public, RedactionPolicy()) != public:
            raise ValueError("goal context contains private material")
        return value


class ToolDescriptor(BaseModel):
    """Expose an agent-safe description of an available read or effect tool."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: _BoundedName
    kind: Literal["read", "effect"]
    description: _BoundedText
    input_schema: JsonObject
    reversibility: Reversibility | None
    policy_constraints: JsonObject = Field(default_factory=dict)

    @classmethod
    def from_definition[CommandT: BaseModel, ResultT: BaseModel](
        cls,
        definition: ReadToolDefinition[CommandT, ResultT] | EffectToolDefinition[CommandT],
    ) -> ToolDescriptor:
        if isinstance(definition, EffectToolDefinition):
            return _effect_descriptor(definition)
        return _read_descriptor(definition)


class ReadEvidence(BaseModel):
    """Retain one bounded, public read command and its durable outcome."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    tool_name: _BoundedName
    command: JsonObject
    observed_at_saga_seq: int = Field(strict=True, ge=1)
    freshness: Literal["fresh", "stale"]
    result: JsonObject | None = None
    unavailable_reason: _ReasonCode | None = None

    @model_validator(mode="after")
    def require_exactly_one_outcome(self) -> Self:
        if (self.result is None) == (self.unavailable_reason is None):
            raise ValueError("read evidence must contain exactly one durable outcome")
        return self


class ControlProposalCapabilities(BaseModel):
    """Expose only control proposals the deterministic kernel can currently accept."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    finish_targets: tuple[TerminalStatus, ...] = ()
    begin_compensation: bool = False
    escalate_to_human: bool = False

    @field_validator("finish_targets")
    @classmethod
    def require_unique_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("finish targets must be unique")
        return value


class SagaObservation(BaseModel):
    """Present the agent with the current projection and remaining turn budget."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    saga_id: SagaId
    saga_seq: int = Field(strict=True, ge=1)
    state: SagaStatus
    goal: SagaGoal
    last_action: JsonObject | None
    projection: JsonObject
    remaining_budget: ExecutionBudget
    read_evidence: tuple[ReadEvidence, ...] = ()
    proposal_controls: ControlProposalCapabilities = Field(
        default_factory=ControlProposalCapabilities
    )


class SagaResult(BaseModel):
    """Report the durable state reached when a runtime invocation yields."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    saga_id: SagaId
    state: SagaStatus
    saga_seq: int = Field(strict=True, ge=1)
    autonomous_quiescent: bool
    human_required_reason: _ReasonCode | None = None


@runtime_checkable
class AgentDriver(Protocol):
    """Propose one action from bounded saga context and advertised tools."""

    async def next_action(
        self,
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> AgentProposal: ...


def _schema(model: type[BaseModel]) -> JsonObject:
    return _JSON_OBJECT.validate_python(model.model_json_schema())


def _read_descriptor[CommandT: BaseModel, ResultT: BaseModel](
    definition: ReadToolDefinition[CommandT, ResultT],
) -> ToolDescriptor:
    return ToolDescriptor(
        name=definition.name,
        kind="read",
        description=definition.description,
        input_schema=_schema(definition.input_model),
        reversibility=None,
    )


def _effect_descriptor[CommandT: BaseModel](
    definition: EffectToolDefinition[CommandT],
) -> ToolDescriptor:
    return ToolDescriptor(
        name=definition.name,
        kind="effect",
        description=definition.description,
        input_schema=_schema(definition.input_model),
        reversibility=definition.capabilities.reversibility,
    )


__all__ = [
    "AgentDriver",
    "ControlProposalCapabilities",
    "ExecutionBudget",
    "ReadEvidence",
    "SagaGoal",
    "SagaObservation",
    "SagaResult",
    "SagaStatus",
    "TerminalRequirement",
    "TerminalStatus",
    "ToolDescriptor",
]
