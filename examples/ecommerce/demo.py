from __future__ import annotations

import asyncio
import hmac
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter
from temporalio import activity
from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentic_saga.contracts.actions import AgentProposal, Finish, ToolCall
from agentic_saga.contracts.common import JsonObject, thaw_json_object
from agentic_saga.contracts.runtime import (
    ExecutionBudget,
    SagaGoal,
    SagaObservation,
    SagaStatus,
    ToolDescriptor,
)
from agentic_saga.contracts.tools import EffectToolDefinition, ToolRegistry
from agentic_saga.contracts.trace import RunTrace
from agentic_saga.manifest import SagaContext, load_saga_context
from agentic_saga.temporal import project_run_trace
from agentic_saga.temporal.activities import TemporalActivities
from agentic_saga.temporal.contracts import (
    HumanCompensationResolution,
    HumanResolutionVerificationRequest,
    HumanResolutionVerificationResult,
    SagaWorkflowInput,
    WorkflowState,
    WorkflowTool,
)
from agentic_saga.temporal.worker import build_worker
from agentic_saga.temporal.workflow import HUMAN_RESOLUTION_ACTIVITY, AgenticSagaWorkflow
from examples.ecommerce.domain import (
    ChargePayment,
    InspectOrder,
    ProviderCount,
    ProviderState,
    ReserveInventory,
    ScenarioName,
    ScheduleFulfillment,
    StrictModel,
)
from examples.ecommerce.provider import EcommerceProvider, build_registry

_JSON: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_EXPECTED_FORWARD = (
    "reserve_inventory",
    "charge_payment",
    "schedule_fulfillment",
    "verify_order",
)
_PREREQUISITES = {
    "reserve_inventory": (),
    "charge_payment": ("reserve_inventory",),
    "schedule_fulfillment": ("charge_payment",),
    "verify_order": ("schedule_fulfillment",),
}
_DEMO_AUTHORIZATION_SECRET = secrets.token_bytes(32)
_UNAUTHORIZED = "authz_untrusted_actor_0000001"
_DEFINITION_VERSION = "ecommerce-temporal-v1"
_MANIFEST_PATH = Path(__file__).with_name("saga.yaml")
_INVARIANT_CHECKS = ("no_external_effects", "obligations_reversed", "order_verified")


class _CheckoutContext(StrictModel):
    order_id: str
    customer_id: str
    sku: str
    quantity: int
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class EcommerceRun:
    scenario: ScenarioName
    state: WorkflowState
    trace: RunTrace
    counts: tuple[ProviderCount, ...]
    compensation_order: tuple[str, ...]
    provider_events: tuple[str, ...]
    proposals: tuple[str, ...]
    provider_state: ProviderState
    human_pause_status: SagaStatus | None = None
    stale_update_rejected: bool = False
    unauthorized_update_rejected: bool = False

    def count(self, tool: str) -> ProviderCount:
        match = next((item for item in self.counts if item.tool == tool), None)
        return match or ProviderCount(tool, 0, 0, 0)


@dataclass(frozen=True)
class _Completion:
    provider: EcommerceProvider
    agent: CheckoutAgent
    state: WorkflowState
    pause: SagaStatus | None
    stale_rejected: bool
    unauthorized_rejected: bool


@dataclass(frozen=True)
class _Execution:
    client: Client
    workflow_input: SagaWorkflowInput
    registry: ToolRegistry
    agent: CheckoutAgent
    provider: EcommerceProvider
    queue: str


@activity.defn(name=HUMAN_RESOLUTION_ACTIVITY)
async def verify_demo_human_resolution(
    request: HumanResolutionVerificationRequest,
) -> HumanResolutionVerificationResult:
    expected = _demo_authorization(
        request.saga_id,
        request.operation_id,
        request.based_on_event_seq,
    )
    if not hmac.compare_digest(request.authorization_reference, expected):
        return HumanResolutionVerificationResult.rejected("authorization_denied")
    return HumanResolutionVerificationResult.accepted(
        trusted_actor="checkout_operator",
        authorization_id=request.authorization_reference,
    )


class CheckoutAgent:
    """Deterministic driver using the same boundary as a model-backed agent."""

    def __init__(self, plan: Sequence[str]) -> None:
        self._plan = tuple(plan)
        self.proposals: list[str] = []

    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        names = frozenset(item.name for item in available_tools)
        tool = next((name for name in self._plan if name in names), None)
        if tool is None:
            self.proposals.append("finish")
            return _finish(observation)
        self.proposals.append(tool)
        return _tool_call(tool, observation)


async def run_scenario(scenario: ScenarioName | str) -> EcommerceRun:
    selected = ScenarioName(scenario)
    return await _run_test_scenario(selected, _new_saga_id(selected))


async def run_fixture_scenario(scenario: ScenarioName | str) -> EcommerceRun:
    selected = ScenarioName(scenario)
    return await _run_test_scenario(
        selected,
        _fixture_saga_id(selected),
        resolve_human=False,
    )


async def _run_test_scenario(
    selected: ScenarioName,
    saga_id: str,
    *,
    resolve_human: bool = True,
) -> EcommerceRun:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        execution = _execution(environment.client, selected, None, saga_id)
        return _demo_run(selected, await _run_workflow(execution, resolve_human))


async def run_with_client(
    client: Client,
    scenario: ScenarioName | str,
    *,
    task_queue: str | None = None,
) -> EcommerceRun:
    selected = ScenarioName(scenario)
    saga_id = _new_saga_id(selected)
    execution = _execution(client, selected, task_queue, saga_id)
    return _demo_run(selected, await _run_workflow(execution, True))


def _execution(
    client: Client, selected: ScenarioName, task_queue: str | None, saga_id: str
) -> _Execution:
    provider = EcommerceProvider(selected)
    registry = build_registry(provider)
    context = _manifest_context(registry)
    agent = CheckoutAgent(_EXPECTED_FORWARD)
    workflow_input = _workflow_input(registry, context, saga_id)
    queue = task_queue or f"ecommerce-{workflow_input.saga_id}"
    return _Execution(client, workflow_input, registry, agent, provider, queue)


async def _run_workflow(execution: _Execution, resolve_human: bool) -> _Completion:
    worker = _worker(execution.client, execution.queue, execution.registry, execution.agent)
    async with worker:
        handle = await _start(execution.client, execution.workflow_input, execution.queue)
        if execution.provider.scenario is not ScenarioName.COMPENSATION_FAILURE:
            return _completed(execution.provider, execution.agent, await handle.result())
        if not resolve_human:
            return await _capture_human_stop(execution, handle)
        state, pause, stale, unauthorized = await _resolve_human_stop(handle)
        return _Completion(execution.provider, execution.agent, state, pause, stale, unauthorized)


async def _capture_human_stop(
    execution: _Execution,
    handle: WorkflowHandle[AgenticSagaWorkflow, WorkflowState],
) -> _Completion:
    state = await _wait_for_human(handle)
    await handle.terminate(reason="reference fixture captured at the human boundary")
    return _Completion(execution.provider, execution.agent, state, state.status, False, False)


def _worker(client: Client, queue: str, registry: ToolRegistry, agent: CheckoutAgent) -> Worker:
    activities = TemporalActivities(agent, registry, _budget())
    return build_worker(
        client,
        task_queue=queue,
        activities=activities,
        human_resolution_activity=verify_demo_human_resolution,
    )


async def _start(
    client: Client, workflow_input: SagaWorkflowInput, queue: str
) -> WorkflowHandle[AgenticSagaWorkflow, WorkflowState]:
    return await client.start_workflow(
        AgenticSagaWorkflow.run,
        workflow_input,
        id=workflow_input.saga_id,
        task_queue=queue,
    )


async def _resolve_human_stop(
    handle: WorkflowHandle[AgenticSagaWorkflow, WorkflowState],
) -> tuple[WorkflowState, SagaStatus, bool, bool]:
    state = await _wait_for_human(handle)
    unresolved = next(item for item in state.compensations if item.state == "unresolved")
    operation_id = unresolved.compensation_identity.operation_id
    stale, unauthorized = await _rejected_resolutions(handle, state, operation_id)
    await handle.execute_update(
        AgenticSagaWorkflow.resolve_compensation,
        _resolution(state, operation_id, 0),
    )
    return await handle.result(), state.status, stale, unauthorized


async def _rejected_resolutions(
    handle: WorkflowHandle[AgenticSagaWorkflow, WorkflowState],
    state: WorkflowState,
    operation_id: str,
) -> tuple[bool, bool]:
    stale = await _update_is_rejected(handle, _resolution(state, operation_id, -1))
    unauthorized = _resolution(state, operation_id, 0, _UNAUTHORIZED)
    return stale, await _update_is_rejected(handle, unauthorized)


async def _update_is_rejected(
    handle: WorkflowHandle[AgenticSagaWorkflow, WorkflowState],
    resolution: HumanCompensationResolution,
) -> bool:
    try:
        await handle.execute_update(AgenticSagaWorkflow.resolve_compensation, resolution)
    except WorkflowUpdateFailedError:
        return True
    return False


def _resolution(
    state: WorkflowState,
    operation_id: str,
    sequence_delta: int,
    authorization: str | None = None,
) -> HumanCompensationResolution:
    event_seq = len(state.events) + sequence_delta
    return HumanCompensationResolution(
        operation_id=operation_id,
        based_on_event_seq=event_seq,
        authorization_reference=authorization
        or _demo_authorization(state.saga_id, operation_id, event_seq),
        receipt={"operator_confirmation": "refund_verified"},
    )


def _demo_authorization(saga_id: str, operation_id: str, event_seq: int) -> str:
    material = f"{saga_id}\0{operation_id}\0{event_seq}".encode()
    digest = hmac.new(_DEMO_AUTHORIZATION_SECRET, material, sha256).hexdigest()
    return f"authz_{digest}"


async def _wait_for_human(
    handle: WorkflowHandle[AgenticSagaWorkflow, WorkflowState],
) -> WorkflowState:
    for _ in range(100):
        state = await handle.query(AgenticSagaWorkflow.state)
        if state.status is SagaStatus.HUMAN_REQUIRED:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError("checkout Saga did not pause for a human")


def _workflow_input(
    registry: ToolRegistry, context: SagaContext, saga_id: str
) -> SagaWorkflowInput:
    return SagaWorkflowInput(
        saga_id=saga_id,
        goal=_goal(context),
        tools=_workflow_tools(registry, _EXPECTED_FORWARD),
        max_agent_turns=context.budget.turn_limit,
        max_tool_calls=context.budget.tool_call_limit,
    )


def _goal(context: SagaContext) -> SagaGoal:
    state = ProviderState()
    public = _CheckoutContext(
        order_id=state.order_id,
        customer_id=state.customer_id,
        sku=state.sku,
        quantity=state.order_quantity,
        amount_minor=state.order_amount_minor,
        currency=state.currency,
    )
    return SagaGoal(
        goal_id="checkout_order",
        text=context.manifest.objective,
        context=_JSON.validate_python(public.model_dump(mode="json")),
    )


def _manifest_context(registry: ToolRegistry) -> SagaContext:
    context = load_saga_context(
        _MANIFEST_PATH,
        registry=registry,
        policy_checks=(),
        invariant_checks=_INVARIANT_CHECKS,
    )
    if frozenset(context.manifest.tools.allowed) != frozenset(_EXPECTED_FORWARD):
        raise ValueError("manifest must expose exactly the canonical forward tools")
    return context


def _workflow_tools(registry: ToolRegistry, names: Sequence[str]) -> tuple[WorkflowTool, ...]:
    return tuple(_workflow_tool(registry, name) for name in names)


def _workflow_tool(registry: ToolRegistry, name: str) -> WorkflowTool:
    definition = registry.definition(name)
    if isinstance(definition, EffectToolDefinition):
        return WorkflowTool(
            name=name,
            kind="effect",
            compensation_tool=definition.compensate_with,
            prerequisites=_PREREQUISITES[name],
        )
    return WorkflowTool(
        name=name,
        kind="read",
        proof_for_success=name == "verify_order",
        prerequisites=_PREREQUISITES[name],
    )


def _budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=6,
        tool_call_limit=4,
        elapsed_ms_limit=180_000,
        token_limit=6_000,
    )


def _finish(observation: SagaObservation) -> Finish:
    return Finish(
        proposal_id=_proposal_id(observation),
        based_on_saga_seq=observation.saga_seq,
        rationale="Every required checkout change has fresh authoritative proof.",
        target_status="succeeded_verified",
    )


def _tool_call(tool: str, observation: SagaObservation) -> ToolCall:
    return ToolCall(
        proposal_id=_proposal_id(observation),
        tool_name=tool,
        arguments=_arguments(tool, observation.goal),
        based_on_saga_seq=observation.saga_seq,
        rationale="Choose the next eligible checkout capability.",
    )


def _proposal_id(observation: SagaObservation) -> str:
    return f"proposal_{observation.saga_seq:08d}"


def _arguments(tool: str, goal: SagaGoal) -> JsonObject:
    context = _CheckoutContext.model_validate(thaw_json_object(goal.context))
    builders = {
        "reserve_inventory": _reserve_command,
        "charge_payment": _charge_command,
        "schedule_fulfillment": _fulfillment_command,
        "verify_order": _verify_command,
    }
    return _JSON.validate_python(builders[tool](context).model_dump(mode="json"))


def _reserve_command(context: _CheckoutContext) -> ReserveInventory:
    return ReserveInventory(
        order_id=context.order_id,
        sku=context.sku,
        quantity=context.quantity,
        expected_version=1,
    )


def _charge_command(context: _CheckoutContext) -> ChargePayment:
    return ChargePayment(
        order_id=context.order_id,
        customer_id=context.customer_id,
        amount_minor=context.amount_minor,
        currency=context.currency,
    )


def _fulfillment_command(context: _CheckoutContext) -> ScheduleFulfillment:
    return ScheduleFulfillment(order_id=context.order_id)


def _verify_command(context: _CheckoutContext) -> InspectOrder:
    return InspectOrder(order_id=context.order_id)


def _completed(
    provider: EcommerceProvider, agent: CheckoutAgent, state: WorkflowState
) -> _Completion:
    return _Completion(provider, agent, state, None, False, False)


def _demo_run(scenario: ScenarioName, completed: _Completion) -> EcommerceRun:
    provider = completed.provider
    return EcommerceRun(
        scenario=scenario,
        state=completed.state,
        trace=project_run_trace(completed.state, definition_version=_DEFINITION_VERSION),
        counts=provider.counts(),
        compensation_order=provider.compensation_order(),
        provider_events=tuple(provider.events),
        proposals=tuple(completed.agent.proposals),
        provider_state=provider.state,
        human_pause_status=completed.pause,
        stale_update_rejected=completed.stale_rejected,
        unauthorized_update_rejected=completed.unauthorized_rejected,
    )


def _new_saga_id(scenario: ScenarioName) -> str:
    material = f"{scenario.value}:{asyncio.get_running_loop().time()}"
    return f"saga_{sha256(material.encode()).hexdigest()}"


def _fixture_saga_id(scenario: ScenarioName) -> str:
    digest = sha256(f"fixture:{scenario.value}".encode()).hexdigest()
    return f"saga_{digest}"


__all__ = [
    "CheckoutAgent",
    "EcommerceRun",
    "run_fixture_scenario",
    "run_scenario",
    "run_with_client",
]
