from __future__ import annotations

from datetime import timedelta
from typing import cast

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from agentic_saga.contracts.actions import Finish, ToolCall
    from agentic_saga.contracts.common import Direction, JsonObject
    from agentic_saga.contracts.runtime import SagaStatus
    from agentic_saga.temporal.contracts import (
        ActivityIdentity,
        AgentDecisionObservation,
        AgentDecisionRequest,
        AgentDecisionResult,
        CompensationActivityRequest,
        CompensationActivityResult,
        ForwardActivityRequest,
        ForwardActivityResult,
        HumanCompensationResolution,
        HumanResolutionVerificationRequest,
        HumanResolutionVerificationResult,
        ReconciliationActivityRequest,
        ReconciliationActivityResult,
        SagaWorkflowInput,
        ToolActivityRequest,
        ToolActivityResult,
        ToolCallCount,
        WorkflowEvent,
        WorkflowState,
        WorkflowTool,
    )
    from agentic_saga.temporal.journal import CompensationJournal

AGENT_DECISION_ACTIVITY = "agentic_saga.decide"
BUSINESS_TOOL_ACTIVITY = "agentic_saga.execute_tool"
RECONCILIATION_ACTIVITY = "agentic_saga.reconcile"
HUMAN_RESOLUTION_ACTIVITY = "agentic_saga.verify_human_resolution"
_TIMEOUT = timedelta(seconds=30)
_AGENT_RETRY = RetryPolicy(maximum_attempts=1)
_TOOL_RETRY = RetryPolicy(maximum_attempts=2)


@workflow.defn(name="agentic_saga.workflow")
class AgenticSagaWorkflow:
    """Orchestrate bounded agent choices with deterministic Saga recovery."""

    def __init__(self) -> None:
        self._input: SagaWorkflowInput | None = None
        self._state_value: WorkflowState | None = None
        self._journal = CompensationJournal()
        self._human_resolved = False
        self._successful_tools: set[str] = set()
        self._fresh_proofs: set[str] = set()
        self._tool_calls: dict[str, int] = {}
        self._agent_calls = 0

    @workflow.run
    async def run(self, saga: SagaWorkflowInput) -> WorkflowState:
        self._start(saga)
        for _ in range(saga.max_agent_turns):
            terminal = await self._run_turn()
            if terminal is not None:
                return terminal
        return await self._stop("agent_turn_limit_exhausted")

    async def _run_turn(self) -> WorkflowState | None:
        proposal = await self._decide_or_none()
        if proposal is None:
            return await self._stop("agent_decision_failed")
        error = self._proposal_error(proposal)
        self._record_agent_decision(proposal, error)
        if error is not None:
            return await self._stop(error)
        if isinstance(proposal, Finish):
            return await self._finish(proposal)
        result = await self._run_forward(cast(ToolCall, proposal))
        return None if result.outcome == "succeeded" else await self._handle_forward_stop(result)

    async def _decide_or_none(self) -> Finish | ToolCall | object | None:
        try:
            return await self._decide()
        except ActivityError:
            return None

    @workflow.query
    def state(self) -> WorkflowState:
        return self._state()

    @workflow.update
    async def resolve_compensation(self, resolution: HumanCompensationResolution) -> WorkflowState:
        verification = await self._verify_human_resolution(resolution)
        if not verification.verified:
            raise ApplicationError(
                "human resolution authorization was rejected",
                type="HumanResolutionRejected",
                non_retryable=True,
            )
        self._journal = self._journal.resolve_unresolved(
            resolution.operation_id, resolution.receipt
        )
        self._state_value = self._resumed_state(resolution, verification)
        self._human_resolved = True
        return self._state()

    @resolve_compensation.validator
    def validate_resolution(self, resolution: HumanCompensationResolution) -> None:
        if self._state().status is not SagaStatus.HUMAN_REQUIRED:
            raise ValueError("workflow is not waiting for a human resolution")
        if resolution.based_on_event_seq != len(self._state().events):
            raise ValueError("human resolution is stale")
        if not self._journal.can_resolve(resolution.operation_id):
            raise ValueError("resolution does not match unresolved compensation")

    def _start(self, saga: SagaWorkflowInput) -> None:
        self._input = saga
        event = WorkflowEvent(
            seq=1,
            kind="started",
            details={"goal_id": saga.goal.goal_id},
            recorded_at=workflow.now(),
            before_status=SagaStatus.RUNNING,
            after_status=SagaStatus.RUNNING,
        )
        self._state_value = WorkflowState(
            saga_id=saga.saga_id,
            status=SagaStatus.RUNNING,
            events=(event,),
            compensations=(),
        )

    async def _decide(self) -> Finish | ToolCall | object:
        request = self._decision_request()
        self._agent_calls += 1
        result = await workflow.execute_activity(
            AGENT_DECISION_ACTIVITY,
            request,
            result_type=AgentDecisionResult,
            start_to_close_timeout=_TIMEOUT,
            retry_policy=_AGENT_RETRY,
            activity_id=f"agent-decision-{request.saga_seq}",
        )
        return cast(AgentDecisionResult, result).proposal

    def _decision_request(self) -> AgentDecisionRequest:
        saga = self._saga()
        state = self._state()
        return AgentDecisionRequest(
            saga_id=saga.saga_id,
            saga_seq=len(state.events),
            status=state.status,
            goal=saga.goal,
            available_tools=tuple(tool.name for tool in saga.tools if self._is_eligible(tool)),
            state=self._decision_observation(),
            finish_allowed=self._terminal_ready(),
        )

    def _decision_observation(self) -> AgentDecisionObservation:
        state = self._state()
        counts = tuple(
            ToolCallCount(tool_name=name, count=count)
            for name, count in sorted(self._tool_calls.items())
        )
        return AgentDecisionObservation(
            recent_events=state.events[-20:],
            completed_tools=tuple(sorted(self._successful_tools)),
            tool_call_counts=counts,
            compensation_count=len(state.compensations),
            total_event_count=len(state.events),
            remaining_turns=self._saga().max_agent_turns - self._agent_calls,
            remaining_tool_calls=self._saga().max_tool_calls - self._tool_call_total(),
        )

    def _proposal_error(self, proposal: object) -> str | None:
        based_on = getattr(proposal, "based_on_saga_seq", None)
        if based_on != len(self._state().events):
            return "stale_agent_proposal"
        if isinstance(proposal, ToolCall) and self._eligible_tool(proposal.tool_name) is None:
            return "ineligible_agent_tool"
        if not isinstance(proposal, (ToolCall, Finish)):
            return "unsupported_agent_proposal"
        return None

    def _record_agent_decision(self, proposal: object, error: str | None) -> None:
        details = cast(
            JsonObject,
            {
                "accepted": error is None,
                "proposal_id": getattr(proposal, "proposal_id", "invalid"),
                "proposal_kind": _proposal_kind(proposal),
                "tool_name": getattr(proposal, "tool_name", None),
            },
        )
        self._record_event("agent_decision", details)

    async def _run_forward(self, proposal: ToolCall) -> ToolActivityResult:
        tool = self._required_tool(proposal.tool_name)
        self._tool_calls[tool.name] = self._tool_calls.get(tool.name, 0) + 1
        request = self._forward_request(proposal, tool)
        result = await self._forward_activity_or_none(request)
        if result is None:
            return await self._recover_forward_activity_error(request, tool)
        result = _normalize_proof_result(tool, result)
        if result.outcome == "unresolved":
            self._append_forward_event(request, result, tool)
            return await self._reconcile_forward(request, tool)
        self._record_forward_result(request, result, tool)
        return result

    def _forward_request(self, proposal: ToolCall, tool: WorkflowTool) -> ForwardActivityRequest:
        return ForwardActivityRequest(
            identity=self._forward_identity(proposal),
            tool_name=proposal.tool_name,
            arguments=proposal.arguments,
            declared_kind=tool.kind,
            declared_compensation_tool=tool.compensation_tool,
        )

    async def _forward_activity_or_none(
        self, request: ForwardActivityRequest
    ) -> ToolActivityResult | None:
        try:
            return await self._execute_tool(ToolActivityRequest(forward=request))
        except ActivityError:
            return None

    async def _recover_forward_activity_error(
        self, request: ForwardActivityRequest, tool: WorkflowTool
    ) -> ToolActivityResult:
        if tool.kind == "read":
            result = ToolActivityResult.failed("read_activity_failed")
            self._record_forward_result(request, result, tool)
            return result
        unknown = ToolActivityResult.unresolved("forward_activity_failed")
        self._append_forward_event(request, unknown, tool)
        return await self._reconcile_forward(request, tool)

    def _record_forward_result(
        self,
        request: ForwardActivityRequest,
        result: ToolActivityResult,
        tool: WorkflowTool,
    ) -> None:
        self._record_forward_evidence(request, result, tool)
        self._append_forward_event(request, result, tool)

    def _record_forward_evidence(
        self,
        request: ForwardActivityRequest,
        result: ToolActivityResult,
        tool: WorkflowTool,
    ) -> None:
        if result.outcome == "succeeded" and result.receipt is not None:
            self._record_proof(tool, result.receipt)
            self._successful_tools.add(tool.name)
        if _creates_obligation(tool, result):
            typed = ForwardActivityResult.succeeded(cast(JsonObject, result.receipt))
            self._journal = self._journal.record_forward_success(
                request=request,
                result=typed,
                compensation_tool=cast(str, tool.compensation_tool),
                compensation_arguments=request.arguments,
            )

    def _record_proof(self, tool: WorkflowTool, receipt: JsonObject) -> None:
        if tool.kind == "effect":
            self._fresh_proofs.clear()
            return
        if tool.proof_for_success and receipt.get("verified") is True:
            self._fresh_proofs.add(tool.name)
        elif tool.proof_for_success:
            self._fresh_proofs.discard(tool.name)

    async def _reconcile_forward(
        self, forward: ForwardActivityRequest, tool: WorkflowTool
    ) -> ToolActivityResult:
        request = _reconciliation_request(forward)
        try:
            result = await self._execute_reconciliation(request)
        except ActivityError:
            return ToolActivityResult.unresolved("reconciliation_activity_failed")
        self._append_reconciliation_event(request, result)
        translated = _reconciled_tool_result(result)
        if translated.outcome == "succeeded":
            self._record_forward_evidence(forward, translated, tool)
            self._sync_compensations()
        return translated

    async def _handle_forward_stop(self, result: ToolActivityResult) -> WorkflowState:
        if result.outcome == "unresolved":
            return await self._unwind_before_human(cast(str, result.reason_code))
        if not self._journal.entries:
            return self._set_status(SagaStatus.ABORTED_CLEAN)
        return await self._compensate()

    async def _stop(self, reason: str) -> WorkflowState:
        if not self._journal.entries:
            return self._require_human(reason)
        self._record_event("guard_rejected", cast(JsonObject, {"reason_code": reason}))
        return await self._compensate()

    async def _compensate(self) -> WorkflowState:
        await self._run_compensations()
        self._record_compensation_proof(SagaStatus.COMPENSATED_VERIFIED)
        return self._set_status(SagaStatus.COMPENSATED_VERIFIED)

    async def _unwind_before_human(self, reason: str) -> WorkflowState:
        if not self._journal.entries:
            return self._require_human(reason)
        await self._run_compensations()
        self._record_compensation_proof(SagaStatus.HUMAN_REQUIRED)
        return self._require_human(reason)

    async def _run_compensations(self) -> None:
        self._state_value = self._set_status(SagaStatus.COMPENSATING)
        while request := self._journal.next_request():
            result = await self._run_compensation_activity(request)
            self._record_compensation(request, result)
            if self._journal.human_required_reason is not None:
                await self._wait_for_human()

    def _record_compensation_proof(self, target: SagaStatus) -> None:
        details = cast(
            JsonObject,
            {
                "all_passed": True,
                "invariant_version": "temporal-compensation-proof-v1",
                "results": {"obligations_reversed": True},
                "rule_id": "obligations_reversed",
                "target_status": target.value,
                "verified": True,
            },
        )
        self._record_event("compensation_verified", details)

    async def _run_compensation_activity(
        self, request: CompensationActivityRequest
    ) -> ToolActivityResult:
        try:
            result = await self._execute_tool(ToolActivityRequest(compensation=request))
        except ActivityError:
            return await self._reconcile_compensation(request)
        if result.outcome == "unresolved":
            return await self._reconcile_compensation(request)
        return result

    async def _reconcile_compensation(
        self, compensation: CompensationActivityRequest
    ) -> ToolActivityResult:
        request = _compensation_reconciliation_request(compensation)
        try:
            result = await self._execute_reconciliation(request)
        except ActivityError:
            return ToolActivityResult.unresolved("compensation_reconciliation_failed")
        self._append_reconciliation_event(request, result)
        return _reconciled_compensation_result(result)

    def _record_compensation(self, request: object, result: ToolActivityResult) -> None:
        typed_request = cast("CompensationActivityRequest", request)
        typed_result = self._compensation_result(result)
        self._journal = self._journal.record_compensation(typed_request, typed_result)
        self._append_compensation_event(typed_request, result)

    async def _wait_for_human(self) -> None:
        reason = cast(str, self._journal.human_required_reason)
        self._state_value = self._set_status(SagaStatus.HUMAN_REQUIRED, reason)
        self._human_resolved = False
        await workflow.wait_condition(lambda: self._human_resolved)
        self._human_resolved = False

    async def _execute_tool(self, request: ToolActivityRequest) -> ToolActivityResult:
        result = await workflow.execute_activity(
            BUSINESS_TOOL_ACTIVITY,
            request,
            result_type=ToolActivityResult,
            start_to_close_timeout=_TIMEOUT,
            retry_policy=_TOOL_RETRY,
            activity_id=request.identity().operation_id,
        )
        return cast(ToolActivityResult, result)

    async def _execute_reconciliation(
        self, request: ReconciliationActivityRequest
    ) -> ReconciliationActivityResult:
        result = await workflow.execute_activity(
            RECONCILIATION_ACTIVITY,
            request,
            result_type=ReconciliationActivityResult,
            start_to_close_timeout=_TIMEOUT,
            retry_policy=_AGENT_RETRY,
            activity_id=f"reconcile-{request.correlation_id}",
        )
        return cast(ReconciliationActivityResult, result)

    async def _verify_human_resolution(
        self, resolution: HumanCompensationResolution
    ) -> HumanResolutionVerificationResult:
        request = _human_verification_request(self._state(), resolution)
        try:
            return await self._execute_human_verification(request)
        except ActivityError:
            raise ApplicationError(
                "human resolution verification failed",
                type="HumanResolutionVerificationFailed",
                non_retryable=True,
            ) from None

    async def _execute_human_verification(
        self, request: HumanResolutionVerificationRequest
    ) -> HumanResolutionVerificationResult:
        result = await workflow.execute_activity(
            HUMAN_RESOLUTION_ACTIVITY,
            request,
            result_type=HumanResolutionVerificationResult,
            start_to_close_timeout=_TIMEOUT,
            retry_policy=_AGENT_RETRY,
        )
        return cast(HumanResolutionVerificationResult, result)

    async def _finish(self, proposal: Finish) -> WorkflowState:
        if proposal.target_status != SagaStatus.SUCCEEDED_VERIFIED.value:
            return await self._stop("unsupported_finish_target")
        if not self._terminal_ready():
            return await self._stop("terminal_proof_missing")
        return self._set_status(SagaStatus.SUCCEEDED_VERIFIED)

    def _forward_identity(self, proposal: ToolCall) -> ActivityIdentity:
        state = self._state()
        return ActivityIdentity.create(
            state.saga_id,
            f"step_{proposal.based_on_saga_seq:08d}",
            Direction.FORWARD,
            0,
        )

    def _append_forward_event(
        self,
        request: ForwardActivityRequest,
        result: ToolActivityResult,
        tool: WorkflowTool,
    ) -> None:
        details = _event_details(request, result, tool)
        self._record_event("forward_result", details)
        self._sync_compensations()

    def _append_compensation_event(self, request: object, result: ToolActivityResult) -> None:
        typed = cast("CompensationActivityRequest", request)
        details = _compensation_event_details(typed, result)
        self._record_event("compensation_result", details)
        self._sync_compensations()

    def _append_reconciliation_event(
        self,
        request: ReconciliationActivityRequest,
        result: ReconciliationActivityResult,
    ) -> None:
        details = _reconciliation_event_details(request, result)
        self._record_event("reconciliation_result", details)

    def _record_event(self, kind: str, details: JsonObject) -> None:
        state = self._state()
        event = _workflow_event(state, kind, details, state.status)
        self._state_value = state.model_copy(update={"events": (*state.events, event)})

    def _sync_compensations(self) -> None:
        self._state_value = self._state().model_copy(
            update={"compensations": self._journal.entries}
        )

    def _resumed_state(
        self,
        resolution: HumanCompensationResolution,
        verification: HumanResolutionVerificationResult,
    ) -> WorkflowState:
        self._sync_compensations()
        details = {
            "actor": verification.trusted_actor,
            "authorization_id": verification.authorization_id,
            "operation_id": resolution.operation_id,
        }
        return self._transition_status(
            SagaStatus.COMPENSATING,
            None,
            kind="human_resolved",
            details=cast(JsonObject, details),
        )

    def _require_human(self, reason: str) -> WorkflowState:
        return self._set_status(SagaStatus.HUMAN_REQUIRED, reason)

    def _set_status(self, status: SagaStatus, reason: str | None = None) -> WorkflowState:
        details = cast(JsonObject, {"reason_code": reason, "status": status.value})
        return self._transition_status(status, reason, kind="status_changed", details=details)

    def _transition_status(
        self,
        status: SagaStatus,
        reason: str | None,
        *,
        kind: str,
        details: JsonObject,
    ) -> WorkflowState:
        state = self._state()
        event = _workflow_event(state, kind, details, status)
        self._state_value = state.model_copy(
            update={
                "status": status,
                "human_required_reason": reason,
                "events": (*state.events, event),
            }
        )
        return self._state()

    def _tool(self, name: str) -> WorkflowTool | None:
        return next((tool for tool in self._saga().tools if tool.name == name), None)

    def _eligible_tool(self, name: str) -> WorkflowTool | None:
        tool = self._tool(name)
        return tool if tool is not None and self._is_eligible(tool) else None

    def _is_eligible(self, tool: WorkflowTool) -> bool:
        under_tool_limit = self._tool_calls.get(tool.name, 0) < tool.max_calls
        under_global_limit = self._tool_call_total() < self._saga().max_tool_calls
        prerequisites_met = set(tool.prerequisites).issubset(self._successful_tools)
        return under_tool_limit and under_global_limit and prerequisites_met

    def _tool_call_total(self) -> int:
        return sum(self._tool_calls.values())

    def _success_requirements(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self._saga().tools if tool.required_for_success)

    def _proof_requirements(self) -> frozenset[str]:
        return frozenset(tool.name for tool in self._saga().tools if tool.proof_for_success)

    def _terminal_ready(self) -> bool:
        tools_ready = self._success_requirements().issubset(self._successful_tools)
        proofs_fresh = self._proof_requirements().issubset(self._fresh_proofs)
        return tools_ready and proofs_fresh

    def _required_tool(self, name: str) -> WorkflowTool:
        tool = self._tool(name)
        if tool is None:
            raise ValueError("tool was not advertised")
        return tool

    def _state(self) -> WorkflowState:
        if self._state_value is None:
            raise RuntimeError("workflow state is not initialized")
        return self._state_value

    def _saga(self) -> SagaWorkflowInput:
        if self._input is None:
            raise RuntimeError("workflow input is not initialized")
        return self._input

    @staticmethod
    def _compensation_result(result: ToolActivityResult) -> CompensationActivityResult:
        if result.outcome == "succeeded" and result.receipt is not None:
            return CompensationActivityResult.succeeded(result.receipt)
        return CompensationActivityResult.unresolved(cast(str, result.reason_code))


def _event_details(
    request: ForwardActivityRequest,
    result: ToolActivityResult,
    tool: WorkflowTool,
) -> JsonObject:
    details = cast(
        JsonObject,
        {
            "arguments": request.arguments,
            "operation_id": request.identity.operation_id,
            "outcome": result.outcome,
            "proof_for_success": tool.proof_for_success,
            "public_receipt": result.receipt,
            "reason_code": result.reason_code,
            "semantic_generation": request.identity.semantic_generation,
            "step_instance_id": request.identity.step_instance_id,
            "tool_name": request.tool_name,
            "verified": _receipt_verified(result.receipt),
        },
    )
    if tool.kind == "read":
        return cast(JsonObject, {**details, "declared_kind": "read"})
    return details


def _compensation_event_details(
    request: CompensationActivityRequest, result: ToolActivityResult
) -> JsonObject:
    return cast(
        JsonObject,
        {
            "arguments": request.arguments,
            "compensates_operation_id": request.compensates_operation_id,
            "operation_id": request.identity.operation_id,
            "outcome": result.outcome,
            "public_receipt": result.receipt,
            "reason_code": result.reason_code,
            "semantic_generation": request.identity.semantic_generation,
            "step_instance_id": request.identity.step_instance_id,
            "tool_name": request.tool_name,
        },
    )


def _workflow_event(
    state: WorkflowState,
    kind: str,
    details: JsonObject,
    after_status: SagaStatus,
) -> WorkflowEvent:
    return WorkflowEvent(
        seq=len(state.events) + 1,
        kind=kind,
        details=details,
        recorded_at=workflow.now(),
        before_status=state.status,
        after_status=after_status,
    )


def _receipt_verified(receipt: JsonObject | None) -> bool:
    return receipt is not None and receipt.get("verified") is True


def _proposal_kind(proposal: object) -> str:
    if isinstance(proposal, ToolCall):
        return "tool_call"
    if isinstance(proposal, Finish):
        return "finish"
    return "invalid"


def _normalize_proof_result(tool: WorkflowTool, result: ToolActivityResult) -> ToolActivityResult:
    if (
        tool.proof_for_success
        and result.outcome == "succeeded"
        and not _receipt_verified(result.receipt)
    ):
        return ToolActivityResult.failed("terminal_proof_failed")
    return result


def _creates_obligation(tool: WorkflowTool, result: ToolActivityResult) -> bool:
    return (
        tool.kind == "effect"
        and tool.compensation_tool is not None
        and result.outcome == "succeeded"
        and result.receipt is not None
    )


def _reconciliation_request(
    forward: ForwardActivityRequest,
) -> ReconciliationActivityRequest:
    return ReconciliationActivityRequest(
        forward_identity=forward.identity,
        tool_name=forward.tool_name,
        arguments=forward.arguments,
        correlation_id=forward.identity.operation_id,
        declared_kind=forward.declared_kind,
        declared_compensation_tool=forward.declared_compensation_tool,
    )


def _compensation_reconciliation_request(
    compensation: CompensationActivityRequest,
) -> ReconciliationActivityRequest:
    identity = compensation.identity
    forward = ActivityIdentity.create(
        identity.saga_id,
        identity.step_instance_id,
        Direction.FORWARD,
        identity.semantic_generation,
    )
    return ReconciliationActivityRequest(
        forward_identity=forward,
        tool_name=compensation.tool_name,
        arguments=compensation.arguments,
        correlation_id=compensation.identity.operation_id,
        declared_kind="effect",
        declared_compensation_tool=compensation.tool_name,
        compensation=compensation,
    )


def _reconciled_tool_result(result: ReconciliationActivityResult) -> ToolActivityResult:
    if result.outcome == "confirmed_effect":
        return ToolActivityResult.succeeded(cast(JsonObject, result.receipt))
    if result.outcome == "confirmed_no_effect":
        return ToolActivityResult.failed(cast(str, result.reason_code))
    return ToolActivityResult.unresolved(cast(str, result.reason_code))


def _reconciled_compensation_result(
    result: ReconciliationActivityResult,
) -> ToolActivityResult:
    if result.outcome == "confirmed_effect":
        return ToolActivityResult.succeeded(cast(JsonObject, result.receipt))
    return ToolActivityResult.unresolved(cast(str, result.reason_code))


def _reconciliation_event_details(
    request: ReconciliationActivityRequest,
    result: ReconciliationActivityResult,
) -> JsonObject:
    return cast(
        JsonObject,
        {
            "arguments": request.arguments,
            "compensates_operation_id": _reconciled_forward_operation(request),
            "correlation_id": request.correlation_id,
            "direction": _reconciliation_direction(request),
            "operation_id": request.forward_identity.operation_id,
            "outcome": result.outcome,
            "public_receipt": result.receipt,
            "reason_code": result.reason_code,
            "semantic_generation": request.forward_identity.semantic_generation,
            "step_instance_id": request.forward_identity.step_instance_id,
            "tool_name": request.tool_name,
        },
    )


def _reconciliation_direction(request: ReconciliationActivityRequest) -> str:
    return "compensation" if request.compensation is not None else "forward"


def _reconciled_forward_operation(request: ReconciliationActivityRequest) -> str | None:
    if request.compensation is None:
        return None
    return request.compensation.compensates_operation_id


def _human_verification_request(
    state: WorkflowState,
    resolution: HumanCompensationResolution,
) -> HumanResolutionVerificationRequest:
    return HumanResolutionVerificationRequest(
        saga_id=state.saga_id,
        operation_id=resolution.operation_id,
        based_on_event_seq=resolution.based_on_event_seq,
        authorization_reference=resolution.authorization_reference,
        receipt=resolution.receipt,
    )
