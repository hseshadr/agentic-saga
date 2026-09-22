"""Optional planning adapters; core imports do not load provider dependencies."""

from agentic_saga.agents.choice import (
    ChoiceAgentDriver,
    DecisionSelection,
    ProposalCandidate,
)
from agentic_saga.agents.jev import JevSettings, build_jev_driver
from agentic_saga.agents.openrouter import OpenRouterSettings, build_openrouter_driver
from agentic_saga.agents.openrouter_decisions import (
    OpenRouterDecisionsSettings,
    build_openrouter_decisions_driver,
)
from agentic_saga.agents.proposals import (
    FinishIntent,
    ProposalIntent,
    ToolCallIntent,
)
from agentic_saga.agents.pydanticai import PydanticAIDriver, native_proposal_tool_names

__all__ = [
    "ChoiceAgentDriver",
    "DecisionSelection",
    "FinishIntent",
    "JevSettings",
    "OpenRouterDecisionsSettings",
    "OpenRouterSettings",
    "ProposalCandidate",
    "ProposalIntent",
    "PydanticAIDriver",
    "ToolCallIntent",
    "build_jev_driver",
    "build_openrouter_decisions_driver",
    "build_openrouter_driver",
    "native_proposal_tool_names",
]
