from __future__ import annotations

from pathlib import Path

import pytest

from agentic_saga.contracts.common import Reversibility
from agentic_saga.contracts.events import (
    AgentTurnReserved,
    HumanRequired,
    ReconciliationRecorded,
)
from agentic_saga.contracts.runtime import ExecutionBudget, SagaGoal, SagaStatus
from agentic_saga.contracts.tools import (
    EffectToolDefinition,
    ToolCapabilities,
    ToolRegistry,
)
from tests.support.reconciliation_adapter import ProbeAdapter, initialize_probe, probe_counts
from tests.unit.execution.test_runtime_loop import (
    ReadCommand,
    _agent,
    _budget,
    _harness,
)


def _unknown_registry(probe: Path) -> ToolRegistry:
    adapter = ProbeAdapter({"mode": "unknown", "probe_path": str(probe)})
    capabilities = ToolCapabilities(
        idempotency_retention_seconds=None,
        reconciliation_supported=False,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.IRREVERSIBLE,
        partial_effects_possible=False,
    )
    definition = EffectToolDefinition(
        "mutate_generic",
        "mutate-v1",
        "command-v1",
        ReadCommand,
        adapter,
        capabilities,
        None,
    )
    return ToolRegistry((definition,))


@pytest.mark.asyncio
async def test_unknown_forward_effect_reconciles_to_human_without_blind_delivery(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "probe.db"
    initialize_probe(probe)
    harness = _harness(tmp_path, _unknown_registry(probe))

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id="goal_unknown_unwind", text="Fail safely.", context={}),
        agent=_agent(["effect"], tool_name="mutate_generic"),
    )

    events = harness.store.read_events(result.saga_id)
    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert probe_counts(probe)[0] == 1
    assert sum(isinstance(item, AgentTurnReserved) for item in events) == 1
    assert any(isinstance(item, ReconciliationRecorded) for item in events)
    assert any(isinstance(item, HumanRequired) for item in events)


def _exhausted(field: str) -> ExecutionBudget:
    return _budget().model_copy(update={field: 0})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "turn_limit",
        "tool_call_limit",
    ],
)
async def test_each_valid_initial_zero_budget_stops_before_agent(
    tmp_path: Path, field: str
) -> None:
    harness = _harness(tmp_path, execution_budget=_exhausted(field))

    result = await harness.runtime.start(
        definition=harness.definition,
        goal=SagaGoal(goal_id=f"goal_budget_{field}", text="Stay bounded.", context={}),
        agent=_agent([]),
    )

    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert not any(
        isinstance(item, AgentTurnReserved) for item in harness.store.read_events(result.saga_id)
    )
