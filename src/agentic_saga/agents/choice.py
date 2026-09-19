"""Bounded finite-choice adapter for non-generative decision engines."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from math import isclose, isfinite
from typing import Annotated, Never

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agentic_saga.agents.deepagents import AgentFailureCategory, AgentPlanningError
from agentic_saga.agents.proposals import (
    BeginCompensationIntent,
    EscalateIntent,
    FinishIntent,
    ProposalIntent,
    ToolCallIntent,
    bind_proposal,
)
from agentic_saga.contracts.actions import AgentProposal
from agentic_saga.contracts.common import JsonPayloadError, require_bounded_json
from agentic_saga.contracts.runtime import AgentDriver, SagaObservation, ToolDescriptor
from agentic_saga.manifest import SagaContext, require_public_agent_payload

type _CandidateId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^choice_[a-z0-9]{8,32}$"),
]
type _Description = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=500),
]
type _Probability = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
type CandidateFactory = Callable[
    [SagaObservation, Sequence[ToolDescriptor]],
    Awaitable[Sequence[ProposalCandidate]],
]
type DecisionCall = Callable[[dict[str, object], dict[str, object]], Awaitable[DecisionSelection]]

_MAX_CANDIDATES = 255
_PROBABILITY_TOLERANCE = 0.000_001
_CONTROL_TOOLS = frozenset({"finish_saga", "begin_compensation", "escalate_to_human"})


class ProposalCandidate(BaseModel):
    """One concrete, public, unbound proposal offered to a decision engine."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    candidate_id: _CandidateId
    criteria: _Description
    minimum_confidence: float = Field(default=0.8, strict=True, ge=0, le=1, allow_inf_nan=False)
    proposal: ProposalIntent


class DecisionSelection(BaseModel):
    """Validated finite-choice evidence returned by a decision engine."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    choice_id: _CandidateId
    confidence: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    probabilities: dict[_CandidateId, _Probability] = Field(min_length=1, max_length=255)


@dataclass(frozen=True)
class ChoiceAgentDriver(AgentDriver):
    """Select one host-built proposal; never ask a model to invent arguments."""

    context: SagaContext
    candidate_factory: CandidateFactory
    decision_call: DecisionCall
    min_confidence: float = 0.8
    provider_id: str = "injected"
    model_route: tuple[str, ...] = ("injected",)

    def __post_init__(self) -> None:
        if not _valid_threshold(self.min_confidence):
            raise ValueError("min_confidence must be finite and between zero and one")

    async def next_action(
        self,
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> AgentProposal:
        tools = tuple(available_tools)
        _require_catalog(self.context, tools)
        candidates = await self.candidate_factory(observation, tools)
        validated = _validated_candidates(candidates, observation, tools)
        selected = await self._select(validated, observation, tools)
        return bind_proposal(selected.proposal, observation, selected.candidate_id)

    async def _select(
        self,
        candidates: tuple[ProposalCandidate, ...],
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> ProposalCandidate:
        if len(candidates) == 1:
            return candidates[0]
        state, criteria = _decision_payload(observation, available_tools, candidates)
        selection = await self.decision_call(state, criteria)
        return _selected_candidate(selection, candidates, self.min_confidence)


def _validated_candidates(
    candidates: Sequence[ProposalCandidate],
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> tuple[ProposalCandidate, ...]:
    values = tuple(candidates)
    if not 1 <= len(values) <= _MAX_CANDIDATES:
        _invalid_response()
    if any(type(item) is not ProposalCandidate for item in values):
        _invalid_response()
    _require_unique_ids(values)
    for candidate in values:
        _require_candidate(candidate, observation, available_tools)
    return values


def _require_unique_ids(candidates: tuple[ProposalCandidate, ...]) -> None:
    identifiers = tuple(item.candidate_id for item in candidates)
    if len(identifiers) != len(set(identifiers)):
        _invalid_response()


def _require_candidate(
    candidate: ProposalCandidate,
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> None:
    try:
        payload = candidate.model_dump(mode="json")
        require_bounded_json(payload)
        require_public_agent_payload(payload)
    except (JsonPayloadError, TypeError, ValueError):
        _invalid_response()
    if not _proposal_is_eligible(candidate.proposal, observation, available_tools):
        _invalid_response()


def _proposal_is_eligible(
    proposal: ProposalIntent,
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
) -> bool:
    if isinstance(proposal, ToolCallIntent):
        return proposal.tool_name in _tool_names(available_tools)
    return _control_is_eligible(proposal, observation)


def _tool_names(available_tools: Sequence[ToolDescriptor]) -> frozenset[str]:
    return frozenset(item.name for item in available_tools)


def _control_is_eligible(proposal: ProposalIntent, observation: SagaObservation) -> bool:
    controls = observation.proposal_controls
    if isinstance(proposal, FinishIntent):
        return proposal.target_status in controls.finish_targets
    if isinstance(proposal, BeginCompensationIntent):
        return controls.begin_compensation
    if isinstance(proposal, EscalateIntent):
        return controls.escalate_to_human
    return False


def _decision_payload(
    observation: SagaObservation,
    available_tools: Sequence[ToolDescriptor],
    candidates: tuple[ProposalCandidate, ...],
) -> tuple[dict[str, object], dict[str, object]]:
    state: dict[str, object] = {
        "observation": observation.model_dump(mode="json"),
        "available_tools": [item.model_dump(mode="json") for item in available_tools],
    }
    criteria: dict[str, object] = {item.candidate_id: _criterion(item) for item in candidates}
    _require_provider_payload(state, criteria)
    return state, criteria


def _criterion(candidate: ProposalCandidate) -> dict[str, object]:
    return {
        "criteria": candidate.criteria,
        "proposal": candidate.proposal.model_dump(mode="json"),
    }


def _require_provider_payload(state: dict[str, object], criteria: dict[str, object]) -> None:
    try:
        payload = {"state": state, "criteria": criteria}
        require_bounded_json(payload)
        require_public_agent_payload(payload)
    except (JsonPayloadError, TypeError, ValueError):
        _invalid_response()


def _selected_candidate(
    selection: DecisionSelection,
    candidates: tuple[ProposalCandidate, ...],
    min_confidence: float,
) -> ProposalCandidate:
    if type(selection) is not DecisionSelection:
        _invalid_response()
    by_id = {item.candidate_id: item for item in candidates}
    selected = by_id.get(selection.choice_id)
    if selected is None:
        _invalid_response()
    threshold = max(min_confidence, selected.minimum_confidence)
    _require_selection(selection, frozenset(by_id), threshold)
    return selected


def _require_selection(
    selection: DecisionSelection,
    identifiers: frozenset[str],
    min_confidence: float,
) -> None:
    if not _selection_is_valid(selection, identifiers, min_confidence):
        _invalid_response()


def _selection_is_valid(
    selection: DecisionSelection,
    identifiers: frozenset[str],
    minimum: float,
) -> bool:
    checks = (
        _valid_threshold(minimum),
        set(selection.probabilities) == identifiers,
        selection.choice_id in identifiers,
        selection.confidence >= minimum,
        _normalized(selection.probabilities),
        _selected_maximum(selection),
    )
    return all(checks)


def _valid_threshold(value: float) -> bool:
    return isfinite(value) and 0 <= value <= 1


def _normalized(probabilities: Mapping[str, float]) -> bool:
    return isclose(sum(probabilities.values()), 1.0, abs_tol=_PROBABILITY_TOLERANCE)


def _selected_maximum(selection: DecisionSelection) -> bool:
    probabilities = selection.probabilities
    selected = probabilities.get(selection.choice_id)
    return selected is not None and selected == max(probabilities.values())


def _require_catalog(context: SagaContext, available_tools: Sequence[ToolDescriptor]) -> None:
    pinned = {item.name: item for item in context.tool_descriptors}
    names = tuple(item.name for item in available_tools)
    _require_catalog_names(names)
    matches = (_same_descriptor(pinned.get(item.name), item) for item in available_tools)
    if not all(matches):
        raise ValueError("available descriptor catalog differs from Saga Context")


def _require_catalog_names(names: tuple[str, ...]) -> None:
    if len(names) != len(set(names)):
        raise ValueError("available descriptor catalog differs from Saga Context")
    if _CONTROL_TOOLS.intersection(names):
        raise ValueError("available descriptor catalog differs from Saga Context")


def _same_descriptor(expected: ToolDescriptor | None, current: ToolDescriptor) -> bool:
    if expected is None:
        return False
    fields = ("name", "kind", "description", "input_schema", "reversibility")
    return all(getattr(expected, field) == getattr(current, field) for field in fields)


def _invalid_response() -> Never:
    raise AgentPlanningError(AgentFailureCategory.INVALID_RESPONSE) from None


__all__ = ["ChoiceAgentDriver", "DecisionSelection", "ProposalCandidate"]
