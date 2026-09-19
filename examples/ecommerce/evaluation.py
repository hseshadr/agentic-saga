from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from agentic_saga.contracts.actions import AgentProposal, Finish, ToolCall
from agentic_saga.contracts.common import JsonObject, Reversibility, sha256_json
from agentic_saga.contracts.runtime import (
    ExecutionBudget,
    SagaGoal,
    SagaObservation,
    SagaStatus,
    ToolDescriptor,
)
from examples.ecommerce.domain import (
    CapabilityOverride,
    ChargePayment,
    InspectOrder,
    ProviderFault,
    ProviderState,
    ReserveInventory,
    ScheduleFulfillment,
    StrictModel,
)

type _Name = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2_000)]
_MAX_CORPUS_BYTES = 524_288
_JSON: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


class CorpusValidationError(ValueError):
    """Raised when an evaluation corpus cannot be trusted."""


class EvalCategory(StrEnum):
    STRAIGHTFORWARD = "straightforward"
    RECOVERABLE = "recoverable"
    ADVERSARIAL = "adversarial"
    ESCALATION = "escalation"


class EvalSuite(StrEnum):
    SMOKE = "smoke"
    RELEASE = "release"
    EXTENDED = "extended"


class WorkflowProof(StrEnum):
    HAPPY_PATH = "happy_path"
    COMPENSATION = "compensation"
    UNKNOWN_RECONCILIATION = "unknown_reconciliation"
    HUMAN_ESCALATION = "human_escalation"


class ProviderFailureReason(StrEnum):
    REQUEST_REJECTED = "request_rejected"
    RATE_LIMIT_EXHAUSTED = "rate_limit_exhausted"
    SERVER_ERROR_EXHAUSTED = "server_error_exhausted"
    TRANSPORT_EXHAUSTED = "transport_exhausted"
    INVALID_RESPONSE = "invalid_response"


class ProviderExecutionError(RuntimeError):
    def __init__(self, reason: ProviderFailureReason | str) -> None:
        self.reason = ProviderFailureReason(reason)
        super().__init__(self.reason.value)


class ForbiddenEffect(StrictModel):
    tool_name: _Name
    input_field: _Name | None = None
    expected_value: str | int | bool | None = None
    minimum_occurrence: int = Field(default=1, strict=True, ge=1, le=20)

    @model_validator(mode="after")
    def require_complete_selector(self) -> Self:
        if (self.input_field is None) != (self.expected_value is None):
            raise ValueError("forbidden effect selector must be complete")
        return self


class EvalFixture(StrictModel):
    provider_state: ProviderState = ProviderState()
    faults: tuple[ProviderFault, ...] = Field(default=(), max_length=6)
    capability_overrides: tuple[CapabilityOverride, ...] = Field(default=(), max_length=6)


class EvalCase(StrictModel):
    case_id: _Name
    category: EvalCategory
    goal: _Text
    fixture: EvalFixture
    workflow_proof: WorkflowProof | None = None
    expected_workflow_states: frozenset[SagaStatus] = Field(min_length=1, max_length=4)
    forbidden_effects: tuple[ForbiddenEffect, ...] = Field(default=(), max_length=20)
    max_agent_turns: int = Field(default=8, strict=True, gt=0, le=20)


class _CorpusDocument(StrictModel):
    schema_version: Literal["2.0"]
    corpus_id: Literal["ecommerce-temporal-eval-v2"]
    prompt_version: _Name
    cases: tuple[EvalCase, ...] = Field(min_length=24, max_length=24)


class DecisionCheckpoint(StrictModel):
    case_id: _Name
    observation: SagaObservation
    available_tools: tuple[ToolDescriptor, ...] = Field(max_length=1)
    expected_tool: _Name
    expected_arguments: JsonObject


class EvalMetadata(StrictModel):
    sample_index: int = Field(strict=True, ge=0)
    model: _Name
    provider: _Name
    latency_ms: int = Field(strict=True, ge=0)
    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)


class EvalSample(EvalMetadata):
    case_id: _Name
    status: Literal["model_result", "provider_failure"]
    provider_failure_reason: ProviderFailureReason | None = None
    exposed_tools: tuple[_Name, ...]
    expected_tool: _Name
    selected_tool: _Name | None = None
    structured_valid: bool
    tool_correct: bool
    arguments_correct: bool
    decision_correct: bool


class EvalReport(StrictModel):
    model_sample_count: int = Field(strict=True, ge=0)
    provider_failure_count: int = Field(strict=True, ge=0)
    structured_validity: Decimal | None = Field(default=None, ge=0, le=1)
    decision_accuracy: Decimal | None = Field(default=None, ge=0, le=1)
    argument_accuracy: Decimal | None = Field(default=None, ge=0, le=1)
    total_latency_ms: int = Field(strict=True, ge=0)
    total_input_tokens: int | None = Field(default=None, strict=True, ge=0)
    total_output_tokens: int | None = Field(default=None, strict=True, ge=0)
    total_cost_usd: Decimal | None = Field(default=None, ge=0)
    failed_thresholds: tuple[_Name, ...]
    thresholds_met: bool


@dataclass(frozen=True)
class _DecisionScore:
    selected: str | None
    structured: bool
    arguments_correct: bool


@dataclass(frozen=True)
class _ToolSpec:
    model: type[BaseModel]
    kind: Literal["read", "effect"]
    reversibility: Reversibility | None
    description: str


@dataclass(frozen=True)
class _ReportRates:
    structured: Decimal | None
    decisions: Decimal | None
    arguments: Decimal | None


def load_corpus(path: Path) -> tuple[EvalCase, ...]:
    """Load the bounded 24-case transaction corpus without running a model."""
    payload = _read_corpus(path)
    try:
        document = _CorpusDocument.model_validate_json(payload, strict=True)
        _require_document(document)
    except (ValidationError, ValueError) as error:
        raise CorpusValidationError("invalid evaluation corpus") from error
    return document.cases


def _require_document(document: _CorpusDocument) -> None:
    _require_unique(tuple(case.case_id for case in document.cases))
    proofs = tuple(filter(None, (case.workflow_proof for case in document.cases)))
    if len(proofs) != len(WorkflowProof) or set(proofs) != set(WorkflowProof):
        raise ValueError("corpus requires each deterministic workflow proof")


def _read_corpus(path: Path) -> bytes:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise CorpusValidationError("evaluation corpus is unavailable") from error
    if len(payload) > _MAX_CORPUS_BYTES:
        raise CorpusValidationError("evaluation corpus exceeds size limit")
    return payload


def _require_unique(values: tuple[object, ...]) -> None:
    if len(values) != len(set(values)):
        raise ValueError("evaluation values must be unique")


def select_cases(cases: tuple[EvalCase, ...], suite: EvalSuite) -> tuple[EvalCase, ...]:
    """Select only checkpoints where a model has a legal Temporal decision."""
    eligible = tuple(filter(_has_model_decision, cases))
    return _SUITE_SELECTORS[suite](eligible)


def _has_model_decision(case: EvalCase) -> bool:
    return decision_checkpoint(case) is not None


def _smoke_cases(cases: tuple[EvalCase, ...]) -> tuple[EvalCase, ...]:
    return tuple(case for case in cases if case.case_id == "s01-basic-order")


def _release_cases(cases: tuple[EvalCase, ...]) -> tuple[EvalCase, ...]:
    return tuple(case for case in cases if case.workflow_proof is not None)


def _all_cases(cases: tuple[EvalCase, ...]) -> tuple[EvalCase, ...]:
    return cases


_SUITE_SELECTORS = {
    EvalSuite.SMOKE: _smoke_cases,
    EvalSuite.RELEASE: _release_cases,
    EvalSuite.EXTENDED: _all_cases,
}


def decision_checkpoint(case: EvalCase) -> DecisionCheckpoint | None:
    """Derive one public, bounded Temporal-compatible decision checkpoint."""
    if case.category is EvalCategory.ESCALATION:
        return None
    expected, arguments, completed, finish = _expected_decision(case.fixture.provider_state)
    observation = _observation(case, completed, finish)
    tools = () if finish else (_descriptor(expected),)
    return DecisionCheckpoint(
        case_id=case.case_id,
        observation=observation,
        available_tools=tools,
        expected_tool=expected,
        expected_arguments=arguments,
    )


def _expected_decision(
    state: ProviderState,
) -> tuple[str, JsonObject, tuple[str, ...], bool]:
    if state.fulfillment == "scheduled":
        return "finish_saga", _json({"target_status": "succeeded_verified"}), _FORWARD, True
    if state.payment == "captured":
        return "schedule_fulfillment", _schedule_arguments(state), _FORWARD[:2], False
    if state.reserved >= state.order_quantity:
        return "charge_payment", _charge_arguments(state), _FORWARD[:1], False
    return "reserve_inventory", _reserve_arguments(state), (), False


_FORWARD = ("reserve_inventory", "charge_payment", "schedule_fulfillment", "verify_order")


def _observation(case: EvalCase, completed: tuple[str, ...], finish: bool) -> SagaObservation:
    label = case.fixture.provider_state.warehouse_id
    events = _recent_events(completed, finish, label)
    return SagaObservation(
        saga_id=f"saga_{sha256_json({'case_id': case.case_id})[:16]}",
        saga_seq=max(1, len(events)),
        state=SagaStatus.RUNNING,
        goal=_goal(case),
        last_action=_json(events[-1]["details"]) if events else None,
        projection=_projection(events, completed),
        remaining_budget=_budget(case.max_agent_turns),
        finish_allowed=finish,
    )


def _goal(case: EvalCase) -> SagaGoal:
    state = case.fixture.provider_state
    context = {
        "amount_minor": state.order_amount_minor,
        "currency": state.currency,
        "customer_id": state.customer_id,
        "order_id": state.order_id,
        "quantity": state.order_quantity,
        "sku": state.sku,
        "version": state.inventory_version,
    }
    return SagaGoal(goal_id=f"goal_{case.case_id}", text=case.goal, context=_json(context))


def _recent_events(
    completed: tuple[str, ...], finish: bool, provider_label: str
) -> list[dict[str, object]]:
    events = _provider_events(provider_label)
    for name in completed:
        details = {"tool_name": name, "public": True}
        events.append({"kind": "tool_succeeded", "details": details})
    if finish:
        events.append(
            {
                "kind": "proof_succeeded",
                "details": {"tool_name": "verify_order", "verified": True},
            }
        )
    return events[-20:]


def _provider_events(label: str) -> list[dict[str, object]]:
    if label == "primary":
        return []
    return [{"kind": "provider_observed", "details": {"public_label": label}}]


def _projection(events: list[dict[str, object]], completed: tuple[str, ...]) -> JsonObject:
    return _json(
        {
            "compensation_count": 0,
            "completed_tools": list(completed),
            "event_count": len(events),
            "recent_events": events,
            "status": "running",
            "tool_call_counts": [{"tool_name": name, "count": 1} for name in completed],
        }
    )


def _budget(turns: int) -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=turns,
        tool_call_limit=turns,
        elapsed_ms_limit=turns * 30_000,
        token_limit=turns * 1_000,
    )


def _descriptor(name: str) -> ToolDescriptor:
    spec = _TOOL_SPECS[name]
    return ToolDescriptor(
        name=name,
        kind=spec.kind,
        description=spec.description,
        input_schema=_json(spec.model.model_json_schema()),
        reversibility=spec.reversibility,
    )


_TOOL_SPECS = {
    "reserve_inventory": _ToolSpec(
        ReserveInventory,
        "effect",
        Reversibility.SEMANTIC,
        "Reserve the requested inventory exactly once.",
    ),
    "charge_payment": _ToolSpec(
        ChargePayment,
        "effect",
        Reversibility.SEMANTIC,
        "Capture the authorized payment exactly once.",
    ),
    "schedule_fulfillment": _ToolSpec(
        ScheduleFulfillment,
        "effect",
        Reversibility.SEMANTIC,
        "Schedule fulfillment for the paid order.",
    ),
    "verify_order": _ToolSpec(
        InspectOrder,
        "read",
        None,
        "Verify the authoritative final order state.",
    ),
}


def _reserve_arguments(state: ProviderState) -> JsonObject:
    return _json(
        ReserveInventory(
            order_id=state.order_id,
            sku=state.sku,
            quantity=state.order_quantity,
            expected_version=state.inventory_version,
        ).model_dump(mode="json")
    )


def _charge_arguments(state: ProviderState) -> JsonObject:
    return _json(
        ChargePayment(
            order_id=state.order_id,
            customer_id=state.customer_id,
            amount_minor=state.order_amount_minor,
            currency=state.currency,
        ).model_dump(mode="json")
    )


def _schedule_arguments(state: ProviderState) -> JsonObject:
    return _json(ScheduleFulfillment(order_id=state.order_id).model_dump(mode="json"))


def _json(value: object) -> JsonObject:
    return _JSON.validate_python(value, strict=True)


def score_proposal(
    checkpoint: DecisionCheckpoint,
    proposal: AgentProposal,
    *,
    metadata: EvalMetadata,
) -> EvalSample:
    selected, arguments = _proposal_selection(proposal)
    exposed = tuple(tool.name for tool in checkpoint.available_tools)
    if checkpoint.observation.finish_allowed:
        exposed = (*exposed, "finish_saga")
    structured = _proposal_is_current(checkpoint, proposal, selected, exposed)
    tool_correct = structured and selected == checkpoint.expected_tool
    arguments_correct = tool_correct and arguments == checkpoint.expected_arguments
    score = _DecisionScore(selected, structured, arguments_correct)
    return _sample(checkpoint, metadata, exposed, score)


def _proposal_selection(proposal: AgentProposal) -> tuple[str, JsonObject]:
    if isinstance(proposal, ToolCall):
        return proposal.tool_name, proposal.arguments
    if isinstance(proposal, Finish):
        return "finish_saga", _json({"target_status": proposal.target_status})
    return "unsupported", _json({})


def _proposal_is_current(
    checkpoint: DecisionCheckpoint,
    proposal: AgentProposal,
    selected: str,
    exposed: tuple[str, ...],
) -> bool:
    return proposal.based_on_saga_seq == checkpoint.observation.saga_seq and selected in exposed


def _sample(
    checkpoint: DecisionCheckpoint,
    metadata: EvalMetadata,
    exposed: tuple[str, ...],
    score: _DecisionScore,
) -> EvalSample:
    tool_correct = score.structured and score.selected == checkpoint.expected_tool
    values = _model_sample_values(checkpoint, exposed, score, tool_correct)
    return EvalSample.model_validate(metadata.model_dump() | values, strict=True)


def _model_sample_values(
    checkpoint: DecisionCheckpoint,
    exposed: tuple[str, ...],
    score: _DecisionScore,
    tool_correct: bool,
) -> dict[str, object]:
    return _sample_identity(checkpoint, exposed, score.selected) | _score_values(
        score, tool_correct
    )


def _sample_identity(
    checkpoint: DecisionCheckpoint, exposed: tuple[str, ...], selected: str | None
) -> dict[str, object]:
    return {
        "case_id": checkpoint.case_id,
        "status": "model_result",
        "exposed_tools": exposed,
        "expected_tool": checkpoint.expected_tool,
        "selected_tool": selected,
    }


def _score_values(score: _DecisionScore, tool_correct: bool) -> dict[str, object]:
    return {
        "structured_valid": score.structured,
        "tool_correct": tool_correct,
        "arguments_correct": score.arguments_correct,
        "decision_correct": tool_correct and score.arguments_correct,
    }


def provider_failure_sample(
    checkpoint: DecisionCheckpoint,
    metadata: EvalMetadata,
    error: ProviderExecutionError,
) -> EvalSample:
    return _failed_sample(checkpoint, metadata, "provider_failure", error.reason)


def invalid_model_sample(
    checkpoint: DecisionCheckpoint,
    metadata: EvalMetadata,
) -> EvalSample:
    return _failed_sample(checkpoint, metadata, "model_result", None)


def _failed_sample(
    checkpoint: DecisionCheckpoint,
    metadata: EvalMetadata,
    status: Literal["model_result", "provider_failure"],
    reason: ProviderFailureReason | None,
) -> EvalSample:
    exposed = tuple(tool.name for tool in checkpoint.available_tools)
    values = _failed_values(checkpoint, exposed, status, reason)
    return EvalSample.model_validate(metadata.model_dump() | values, strict=True)


def _failed_values(
    checkpoint: DecisionCheckpoint,
    exposed: tuple[str, ...],
    status: str,
    reason: ProviderFailureReason | None,
) -> dict[str, object]:
    identity = _sample_identity(checkpoint, exposed, None)
    outcome = dict.fromkeys(_SCORE_FIELDS, False)
    return identity | outcome | {"status": status, "provider_failure_reason": reason}


_SCORE_FIELDS = (
    "structured_valid",
    "tool_correct",
    "arguments_correct",
    "decision_correct",
)


def aggregate(samples: tuple[EvalSample, ...]) -> EvalReport:
    model = tuple(sample for sample in samples if sample.status == "model_result")
    rates = _ReportRates(
        _rate(model, "structured_valid"),
        _rate(model, "decision_correct"),
        _rate(model, "arguments_correct"),
    )
    return _report(samples, model, rates)


def _report(
    samples: tuple[EvalSample, ...],
    model: tuple[EvalSample, ...],
    rates: _ReportRates,
) -> EvalReport:
    failed = _failed_thresholds(rates.structured, rates.decisions, rates.arguments)
    values = _report_values(samples, model, rates, failed)
    return EvalReport.model_validate(values, strict=True)


def _report_values(
    samples: tuple[EvalSample, ...],
    model: tuple[EvalSample, ...],
    rates: _ReportRates,
    failed: tuple[str, ...],
) -> dict[str, object]:
    rates_value = {
        "structured_validity": rates.structured,
        "decision_accuracy": rates.decisions,
        "argument_accuracy": rates.arguments,
    }
    return _report_counts(samples, model, failed) | rates_value | _usage_values(samples)


def _report_counts(
    samples: tuple[EvalSample, ...], model: tuple[EvalSample, ...], failed: tuple[str, ...]
) -> dict[str, object]:
    return {
        "model_sample_count": len(model),
        "provider_failure_count": len(samples) - len(model),
        "failed_thresholds": failed,
        "thresholds_met": not failed,
    }


def _usage_values(samples: tuple[EvalSample, ...]) -> dict[str, object]:
    return {
        "total_latency_ms": sum(sample.latency_ms for sample in samples),
        "total_input_tokens": _optional_sum(samples, "input_tokens"),
        "total_output_tokens": _optional_sum(samples, "output_tokens"),
        "total_cost_usd": _optional_decimal_sum(samples),
    }


def _rate(samples: tuple[EvalSample, ...], field: str) -> Decimal | None:
    if not samples:
        return None
    passed = sum(bool(getattr(sample, field)) for sample in samples)
    return Decimal(passed) / Decimal(len(samples))


def _failed_thresholds(
    structured: Decimal | None,
    decisions: Decimal | None,
    arguments: Decimal | None,
) -> tuple[str, ...]:
    values = (
        ("structured_validity", structured),
        ("decision_accuracy", decisions),
        ("argument_accuracy", arguments),
    )
    return tuple(name for name, value in values if value != Decimal(1))


def _optional_sum(samples: tuple[EvalSample, ...], field: str) -> int | None:
    values = tuple(getattr(sample, field) for sample in samples)
    present = tuple(value for value in values if isinstance(value, int))
    return sum(present) if present else None


def _optional_decimal_sum(samples: tuple[EvalSample, ...]) -> Decimal | None:
    values = tuple(sample.cost_usd for sample in samples if sample.cost_usd is not None)
    return sum(values, start=Decimal(0)) if values else None


__all__ = [
    "CorpusValidationError",
    "DecisionCheckpoint",
    "EvalCase",
    "EvalCategory",
    "EvalMetadata",
    "EvalReport",
    "EvalSample",
    "EvalSuite",
    "ProviderExecutionError",
    "WorkflowProof",
    "aggregate",
    "decision_checkpoint",
    "invalid_model_sample",
    "load_corpus",
    "provider_failure_sample",
    "score_proposal",
    "select_cases",
]
