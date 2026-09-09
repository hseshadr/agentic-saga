from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic_saga.contracts.actions import AgentProposal, Escalate
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.runtime import SagaObservation, SagaStatus, ToolDescriptor
from agentic_saga.contracts.tools import EffectToolDefinition
from agentic_saga.manifest import SagaManifest
from examples.ecommerce.demo import (
    ScriptedProposalDriver,
    _reopen,
    prepare_eval_case,
    run_with_agent,
)
from examples.ecommerce.domain import CheckInventory
from examples.ecommerce.evaluation import load_corpus
from examples.ecommerce.provider import BusinessEffectRejected, EcommerceProvider

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "examples/ecommerce/eval-corpus-v1.json"
NOW = datetime(2026, 9, 8, tzinfo=UTC)


class EscalatingDriver:
    async def next_action(
        self, observation: SagaObservation, tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        del tools
        return Escalate(
            proposal_id=f"proposal_{'1' * 20}",
            based_on_saga_seq=observation.saga_seq,
            reason_code="fixture_test_pause",
            rationale="Pause after proving the injected driver reached the runtime.",
        )


def test_every_case_fixture_builds_the_same_provider_and_catalog_twice(tmp_path: Path) -> None:
    for case in load_corpus(CORPUS):
        first = prepare_eval_case(case, tmp_path / "first" / case.case_id, FakeClock(NOW))
        second = prepare_eval_case(case, tmp_path / "second" / case.case_id, FakeClock(NOW))

        assert first.provider.snapshot() == second.provider.snapshot()
        assert first.context.tool_descriptors == second.context.tool_descriptors


@pytest.mark.asyncio
async def test_eval_case_runs_through_real_runtime_with_injected_driver(tmp_path: Path) -> None:
    case = load_corpus(CORPUS)[0]
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))

    evidence = await run_with_agent(prepared, case, EscalatingDriver())

    assert evidence.result.state is SagaStatus.HUMAN_REQUIRED
    assert evidence.trace.saga_id == evidence.result.saga_id
    assert any(event.event_type == "human_required" for event in evidence.trace.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "required_event"),
    [
        ("r01-alternate-warehouse", "read_observed:check_inventory"),
        ("r02-restock-within-policy", "read_unavailable:check_inventory"),
        ("r03-primary-version-conflict", "proposal_rejected"),
        ("r04-transient-inventory-read", "read_unavailable:check_inventory"),
    ],
)
async def test_diverse_recovery_fixture_executes_with_one_generic_scripted_driver(
    tmp_path: Path, case_id: str, required_event: str
) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == case_id)
    prepared = prepare_eval_case(case, tmp_path / case_id, FakeClock(NOW))

    evidence = await run_with_agent(prepared, case, ScriptedProposalDriver())
    qualified = {
        f"{event.event_type}:{event.tool_name}" if event.tool_name else event.event_type
        for event in evidence.trace.events
    }

    assert evidence.result.state is SagaStatus.SUCCEEDED_VERIFIED
    assert required_event in qualified


@pytest.mark.asyncio
async def test_consumed_read_fault_stays_consumed_after_provider_reopen(tmp_path: Path) -> None:
    case = next(
        item for item in load_corpus(CORPUS) if item.case_id == "r04-transient-inventory-read"
    )
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))
    command = CheckInventory(sku=case.fixture.provider_state.sku, quantity=1)

    with pytest.raises(BusinessEffectRejected, match="temporarily unavailable"):
        await prepared.provider.read("check_inventory", command)
    reopened = EcommerceProvider.open(prepared.provider.path, prepared.clock)

    assert (await reopened.read("check_inventory", command))["available"] == 2


def test_capability_override_repins_the_effective_agent_catalog(tmp_path: Path) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == "e06-short-dedup-horizon")
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))
    context = prepared.context
    actual = SagaManifest.tool_catalog_sha256(
        prepared.definition.registry, context.manifest.tools.allowed
    )
    charge = prepared.definition.registry.definition("charge_payment")

    assert isinstance(charge, EffectToolDefinition)
    assert charge.capabilities.idempotency_retention_seconds == 1
    assert context.manifest.tools.catalog_sha256 == actual


def test_capability_override_survives_runtime_reopen(tmp_path: Path) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == "e06-short-dedup-horizon")
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))

    reopened = _reopen(prepared, prepared.clock)
    charge = reopened.definition.registry.definition("charge_payment")

    assert isinstance(charge, EffectToolDefinition)
    assert charge.capabilities.idempotency_retention_seconds == 1
