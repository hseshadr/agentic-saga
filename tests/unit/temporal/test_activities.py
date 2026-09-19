from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field
from temporalio.api.failure.v1 import Failure
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError

from agentic_saga.contracts.actions import AgentProposal, ToolCall
from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    Reversibility,
    thaw_json_object,
)
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    NoEffectConfirmed,
    OutcomeUnknown,
    PartialEffectConfirmed,
    ReconcileConflict,
    ReconcileEffectConfirmed,
    ReconcileNoEffectConfirmed,
    ReconcilePending,
    ReconcileUnsupported,
    ReconciliationOutcome,
)
from agentic_saga.contracts.runtime import (
    AgentDriver,
    ExecutionBudget,
    SagaGoal,
    SagaObservation,
    SagaStatus,
    ToolDescriptor,
)
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ReadToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.temporal.activities import TemporalActivities
from agentic_saga.temporal.contracts import (
    ActivityIdentity,
    AgentDecisionObservation,
    AgentDecisionRequest,
    CompensationActivityRequest,
    ForwardActivityRequest,
    ReconciliationActivityRequest,
    ToolActivityRequest,
    ToolCallCount,
    WorkflowEvent,
)

pytestmark = pytest.mark.asyncio
SAGA_ID = "saga_0123456789abcdef"
_SYNTHETIC_PRIVATE_VALUE = "Bearer synthetic-provider-secret"


class Command(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    amount: int = Field(strict=True, gt=0)


class ReadResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    available: int = Field(strict=True, ge=0)


class CompensationCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    charge_id: str


class RecordingAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[SagaObservation, tuple[ToolDescriptor, ...]]] = []

    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        tools = tuple(available_tools)
        self.calls.append((observation, tools))
        return ToolCall(
            proposal_id="proposal_12345678",
            tool_name=tools[0].name,
            arguments={"amount": 1},
            based_on_saga_seq=observation.saga_seq,
            rationale="Use the eligible capability.",
        )


class ExplodingAgent:
    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        del observation, available_tools
        raise RuntimeError(_SYNTHETIC_PRIVATE_VALUE)


class ReadAdapter:
    async def read(self, command: Command) -> ReadResult:
        return ReadResult(available=command.amount)


class RecordingEffectAdapter:
    def __init__(self, outcome: EffectOutcome | ReconciliationOutcome | None) -> None:
        self.outcome = outcome
        self.calls: list[tuple[Command, EffectContext]] = []
        self.reconciliations: list[tuple[Command, ReconcileContext]] = []

    async def execute(self, command: Command, context: EffectContext) -> EffectOutcome:
        self.calls.append((command, context))
        return cast(EffectOutcome, self.outcome)

    async def reconcile(self, command: Command, context: ReconcileContext) -> ReconciliationOutcome:
        self.reconciliations.append((command, context))
        return cast(ReconciliationOutcome, self.outcome)


class ExplodingEffectAdapter(RecordingEffectAdapter):
    def __init__(self) -> None:
        super().__init__(None)

    async def execute(self, command: Command, context: EffectContext) -> EffectOutcome:
        del command, context
        raise RuntimeError(_SYNTHETIC_PRIVATE_VALUE)

    async def reconcile(self, command: Command, context: ReconcileContext) -> ReconciliationOutcome:
        del command, context
        raise RuntimeError(_SYNTHETIC_PRIVATE_VALUE)


class CompensationAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[CompensationCommand, EffectContext]] = []

    async def execute(self, command: CompensationCommand, context: EffectContext) -> EffectOutcome:
        self.calls.append((command, context))
        return EffectConfirmed(receipt={"refunded": True})

    async def reconcile(
        self, command: CompensationCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileUnsupported(reason="unsupported")


def _capabilities() -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.EXACT,
        partial_effects_possible=True,
    )


def _effect(
    name: str,
    adapter: RecordingEffectAdapter,
    compensation: str | None = None,
) -> EffectToolDefinition[Command]:
    return EffectToolDefinition(
        name=name,
        definition_version="1",
        command_schema_version="1",
        input_model=Command,
        adapter=adapter,
        capabilities=_capabilities(),
        compensate_with=compensation,
    )


def _registry(adapter: RecordingEffectAdapter) -> ToolRegistry:
    read = ReadToolDefinition("lookup", Command, ReadResult, ReadAdapter())
    return ToolRegistry((read, _effect("charge", adapter, "refund"), _effect("refund", adapter)))


def _compensation_registry(adapter: CompensationAdapter) -> ToolRegistry:
    refund = EffectToolDefinition(
        name="refund",
        definition_version="1",
        command_schema_version="1",
        input_model=CompensationCommand,
        adapter=adapter,
        capabilities=_capabilities(),
        compensate_with=None,
    )
    return ToolRegistry((refund,))


def _budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=10,
        tool_call_limit=10,
        elapsed_ms_limit=30_000,
        token_limit=5_000,
    )


def _decision_request() -> AgentDecisionRequest:
    event = WorkflowEvent(
        seq=1,
        kind="started",
        details={"public": "evidence"},
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        before_status=SagaStatus.RUNNING,
        after_status=SagaStatus.RUNNING,
    )
    state = AgentDecisionObservation(
        recent_events=(event,),
        completed_tools=(),
        tool_call_counts=(ToolCallCount(tool_name="lookup", count=1),),
        compensation_count=0,
        total_event_count=1,
        remaining_turns=4,
        remaining_tool_calls=3,
    )
    return AgentDecisionRequest(
        saga_id=SAGA_ID,
        saga_seq=1,
        status=SagaStatus.RUNNING,
        goal=SagaGoal(goal_id="checkout", text="Complete checkout.", context={}),
        available_tools=("lookup", "charge"),
        state=state,
        finish_allowed=False,
    )


def _identity(direction: Direction) -> ActivityIdentity:
    return ActivityIdentity.create(SAGA_ID, "step_00000001", direction, 0)


def _forward(name: str = "charge") -> ToolActivityRequest:
    kind: Literal["read", "effect"] = "read" if name == "lookup" else "effect"
    request = ForwardActivityRequest(
        identity=_identity(Direction.FORWARD),
        tool_name=name,
        arguments={"amount": 7},
        declared_kind=kind,
        declared_compensation_tool=None if kind == "read" else "refund",
    )
    return ToolActivityRequest(forward=request)


def _serialized_failure(error: BaseException) -> Failure:
    failure = Failure()
    pydantic_data_converter.failure_converter.to_failure(
        error,
        pydantic_data_converter.payload_converter,
        failure,
    )
    return failure


def _assert_sanitized_failure(
    error: ApplicationError, *, expected_message: str, expected_type: str
) -> None:
    failure = _serialized_failure(error)
    assert failure.message == expected_message
    assert failure.application_failure_info.type == expected_type
    assert not failure.HasField("cause")
    assert _SYNTHETIC_PRIVATE_VALUE.encode() not in failure.SerializeToString()


async def test_decision_receives_bounded_public_evidence_and_exact_descriptors() -> None:
    driver = RecordingAgent()
    activities = TemporalActivities(driver, _registry(RecordingEffectAdapter(None)), _budget())

    result = await activities.decide(_decision_request())

    observation, tools = driver.calls[0]
    assert isinstance(result.proposal, ToolCall)
    assert result.proposal.tool_name == "lookup"
    assert observation.last_action == {"public": "evidence"}
    assert observation.remaining_budget.turn_limit == 4
    assert observation.remaining_budget.tool_call_limit == 3
    assert observation.finish_allowed is False
    assert [tool.name for tool in tools] == ["lookup", "charge"]
    assert [tool.kind for tool in tools] == ["read", "effect"]


async def test_driver_exception_is_sanitized_before_temporal_serialization() -> None:
    activities = TemporalActivities(
        ExplodingAgent(), _registry(RecordingEffectAdapter(None)), _budget()
    )

    with pytest.raises(ApplicationError) as raised:
        await activities.decide(_decision_request())

    _assert_sanitized_failure(
        raised.value,
        expected_message="agent driver failed",
        expected_type="AgentDriverFailure",
    )


async def test_decision_preserves_recent_public_event_evidence() -> None:
    driver = RecordingAgent()
    activities = TemporalActivities(driver, _registry(RecordingEffectAdapter(None)), _budget())
    request = _decision_request()
    read = WorkflowEvent(
        seq=2,
        kind="forward_result",
        details=cast(
            JsonObject,
            {
                "outcome": "succeeded",
                "public_receipt": {"available": 3},
                "tool_name": "lookup",
            },
        ),
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        before_status=SagaStatus.RUNNING,
        after_status=SagaStatus.RUNNING,
    )
    state = request.state.model_copy(
        update={"recent_events": (*request.state.recent_events, read), "total_event_count": 2}
    )

    await activities.decide(request.model_copy(update={"saga_seq": 2, "state": state}))

    projection = thaw_json_object(driver.calls[0][0].projection)
    recent = cast(list[dict[str, object]], projection["recent_events"])
    details = cast(dict[str, object], recent[-1]["details"])
    assert details["public_receipt"] == {"available": 3}


async def test_finish_is_advertised_only_when_workflow_allows_it() -> None:
    driver = RecordingAgent()
    activities = TemporalActivities(driver, _registry(RecordingEffectAdapter(None)), _budget())

    await activities.decide(_decision_request().model_copy(update={"finish_allowed": True}))

    assert driver.calls[0][0].finish_allowed is True


async def test_read_command_is_strictly_validated_and_public() -> None:
    activities = TemporalActivities(
        RecordingAgent(), _registry(RecordingEffectAdapter(None)), _budget()
    )

    result = await activities.execute_tool(_forward("lookup"))

    assert result.outcome == "succeeded"
    assert result.receipt == {"available": 7}


async def test_effect_uses_stable_identity_and_returns_public_receipt() -> None:
    adapter = RecordingEffectAdapter(
        EffectConfirmed(receipt={"provider_reference": "safe", "api_key": "hidden"})
    )
    activities = TemporalActivities(RecordingAgent(), _registry(adapter), _budget())

    result = await activities.execute_tool(_forward())

    context = adapter.calls[0][1]
    assert context.operation_id == _identity(Direction.FORWARD).operation_id
    assert context.step_instance_id == "step_00000001"
    assert result.outcome == "succeeded"
    assert result.receipt == {"provider_reference": "safe"}


async def test_effect_adapter_exception_is_sanitized_before_temporal_serialization() -> None:
    activities = TemporalActivities(
        ExplodingAgent(), _registry(ExplodingEffectAdapter()), _budget()
    )

    with pytest.raises(ApplicationError) as raised:
        await activities.execute_tool(_forward())

    _assert_sanitized_failure(
        raised.value,
        expected_message="business tool adapter failed",
        expected_type="BusinessToolAdapterFailure",
    )


@pytest.mark.parametrize(
    ("outcome", "expected", "reason"),
    [
        (NoEffectConfirmed(reason="declined"), "failed", "provider_confirmed_no_effect"),
        (
            PartialEffectConfirmed(receipts=({"provider_reference": "one"},)),
            "unresolved",
            "partial_effect_confirmed",
        ),
        (
            OutcomeUnknown(correlation="opaque://provider/reference_12345678/v1"),
            "unresolved",
            "provider_outcome_unknown",
        ),
    ],
)
async def test_effect_outcomes_fail_closed(
    outcome: EffectOutcome, expected: str, reason: str
) -> None:
    activities = TemporalActivities(
        RecordingAgent(), _registry(RecordingEffectAdapter(outcome)), _budget()
    )

    result = await activities.execute_tool(_forward())

    assert result.outcome == expected
    assert result.reason_code == reason
    assert result.receipt is None


async def test_compensation_receives_exact_receipt_and_stable_identity() -> None:
    adapter = RecordingEffectAdapter(EffectConfirmed(receipt={"refunded": True}))
    activities = TemporalActivities(RecordingAgent(), _registry(adapter), _budget())
    forward = _identity(Direction.FORWARD)
    request = CompensationActivityRequest(
        identity=_identity(Direction.COMPENSATION),
        tool_name="refund",
        arguments={"amount": 7},
        compensates_operation_id=forward.operation_id,
        forward_receipt={"charge_id": "charge_1"},
    )

    result = await activities.execute_tool(ToolActivityRequest(compensation=request))

    context = adapter.calls[0][1]
    assert result.outcome == "succeeded"
    assert context.operation_id == request.identity.operation_id
    assert context.forward_receipts == ({"charge_id": "charge_1"},)


async def test_compensation_command_uses_receipt_and_drops_forward_only_fields() -> None:
    adapter = CompensationAdapter()
    activities = TemporalActivities(RecordingAgent(), _compensation_registry(adapter), _budget())
    request = CompensationActivityRequest(
        identity=_identity(Direction.COMPENSATION),
        tool_name="refund",
        arguments={"amount": 7, "expected_version": 2},
        compensates_operation_id=_identity(Direction.FORWARD).operation_id,
        forward_receipt={"charge_id": "charge_1"},
    )

    await activities.execute_tool(ToolActivityRequest(compensation=request))

    assert adapter.calls[0][0] == CompensationCommand(charge_id="charge_1")


async def test_compensation_fails_closed_when_receipt_lacks_required_field() -> None:
    adapter = CompensationAdapter()
    activities = TemporalActivities(RecordingAgent(), _compensation_registry(adapter), _budget())
    request = CompensationActivityRequest(
        identity=_identity(Direction.COMPENSATION),
        tool_name="refund",
        arguments={"amount": 7},
        compensates_operation_id=_identity(Direction.FORWARD).operation_id,
        forward_receipt={"provider_reference": "missing_charge_id"},
    )

    result = await activities.execute_tool(ToolActivityRequest(compensation=request))

    assert result.outcome == "failed"
    assert result.reason_code == "command_rejected"
    assert adapter.calls == []


async def test_compensation_rejects_mismatched_argument_receipt_collision() -> None:
    adapter = CompensationAdapter()
    activities = TemporalActivities(RecordingAgent(), _compensation_registry(adapter), _budget())
    request = CompensationActivityRequest(
        identity=_identity(Direction.COMPENSATION),
        tool_name="refund",
        arguments={"charge_id": "trusted_charge"},
        compensates_operation_id=_identity(Direction.FORWARD).operation_id,
        forward_receipt={"charge_id": "redirected_charge"},
    )

    result = await activities.execute_tool(ToolActivityRequest(compensation=request))

    assert result.outcome == "failed"
    assert result.reason_code == "command_rejected"
    assert adapter.calls == []


async def test_compensation_permits_equal_argument_receipt_collision() -> None:
    adapter = CompensationAdapter()
    activities = TemporalActivities(RecordingAgent(), _compensation_registry(adapter), _budget())
    request = CompensationActivityRequest(
        identity=_identity(Direction.COMPENSATION),
        tool_name="refund",
        arguments={"charge_id": "trusted_charge"},
        compensates_operation_id=_identity(Direction.FORWARD).operation_id,
        forward_receipt={"charge_id": "trusted_charge"},
    )

    result = await activities.execute_tool(ToolActivityRequest(compensation=request))

    assert result.outcome == "succeeded"
    assert adapter.calls[0][0] == CompensationCommand(charge_id="trusted_charge")


@pytest.mark.parametrize(
    ("outcome", "expected", "reason"),
    [
        (ReconcileEffectConfirmed(receipt={"charge_id": "c1"}), "confirmed_effect", None),
        (
            ReconcileNoEffectConfirmed(reason="not_found"),
            "confirmed_no_effect",
            "provider_confirmed_no_effect",
        ),
        (
            ReconcilePending(correlation="pending", check_after=datetime(2026, 1, 1, tzinfo=UTC)),
            "pending",
            "provider_pending",
        ),
        (ReconcileConflict(reason="mismatch"), "conflict", "provider_evidence_conflict"),
        (
            ReconcileUnsupported(reason="unsupported"),
            "unsupported",
            "provider_reconciliation_unsupported",
        ),
    ],
)
async def test_reconciliation_maps_every_provider_outcome(
    outcome: ReconciliationOutcome, expected: str, reason: str | None
) -> None:
    adapter = RecordingEffectAdapter(outcome)
    activities = TemporalActivities(RecordingAgent(), _registry(adapter), _budget())
    identity = _identity(Direction.FORWARD)
    request = ReconciliationActivityRequest(
        forward_identity=identity,
        tool_name="charge",
        arguments={"amount": 7},
        correlation_id=identity.operation_id,
        declared_kind="effect",
        declared_compensation_tool="refund",
    )

    result = await activities.reconcile(request)

    context = adapter.reconciliations[0][1]
    assert result.outcome == expected
    assert result.reason_code == reason
    assert context.operation_id == identity.operation_id


async def test_reconciliation_exception_is_sanitized_before_temporal_serialization() -> None:
    activities = TemporalActivities(
        ExplodingAgent(), _registry(ExplodingEffectAdapter()), _budget()
    )
    identity = _identity(Direction.FORWARD)
    request = ReconciliationActivityRequest(
        forward_identity=identity,
        tool_name="charge",
        arguments={"amount": 7},
        correlation_id=identity.operation_id,
        declared_kind="effect",
        declared_compensation_tool="refund",
    )

    with pytest.raises(ApplicationError) as raised:
        await activities.reconcile(request)

    _assert_sanitized_failure(
        raised.value,
        expected_message="reconciliation adapter failed",
        expected_type="ReconciliationAdapterFailure",
    )


async def test_invalid_command_never_reaches_business_adapter() -> None:
    adapter = RecordingEffectAdapter(EffectConfirmed(receipt={"ok": True}))
    activities = TemporalActivities(RecordingAgent(), _registry(adapter), _budget())
    request = ForwardActivityRequest(
        identity=_identity(Direction.FORWARD),
        tool_name="charge",
        arguments={"amount": "7"},
        declared_kind="effect",
        declared_compensation_tool="refund",
    )

    result = await activities.execute_tool(ToolActivityRequest(forward=request))

    assert result.outcome == "failed"
    assert result.reason_code == "command_rejected"
    assert adapter.calls == []


@pytest.mark.parametrize(
    ("kind", "compensation"),
    [("read", None), ("effect", "arbitrary_compensation")],
)
async def test_declared_tool_contract_mismatch_never_calls_adapter(
    kind: str, compensation: str | None
) -> None:
    adapter = RecordingEffectAdapter(EffectConfirmed(receipt={"ok": True}))
    activities = TemporalActivities(RecordingAgent(), _registry(adapter), _budget())
    request = ForwardActivityRequest(
        identity=_identity(Direction.FORWARD),
        tool_name="charge",
        arguments={"amount": 7},
        declared_kind=cast(Literal["read", "effect"], kind),
        declared_compensation_tool=compensation,
    )

    result = await activities.execute_tool(ToolActivityRequest(forward=request))

    assert result.outcome == "failed"
    assert result.reason_code == "tool_contract_mismatch"
    assert adapter.calls == []


async def test_unknown_tool_is_a_definite_command_rejection() -> None:
    adapter = RecordingEffectAdapter(EffectConfirmed(receipt={"ok": True}))
    activities = TemporalActivities(RecordingAgent(), _registry(adapter), _budget())
    request = ForwardActivityRequest(
        identity=_identity(Direction.FORWARD),
        tool_name="missing",
        arguments={"amount": 7},
        declared_kind="read",
    )

    result = await activities.execute_tool(ToolActivityRequest(forward=request))

    assert result.outcome == "failed"
    assert result.reason_code == "command_rejected"
    assert adapter.calls == []


async def test_recording_agent_satisfies_driver_contract() -> None:
    assert isinstance(RecordingAgent(), AgentDriver)
