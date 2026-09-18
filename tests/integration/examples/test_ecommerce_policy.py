from __future__ import annotations

import gc
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic_saga.contracts.actions import AgentProposal, BeginCompensation, Escalate, ToolCall
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import thaw_json_object
from agentic_saga.contracts.events import CompensationStarted, HumanRequired, ProposalRejected
from agentic_saga.contracts.runtime import SagaGoal, SagaObservation, SagaStatus, ToolDescriptor
from examples.ecommerce import provider as provider_module
from examples.ecommerce.demo import (
    ScriptedProposalDriver,
    _Assembly,
    _initialize,
    _reopen,
    run_scenario,
)
from examples.ecommerce.domain import ScenarioName
from examples.ecommerce.provider import EcommerceProvider


@dataclass
class TamperedPaymentDriver:
    tool: str
    changes: Mapping[str, object]
    delegate: ScriptedProposalDriver = field(default_factory=ScriptedProposalDriver)

    async def next_action(
        self, observation: SagaObservation, tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        proposal = await self.delegate.next_action(observation, tools)
        if not isinstance(proposal, ToolCall) or proposal.tool_name != self.tool:
            return proposal
        arguments = thaw_json_object(proposal.arguments) | dict(self.changes)
        return ToolCall.model_validate(proposal.model_dump() | {"arguments": arguments})


@dataclass
class PrematureCompensationDriver:
    delegate: ScriptedProposalDriver = field(default_factory=ScriptedProposalDriver)

    async def next_action(
        self, observation: SagaObservation, tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        proposal_id = f"proposal_{observation.saga_seq:020d}"
        if _last_action_was_rejected(observation):
            return Escalate(
                proposal_id=proposal_id,
                based_on_saga_seq=observation.saga_seq,
                reason_code="premature_compensation_rejected",
                rationale="A human should inspect the rejected compensation request.",
            )
        if _confirmed_reservation(observation):
            return BeginCompensation(
                proposal_id=proposal_id,
                based_on_saga_seq=observation.saga_seq,
                reason_code="speculative_failure",
                rationale="Hostile attempt to unwind a healthy forward path.",
            )
        return await self.delegate.next_action(observation, tools)


def _last_action_was_rejected(observation: SagaObservation) -> bool:
    return observation.last_action is not None and (
        observation.last_action.get("event_type") == "proposal_rejected"
    )


def _confirmed_reservation(observation: SagaObservation) -> bool:
    operations = observation.projection.get("operations")
    if not isinstance(operations, Mapping):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("tool_name") == "reserve_inventory"
        and item.get("status") == "effect_confirmed"
        for item in operations.values()
    )


def test_provider_closes_every_sqlite_connection(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        provider = EcommerceProvider.initialize(
            tmp_path / "provider.db", clock, ScenarioName.HAPPY_PATH
        )
        provider.snapshot()
        provider.counts()
        gc.collect()
    assert not [item for item in caught if item.category is ResourceWarning]


def test_provider_closes_connection_when_configuration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_configuration(_: object) -> None:
        raise RuntimeError("configuration rejected")

    monkeypatch.setattr(provider_module, "_configure", reject_configuration)
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        with pytest.raises(RuntimeError, match="configuration rejected"):
            EcommerceProvider.initialize(tmp_path / "provider.db", clock, ScenarioName.HAPPY_PATH)
        gc.collect()
    assert not [item for item in caught if item.category is ResourceWarning]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "tool", "changes"),
    [
        (ScenarioName.HAPPY_PATH, "charge_payment", {"amount_minor": 1}),
        (ScenarioName.HAPPY_PATH, "charge_payment", {"currency": "EUR"}),
        (ScenarioName.HAPPY_PATH, "charge_payment", {"customer_id": "customer_other"}),
        (ScenarioName.BUSINESS_FAILURE, "refund_payment", {"amount_minor": 1}),
        (ScenarioName.BUSINESS_FAILURE, "refund_payment", {"currency": "EUR"}),
        (ScenarioName.BUSINESS_FAILURE, "refund_payment", {"customer_id": "customer_other"}),
    ],
)
async def test_tampered_payment_never_reaches_provider(
    tmp_path: Path, scenario: ScenarioName, tool: str, changes: Mapping[str, object]
) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    assembly = _initialize(tmp_path, scenario, clock)
    goal = SagaGoal(
        goal_id="goal_policy_test", text=assembly.context.manifest.objective, context={}
    )

    result = await assembly.runtime.start(
        definition=assembly.definition,
        goal=goal,
        agent=TamperedPaymentDriver(tool, changes),
    )

    count = next(item for item in assembly.provider.counts() if item.tool == tool)
    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert (count.executes, count.effects, count.reconciliations) == (0, 0, 0)
    if tool == "refund_payment":
        await _assert_stalled_compensation_is_durable(assembly, result.saga_id, clock)


@pytest.mark.asyncio
async def test_healthy_forward_path_rejects_premature_compensation(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    assembly = _initialize(tmp_path, ScenarioName.HAPPY_PATH, clock)
    goal = SagaGoal(
        goal_id="goal_compensation_guard", text=assembly.context.manifest.objective, context={}
    )

    result = await assembly.runtime.start(
        definition=assembly.definition,
        goal=goal,
        agent=PrematureCompensationDriver(),
    )

    events = assembly.store.read_events(result.saga_id)
    assert result.state is SagaStatus.HUMAN_REQUIRED
    assert not any(isinstance(item, CompensationStarted) for item in events)
    rejection = next(item for item in events if isinstance(item, ProposalRejected))
    assert rejection.reason_code == "compensation_not_justified"


async def _assert_stalled_compensation_is_durable(
    assembly: _Assembly, saga_id: str, clock: FakeClock
) -> None:
    events = assembly.store.read_events(saga_id)
    snapshot = assembly.store.load_snapshot(saga_id)
    assert sum(isinstance(item, CompensationStarted) for item in events) == 1
    assert isinstance(events[-1], HumanRequired)
    assert events[-1].reason_code == "emergency_unwind_compensation_progress_stalled"
    assert snapshot.resume_status is SagaStatus.COMPENSATING
    assert assembly.store.runnable_count(saga_id) == 0
    reopened = _reopen(assembly, clock)
    resumed = await reopened.runtime.resume(saga_id=saga_id, agent=ScriptedProposalDriver())
    assert (resumed.state, resumed.saga_seq) == (SagaStatus.HUMAN_REQUIRED, snapshot.seq)


@pytest.mark.asyncio
async def test_evidence_order_pairs_each_intent_with_its_effect(tmp_path: Path) -> None:
    run = await run_scenario(ScenarioName.HAPPY_PATH, tmp_path)
    events = list(run.trace.events)
    outcomes = [item for item in events if item.event_type == "effect_outcome_recorded"]
    first = events.index(outcomes[0])
    events[first] = outcomes[0].model_copy(update={"operation_id": outcomes[1].operation_id})
    altered = replace(run, trace=run.trace.model_copy(update={"events": tuple(events)}))

    assert altered.evidence_order_is_valid() is False
