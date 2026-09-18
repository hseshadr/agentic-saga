from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from agentic_saga.contracts.actions import (
    AgentProposal,
    BeginCompensation,
    Escalate,
    Finish,
    ToolCall,
)
from agentic_saga.contracts.clock import FakeClock
from agentic_saga.contracts.common import JsonObject
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
from examples.ecommerce.evaluation import EvalCase, load_corpus
from examples.ecommerce.provider import BusinessEffectRejected, EcommerceProvider

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "examples/ecommerce/eval-corpus-v1.json"
NOW = datetime(2026, 9, 8, tzinfo=UTC)
_JSON: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


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


class ChargeThenEscalateDriver:
    def __init__(self) -> None:
        self.calls = 0
        self.charge_constraints: dict[str, object] = {}
        self.customer_authorized: object = None

    async def next_action(
        self, observation: SagaObservation, tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        charge = next(item for item in tools if item.name == "charge_payment")
        self.charge_constraints = dict(charge.policy_constraints)
        self.customer_authorized = observation.goal.context.get("customer_authorized")
        self.calls += 1
        if self.calls > 1:
            return Escalate(
                proposal_id=f"proposal_{'2' * 20}",
                based_on_saga_seq=observation.saga_seq,
                reason_code="unsafe_recovery_retention",
                rationale="The deterministic recovery constraint requires a human decision.",
            )
        return ToolCall(
            proposal_id=f"proposal_{'3' * 20}",
            based_on_saga_seq=observation.saga_seq,
            tool_name="charge_payment",
            arguments={
                "order_id": "order_demo_001",
                "customer_id": "customer_demo_001",
                "amount_minor": 7900,
                "currency": "USD",
            },
            rationale="Exercise deterministic policy even if an agent ignores the constraint.",
        )


class HostileEffectThenEscalateDriver:
    def __init__(self, tool_name: str, arguments: Mapping[str, object] | None = None) -> None:
        self.tool_name = tool_name
        self.arguments = None if arguments is None else _JSON.validate_python(arguments)
        self.calls = 0
        self.constraints: dict[str, object] = {}

    async def next_action(
        self, observation: SagaObservation, tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        self.calls += 1
        if self.calls > 1:
            return Escalate(
                proposal_id=f"proposal_{'6' * 19}{self.calls}",
                based_on_saga_seq=observation.saga_seq,
                reason_code="deterministic_policy_blocked",
                rationale="Stop after deliberately challenging the deterministic policy.",
            )
        descriptor = next(item for item in tools if item.name == self.tool_name)
        self.constraints = dict(descriptor.policy_constraints)
        return ToolCall(
            proposal_id=f"proposal_{'7' * 20}",
            based_on_saga_seq=observation.saga_seq,
            tool_name=self.tool_name,
            arguments=self.arguments or _hostile_arguments(self.tool_name),
            rationale="Deliberately attempt a forward mutation that policy must reject.",
        )


def _hostile_arguments(tool_name: str) -> JsonObject:
    shared = {"order_id": "order_demo_001"}
    values = {
        "reserve_inventory": shared
        | {"sku": "sku_travel_pack", "quantity": 1, "expected_version": 1},
        "charge_payment": shared
        | {
            "customer_id": "customer_demo_001",
            "amount_minor": 7900,
            "currency": "USD",
        },
        "schedule_fulfillment": shared,
    }
    return _JSON.validate_python(values[tool_name])


class CleanAbortDriver:
    async def next_action(
        self, observation: SagaObservation, tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        del tools
        return Finish(
            proposal_id=f"proposal_{'4' * 20}",
            based_on_saga_seq=observation.saga_seq,
            rationale="No external effect exists, so fresh proof can establish a clean abort.",
            target_status="aborted_clean",
        )


class BudgetCaptureDriver:
    def __init__(self) -> None:
        self.observations: list[SagaObservation] = []

    async def next_action(
        self, observation: SagaObservation, tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        del tools
        self.observations.append(observation)
        return Escalate(
            proposal_id=f"proposal_{'5' * 20}",
            based_on_saga_seq=observation.saga_seq,
            reason_code="fixture_budget_observed",
            rationale="Pause after recording the real budget presented by the runtime.",
        )


class ReserveThenCompensateDriver:
    async def next_action(
        self, observation: SagaObservation, tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        proposal_id = f"proposal_{observation.saga_seq:020d}"
        if observation.state is SagaStatus.RUNNING and _has_forward_effect(observation):
            return BeginCompensation(
                proposal_id=proposal_id,
                based_on_saga_seq=observation.saga_seq,
                reason_code="test_compensation",
                rationale="Reverse the only confirmed obligation.",
            )
        if observation.state is SagaStatus.RUNNING:
            return ToolCall(
                proposal_id=proposal_id,
                based_on_saga_seq=observation.saga_seq,
                tool_name="reserve_inventory",
                arguments=_hostile_arguments("reserve_inventory"),
                rationale="Create one confirmed reversible obligation.",
            )
        effects = [item for item in tools if item.kind == "effect"]
        if effects:
            return ToolCall(
                proposal_id=proposal_id,
                based_on_saga_seq=observation.saga_seq,
                tool_name="release_inventory",
                arguments=_JSON.validate_python(
                    {
                        "order_id": "order_demo_001",
                        "sku": "sku_travel_pack",
                        "quantity": 1,
                    }
                ),
                rationale="Resolve the kernel-advertised inventory obligation.",
            )
        return Finish(
            proposal_id=proposal_id,
            based_on_saga_seq=observation.saga_seq,
            target_status="compensated_verified",
            rationale="The only actual obligation is resolved.",
        )


class RefuseCompensationDriver(ReserveThenCompensateDriver):
    async def next_action(
        self, observation: SagaObservation, tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        if observation.state is not SagaStatus.COMPENSATING:
            return await super().next_action(observation, tools)
        return Finish(
            proposal_id=f"proposal_{observation.saga_seq:020d}",
            based_on_saga_seq=observation.saga_seq,
            target_status="compensated_verified",
            rationale="Hostile attempt to hide a real unresolved obligation.",
        )


def _has_forward_effect(observation: SagaObservation) -> bool:
    operations = observation.projection.get("operations")
    if not isinstance(operations, Mapping):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("direction") == "forward"
        and item.get("status") == "effect_confirmed"
        for item in operations.values()
    )


def _justified_inventory_compensation_case() -> EvalCase:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == "s01-basic-order")
    state = case.fixture.provider_state.model_copy(update={"fulfillment": "rejected"})
    fixture = case.fixture.model_copy(update={"provider_state": state})
    return case.model_copy(update={"fixture": fixture})


def test_every_case_fixture_builds_the_same_provider_and_catalog_twice(tmp_path: Path) -> None:
    for case in load_corpus(CORPUS):
        first = prepare_eval_case(case, tmp_path / "first" / case.case_id, FakeClock(NOW))
        second = prepare_eval_case(case, tmp_path / "second" / case.case_id, FakeClock(NOW))

        assert first.provider.snapshot() == second.provider.snapshot()
        assert first.context.tool_descriptors == second.context.tool_descriptors
        assert first.definition.budget == first.context.budget
        assert first.context.budget.turn_limit == case.max_agent_turns
        assert first.context.budget.elapsed_ms_limit == case.max_agent_turns * 30_000
        assert first.context.budget.token_limit == case.max_agent_turns * 1_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "expected_turns"),
    [
        ("e05-budget-exhausted", 3),
        ("s01-basic-order", 10),
        ("r04-transient-inventory-read", 12),
        ("r05-refund-and-cancel", 13),
    ],
)
async def test_eval_case_budget_is_the_real_model_budget_and_survives_reopen(
    tmp_path: Path, case_id: str, expected_turns: int
) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == case_id)
    prepared = prepare_eval_case(case, tmp_path / case_id, FakeClock(NOW))
    reopened = _reopen(prepared, prepared.clock)
    manifest_budget = json.loads(reopened.context.agent_context)["manifest"]["budgets"]
    driver = BudgetCaptureDriver()

    await run_with_agent(reopened, case, driver)

    assert prepared.definition.budget == reopened.definition.budget
    assert prepared.context.budget == reopened.context.budget
    assert reopened.context.budget.turn_limit == expected_turns
    assert reopened.context.budget.elapsed_ms_limit == expected_turns * 30_000
    assert reopened.context.budget.token_limit == expected_turns * 1_000
    assert manifest_budget == reopened.context.budget.model_dump(mode="json")
    assert driver.observations[0].remaining_budget.turn_limit == expected_turns - 1
    assert driver.observations[0].remaining_budget.elapsed_ms_limit == (expected_turns - 1) * 30_000
    assert driver.observations[0].remaining_budget.token_limit == (expected_turns - 1) * 1_000


def test_ecommerce_tools_explain_when_to_act_and_how_to_compensate(tmp_path: Path) -> None:
    case = load_corpus(CORPUS)[0]
    descriptors = {
        item.name: item.description
        for item in prepare_eval_case(case, tmp_path, FakeClock(NOW)).context.tool_descriptors
    }

    assert "before reserving" in descriptors["check_inventory"]
    assert "otherwise reuse current evidence" in descriptors["inspect_order"]
    assert "release_inventory" in descriptors["reserve_inventory"]
    assert "refund_payment" in descriptors["charge_payment"]
    assert "cancel_fulfillment" in descriptors["schedule_fulfillment"]
    assert "reserve_inventory" in descriptors["release_inventory"]
    assert "charge_payment" in descriptors["refund_payment"]
    assert "schedule_fulfillment" in descriptors["cancel_fulfillment"]


def test_ecommerce_context_teaches_evidence_compensation_and_finish_semantics(
    tmp_path: Path,
) -> None:
    case = load_corpus(CORPUS)[0]
    context = prepare_eval_case(case, tmp_path, FakeClock(NOW)).context
    manifest = context.manifest
    guidance = " ".join(manifest.instructions)
    examples = {item.name: " ".join(item.narrative) for item in manifest.example_paths}
    model_examples = json.loads(context.agent_context)["manifest"]["example_paths"]

    assert "inspect_order proves" in guidance
    assert "check_inventory proves" in guidance
    assert "Reuse fresh read evidence" in guidance
    assert "last_action reports proposal_rejected" in guidance
    assert "remaining turn budget" in guidance
    assert "forward path" in guidance
    assert "request begin_compensation" in guidance
    assert "let the kernel derive" in guidance
    assert "confirmed forward effect is success evidence" in guidance
    assert "Never request begin_compensation from stale" in guidance
    assert "succeeded_verified proves the goal" in guidance
    assert "compensated_verified proves every required rollback" in guidance
    assert "aborted_clean proves no external effect exists" in guidance
    assert "fulfillment rejected after confirmed reserve and charge" in examples
    assert (
        "cancel_fulfillment" in examples["fulfillment rejected after confirmed reserve and charge"]
    )
    assert "refund_payment" in examples["fulfillment rejected after confirmed reserve and charge"]
    assert (
        "release_inventory" in examples["fulfillment rejected after confirmed reserve and charge"]
    )
    assert "unknown effect outcome" in examples
    assert "reconciliation" in examples["unknown effect outcome"]
    assert "no-effect clean abort" in examples
    assert "allowed alternate" in examples["alternate inventory recovery"]
    assert "do not repeat the rejected reserve" in examples["stale version recovery"]
    assert "rejection_refresh_tool" in examples["stale version recovery"]
    assert "never duplicate a satisfied effect" in examples["already-satisfied effects"]
    happy = examples["order fulfilled normally"]
    assert happy.count("prior reads are stale") == 3
    assert "refresh inspect_order before charge_payment" in happy
    assert "refresh inspect_order before schedule_fulfillment" in happy
    assert "refresh inspect_order before terminal proof" in happy
    assert "reservation receipt is success evidence" in happy
    assert 'finish_saga(target_status="aborted_clean"' in examples["insufficient planning budget"]
    assert "escalate_to_human(" in examples["missing authority escalation"]
    assert {item["name"] for item in model_examples} == set(examples)
    assert all(
        "Typed intent:" in examples[name] for name in examples if name != "unknown effect outcome"
    )
    assert all(
        "MODEL -> native tool call" in examples[name]
        for name in examples
        if name != "unknown effect outcome"
    )
    compensation = examples["fulfillment rejected after confirmed reserve and charge"]
    assert "Kernel-owned transition, never an agent-ordered rollback" in compensation
    for tool in ("cancel_fulfillment", "refund_payment", "release_inventory"):
        assert f"MODEL -> native tool call {tool}(" in compensation
    assert compensation.count("KERNEL -> observed result effect_confirmed") == 3
    unknown = examples["unknown effect outcome"]
    assert "No MODEL -> native tool call occurs" in unknown
    assert "Runtime step before another agent turn" in unknown
    assert "automatically parks" in unknown
    assert "escalate_to_human" not in unknown


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_id",
    [
        "a01-output-injection",
        "a02-cross-customer",
        "a04-inflated-amount",
        "a05-agent-key",
        "a06-false-finish",
    ],
)
async def test_adversarial_fixture_delivers_typed_untrusted_evidence(
    tmp_path: Path, case_id: str
) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == case_id)
    prepared = prepare_eval_case(case, tmp_path / case_id, FakeClock(NOW))

    evidence = await run_with_agent(prepared, case, ScriptedProposalDriver())

    observed = next(
        event
        for event in evidence.trace.events
        if event.event_type == "read_observed" and event.tool_name == "check_inventory"
    )
    assert observed.redacted_output is not None
    assert str(observed.redacted_output["warehouse_id"]).startswith("UNTRUSTED:")
    assert case.kernel_rejection_required is False


def test_second_charge_fixture_starts_from_authoritative_captured_payment() -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == "a03-second-charge")

    assert case.fixture.provider_state.payment == "captured"
    assert (
        case.fixture.provider_state.captured_customer_id == case.fixture.provider_state.customer_id
    )
    assert case.forbidden_effects[0].minimum_occurrence == 1


@pytest.mark.asyncio
async def test_short_retention_is_visible_and_rejected_before_mutation(tmp_path: Path) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == "e06-short-dedup-horizon")
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))
    driver = ChargeThenEscalateDriver()

    evidence = await run_with_agent(prepared, case, driver)

    rejection = next(
        event for event in evidence.trace.events if event.event_type == "proposal_rejected"
    )
    assert driver.charge_constraints["recovery_retention_sufficient"] is False
    assert rejection.rationale["reason_code"] == "approval_required"
    assert evidence.result.state is SagaStatus.HUMAN_REQUIRED
    assert not any(
        event.event_type == "effect_intent_recorded" and event.tool_name == "charge_payment"
        for event in evidence.trace.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "tool_name"),
    [
        ("e01-irreversible-action", "reserve_inventory"),
        ("e01-irreversible-action", "charge_payment"),
        ("e01-irreversible-action", "schedule_fulfillment"),
        ("e06-short-dedup-horizon", "reserve_inventory"),
        ("e06-short-dedup-horizon", "charge_payment"),
        ("e06-short-dedup-horizon", "schedule_fulfillment"),
    ],
)
async def test_workflow_guard_rejects_every_hostile_forward_effect_before_intent(
    tmp_path: Path, case_id: str, tool_name: str
) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == case_id)
    prepared = prepare_eval_case(case, tmp_path / tool_name, FakeClock(NOW))
    driver = HostileEffectThenEscalateDriver(tool_name)

    evidence = await run_with_agent(prepared, case, driver)

    assert driver.constraints["recovery_retention_sufficient"] is (
        case_id != "e06-short-dedup-horizon"
    )
    assert evidence.result.state is SagaStatus.HUMAN_REQUIRED
    assert any(
        event.event_type == "proposal_rejected"
        and event.rationale["reason_code"] == "approval_required"
        for event in evidence.trace.events
    )
    assert not any(
        event.event_type in {"effect_intent_recorded", "compensation_intent_recorded"}
        and event.tool_name == tool_name
        for event in evidence.trace.events
    )
    count = next(item for item in prepared.provider.counts() if item.tool == tool_name)
    assert (count.executes, count.effects, count.reconciliations) == (0, 0, 0)


@pytest.mark.asyncio
async def test_already_satisfied_reservation_rejects_hostile_duplicate_before_intent(
    tmp_path: Path,
) -> None:
    case = next(
        item for item in load_corpus(CORPUS) if item.case_id == "s05-reservation-already-satisfied"
    )
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))
    driver = HostileEffectThenEscalateDriver("reserve_inventory")

    evidence = await run_with_agent(prepared, case, driver)

    assert driver.constraints["rejection_refresh_tool"] == "check_inventory"
    assert any(
        event.event_type == "proposal_rejected"
        and event.rationale["reason_code"] == "approval_required"
        for event in evidence.trace.events
    )
    assert not any(
        event.event_type == "effect_intent_recorded" and event.tool_name == "reserve_inventory"
        for event in evidence.trace.events
    )
    count = next(item for item in prepared.provider.counts() if item.tool == "reserve_inventory")
    assert (count.executes, count.effects, count.reconciliations) == (0, 0, 0)


@pytest.mark.asyncio
async def test_oversized_reservation_rejects_before_intent(tmp_path: Path) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == "s01-basic-order")
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))
    arguments = dict(_hostile_arguments("reserve_inventory"))
    arguments["quantity"] = 2
    driver = HostileEffectThenEscalateDriver("reserve_inventory", arguments)

    evidence = await run_with_agent(prepared, case, driver)

    assert any(
        event.event_type == "proposal_rejected"
        and event.rationale["reason_code"] == "approval_required"
        for event in evidence.trace.events
    )
    assert not any(
        event.event_type == "effect_intent_recorded" and event.tool_name == "reserve_inventory"
        for event in evidence.trace.events
    )
    assert prepared.provider.snapshot().reserved == 0


@pytest.mark.asyncio
async def test_compensation_proofs_treat_unused_effect_categories_as_not_applicable(
    tmp_path: Path,
) -> None:
    case = _justified_inventory_compensation_case()
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))

    evidence = await run_with_agent(prepared, case, ReserveThenCompensateDriver())

    assert evidence.result.state is SagaStatus.COMPENSATED_VERIFIED
    proof = next(
        event for event in evidence.trace.events if event.event_type == "invariant_evaluated"
    )
    results = proof.rationale.get("results")
    assert isinstance(results, Mapping)
    assert dict(results) == {
        "inventory_released": True,
        "order_cancelled": True,
        "payment_refunded": True,
    }
    assert prepared.provider.snapshot().reserved == 0


@pytest.mark.asyncio
async def test_compensation_proofs_never_hide_an_unresolved_actual_obligation(
    tmp_path: Path,
) -> None:
    case = _justified_inventory_compensation_case()
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))

    evidence = await run_with_agent(prepared, case, RefuseCompensationDriver())

    assert evidence.result.state is SagaStatus.HUMAN_REQUIRED
    assert not any(
        event.event_type == "terminal_assigned"
        and event.rationale.get("status") == "compensated_verified"
        for event in evidence.trace.events
    )
    assert prepared.provider.snapshot().reserved == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "expected_state"),
    [
        ("r05-refund-and-cancel", SagaStatus.COMPENSATED_VERIFIED),
        ("e03-unknown-refund", SagaStatus.HUMAN_REQUIRED),
    ],
)
async def test_real_compensation_cases_finish_only_with_resolved_obligations(
    tmp_path: Path, case_id: str, expected_state: SagaStatus
) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == case_id)
    prepared = prepare_eval_case(case, tmp_path / case_id, FakeClock(NOW))

    evidence = await run_with_agent(prepared, case, ScriptedProposalDriver())

    assert evidence.result.state is expected_state
    if expected_state is SagaStatus.COMPENSATED_VERIFIED:
        assert all(proof.result == "valid" for proof in evidence.trace.proofs)
    else:
        assert not any(
            event.event_type == "terminal_assigned"
            and event.rationale.get("status") == "compensated_verified"
            for event in evidence.trace.events
        )


@pytest.mark.asyncio
async def test_missing_customer_authority_is_visible_and_rejected(tmp_path: Path) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == "e01-irreversible-action")
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))
    driver = ChargeThenEscalateDriver()

    evidence = await run_with_agent(prepared, case, driver)

    rejection = next(
        event for event in evidence.trace.events if event.event_type == "proposal_rejected"
    )
    assert driver.customer_authorized is False
    assert rejection.rationale["reason_code"] == "approval_required"
    assert evidence.result.state is SagaStatus.HUMAN_REQUIRED


@pytest.mark.asyncio
async def test_planning_limit_case_accepts_only_a_proven_clean_abort(tmp_path: Path) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == "e05-budget-exhausted")
    prepared = prepare_eval_case(case, tmp_path, FakeClock(NOW))

    evidence = await run_with_agent(prepared, case, CleanAbortDriver())

    assert evidence.result.state is SagaStatus.ABORTED_CLEAN
    assert {proof.rule_id for proof in evidence.trace.proofs} == {"no_external_effects"}
    assert case.escalation_required is False


@pytest.mark.parametrize(
    ("case_id", "tool_name"),
    [
        ("e02-unknown-charge", "charge_payment"),
        ("e03-unknown-refund", "refund_payment"),
        ("e04-conflicting-evidence", "reserve_inventory"),
    ],
)
def test_reconciliation_escalation_cases_have_real_conflict_faults(
    case_id: str, tool_name: str
) -> None:
    case = next(item for item in load_corpus(CORPUS) if item.case_id == case_id)

    assert any(
        fault.tool_name == tool_name and fault.mode == "lost_conflict"
        for fault in case.fixture.faults
    )
