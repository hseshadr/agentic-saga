from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import cast

import pytest
from pydantic import ValidationError

import agentic_saga.agents as agents_api
import agentic_saga.agents.proposals as proposal_contracts
from agentic_saga.agents.choice import ChoiceAgentDriver, DecisionSelection, ProposalCandidate
from agentic_saga.agents.pydanticai import AgentFailureCategory, AgentPlanningError
from agentic_saga.contracts.actions import Finish, ToolCall
from agentic_saga.contracts.runtime import SagaObservation, ToolDescriptor
from tests.unit.agents.test_pydanticai import _context, _descriptor, _observation

type CandidateFactory = Callable[
    [SagaObservation, Sequence[ToolDescriptor]],
    Awaitable[Sequence[ProposalCandidate]],
]


def test_agent_controls_are_not_public_proposal_intents() -> None:
    assert not hasattr(proposal_contracts, "BeginCompensationIntent")
    assert not hasattr(proposal_contracts, "EscalateIntent")
    assert "BeginCompensationIntent" not in agents_api.__all__
    assert "EscalateIntent" not in agents_api.__all__


def _candidate(
    candidate_id: str = "choice_00000001",
    *,
    proposal: dict[str, object] | None = None,
    criteria: str = "Inspect the current public state.",
    minimum_confidence: float = 0.8,
) -> ProposalCandidate:
    return ProposalCandidate.model_validate(
        {
            "candidate_id": candidate_id,
            "criteria": criteria,
            "minimum_confidence": minimum_confidence,
            "proposal": proposal
            or {
                "kind": "tool_call",
                "tool_name": "inspect",
                "arguments": {},
                "rationale": "Inspect current evidence.",
            },
        },
        strict=True,
    )


def _factory(*candidates: ProposalCandidate) -> CandidateFactory:
    async def create(
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> Sequence[ProposalCandidate]:
        del observation, available_tools
        return candidates

    return create


@pytest.mark.asyncio
async def test_should_bind_a_single_candidate_without_calling_decision_engine() -> None:
    async def unexpected_call(
        state: dict[str, object], criteria: dict[str, object]
    ) -> DecisionSelection:
        del state, criteria
        raise AssertionError("single candidate must not call the decision engine")

    driver = ChoiceAgentDriver(
        context=_context("inspect"),
        candidate_factory=_factory(_candidate(minimum_confidence=0.99)),
        decision_call=unexpected_call,
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert isinstance(proposal, ToolCall)
    assert proposal.tool_name == "inspect"
    assert proposal.based_on_saga_seq == 3
    assert proposal.proposal_id.startswith("proposal_")


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf")])
def test_should_validate_injected_driver_confidence_floor(value: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        ChoiceAgentDriver(
            context=_context("inspect"),
            candidate_factory=_factory(_candidate()),
            decision_call=_never_choose,
            min_confidence=value,
        )


@pytest.mark.asyncio
async def test_should_bind_proposal_identity_to_candidate_content() -> None:
    first = ChoiceAgentDriver(
        context=_context("inspect"),
        candidate_factory=_factory(_candidate()),
        decision_call=_never_choose,
    )
    changed = _candidate(
        proposal={
            "kind": "tool_call",
            "tool_name": "inspect",
            "arguments": {"version": 2},
            "rationale": "Inspect the newer public version.",
        }
    )
    second = ChoiceAgentDriver(
        context=_context("inspect"),
        candidate_factory=_factory(changed),
        decision_call=_never_choose,
    )

    first_proposal = await first.next_action(_observation(), (_descriptor("inspect"),))
    second_proposal = await second.next_action(_observation(), (_descriptor("inspect"),))

    assert first_proposal.proposal_id != second_proposal.proposal_id


@pytest.mark.asyncio
async def test_should_send_only_bounded_public_state_and_select_one_candidate() -> None:
    seen: dict[str, object] = {}
    finish = _candidate(
        "choice_00000002",
        proposal={
            "kind": "finish",
            "target_status": "succeeded_verified",
            "rationale": "Required evidence proves success.",
        },
        criteria="Finish because required evidence proves success.",
    )

    async def choose(state: dict[str, object], criteria: dict[str, object]) -> DecisionSelection:
        seen.update(state=state, criteria=criteria)
        return DecisionSelection(
            choice_id="choice_00000002",
            confidence=0.91,
            probabilities={"choice_00000001": 0.09, "choice_00000002": 0.91},
        )

    driver = ChoiceAgentDriver(
        context=_context("inspect"),
        candidate_factory=_factory(_candidate(), finish),
        decision_call=choose,
        min_confidence=0.8,
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert isinstance(proposal, Finish)
    assert proposal.target_status == "succeeded_verified"
    assert set(seen) == {"state", "criteria"}
    criteria = cast(Mapping[str, object], seen["criteria"])
    assert set(criteria) == {"choice_00000001", "choice_00000002"}
    first_criterion = cast(Mapping[str, object], criteria["choice_00000001"])
    assert set(first_criterion) == {"criteria", "proposal"}
    assert "proposal_id" not in repr(seen)


@pytest.mark.parametrize("count", [0, 256])
@pytest.mark.asyncio
async def test_should_enforce_a_bounded_candidate_count(count: int) -> None:
    candidates = tuple(_candidate(f"choice_{index:08d}") for index in range(count))
    driver = ChoiceAgentDriver(
        context=_context("inspect"),
        candidate_factory=_factory(*candidates),
        decision_call=_never_choose,
    )

    with pytest.raises(AgentPlanningError) as captured:
        await driver.next_action(_observation(), (_descriptor("inspect"),))
    assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_should_reject_duplicate_candidate_ids() -> None:
    driver = ChoiceAgentDriver(
        context=_context("inspect"),
        candidate_factory=_factory(_candidate(), _candidate()),
        decision_call=_never_choose,
    )

    with pytest.raises(AgentPlanningError) as captured:
        await driver.next_action(_observation(), (_descriptor("inspect"),))
    assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE


@pytest.mark.parametrize("candidate_id", ["inspect", "choice_short", "Choice_00000001"])
def test_should_require_opaque_candidate_ids(candidate_id: str) -> None:
    with pytest.raises(ValidationError):
        _candidate(candidate_id)


@pytest.mark.asyncio
async def test_should_reject_unavailable_tool_and_control_candidates() -> None:
    unavailable = _candidate(
        proposal={
            "kind": "tool_call",
            "tool_name": "charge_card",
            "arguments": {},
            "rationale": "Try an unavailable effect.",
        }
    )
    finish = _candidate(
        "choice_00000002",
        proposal={
            "kind": "finish",
            "target_status": "succeeded_verified",
            "rationale": "Claim an unavailable terminal state.",
        },
    )

    for candidate, observation in (
        (unavailable, _observation()),
        (finish, _observation(False)),
    ):
        driver = ChoiceAgentDriver(
            context=_context("inspect"),
            candidate_factory=_factory(candidate),
            decision_call=_never_choose,
        )
        with pytest.raises(AgentPlanningError) as captured:
            await driver.next_action(observation, (_descriptor("inspect"),))
        assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_should_reject_private_candidate_material_before_provider_call() -> None:
    private = "Bearer " + "private-provider-value"
    driver = ChoiceAgentDriver(
        context=_context("inspect"),
        candidate_factory=_factory(_candidate(criteria=private)),
        decision_call=_never_choose,
    )

    with pytest.raises(AgentPlanningError) as captured:
        await driver.next_action(_observation(), (_descriptor("inspect"),))
    assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE
    assert private not in f"{captured.value!s}{captured.value!r}"


@pytest.mark.parametrize(
    "selection",
    [
        DecisionSelection(
            choice_id="choice_99999999",
            confidence=0.9,
            probabilities={"choice_00000001": 0.1, "choice_00000002": 0.9},
        ),
        DecisionSelection(
            choice_id="choice_00000001",
            confidence=0.9,
            probabilities={"choice_00000001": 1.0},
        ),
        DecisionSelection(
            choice_id="choice_00000001",
            confidence=0.9,
            probabilities={"choice_00000001": 0.2, "choice_00000002": 0.8},
        ),
        DecisionSelection(
            choice_id="choice_00000001",
            confidence=0.79,
            probabilities={"choice_00000001": 0.8, "choice_00000002": 0.2},
        ),
        DecisionSelection(
            choice_id="choice_00000001",
            confidence=0.9,
            probabilities={"choice_00000001": 0.50001, "choice_00000002": 0.49998},
        ),
    ],
)
@pytest.mark.asyncio
async def test_should_fail_closed_on_invalid_or_uncertain_selection(
    selection: DecisionSelection,
) -> None:
    async def choose(state: dict[str, object], criteria: dict[str, object]) -> DecisionSelection:
        del state, criteria
        return selection

    driver = ChoiceAgentDriver(
        context=_context("inspect"),
        candidate_factory=_factory(_candidate(), _candidate("choice_00000002")),
        decision_call=choose,
        min_confidence=0.8,
    )

    with pytest.raises(AgentPlanningError) as captured:
        await driver.next_action(_observation(), (_descriptor("inspect"),))
    assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_should_enforce_the_selected_candidates_risk_threshold() -> None:
    async def choose(state: dict[str, object], criteria: dict[str, object]) -> DecisionSelection:
        del state, criteria
        return DecisionSelection(
            choice_id="choice_00000002",
            confidence=0.9,
            probabilities={"choice_00000001": 0.1, "choice_00000002": 0.9},
        )

    high_risk = _candidate("choice_00000002", minimum_confidence=0.95)
    driver = ChoiceAgentDriver(
        context=_context("inspect"),
        candidate_factory=_factory(_candidate(), high_risk),
        decision_call=choose,
        min_confidence=0.8,
    )

    with pytest.raises(AgentPlanningError) as captured:
        await driver.next_action(_observation(), (_descriptor("inspect"),))
    assert captured.value.category is AgentFailureCategory.INVALID_RESPONSE


@pytest.mark.parametrize(
    "values",
    [
        {"confidence": float("nan")},
        {"confidence": float("inf")},
        {"probabilities": {"choice_00000001": float("nan")}},
        {"probabilities": {"choice_00000001": -0.1}},
    ],
)
def test_should_reject_non_finite_or_out_of_range_decision_numbers(
    values: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "choice_id": "choice_00000001",
        "confidence": 1.0,
        "probabilities": {"choice_00000001": 1.0},
    }
    payload.update(values)
    with pytest.raises(ValidationError):
        DecisionSelection.model_validate(payload, strict=True)


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf")])
def test_should_require_a_finite_candidate_risk_threshold(value: float) -> None:
    with pytest.raises(ValidationError):
        _candidate(minimum_confidence=value)


@pytest.mark.asyncio
async def test_should_reject_runtime_catalog_drift_before_candidates_run() -> None:
    changed = _descriptor("inspect").model_copy(update={"description": "Changed."})
    called = False

    async def factory(
        observation: SagaObservation, tools: Sequence[ToolDescriptor]
    ) -> Sequence[ProposalCandidate]:
        nonlocal called
        del observation, tools
        called = True
        return (_candidate(),)

    driver = ChoiceAgentDriver(
        context=_context("inspect"),
        candidate_factory=factory,
        decision_call=_never_choose,
    )

    with pytest.raises(ValueError, match="catalog"):
        await driver.next_action(_observation(), (changed,))
    assert called is False


async def _never_choose(state: dict[str, object], criteria: dict[str, object]) -> DecisionSelection:
    del state, criteria
    raise AssertionError("decision engine must not be called")
