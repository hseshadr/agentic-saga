from __future__ import annotations

from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from agentic_saga.contracts.actions import AgentProposal, ToolCall
from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    OperationId,
    SagaId,
    StepInstanceId,
    sha256_json,
)
from agentic_saga.contracts.redaction import RedactionPolicy, contains_sensitive_json
from agentic_saga.contracts.runtime import SagaGoal, SagaStatus

type _Name = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _Reason = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,99}$")]
type _ActivityOutcome = Literal["succeeded", "unresolved"]
type _ToolOutcome = Literal["succeeded", "failed", "unresolved"]
type _ReconciliationOutcome = Literal[
    "confirmed_effect", "confirmed_no_effect", "pending", "conflict", "unsupported"
]
type _UnresolvedReconciliationOutcome = Literal["pending", "conflict", "unsupported"]
type _CompensationState = Literal["pending", "succeeded", "unresolved"]
type _AuthorizationReference = Annotated[
    str, StringConstraints(strict=True, pattern=r"^authz_[A-Za-z0-9_-]{16,128}$")
]
_IDENTITY_DOMAIN = "agentic-saga:temporal-activity:v1"


class _Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)


def _operation_id(
    saga_id: str,
    step_instance_id: str,
    direction: Direction,
    semantic_generation: int,
) -> str:
    material = {
        "direction": direction.value,
        "domain": _IDENTITY_DOMAIN,
        "saga_id": saga_id,
        "semantic_generation": semantic_generation,
        "step_instance_id": step_instance_id,
    }
    return f"op_{sha256_json(material)}"


class ActivityIdentity(_Contract):
    """Stable provider idempotency identity, independent of an Activity retry."""

    saga_id: SagaId
    step_instance_id: StepInstanceId
    direction: Direction
    semantic_generation: int = Field(strict=True, ge=0)
    operation_id: OperationId
    idempotency_key: OperationId

    @classmethod
    def create(
        cls,
        saga_id: str,
        step_instance_id: str,
        direction: Direction,
        semantic_generation: int,
    ) -> Self:
        operation_id = _operation_id(saga_id, step_instance_id, direction, semantic_generation)
        return cls(
            saga_id=saga_id,
            step_instance_id=step_instance_id,
            direction=direction,
            semantic_generation=semantic_generation,
            operation_id=operation_id,
            idempotency_key=operation_id,
        )

    @model_validator(mode="after")
    def require_stable_operation_id(self) -> Self:
        expected = _operation_id(
            self.saga_id,
            self.step_instance_id,
            self.direction,
            self.semantic_generation,
        )
        if self.operation_id != expected or self.idempotency_key != expected:
            raise ValueError("operation identity does not match its stable inputs")
        return self


class ForwardActivityRequest(_Contract):
    """Describe one workflow-authorized forward business operation."""

    identity: ActivityIdentity
    tool_name: _Name
    arguments: JsonObject
    declared_kind: Literal["read", "effect"]
    declared_compensation_tool: _Name | None = None

    @model_validator(mode="after")
    def require_forward_identity(self) -> Self:
        if self.identity.direction is not Direction.FORWARD:
            raise ValueError("forward request requires forward identity")
        has_compensation = self.declared_compensation_tool is not None
        if (self.declared_kind == "effect") != has_compensation:
            raise ValueError("forward declaration requires matching compensation")
        return self


class CompensationActivityRequest(_Contract):
    """Describe one reverse operation bound to a confirmed forward effect."""

    identity: ActivityIdentity
    tool_name: _Name
    arguments: JsonObject
    compensates_operation_id: OperationId
    forward_receipt: JsonObject

    @model_validator(mode="after")
    def require_compensation_identity(self) -> Self:
        if self.identity.direction is not Direction.COMPENSATION:
            raise ValueError("compensation request requires compensation identity")
        expected = _operation_id(
            self.identity.saga_id,
            self.identity.step_instance_id,
            Direction.FORWARD,
            self.identity.semantic_generation,
        )
        if self.compensates_operation_id != expected:
            raise ValueError("compensation target does not match its stable identity")
        if not self.forward_receipt:
            raise ValueError("compensation requires a forward receipt")
        return self


class _ActivityResult(_Contract):
    outcome: _ActivityOutcome
    receipt: JsonObject | None = None
    reason_code: _Reason | None = None

    @classmethod
    def succeeded(cls, receipt: JsonObject) -> Self:
        return cls(outcome="succeeded", receipt=receipt)

    @classmethod
    def unresolved(cls, reason_code: str) -> Self:
        return cls(outcome="unresolved", reason_code=reason_code)

    @model_validator(mode="after")
    def require_exact_outcome_shape(self) -> Self:
        success = self.outcome == "succeeded"
        if success != (self.receipt is not None):
            raise ValueError("successful activity result requires exactly one receipt")
        if success == (self.reason_code is not None):
            raise ValueError("unresolved activity result requires exactly one reason")
        return self


class ForwardActivityResult(_ActivityResult):
    """Public result recorded after a forward Activity attempt."""


class ReconciliationActivityRequest(_Contract):
    """Safe query for a forward or compensation effect."""

    forward_identity: ActivityIdentity
    tool_name: _Name
    arguments: JsonObject
    correlation_id: OperationId
    declared_kind: Literal["read", "effect"]
    declared_compensation_tool: _Name | None = None
    compensation: CompensationActivityRequest | None = None

    @model_validator(mode="after")
    def require_safe_correlation(self) -> Self:
        _require_reconciliation_identity(self)
        _require_safe_reconciliation_correlation(self)
        _require_reversible_effect(self)
        return self


class ReconciliationActivityResult(_Contract):
    """Public evidence returned by a provider reconciliation query."""

    outcome: _ReconciliationOutcome
    receipt: JsonObject | None = None
    reason_code: _Reason | None = None

    @field_validator("receipt")
    @classmethod
    def require_public_receipt(cls, value: JsonObject | None) -> JsonObject | None:
        if value is not None and contains_sensitive_json(value, RedactionPolicy()):
            raise ValueError("reconciliation receipt must be public and redacted")
        return value

    @classmethod
    def confirmed_effect(cls, receipt: JsonObject) -> Self:
        return cls(outcome="confirmed_effect", receipt=receipt)

    @classmethod
    def confirmed_no_effect(cls, reason_code: str) -> Self:
        return cls(outcome="confirmed_no_effect", reason_code=reason_code)

    @classmethod
    def unresolved(cls, outcome: _UnresolvedReconciliationOutcome, reason_code: str) -> Self:
        return cls(outcome=outcome, reason_code=reason_code)

    @model_validator(mode="after")
    def require_exact_outcome_shape(self) -> Self:
        confirmed = self.outcome == "confirmed_effect"
        if confirmed != (self.receipt is not None):
            raise ValueError("confirmed effect requires exactly one receipt")
        if confirmed == (self.reason_code is not None):
            raise ValueError("non-effect reconciliation requires exactly one reason")
        return self


class CompensationActivityResult(_ActivityResult):
    """Public result recorded after a compensation Activity attempt."""


class CompensationRecord(_Contract):
    """Track one confirmed forward obligation and its compensation state."""

    forward_identity: ActivityIdentity
    compensation_identity: ActivityIdentity
    compensation_tool: _Name
    compensation_arguments: JsonObject
    forward_receipt: JsonObject
    state: _CompensationState = "pending"
    compensation_receipt: JsonObject | None = None
    unresolved_reason: _Reason | None = None

    @model_validator(mode="after")
    def require_record_shape(self) -> Self:
        _require_record_identities(self)
        _require_record_evidence(self)
        _require_record_resolution(self)
        return self


def _require_record_identities(record: CompensationRecord) -> None:
    if record.forward_identity.direction is not Direction.FORWARD:
        raise ValueError("record requires a forward identity")
    if record.compensation_identity.direction is not Direction.COMPENSATION:
        raise ValueError("record requires a compensation identity")


def _require_record_evidence(record: CompensationRecord) -> None:
    if not record.forward_receipt:
        raise ValueError("record requires a forward receipt")


def _require_record_resolution(record: CompensationRecord) -> None:
    if record.state == "succeeded" and record.compensation_receipt is None:
        raise ValueError("succeeded compensation requires a receipt")
    if record.state == "unresolved" and record.unresolved_reason is None:
        raise ValueError("unresolved compensation requires a reason")


class WorkflowEvent(_Contract):
    """Record one bounded, public event in the durable workflow projection."""

    seq: int = Field(strict=True, ge=1)
    kind: _Name
    details: JsonObject
    recorded_at: AwareDatetime
    before_status: SagaStatus
    after_status: SagaStatus

    @field_validator("recorded_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("workflow event timestamp must use UTC")
        return value


class WorkflowState(_Contract):
    """Represent the queryable durable state owned by the Temporal Workflow."""

    saga_id: SagaId
    status: SagaStatus
    events: tuple[WorkflowEvent, ...] = Field(max_length=10_000)
    compensations: tuple[CompensationRecord, ...] = Field(max_length=1_000)
    human_required_reason: _Reason | None = None

    @model_validator(mode="after")
    def require_ordered_events(self) -> Self:
        if any(event.seq != index for index, event in enumerate(self.events, start=1)):
            raise ValueError("workflow events must be contiguous")
        return self

    @model_validator(mode="after")
    def require_human_reason(self) -> Self:
        required = self.status is SagaStatus.HUMAN_REQUIRED
        if required != (self.human_required_reason is not None):
            raise ValueError("human-required state needs exactly one reason")
        return self


class WorkflowResult(_Contract):
    """Return the durable state summary when a workflow run completes."""

    saga_id: SagaId
    status: SagaStatus
    event_count: int = Field(strict=True, ge=0)
    human_required_reason: _Reason | None = None

    @classmethod
    def from_state(cls, state: WorkflowState) -> Self:
        return cls(
            saga_id=state.saga_id,
            status=state.status,
            event_count=len(state.events),
            human_required_reason=state.human_required_reason,
        )

    @model_validator(mode="after")
    def require_human_reason(self) -> Self:
        required = self.status is SagaStatus.HUMAN_REQUIRED
        if required != (self.human_required_reason is not None):
            raise ValueError("human-required result needs a reason only in that state")
        return self


class WorkflowTool(_Contract):
    """One explicitly classified capability and its safety boundary."""

    name: _Name
    kind: Literal["read", "effect"]
    compensation_tool: _Name | None = None
    required_for_success: bool = True
    proof_for_success: bool = False
    prerequisites: tuple[_Name, ...] = Field(default=(), max_length=100)
    max_calls: int = Field(default=1, strict=True, ge=1, le=100)

    @model_validator(mode="after")
    def require_kind_consistent_compensation(self) -> Self:
        _require_tool_compensation(self)
        _require_tool_proof(self)
        _require_tool_prerequisites(self)
        return self


class SagaWorkflowInput(_Contract):
    """Supply the complete typed definition for one Saga workflow run."""

    saga_id: SagaId
    goal: SagaGoal
    tools: tuple[WorkflowTool, ...] = Field(min_length=1, max_length=100)
    max_agent_turns: int = Field(strict=True, ge=1, le=100)
    max_tool_calls: int = Field(strict=True, ge=1, le=100)

    @model_validator(mode="after")
    def require_unique_tools(self) -> Self:
        _require_unique_tool_names(self.tools)
        _require_known_prerequisites(self.tools)
        _require_acyclic_prerequisites(self.tools)
        return self


class ToolCallCount(_Contract):
    """Count workflow-authorized calls to one business tool."""

    tool_name: _Name
    count: int = Field(strict=True, ge=1, le=100)


class AgentDecisionObservation(_Contract):
    """Bounded public projection supplied to one agent decision."""

    recent_events: tuple[WorkflowEvent, ...] = Field(max_length=20)
    completed_tools: tuple[_Name, ...] = Field(max_length=100)
    tool_call_counts: tuple[ToolCallCount, ...] = Field(max_length=100)
    compensation_count: int = Field(strict=True, ge=0, le=1_000)
    total_event_count: int = Field(strict=True, ge=0, le=10_000)
    remaining_turns: int = Field(strict=True, ge=0, le=100)
    remaining_tool_calls: int = Field(strict=True, ge=0, le=100)

    @property
    def events(self) -> tuple[WorkflowEvent, ...]:
        return self.recent_events

    @property
    def compensations(self) -> tuple[None, ...]:
        return (None,) * self.compensation_count


class AgentDecisionRequest(_Contract):
    """Give an agent one bounded observation and the currently eligible tools."""

    saga_id: SagaId
    saga_seq: int = Field(strict=True, ge=1)
    status: SagaStatus
    goal: SagaGoal
    available_tools: tuple[_Name, ...]
    state: AgentDecisionObservation
    finish_allowed: bool

    @property
    def remaining_turns(self) -> int:
        return self.state.remaining_turns

    @property
    def remaining_tool_calls(self) -> int:
        return self.state.remaining_tool_calls


class AgentDecisionResult(_Contract):
    """Carry one public agent proposal back across the Activity boundary."""

    proposal: AgentProposal

    @model_validator(mode="after")
    def require_public_arguments(self) -> Self:
        if isinstance(self.proposal, ToolCall) and contains_sensitive_json(
            self.proposal.arguments, RedactionPolicy()
        ):
            raise ValueError("agent arguments must be public and redacted")
        return self


class ToolActivityRequest(_Contract):
    """Wrap exactly one forward or compensation Activity request."""

    forward: ForwardActivityRequest | None = None
    compensation: CompensationActivityRequest | None = None

    @model_validator(mode="after")
    def require_one_request(self) -> Self:
        if (self.forward is None) == (self.compensation is None):
            raise ValueError("tool activity requires exactly one request")
        return self

    def identity(self) -> ActivityIdentity:
        request = self.forward or self.compensation
        if request is None:
            raise ValueError("tool activity request is absent")
        return request.identity

    def tool_name(self) -> str:
        request = self.forward or self.compensation
        if request is None:
            raise ValueError("tool activity request is absent")
        return request.tool_name


class ToolActivityResult(_Contract):
    """One outcome containing only a public, already-redacted receipt."""

    outcome: _ToolOutcome
    receipt: JsonObject | None = None
    reason_code: _Reason | None = None

    @field_validator("receipt")
    @classmethod
    def require_public_receipt(cls, value: JsonObject | None) -> JsonObject | None:
        if value is not None and contains_sensitive_json(value, RedactionPolicy()):
            raise ValueError("tool receipt must be public and redacted")
        return value

    @classmethod
    def succeeded(cls, receipt: JsonObject) -> Self:
        return cls(outcome="succeeded", receipt=receipt)

    @classmethod
    def failed(cls, reason_code: str) -> Self:
        return cls(outcome="failed", reason_code=reason_code)

    @classmethod
    def unresolved(cls, reason_code: str) -> Self:
        return cls(outcome="unresolved", reason_code=reason_code)

    @model_validator(mode="after")
    def require_exact_outcome_shape(self) -> Self:
        success = self.outcome == "succeeded"
        if success != (self.receipt is not None):
            raise ValueError("successful tool result requires exactly one receipt")
        if success == (self.reason_code is not None):
            raise ValueError("failed tool result requires exactly one reason")
        return self


class HumanCompensationResolution(_Contract):
    """Submit an authorized receipt for one unresolved compensation."""

    operation_id: OperationId
    based_on_event_seq: int = Field(strict=True, ge=1)
    authorization_reference: _AuthorizationReference
    receipt: JsonObject

    @field_validator("receipt")
    @classmethod
    def require_public_receipt(cls, value: JsonObject) -> JsonObject:
        if contains_sensitive_json(value, RedactionPolicy()):
            raise ValueError("human receipt must be public and redacted")
        return value

    @model_validator(mode="after")
    def require_receipt(self) -> Self:
        if not self.receipt:
            raise ValueError("human resolution requires a receipt")
        return self


class HumanResolutionVerificationRequest(_Contract):
    """Ask application-owned code to verify a human compensation decision."""

    saga_id: SagaId
    operation_id: OperationId
    based_on_event_seq: int = Field(strict=True, ge=1)
    authorization_reference: _AuthorizationReference
    receipt: JsonObject


class HumanResolutionVerificationResult(_Contract):
    """Report trusted authorization evidence or a sanitized rejection reason."""

    verified: bool
    trusted_actor: _Name | None = None
    authorization_id: _AuthorizationReference | None = None
    reason_code: _Reason | None = None

    @classmethod
    def accepted(cls, *, trusted_actor: str, authorization_id: str) -> Self:
        return cls(
            verified=True,
            trusted_actor=trusted_actor,
            authorization_id=authorization_id,
        )

    @classmethod
    def rejected(cls, reason_code: str) -> Self:
        return cls(verified=False, reason_code=reason_code)

    @model_validator(mode="after")
    def require_exact_shape(self) -> Self:
        trusted = self.trusted_actor is not None and self.authorization_id is not None
        if self.verified != trusted or self.verified == (self.reason_code is not None):
            raise ValueError("verified result requires exact trusted authorization evidence")
        return self


def _require_reconciliation_identity(request: ReconciliationActivityRequest) -> None:
    if request.forward_identity.direction is not Direction.FORWARD:
        raise ValueError("reconciliation requires a forward identity")


def _require_safe_reconciliation_correlation(request: ReconciliationActivityRequest) -> None:
    identity = request.compensation.identity if request.compensation else request.forward_identity
    if request.correlation_id != identity.operation_id:
        raise ValueError("reconciliation requires exact safe correlation")


def _require_reversible_effect(request: ReconciliationActivityRequest) -> None:
    if request.compensation is not None:
        if request.compensation.compensates_operation_id != request.forward_identity.operation_id:
            raise ValueError("compensation reconciliation requires its causal forward operation")
        return
    reversible = (
        request.declared_kind == "effect" and request.declared_compensation_tool is not None
    )
    if not reversible:
        raise ValueError("reconciliation requires a declared reversible effect")


def _require_tool_compensation(tool: WorkflowTool) -> None:
    if (tool.kind == "effect") != (tool.compensation_tool is not None):
        raise ValueError("tool kind requires matching compensation policy")


def _require_tool_proof(tool: WorkflowTool) -> None:
    if tool.proof_for_success and tool.kind != "read":
        raise ValueError("proof capability requires a read tool")


def _require_tool_prerequisites(tool: WorkflowTool) -> None:
    repeated = len(tool.prerequisites) != len(set(tool.prerequisites))
    if tool.name in tool.prerequisites or repeated:
        raise ValueError("tool prerequisites must be unique and exclude the tool")


def _require_unique_tool_names(tools: tuple[WorkflowTool, ...]) -> None:
    names = tuple(tool.name for tool in tools)
    if len(names) != len(set(names)):
        raise ValueError("workflow tools must be unique")


def _require_known_prerequisites(tools: tuple[WorkflowTool, ...]) -> None:
    names = frozenset(tool.name for tool in tools)
    declared = frozenset(item for tool in tools for item in tool.prerequisites)
    if not declared.issubset(names):
        raise ValueError("workflow prerequisites must reference known tools")


def _require_acyclic_prerequisites(tools: tuple[WorkflowTool, ...]) -> None:
    graph = {tool.name: tool.prerequisites for tool in tools}
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as error:
        raise ValueError("workflow prerequisites must be acyclic") from error
