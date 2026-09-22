from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from agentic_saga.agents.choice import CandidateFactory
from agentic_saga.agents.jev import JevSettings, build_jev_driver
from agentic_saga.agents.openrouter_decisions import (
    OpenRouterDecisionsSettings,
    build_openrouter_decisions_driver,
)
from agentic_saga.contracts.actions import ToolCall
from tests.unit.agents.test_choice import _candidate, _factory
from tests.unit.agents.test_pydanticai import _context, _descriptor, _observation

pytestmark = [pytest.mark.live_model, pytest.mark.network]


@pytest.mark.asyncio
async def test_live_jev_selects_one_bounded_candidate() -> None:
    if os.environ.get("RUN_LIVE_MODEL_EVALS") != "1":
        pytest.skip("set RUN_LIVE_MODEL_EVALS=1 to authorize a paid live request")
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        pytest.skip("set TYPESAFE_API_KEY to run the live Jev smoke test")
    driver = build_jev_driver(
        _context("inspect"),
        _bounded_candidates(),
        JevSettings(api_key=SecretStr(api_key), min_confidence=0.0),
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert isinstance(proposal, ToolCall)
    assert proposal.tool_name == "inspect"
    assert proposal.arguments == {}
    assert proposal.based_on_saga_seq == 3


@pytest.mark.asyncio
async def test_live_openrouter_jev_selects_one_bounded_candidate() -> None:
    if os.environ.get("RUN_LIVE_MODEL_EVALS") != "1":
        pytest.skip("set RUN_LIVE_MODEL_EVALS=1 to authorize a paid live request")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("set OPENROUTER_API_KEY to run the live Jev smoke test")
    driver = build_openrouter_decisions_driver(
        _context("inspect"),
        _bounded_candidates(),
        OpenRouterDecisionsSettings(api_key=SecretStr(api_key), min_confidence=0.0),
    )

    proposal = await driver.next_action(_observation(), (_descriptor("inspect"),))

    assert isinstance(proposal, ToolCall)
    assert proposal.tool_name == "inspect"
    assert proposal.arguments == {}
    assert proposal.based_on_saga_seq == 3


def _bounded_candidates() -> CandidateFactory:
    return _factory(
        _candidate(
            criteria="Inspect the authoritative order record.",
            minimum_confidence=0.0,
            proposal=_proposal("order", "Order evidence is the best next observation."),
        ),
        _candidate(
            "choice_00000002",
            criteria="Inspect the authoritative inventory record.",
            minimum_confidence=0.0,
            proposal=_proposal("inventory", "Inventory evidence is the best next observation."),
        ),
    )


def _proposal(source: str, rationale: str) -> dict[str, object]:
    return {
        "kind": "tool_call",
        "tool_name": "inspect",
        "arguments": {},
        "rationale": f"{source}: {rationale}",
    }
