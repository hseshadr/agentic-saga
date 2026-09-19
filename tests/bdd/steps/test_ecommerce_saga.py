from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from agentic_saga.contracts.common import Direction
from agentic_saga.contracts.outcomes import NoEffectConfirmed
from agentic_saga.contracts.tools import EffectContext
from agentic_saga.temporal.contracts import ActivityIdentity
from examples.ecommerce.demo import EcommerceRun, run_scenario
from examples.ecommerce.domain import ChargePayment, ProviderCount, ProviderState, ScenarioName
from examples.ecommerce.provider import EcommerceProvider

scenarios("../features/ecommerce_saga.feature")


@dataclass
class EcommerceWorld:
    scenario: ScenarioName | None = None
    run: EcommerceRun | None = None


@pytest.fixture
def ecommerce_world() -> EcommerceWorld:
    return EcommerceWorld()


def _run(world: EcommerceWorld) -> EcommerceRun:
    assert world.run is not None
    return world.run


def _count(world: EcommerceWorld, tool: str) -> ProviderCount:
    return _run(world).count(tool)


@given("a healthy checkout provider")
def healthy_provider(ecommerce_world: EcommerceWorld) -> None:
    ecommerce_world.scenario = ScenarioName.HAPPY_PATH


@given("fulfillment rejects the order after accepting it")
def rejected_fulfillment(ecommerce_world: EcommerceWorld) -> None:
    ecommerce_world.scenario = ScenarioName.BUSINESS_FAILURE


@given("payment succeeds but its response is lost")
def lost_payment_response(ecommerce_world: EcommerceWorld) -> None:
    ecommerce_world.scenario = ScenarioName.LOST_RESPONSE


@given("fulfillment rejects and the refund response is uncertain")
def uncertain_refund(ecommerce_world: EcommerceWorld) -> None:
    ecommerce_world.scenario = ScenarioName.COMPENSATION_FAILURE


@when("the agent pursues the checkout goal through a Temporal Saga")
def pursue_checkout(ecommerce_world: EcommerceWorld) -> None:
    assert ecommerce_world.scenario is not None
    ecommerce_world.run = asyncio.run(run_scenario(ecommerce_world.scenario))


@then(parsers.parse('the Temporal Saga finishes as "{status}"'))
def assert_final_status(ecommerce_world: EcommerceWorld, status: str) -> None:
    assert _run(ecommerce_world).state.status.value == status


@then("reservation, payment, and fulfillment each happen once")
def assert_forward_effects_once(ecommerce_world: EcommerceWorld) -> None:
    for tool in ("reserve_inventory", "charge_payment", "schedule_fulfillment"):
        count = _count(ecommerce_world, tool)
        assert (count.executes, count.effects, count.reconciliations) == (1, 1, 0)


@then("the final order is proven by one authoritative read")
def assert_authoritative_proof(ecommerce_world: EcommerceWorld) -> None:
    count = _count(ecommerce_world, "verify_order")
    assert (count.executes, count.effects, count.reconciliations) == (1, 0, 0)


@then("no compensation is needed")
def assert_no_compensation(ecommerce_world: EcommerceWorld) -> None:
    assert _run(ecommerce_world).compensation_order == ()


@then(parsers.parse('compensation happens in the order "{tools}"'))
def assert_compensation_order(ecommerce_world: EcommerceWorld, tools: str) -> None:
    assert _run(ecommerce_world).compensation_order == tuple(tools.split(","))


@then("each completed external change is compensated once")
def assert_compensated_once(ecommerce_world: EcommerceWorld) -> None:
    for tool in ("cancel_fulfillment", "refund_payment", "release_inventory"):
        count = _count(ecommerce_world, tool)
        assert (count.executes, count.effects, count.reconciliations) == (1, 1, 0)


@then(
    parsers.parse(
        'provider "{tool}" reports {executes:d} executions, {effects:d} effects, '
        "and {reconciliations:d} reconciliations"
    )
)
def assert_provider_counts(
    ecommerce_world: EcommerceWorld,
    tool: str,
    executes: int,
    effects: int,
    reconciliations: int,
) -> None:
    count = _count(ecommerce_world, tool)
    assert (count.executes, count.effects, count.reconciliations) == (
        executes,
        effects,
        reconciliations,
    )


@then(
    parsers.parse(
        "payment has {executes:d} execution attempts, {effects:d} effect, "
        "and {reconciliations:d} reconciliation"
    )
)
def assert_payment_counts(
    ecommerce_world: EcommerceWorld,
    executes: int,
    effects: int,
    reconciliations: int,
) -> None:
    count = _count(ecommerce_world, "charge_payment")
    assert (count.executes, count.effects, count.reconciliations) == (
        executes,
        effects,
        reconciliations,
    )


@then("fulfillment continues only after payment is confirmed")
def assert_reconciliation_order(ecommerce_world: EcommerceWorld) -> None:
    events = _run(ecommerce_world).provider_events
    assert events.index("reconcile:charge_payment") < events.index("execute:schedule_fulfillment")


@then(parsers.parse('the Saga pauses in "{status}"'))
def assert_human_pause(ecommerce_world: EcommerceWorld, status: str) -> None:
    pause = _run(ecommerce_world).human_pause_status
    assert pause is not None and pause.value == status


@then("a stale human decision is rejected")
def assert_stale_decision_rejected(ecommerce_world: EcommerceWorld) -> None:
    assert _run(ecommerce_world).stale_update_rejected is True


@then("an unauthorized human decision is rejected")
def assert_unauthorized_decision_rejected(ecommerce_world: EcommerceWorld) -> None:
    assert _run(ecommerce_world).unauthorized_update_rejected is True


@then("a valid human decision resumes compensation")
def assert_human_resume(ecommerce_world: EcommerceWorld) -> None:
    assert any(event.kind == "human_resolved" for event in _run(ecommerce_world).state.events)


def test_provider_rejects_wrong_authoritative_values_before_mutation() -> None:
    provider = EcommerceProvider(ScenarioName.HAPPY_PATH)
    state = ProviderState()
    wrong = ChargePayment(
        order_id=state.order_id,
        customer_id=state.customer_id,
        amount_minor=state.order_amount_minor + 1,
        currency=state.currency,
    )

    outcome = asyncio.run(provider.execute("charge_payment", wrong, _effect_context()))

    assert isinstance(outcome, NoEffectConfirmed)
    assert provider.state == state
    assert provider.counts()[0].effects == 0


def _effect_context() -> EffectContext:
    identity = ActivityIdentity.create(
        "saga_providerrule0001", "step_providerrule0001", Direction.FORWARD, 0
    )
    return EffectContext(
        saga_id=identity.saga_id,
        step_instance_id=identity.step_instance_id,
        operation_id=identity.operation_id,
        fence_token=1,
        delivery_attempt=1,
    )
