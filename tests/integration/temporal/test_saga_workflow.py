from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import count
from typing import cast

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import WorkflowHandle, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentic_saga.contracts.actions import AgentProposal, Finish, ToolCall
from agentic_saga.contracts.common import JsonObject
from agentic_saga.contracts.runtime import SagaGoal, SagaStatus
from agentic_saga.temporal.contracts import (
    AgentDecisionRequest,
    AgentDecisionResult,
    HumanCompensationResolution,
    HumanResolutionVerificationRequest,
    HumanResolutionVerificationResult,
    ReconciliationActivityRequest,
    ReconciliationActivityResult,
    SagaWorkflowInput,
    ToolActivityRequest,
    ToolActivityResult,
    WorkflowState,
    WorkflowTool,
)
from agentic_saga.temporal.worker import saga_workflow_runner
from agentic_saga.temporal.workflow import (
    AGENT_DECISION_ACTIVITY,
    BUSINESS_TOOL_ACTIVITY,
    HUMAN_RESOLUTION_ACTIVITY,
    RECONCILIATION_ACTIVITY,
    AgenticSagaWorkflow,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.temporal]
_IDS = count(1)


@dataclass
class ScenarioActivities:
    decisions: deque[str]
    outcomes: dict[str, ToolActivityResult]
    retry_once: frozenset[str] = frozenset()
    fail_always: frozenset[str] = frozenset()
    reconciliation_fail: bool = False
    stale_decision: bool = False
    calls: list[ToolActivityRequest] = field(default_factory=list)
    decision_requests: list[AgentDecisionRequest] = field(default_factory=list)
    attempts: Counter[str] = field(default_factory=Counter)
    reconciliation_outcomes: dict[str, ReconciliationActivityResult] = field(default_factory=dict)
    reconciliation_calls: list[ReconciliationActivityRequest] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)
    decision_arguments: dict[str, JsonObject] = field(default_factory=dict)
    verification_result: HumanResolutionVerificationResult = field(
        default_factory=lambda: HumanResolutionVerificationResult.accepted(
            trusted_actor="operator_1",
            authorization_id="authz_1234567890abcdef",
        )
    )
    verification_calls: list[HumanResolutionVerificationRequest] = field(default_factory=list)

    @activity.defn(name=AGENT_DECISION_ACTIVITY)
    async def decide(self, request: AgentDecisionRequest) -> AgentDecisionResult:
        self.decision_requests.append(request)
        choice = self.decisions.popleft()
        proposal_id = f"proposal_{request.saga_seq:08d}"
        based_on = request.saga_seq - 1 if self.stale_decision else request.saga_seq
        proposal: AgentProposal
        if choice == "finish":
            proposal = Finish(
                proposal_id=proposal_id,
                based_on_saga_seq=based_on,
                rationale="The requested effects are confirmed.",
                target_status="succeeded_verified",
            )
        else:
            proposal = ToolCall(
                proposal_id=proposal_id,
                tool_name=choice,
                arguments=self.decision_arguments.get(choice, {"order_id": "order_1"}),
                based_on_saga_seq=based_on,
                rationale="Choose one currently eligible effect.",
            )
        return AgentDecisionResult(proposal=proposal)

    @activity.defn(name=BUSINESS_TOOL_ACTIVITY)
    async def execute(self, request: ToolActivityRequest) -> ToolActivityResult:
        name = request.tool_name()
        self.calls.append(request)
        self.timeline.append(f"tool:{name}")
        self.attempts[name] += 1
        if name in self.fail_always:
            raise ApplicationError("exhausted test failure", type="PermanentToolFailure")
        if name in self.retry_once and self.attempts[name] == 1:
            raise ApplicationError("retryable test failure", type="TransientToolFailure")
        return self.outcomes[name]

    @activity.defn(name=RECONCILIATION_ACTIVITY)
    async def reconcile(
        self, request: ReconciliationActivityRequest
    ) -> ReconciliationActivityResult:
        self.reconciliation_calls.append(request)
        self.timeline.append(f"reconcile:{request.tool_name}")
        if self.reconciliation_fail:
            raise ApplicationError("reconciliation failed", type="ReconciliationFailure")
        return self.reconciliation_outcomes[request.tool_name]

    @activity.defn(name=HUMAN_RESOLUTION_ACTIVITY)
    async def verify_human_resolution(
        self, request: HumanResolutionVerificationRequest
    ) -> HumanResolutionVerificationResult:
        self.verification_calls.append(request)
        return self.verification_result


@pytest_asyncio.fixture(scope="module")
async def temporal_environment() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


def _goal() -> SagaGoal:
    return SagaGoal(goal_id="checkout", text="Complete checkout safely.", context={})


def _effect(name: str, compensation: str, *, prerequisites: tuple[str, ...] = ()) -> WorkflowTool:
    return WorkflowTool(
        name=name,
        kind="effect",
        compensation_tool=compensation,
        prerequisites=prerequisites,
    )


def _read(
    name: str,
    *,
    proof: bool = False,
    prerequisites: tuple[str, ...] = (),
    max_calls: int = 1,
) -> WorkflowTool:
    return WorkflowTool(
        name=name,
        kind="read",
        proof_for_success=proof,
        prerequisites=prerequisites,
        max_calls=max_calls,
    )


def _input(*tools: WorkflowTool, max_tool_calls: int = 10) -> SagaWorkflowInput:
    suffix = next(_IDS)
    return SagaWorkflowInput(
        saga_id=f"saga_{suffix:016d}",
        goal=_goal(),
        tools=tools,
        max_agent_turns=10,
        max_tool_calls=max_tool_calls,
    )


@asynccontextmanager
async def _running(
    environment: WorkflowEnvironment,
    activities: ScenarioActivities,
    workflow_input: SagaWorkflowInput,
) -> AsyncIterator[WorkflowHandle[AgenticSagaWorkflow, WorkflowState]]:
    task_queue = f"agentic-saga-{workflow_input.saga_id}"
    worker = Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[AgenticSagaWorkflow],
        activities=[
            activities.decide,
            activities.execute,
            activities.reconcile,
            activities.verify_human_resolution,
        ],
        workflow_runner=saga_workflow_runner(),
    )
    async with worker:
        handle = await environment.client.start_workflow(
            AgenticSagaWorkflow.run,
            workflow_input,
            id=workflow_input.saga_id,
            task_queue=task_queue,
        )
        yield handle


async def test_happy_path_finishes_after_confirmed_effects(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "charge", "finish")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "charge": ToolActivityResult.succeeded({"charge": "c1"}),
        },
    )
    workflow_input = _input(_effect("reserve", "release"), _effect("charge", "refund"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.SUCCEEDED_VERIFIED
    assert result.events[-1].after_status is SagaStatus.SUCCEEDED_VERIFIED
    decisions = [event for event in result.events if event.kind == "agent_decision"]
    assert [event.details["proposal_kind"] for event in decisions] == [
        "tool_call",
        "tool_call",
        "finish",
    ]
    assert all("rationale" not in event.details for event in decisions)
    assert [call.tool_name() for call in activities.calls] == ["reserve", "charge"]


async def test_business_failure_compensates_in_reverse_order(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "charge", "ship")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "charge": ToolActivityResult.succeeded({"charge": "c1"}),
            "ship": ToolActivityResult.failed("carrier_rejected"),
            "refund": ToolActivityResult.succeeded({"refund": "f1"}),
            "release": ToolActivityResult.succeeded({"release": "x1"}),
        },
    )
    workflow_input = _input(
        _effect("reserve", "release"),
        _effect("charge", "refund"),
        _effect("ship", "cancel_ship"),
    )

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.COMPENSATED_VERIFIED
    assert result.events[-1].after_status is SagaStatus.COMPENSATED_VERIFIED
    assert result.events[-2].kind == "compensation_verified"
    assert result.events[-2].details["target_status"] == "compensated_verified"
    assert result.events[-2].details["verified"] is True
    assert [call.tool_name() for call in activities.calls] == [
        "reserve",
        "charge",
        "ship",
        "refund",
        "release",
    ]


async def test_unknown_forward_outcome_stops_before_later_effects(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "charge")),
        outcomes={"reserve": ToolActivityResult.unresolved("provider_outcome_unknown")},
        reconciliation_outcomes={
            "reserve": ReconciliationActivityResult.unresolved("pending", "provider_still_pending")
        },
    )
    workflow_input = _input(_effect("reserve", "release"), _effect("charge", "refund"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.HUMAN_REQUIRED
    assert result.events[-1].after_status is SagaStatus.HUMAN_REQUIRED
    assert result.human_required_reason == "provider_still_pending"
    assert [call.tool_name() for call in activities.calls] == ["reserve"]
    assert len(activities.reconciliation_calls) == 1


async def test_unresolved_compensation_waits_for_validated_update_then_resumes(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "ship")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "ship": ToolActivityResult.failed("carrier_rejected"),
            "release": ToolActivityResult.unresolved("release_outcome_unknown"),
        },
        reconciliation_outcomes={
            "release": ReconciliationActivityResult.unresolved("pending", "release_outcome_unknown")
        },
    )
    workflow_input = _input(_effect("reserve", "release"), _effect("ship", "cancel_ship"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        state = await _wait_for_human(handle)
        unresolved = next(item for item in state.compensations if item.state == "unresolved")
        invalid = HumanCompensationResolution(
            operation_id=unresolved.compensation_identity.operation_id,
            based_on_event_seq=len(state.events) - 1,
            authorization_reference="authz_1234567890abcdef",
            receipt={"operator_confirmation": "released"},
        )
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(AgenticSagaWorkflow.resolve_compensation, invalid)
        resolution = HumanCompensationResolution(
            operation_id=unresolved.compensation_identity.operation_id,
            based_on_event_seq=len(state.events),
            authorization_reference="authz_1234567890abcdef",
            receipt={"operator_confirmation": "released"},
        )
        await handle.execute_update(AgenticSagaWorkflow.resolve_compensation, resolution)
        result = await handle.result()

    assert result.status is SagaStatus.COMPENSATED_VERIFIED
    human_event = next(event for event in result.events if event.kind == "human_resolved")
    assert human_event.details["actor"] == "operator_1"
    assert human_event.details["authorization_id"] == "authz_1234567890abcdef"
    assert len(activities.verification_calls) == 1


async def test_stale_agent_proposal_is_rejected_before_dispatch(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve",)),
        outcomes={},
        stale_decision=True,
    )
    workflow_input = _input(_effect("reserve", "release"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.HUMAN_REQUIRED
    assert result.human_required_reason == "stale_agent_proposal"
    assert activities.calls == []


async def test_unadvertised_agent_tool_is_rejected_before_dispatch(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(decisions=deque(("rogue",)), outcomes={})
    workflow_input = _input(_effect("reserve", "release"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.HUMAN_REQUIRED
    assert result.human_required_reason == "ineligible_agent_tool"
    assert activities.calls == []


async def test_premature_finish_is_rejected_without_terminal_proof(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(decisions=deque(("finish",)), outcomes={})
    workflow_input = _input(_effect("reserve", "release"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.HUMAN_REQUIRED
    assert result.human_required_reason == "terminal_proof_missing"


async def test_duplicate_tool_call_unwinds_confirmed_effect(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "reserve")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "release": ToolActivityResult.succeeded({"release": "x1"}),
        },
    )
    workflow_input = _input(_effect("reserve", "release"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.COMPENSATED_VERIFIED
    assert [call.tool_name() for call in activities.calls] == ["reserve", "release"]


async def test_agent_failure_after_effect_unwinds_before_returning(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve",)),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "release": ToolActivityResult.succeeded({"release": "x1"}),
        },
    )
    workflow_input = _input(_effect("reserve", "release"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.COMPENSATED_VERIFIED
    assert [call.tool_name() for call in activities.calls] == ["reserve", "release"]


async def test_agent_turn_exhaustion_unwinds_confirmed_effect(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve",)),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "release": ToolActivityResult.succeeded({"release": "x1"}),
        },
    )
    workflow_input = _input(_effect("reserve", "release")).model_copy(update={"max_agent_turns": 1})

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.COMPENSATED_VERIFIED
    assert [call.tool_name() for call in activities.calls] == ["reserve", "release"]


async def test_read_or_proof_tool_creates_no_compensation_obligation(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("verify", "finish")),
        outcomes={"verify": ToolActivityResult.succeeded({"verified": True})},
    )
    workflow_input = _input(_read("verify"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.SUCCEEDED_VERIFIED
    assert result.compensations == ()


async def test_next_agent_turn_receives_prior_public_result_evidence(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("verify", "finish")),
        outcomes={"verify": ToolActivityResult.succeeded({"verified": True})},
    )
    workflow_input = _input(_read("verify"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        await handle.result()

    observation = activities.decision_requests[1]
    evidence = observation.state.events[-1].details
    receipt = cast(JsonObject, evidence["public_receipt"])
    assert receipt["verified"] is True
    assert observation.remaining_turns == workflow_input.max_agent_turns - 1


async def _wait_for_human(
    handle: WorkflowHandle[AgenticSagaWorkflow, WorkflowState],
) -> WorkflowState:
    for _ in range(100):
        state = await handle.query(AgenticSagaWorkflow.state)
        if state.status is SagaStatus.HUMAN_REQUIRED:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError("workflow did not enter human-required state")


async def test_activity_retry_reuses_the_same_idempotency_identity(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "finish")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "release": ToolActivityResult.succeeded({"released": True}),
        },
        retry_once=frozenset({"reserve"}),
    )
    workflow_input = _input(_effect("reserve", "release"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    identities = [call.identity().idempotency_key for call in activities.calls]
    assert result.status is SagaStatus.SUCCEEDED_VERIFIED
    assert len(identities) == 2
    assert len(set(identities)) == 1


async def test_lost_response_reconciles_once_without_duplicate_effect(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("charge", "finish")),
        outcomes={"charge": ToolActivityResult.unresolved("provider_outcome_unknown")},
        reconciliation_outcomes={
            "charge": ReconciliationActivityResult.confirmed_effect({"charge": "c1"})
        },
    )
    workflow_input = _input(_effect("charge", "refund"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.SUCCEEDED_VERIFIED
    assert [call.tool_name() for call in activities.calls] == ["charge"]
    assert len(activities.reconciliation_calls) == 1
    assert result.compensations[0].forward_receipt["charge"] == "c1"


async def test_unknown_forward_never_compensates_before_reconciliation(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "charge")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "charge": ToolActivityResult.unresolved("provider_outcome_unknown"),
            "release": ToolActivityResult.succeeded({"release": "r1"}),
        },
        reconciliation_outcomes={
            "charge": ReconciliationActivityResult.unresolved(
                "conflict", "provider_evidence_conflicts"
            )
        },
    )
    workflow_input = _input(_effect("reserve", "release"), _effect("charge", "refund"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.HUMAN_REQUIRED
    assert activities.timeline == [
        "tool:reserve",
        "tool:charge",
        "reconcile:charge",
        "tool:release",
    ]
    assert len(activities.reconciliation_calls) == 1
    assert result.compensations[0].state == "succeeded"


async def test_confirmed_no_effect_compensates_prior_proven_work(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "charge")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "charge": ToolActivityResult.unresolved("provider_outcome_unknown"),
            "release": ToolActivityResult.succeeded({"release": "r1"}),
        },
        reconciliation_outcomes={
            "charge": ReconciliationActivityResult.confirmed_no_effect("declined")
        },
    )
    workflow_input = _input(_effect("reserve", "release"), _effect("charge", "refund"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.COMPENSATED_VERIFIED
    assert activities.timeline == [
        "tool:reserve",
        "tool:charge",
        "reconcile:charge",
        "tool:release",
    ]


async def test_confirmed_no_effect_clean_aborts_without_prior_work(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("charge",)),
        outcomes={"charge": ToolActivityResult.unresolved("provider_outcome_unknown")},
        reconciliation_outcomes={
            "charge": ReconciliationActivityResult.confirmed_no_effect("declined")
        },
    )
    workflow_input = _input(_effect("charge", "refund"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.ABORTED_CLEAN
    assert activities.timeline == ["tool:charge", "reconcile:charge"]


async def test_exhausted_effect_activity_reconciles_same_identity(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("charge", "finish")),
        outcomes={},
        fail_always=frozenset({"charge"}),
        reconciliation_outcomes={
            "charge": ReconciliationActivityResult.confirmed_effect({"charge": "c1"})
        },
    )
    workflow_input = _input(_effect("charge", "refund"))

    async with _running(temporal_environment, activities, workflow_input) as handle:
        result = await handle.result()

    identities = [call.identity().operation_id for call in activities.calls]
    assert result.status is SagaStatus.SUCCEEDED_VERIFIED
    assert len(identities) == 2 and len(set(identities)) == 1
    assert activities.reconciliation_calls[0].correlation_id == identities[0]


async def test_exhausted_read_activity_is_a_definite_failure(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("lookup",)),
        outcomes={},
        fail_always=frozenset({"lookup"}),
    )

    async with _running(temporal_environment, activities, _input(_read("lookup"))) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.ABORTED_CLEAN
    assert activities.reconciliation_calls == []


async def test_reconciliation_activity_failure_requires_human(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("charge",)),
        outcomes={"charge": ToolActivityResult.unresolved("provider_outcome_unknown")},
        reconciliation_fail=True,
    )

    async with _running(
        temporal_environment, activities, _input(_effect("charge", "refund"))
    ) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.HUMAN_REQUIRED
    assert result.human_required_reason == "reconciliation_activity_failed"


async def test_reconciliation_failure_unwinds_prior_proven_work_before_human(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "charge")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "charge": ToolActivityResult.unresolved("provider_outcome_unknown"),
            "release": ToolActivityResult.succeeded({"release": "r1"}),
        },
        reconciliation_fail=True,
    )
    tools = (_effect("reserve", "release"), _effect("charge", "refund"))

    async with _running(temporal_environment, activities, _input(*tools)) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.HUMAN_REQUIRED
    assert result.human_required_reason == "reconciliation_activity_failed"
    assert activities.timeline == [
        "tool:reserve",
        "tool:charge",
        "reconcile:charge",
        "tool:release",
    ]
    assert result.compensations[0].state == "succeeded"


async def test_prerequisites_and_global_budget_are_enforced_before_dispatch(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "charge")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "release": ToolActivityResult.succeeded({"released": True}),
        },
    )
    tools = (
        _effect("reserve", "release"),
        _effect("charge", "refund", prerequisites=("reserve",)),
    )

    async with _running(
        temporal_environment, activities, _input(*tools, max_tool_calls=1)
    ) as handle:
        result = await handle.result()

    assert activities.decision_requests[0].available_tools == ("reserve",)
    assert activities.decision_requests[1].remaining_tool_calls == 0
    assert result.status is SagaStatus.COMPENSATED_VERIFIED


async def test_false_proof_cannot_finish(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("verify", "finish")),
        outcomes={"verify": ToolActivityResult.succeeded({"verified": False})},
    )

    async with _running(
        temporal_environment, activities, _input(_read("verify", proof=True))
    ) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.ABORTED_CLEAN


async def test_effect_invalidates_prior_proof_and_finish_control(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("verify", "charge", "finish")),
        outcomes={
            "verify": ToolActivityResult.succeeded({"verified": True}),
            "charge": ToolActivityResult.succeeded({"charge": "c1"}),
            "refund": ToolActivityResult.succeeded({"refunded": True}),
        },
    )
    tools = (_read("verify", proof=True), _effect("charge", "refund"))

    async with _running(temporal_environment, activities, _input(*tools)) as handle:
        result = await handle.result()

    assert activities.decision_requests[1].finish_allowed is False
    assert activities.decision_requests[2].finish_allowed is False
    assert result.status is SagaStatus.COMPENSATED_VERIFIED


async def test_failed_proof_compensates_prior_effect(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("charge", "verify")),
        outcomes={
            "charge": ToolActivityResult.succeeded({"charge": "c1"}),
            "verify": ToolActivityResult.succeeded({"verified": False}),
            "refund": ToolActivityResult.succeeded({"refund": "r1"}),
        },
    )
    tools = (
        _effect("charge", "refund"),
        _read("verify", proof=True, prerequisites=("charge",)),
    )

    async with _running(temporal_environment, activities, _input(*tools)) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.COMPENSATED_VERIFIED
    assert activities.timeline == ["tool:charge", "tool:verify", "tool:refund"]


async def test_fresh_proof_after_effect_allows_finish(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("charge", "verify", "finish")),
        outcomes={
            "charge": ToolActivityResult.succeeded({"charge": "c1"}),
            "verify": ToolActivityResult.succeeded({"verified": True}),
        },
    )
    tools = (
        _effect("charge", "refund"),
        _read("verify", proof=True, prerequisites=("charge",)),
    )

    async with _running(temporal_environment, activities, _input(*tools)) as handle:
        result = await handle.result()

    assert activities.decision_requests[-1].finish_allowed is True
    assert result.status is SagaStatus.SUCCEEDED_VERIFIED
    proof_event = next(
        event
        for event in result.events
        if event.kind == "forward_result" and event.details["tool_name"] == "verify"
    )
    assert proof_event.details["proof_for_success"] is True
    assert proof_event.details["verified"] is True
    assert result.events[-1].after_status is SagaStatus.SUCCEEDED_VERIFIED
    assert result.events[-1].recorded_at.utcoffset() is not None


async def test_exhausted_compensation_activity_waits_for_verified_human(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "ship")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "ship": ToolActivityResult.failed("carrier_rejected"),
        },
        fail_always=frozenset({"release"}),
        reconciliation_outcomes={
            "release": ReconciliationActivityResult.unresolved(
                "pending", "compensation_not_yet_proven"
            )
        },
    )
    tools = (_effect("reserve", "release"), _effect("ship", "cancel_ship"))

    async with _running(temporal_environment, activities, _input(*tools)) as handle:
        state = await _wait_for_human(handle)
        unresolved = next(item for item in state.compensations if item.state == "unresolved")
        resolution = HumanCompensationResolution(
            operation_id=unresolved.compensation_identity.operation_id,
            based_on_event_seq=len(state.events),
            authorization_reference="authz_1234567890abcdef",
            receipt={"operator_confirmation": "released"},
        )
        await handle.execute_update(AgenticSagaWorkflow.resolve_compensation, resolution)
        result = await handle.result()

    assert state.human_required_reason == "compensation_not_yet_proven"
    assert activities.attempts["release"] == 2
    assert result.status is SagaStatus.COMPENSATED_VERIFIED


async def test_lost_compensation_response_reconciles_and_continues_unwind(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "charge", "ship")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "charge": ToolActivityResult.succeeded({"payment": "p1"}),
            "ship": ToolActivityResult.failed("carrier_rejected"),
            "release": ToolActivityResult.succeeded({"released": True}),
        },
        fail_always=frozenset({"refund"}),
        reconciliation_outcomes={
            "refund": ReconciliationActivityResult.confirmed_effect({"refunded": True})
        },
    )
    tools = (
        _effect("reserve", "release"),
        _effect("charge", "refund", prerequisites=("reserve",)),
        _effect("ship", "cancel_ship", prerequisites=("charge",)),
    )

    async with _running(temporal_environment, activities, _input(*tools)) as handle:
        result = await handle.result()

    assert result.status is SagaStatus.COMPENSATED_VERIFIED
    assert activities.attempts["refund"] == 2
    assert activities.attempts["release"] == 1
    assert [call.tool_name for call in activities.reconciliation_calls] == ["refund"]
    event = next(item for item in result.events if item.kind == "reconciliation_result")
    assert event.details["direction"] == "compensation"
    assert event.details["compensates_operation_id"] is not None


async def test_returned_unknown_compensation_reconciles_before_human(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "ship")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "ship": ToolActivityResult.failed("carrier_rejected"),
            "release": ToolActivityResult.unresolved("provider_outcome_unknown"),
        },
        reconciliation_outcomes={
            "release": ReconciliationActivityResult.confirmed_effect({"released": True})
        },
    )
    tools = (_effect("reserve", "release"), _effect("ship", "cancel_ship"))

    async with _running(temporal_environment, activities, _input(*tools)) as handle:
        result = await asyncio.wait_for(handle.result(), timeout=1)

    assert result.status is SagaStatus.COMPENSATED_VERIFIED
    assert activities.timeline == [
        "tool:reserve",
        "tool:ship",
        "tool:release",
        "reconcile:release",
    ]
    assert len(activities.reconciliation_calls) == 1
    reconciliation = activities.reconciliation_calls[0]
    assert reconciliation.compensation is not None
    identity = result.compensations[0].compensation_identity
    assert reconciliation.correlation_id == identity.operation_id


async def test_unverified_human_resolution_is_rejected_and_remains_waiting(
    temporal_environment: WorkflowEnvironment,
) -> None:
    activities = ScenarioActivities(
        decisions=deque(("reserve", "ship")),
        outcomes={
            "reserve": ToolActivityResult.succeeded({"reservation": "r1"}),
            "ship": ToolActivityResult.failed("carrier_rejected"),
            "release": ToolActivityResult.unresolved("release_outcome_unknown"),
        },
        reconciliation_outcomes={
            "release": ReconciliationActivityResult.unresolved("pending", "release_outcome_unknown")
        },
        verification_result=HumanResolutionVerificationResult.rejected("authorization_rejected"),
    )
    tools = (_effect("reserve", "release"), _effect("ship", "cancel_ship"))

    async with _running(temporal_environment, activities, _input(*tools)) as handle:
        state = await _wait_for_human(handle)
        unresolved = next(item for item in state.compensations if item.state == "unresolved")
        resolution = HumanCompensationResolution(
            operation_id=unresolved.compensation_identity.operation_id,
            based_on_event_seq=len(state.events),
            authorization_reference="authz_1234567890abcdef",
            receipt={"operator_confirmation": "released"},
        )
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(AgenticSagaWorkflow.resolve_compensation, resolution)
        waiting = await handle.query(AgenticSagaWorkflow.state)
        await handle.cancel()

    assert waiting.status is SagaStatus.HUMAN_REQUIRED
    assert waiting.events[-1].after_status is SagaStatus.HUMAN_REQUIRED
