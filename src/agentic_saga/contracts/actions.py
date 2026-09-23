from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SerializeAsAny, StringConstraints

from agentic_saga.contracts.common import Direction, JsonObject, OperationId, SagaId, StepInstanceId

type _BoundedName = Annotated[str, StringConstraints(min_length=1, max_length=200)]
type _HashDigest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
type _Rationale = Annotated[str, StringConstraints(min_length=1, max_length=500)]
type _AuthProof = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
type _ProposalId = Annotated[
    str, StringConstraints(strict=True, pattern=r"^proposal_[a-z0-9][a-z0-9_-]{7,63}$")
]
type _TerminalStatus = Literal["succeeded_verified"]


class ToolCall(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["tool_call"] = "tool_call"
    proposal_id: _ProposalId
    tool_name: _BoundedName
    arguments: JsonObject
    based_on_saga_seq: int = Field(ge=0)
    rationale: _Rationale


class Finish(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["finish"] = "finish"
    proposal_id: _ProposalId
    based_on_saga_seq: int = Field(ge=0)
    rationale: _Rationale
    target_status: _TerminalStatus


type AgentProposal = Annotated[
    ToolCall | Finish,
    Field(discriminator="kind"),
]


class AuthorizedToolCall[CommandT: BaseModel](BaseModel):
    """Bind one command to its stable operation identity.

    Reserved; not enforced by the runtime yet. The Temporal Workflow builds its own Activity
    requests and never constructs or checks this model.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    operation_id: OperationId
    step_instance_id: StepInstanceId
    direction: Direction
    semantic_generation: int = Field(ge=0)
    command: SerializeAsAny[CommandT]


class HumanDecision(BaseModel):
    """Describe a human approval, rejection, or reconciliation decision.

    Reserved; not enforced by the runtime yet. Human recovery is accepted only through the
    Workflow's ``resolve_compensation`` Update and its application-owned verification Activity.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    decision_id: _BoundedName
    saga_id: SagaId
    based_on_saga_seq: int = Field(ge=0)
    action: Literal["approve", "reject", "reconcile"]
    proposal_hash: _HashDigest
    actor: _BoundedName
    issued_at: AwareDatetime
    auth_proof: _AuthProof
