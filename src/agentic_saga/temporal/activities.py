from __future__ import annotations

from collections.abc import Mapping
from typing import Never, cast

from pydantic import BaseModel, TypeAdapter, ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentic_saga.contracts.common import (
    JsonObject,
    JsonPayloadError,
    canonical_json,
    thaw_json_object,
)
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    NoEffectConfirmed,
    PartialEffectConfirmed,
    ReconcileConflict,
    ReconcileEffectConfirmed,
    ReconcileNoEffectConfirmed,
    ReconcilePending,
    ReconciliationOutcome,
    generated_outcome_correlation,
    normalize_effect_outcome,
)
from agentic_saga.contracts.redaction import (
    RedactionPolicy,
    contains_sensitive_json,
    redact_json,
)
from agentic_saga.contracts.runtime import (
    AgentDriver,
    ExecutionBudget,
    SagaObservation,
    ToolDescriptor,
)
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ReadToolDefinition,
    ReconcileContext,
    ToolRegistry,
    UnknownToolError,
)
from agentic_saga.temporal.contracts import (
    ActivityIdentity,
    AgentDecisionRequest,
    AgentDecisionResult,
    CompensationActivityRequest,
    ReconciliationActivityRequest,
    ReconciliationActivityResult,
    ToolActivityRequest,
    ToolActivityResult,
)
from agentic_saga.temporal.workflow import (
    AGENT_DECISION_ACTIVITY,
    BUSINESS_TOOL_ACTIVITY,
    RECONCILIATION_ACTIVITY,
)

_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_NO_EFFECT = "provider_confirmed_no_effect"
_PARTIAL_EFFECT = "partial_effect_confirmed"
_UNKNOWN_EFFECT = "provider_outcome_unknown"
_PENDING = "provider_pending"
_CONFLICT = "provider_evidence_conflict"
_UNSUPPORTED = "provider_reconciliation_unsupported"
_COMMAND_REJECTED = "command_rejected"
_CONTRACT_MISMATCH = "tool_contract_mismatch"
type _Definition = ReadToolDefinition[BaseModel, BaseModel] | EffectToolDefinition[BaseModel]


class TemporalActivities:
    """Keep model calls and business I/O outside deterministic Workflow code."""

    def __init__(
        self, driver: AgentDriver, registry: ToolRegistry, budget: ExecutionBudget
    ) -> None:
        self._driver = driver
        self._registry = registry
        self._budget = budget

    @activity.defn(name=AGENT_DECISION_ACTIVITY)
    async def decide(self, request: AgentDecisionRequest) -> AgentDecisionResult:
        observation = _observation(request, self._budget)
        tools = _descriptors(request, self._registry)
        try:
            proposal = await self._driver.next_action(observation, tools)
            return AgentDecisionResult(proposal=proposal)
        except Exception:  # External driver boundary must not serialize provider details.
            _raise_activity_failure("agent driver failed", "AgentDriverFailure")

    @activity.defn(name=BUSINESS_TOOL_ACTIVITY)
    async def execute_tool(self, request: ToolActivityRequest) -> ToolActivityResult:
        definition = _definition_or_none(self._registry, request.tool_name())
        if definition is None:
            return ToolActivityResult.failed(_COMMAND_REJECTED)
        if not _request_matches_definition(request, definition):
            return ToolActivityResult.failed(_CONTRACT_MISMATCH)
        raw = _command_arguments(request, definition.input_model)
        if raw is None:
            return ToolActivityResult.failed(_COMMAND_REJECTED)
        command = _command_or_none(self._registry, request.tool_name(), raw)
        if command is None:
            return ToolActivityResult.failed(_COMMAND_REJECTED)
        return await _invoke_tool(request, definition, command)

    @activity.defn(name=RECONCILIATION_ACTIVITY)
    async def reconcile(
        self, request: ReconciliationActivityRequest
    ) -> ReconciliationActivityResult:
        definition = _definition_or_none(self._registry, request.tool_name)
        if not _reconciliation_matches_definition(request, definition):
            return ReconciliationActivityResult.unresolved("unsupported", _UNSUPPORTED)
        command = _reconciliation_command(self._registry, request)
        if command is None:
            return ReconciliationActivityResult.confirmed_no_effect(_COMMAND_REJECTED)
        effect = cast(EffectToolDefinition[BaseModel], definition)
        context = _reconcile_context(_reconciliation_identity(request))
        return await _invoke_reconciliation(effect, command, context)


def _observation(request: AgentDecisionRequest, budget: ExecutionBudget) -> SagaObservation:
    return SagaObservation(
        saga_id=request.saga_id,
        saga_seq=request.saga_seq,
        state=request.status,
        goal=request.goal,
        last_action=_last_action(request),
        projection=_projection(request),
        remaining_budget=_remaining_budget(request, budget),
        finish_allowed=request.finish_allowed,
    )


def _last_action(request: AgentDecisionRequest) -> JsonObject | None:
    return request.state.events[-1].details if request.state.events else None


def _projection(request: AgentDecisionRequest) -> JsonObject:
    base = {
        "compensation_count": request.state.compensation_count,
        "completed_tools": list(request.state.completed_tools),
        "event_count": request.state.total_event_count,
        "status": request.status.value,
        "tool_call_counts": [
            item.model_dump(mode="json") for item in request.state.tool_call_counts
        ],
    }
    recent = _recent_event_evidence(request, base)
    return _JSON_OBJECT.validate_python(base | {"recent_events": recent})


def _remaining_budget(
    request: AgentDecisionRequest, configured: ExecutionBudget
) -> ExecutionBudget:
    return configured.model_copy(
        update={
            "turn_limit": min(configured.turn_limit, request.remaining_turns),
            "tool_call_limit": min(configured.tool_call_limit, request.remaining_tool_calls),
        }
    )


def _descriptors(
    request: AgentDecisionRequest, registry: ToolRegistry
) -> tuple[ToolDescriptor, ...]:
    return tuple(
        ToolDescriptor.from_definition(registry.definition(name))
        for name in request.available_tools
    )


def _definition_or_none(registry: ToolRegistry, name: str) -> _Definition | None:
    try:
        return registry.definition(name)
    except UnknownToolError:
        return None


def _request_matches_definition(request: ToolActivityRequest, definition: _Definition) -> bool:
    if request.compensation is not None:
        return isinstance(definition, EffectToolDefinition)
    forward = request.forward
    if forward is None:
        return False
    actual_kind = "effect" if isinstance(definition, EffectToolDefinition) else "read"
    actual_compensation = getattr(definition, "compensate_with", None)
    return (forward.declared_kind, forward.declared_compensation_tool) == (
        actual_kind,
        actual_compensation,
    )


def _reconciliation_matches_definition(
    request: ReconciliationActivityRequest, definition: _Definition | None
) -> bool:
    if request.compensation is not None:
        return isinstance(definition, EffectToolDefinition)
    return (
        isinstance(definition, EffectToolDefinition)
        and request.declared_kind == "effect"
        and request.declared_compensation_tool == definition.compensate_with
    )


def _reconciliation_command(
    registry: ToolRegistry, request: ReconciliationActivityRequest
) -> BaseModel | None:
    if request.compensation is None:
        return _command_or_none(registry, request.tool_name, request.arguments)
    definition = _definition_or_none(registry, request.tool_name)
    if not isinstance(definition, EffectToolDefinition):
        return None
    activity_request = ToolActivityRequest(compensation=request.compensation)
    raw = _command_arguments(activity_request, definition.input_model)
    return _command_or_none(registry, request.tool_name, raw) if raw is not None else None


def _reconciliation_identity(request: ReconciliationActivityRequest) -> ActivityIdentity:
    if request.compensation is not None:
        return request.compensation.identity
    return request.forward_identity


def _command_or_none(registry: ToolRegistry, name: str, raw: JsonObject) -> BaseModel | None:
    try:
        return _validated_command(registry, name, raw)
    except (UnknownToolError, ValidationError):
        return None


async def _invoke_tool(
    request: ToolActivityRequest, definition: _Definition, command: BaseModel
) -> ToolActivityResult:
    try:
        if isinstance(definition, ReadToolDefinition):
            result = await definition.adapter.read(command)
            return ToolActivityResult.succeeded(_public_model(result))
        context = _effect_context(request)
        outcome = await definition.adapter.execute(command, context)
        return _tool_result(outcome, context)
    except Exception:  # External adapter boundary must not serialize provider details.
        _raise_activity_failure("business tool adapter failed", "BusinessToolAdapterFailure")


async def _invoke_reconciliation(
    definition: EffectToolDefinition[BaseModel],
    command: BaseModel,
    context: ReconcileContext,
) -> ReconciliationActivityResult:
    try:
        outcome = await definition.adapter.reconcile(command, context)
        return _reconciliation_result(outcome, context)
    except Exception:  # External adapter boundary must not serialize provider details.
        _raise_activity_failure("reconciliation adapter failed", "ReconciliationAdapterFailure")


def _arguments(request: ToolActivityRequest) -> JsonObject:
    selected = request.forward or request.compensation
    if selected is None:
        raise ValueError("tool activity request is absent")
    return selected.arguments


def _command_arguments(request: ToolActivityRequest, model: type[BaseModel]) -> JsonObject | None:
    if request.compensation is None:
        return _arguments(request)
    arguments = thaw_json_object(request.compensation.arguments)
    receipt = thaw_json_object(request.compensation.forward_receipt)
    if _has_mismatched_overlap(arguments, receipt):
        return None
    combined = arguments | receipt
    selected = {name: combined[name] for name in model.model_fields if name in combined}
    return _JSON_OBJECT.validate_python(selected)


def _has_mismatched_overlap(arguments: Mapping[str, object], receipt: Mapping[str, object]) -> bool:
    return any(
        key in arguments and canonical_json(arguments[key]) != canonical_json(value)
        for key, value in receipt.items()
    )


def _raise_activity_failure(message: str, failure_type: str) -> Never:
    raise ApplicationError(message, type=failure_type) from None


def _recent_event_evidence(
    request: AgentDecisionRequest, base: dict[str, object]
) -> list[dict[str, object]]:
    recent: list[dict[str, object]] = []
    for event in reversed(request.state.events[-20:]):
        recent = _prepend_if_bounded(event.model_dump(mode="json"), recent, base)
    return recent


def _prepend_if_bounded(
    raw: object, recent: list[dict[str, object]], base: dict[str, object]
) -> list[dict[str, object]]:
    try:
        evidence = thaw_json_object(_public_object(raw))
        candidate = [cast(dict[str, object], evidence), *recent]
        _JSON_OBJECT.validate_python(base | {"recent_events": candidate})
    except (JsonPayloadError, ValueError):
        return recent
    return candidate


def _validated_command(registry: ToolRegistry, name: str, raw: JsonObject) -> BaseModel:
    mutable = cast(JsonObject, thaw_json_object(raw))
    return registry.validate_command(name, mutable)


def _effect_context(request: ToolActivityRequest) -> EffectContext:
    identity = request.identity()
    receipts = _forward_receipts(request.compensation)
    return EffectContext(
        saga_id=identity.saga_id,
        step_instance_id=identity.step_instance_id,
        operation_id=identity.operation_id,
        fence_token=identity.semantic_generation + 1,
        delivery_attempt=_activity_attempt(),
        forward_receipts=receipts,
    )


def _forward_receipts(
    request: CompensationActivityRequest | None,
) -> tuple[JsonObject, ...]:
    return () if request is None else (request.forward_receipt,)


def _activity_attempt() -> int:
    try:
        return activity.info().attempt
    except RuntimeError:
        return 1


def _tool_result(outcome: EffectOutcome, context: EffectContext) -> ToolActivityResult:
    normalized = normalize_effect_outcome(
        outcome,
        fallback_correlation=_correlation(context, "effect"),
    )
    if isinstance(normalized, EffectConfirmed):
        return ToolActivityResult.succeeded(_public_object(normalized.receipt))
    if isinstance(normalized, NoEffectConfirmed):
        return ToolActivityResult.failed(_NO_EFFECT)
    if isinstance(normalized, PartialEffectConfirmed):
        return ToolActivityResult.unresolved(_PARTIAL_EFFECT)
    return ToolActivityResult.unresolved(_UNKNOWN_EFFECT)


def _reconcile_context(identity: ActivityIdentity) -> ReconcileContext:
    context = EffectContext(
        saga_id=identity.saga_id,
        step_instance_id=identity.step_instance_id,
        operation_id=identity.operation_id,
        fence_token=identity.semantic_generation + 1,
        delivery_attempt=_activity_attempt(),
    )
    return ReconcileContext(**context.model_dump(), correlation=_correlation(context, "reconcile"))


def _correlation(context: EffectContext, category: str) -> str:
    return generated_outcome_correlation(context.saga_id, context.operation_id, 1, category)


def _reconciliation_result(
    outcome: ReconciliationOutcome, context: EffectContext
) -> ReconciliationActivityResult:
    normalized = _normalize_reconciliation(outcome, context)
    if isinstance(normalized, ReconcileEffectConfirmed):
        return ReconciliationActivityResult.confirmed_effect(normalized.receipt)
    return _unresolved_reconciliation(normalized)


def _normalize_reconciliation(
    outcome: ReconciliationOutcome, context: EffectContext
) -> ReconciliationOutcome:
    if isinstance(outcome, ReconcileEffectConfirmed):
        return ReconcileEffectConfirmed(receipt=_public_object(outcome.receipt))
    if isinstance(outcome, ReconcilePending):
        return outcome.model_copy(update={"correlation": _correlation(context, "pending")})
    return outcome


def _unresolved_reconciliation(
    outcome: ReconciliationOutcome,
) -> ReconciliationActivityResult:
    if isinstance(outcome, ReconcileNoEffectConfirmed):
        return ReconciliationActivityResult.confirmed_no_effect(_NO_EFFECT)
    if isinstance(outcome, ReconcilePending):
        return ReconciliationActivityResult.unresolved("pending", _PENDING)
    if isinstance(outcome, ReconcileConflict):
        return ReconciliationActivityResult.unresolved("conflict", _CONFLICT)
    return ReconciliationActivityResult.unresolved("unsupported", _UNSUPPORTED)


def _public_model(value: BaseModel) -> JsonObject:
    return _public_object(value.model_dump(mode="json"))


def _public_object(value: object) -> JsonObject:
    policy = RedactionPolicy()
    redacted = redact_json(value, policy)
    return _JSON_OBJECT.validate_python(_drop_sensitive_keys(redacted, policy))


def _drop_sensitive_keys(value: object, policy: RedactionPolicy) -> object:
    if isinstance(value, dict):
        return _public_mapping(value, policy)
    if isinstance(value, list):
        return [_drop_sensitive_keys(item, policy) for item in value]
    return value


def _public_mapping(value: dict[object, object], policy: RedactionPolicy) -> dict[str, object]:
    return {
        cast(str, key): _drop_sensitive_keys(item, policy)
        for key, item in value.items()
        if not contains_sensitive_json({key: None}, policy)
    }
