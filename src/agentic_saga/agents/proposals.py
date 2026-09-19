"""Provider-neutral, unbound proposal intents for bounded decision engines."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from agentic_saga.contracts.actions import AgentProposal
from agentic_saga.contracts.common import JsonObject, sha256_json
from agentic_saga.contracts.runtime import SagaObservation

type _BoundedName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _Rationale = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
type _TerminalStatus = Literal["succeeded_verified"]

_PROPOSAL_ID_DOMAIN = "agentic-saga:bounded-choice-proposal:v1"
_AGENT_PROPOSAL: TypeAdapter[AgentProposal] = TypeAdapter(AgentProposal)


class _IntentBase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ToolCallIntent(_IntentBase):
    """A complete tool call whose identity and Saga sequence are not yet bound."""

    kind: Literal["tool_call"] = "tool_call"
    tool_name: _BoundedName
    arguments: JsonObject
    rationale: _Rationale


class FinishIntent(_IntentBase):
    """A complete terminal proposal whose trusted fields are not yet bound."""

    kind: Literal["finish"] = "finish"
    rationale: _Rationale
    target_status: _TerminalStatus


type ProposalIntent = Annotated[
    ToolCallIntent | FinishIntent,
    Field(discriminator="kind"),
]


def bind_proposal(
    intent: ProposalIntent,
    observation: SagaObservation,
    candidate_id: str,
) -> AgentProposal:
    """Bind host-owned identity and sequence after a decision engine selects an intent."""

    payload = intent.model_dump(mode="json")
    payload["proposal_id"] = _proposal_id(observation, candidate_id, intent)
    payload["based_on_saga_seq"] = observation.saga_seq
    return _AGENT_PROPOSAL.validate_python(payload, strict=True)


def _proposal_id(observation: SagaObservation, candidate_id: str, intent: ProposalIntent) -> str:
    payload = {
        "candidate_id": candidate_id,
        "domain": _PROPOSAL_ID_DOMAIN,
        "intent": intent.model_dump(mode="json"),
        "saga_id": observation.saga_id,
        "saga_seq": observation.saga_seq,
    }
    return f"proposal_{sha256_json(payload)}"


__all__ = [
    "FinishIntent",
    "ProposalIntent",
    "ToolCallIntent",
    "bind_proposal",
]
