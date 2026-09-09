from __future__ import annotations

import asyncio

from pytest_bdd import given, parsers, scenarios, then, when

from examples.ecommerce.demo import run_scenario
from examples.ecommerce.domain import DemoRun
from tests.bdd.steps.conftest import BddWorld

scenarios("../features/ecommerce_saga.feature")


def _run(world: BddWorld) -> DemoRun:
    assert world.run is not None
    return world.run


@given(parsers.parse('the "{name}" ecommerce scenario'))
def given_scenario(world: BddWorld, name: str) -> None:
    world.scenario = name


@when("the offline agent pursues the order goal")
def pursue_goal(world: BddWorld) -> None:
    assert world.scenario is not None
    world.run = asyncio.run(run_scenario(world.scenario, world.workdir))


@then(parsers.parse('the saga state is "{state}"'))
def assert_state(world: BddWorld, state: str) -> None:
    assert _run(world).result.state.value == state


@then(parsers.parse('the agent proposal sequence is "{expected}"'))
def assert_proposals(world: BddWorld, expected: str) -> None:
    assert _run(world).proposals == tuple(expected.split(","))


@then(parsers.parse('the compensation sequence is "{expected}"'))
def assert_compensations(world: BddWorld, expected: str) -> None:
    assert _run(world).compensation_tools == tuple(expected.split(","))


@then(
    parsers.parse(
        'provider "{tool}" has {executes:d} execute, {effects:d} effect, '
        "and {reconciliations:d} reconciliation calls"
    )
)
def assert_counts(
    world: BddWorld,
    tool: str,
    executes: int,
    effects: int,
    reconciliations: int,
) -> None:
    count = _run(world).count(tool)
    assert (count.executes, count.effects, count.reconciliations) == (
        executes,
        effects,
        reconciliations,
    )


@then("every forward and compensation provider has 1 execute, 1 effect, and 0 reconciliations")
def assert_one_call_and_effect_each(world: BddWorld) -> None:
    assert all(
        (item.executes, item.effects, item.reconciliations) == (1, 1, 0)
        for item in _run(world).counts
    )


@then("the Saga runtime restarted from durable state")
def assert_restarted(world: BddWorld) -> None:
    assert _run(world).restarted is True


@then("the timeline records intent before effect and fresh proof before terminal state")
def assert_happy_evidence_order(world: BddWorld) -> None:
    assert _run(world).evidence_order_is_valid()


@then("the timeline records the agent compensation request and verified completion")
def assert_compensation_evidence(world: BddWorld) -> None:
    run = _run(world)
    assert "compensation_started" in run.timeline_kinds
    assert run.timeline_kinds[-2:] == ("invariant_evaluated", "terminal_assigned")


@then("the timeline records reconciliation before later forward effects")
def assert_reconciliation_order(world: BddWorld) -> None:
    run = _run(world)
    reconciled = next(
        item.saga_seq for item in run.trace.events if item.event_type == "reconciliation_recorded"
    )
    assert any(
        item.event_type == "effect_intent_recorded" and item.saga_seq > reconciled
        for item in run.trace.events
    )


@then(parsers.parse('the escalation packet identifies "{reason}"'))
def assert_escalation(world: BddWorld, reason: str) -> None:
    packet = _run(world).escalation
    assert packet is not None
    assert packet.reason_code == reason
    assert packet.unresolved_operation_ids


@then("no effect occurs after human escalation")
def assert_quiescent_after_escalation(world: BddWorld) -> None:
    run = _run(world)
    human_index = run.timeline_kinds.index("human_required")
    later = run.trace.events[human_index + 1 :]
    assert not any(
        item.event_type
        in {
            "effect_intent_recorded",
            "compensation_intent_recorded",
            "dispatch_started",
            "effect_outcome_recorded",
        }
        for item in later
    )
