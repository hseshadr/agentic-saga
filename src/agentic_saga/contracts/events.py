from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from agentic_saga.contracts.common import (
    Direction,
    FenceToken,
    JsonObject,
    OperationId,
    SagaId,
    StepInstanceId,
    sha256_json,
)
from agentic_saga.contracts.outcomes import (
    EffectOutcome,
    ReconciliationOutcome,
    effect_outcome_is_safe,
    reconciliation_outcome_is_safe,
    safe_outcome_json,
    safe_reconciliation_json,
)

type _BoundedName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _ReasonCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,99}$")]
type _HashDigest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
type _EventId = Annotated[str, StringConstraints(strict=True, pattern=r"^evt_[a-z0-9]{16,64}$")]
type _TraceId = Annotated[str, StringConstraints(strict=True, pattern=r"^trace_[a-z0-9]{16,64}$")]
type _TerminalStatus = Literal[
    "succeeded_verified",
    "compensated_verified",
    "aborted_clean",
    "resolved_with_exception",
]


class _LedgerEventBase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    event_id: _EventId
    saga_id: SagaId
    saga_seq: int = Field(strict=True, ge=1)
    schema_version: Literal["1.0"] = "1.0"
    definition_version: _BoundedName
    fence_token: FenceToken | None
    actor: _BoundedName
    trace_id: _TraceId
    recorded_at: AwareDatetime

    @field_validator("recorded_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("recorded_at must use UTC")
        return value


class _EffectEventBase(_LedgerEventBase):
    operation_id: OperationId
    step_instance_id: StepInstanceId
    direction: Direction
    semantic_generation: int = Field(strict=True, ge=0)
    delivery_attempt: int = Field(strict=True, ge=1)
    tool_name: _BoundedName
    redacted_command: JsonObject
    command_hash: _HashDigest


class SagaCreated(_LedgerEventBase):
    """Anchor a saga ledger to its immutable definition and redacted goal."""

    event_type: Literal["saga_created"] = "saga_created"
    definition_name: _BoundedName
    definition_fingerprint: _HashDigest
    redacted_goal: JsonObject


class SagaStarted(_LedgerEventBase):
    """Record that deterministic execution has begun for a created saga."""

    event_type: Literal["saga_started"] = "saga_started"


class AgentTurnReserved(_LedgerEventBase):
    """Reserve bounded time and tokens before asking an agent for a proposal."""

    event_type: Literal["agent_turn_reserved"] = "agent_turn_reserved"
    turn_id: _BoundedName
    turn_index: int = Field(strict=True, ge=1)
    reserved_elapsed_ms: int = Field(strict=True, ge=1)
    reserved_tokens: int = Field(strict=True, ge=1)


class AgentTurnFailed(_LedgerEventBase):
    """Record a failed agent turn without inventing a proposed action."""

    event_type: Literal["agent_turn_failed"] = "agent_turn_failed"
    turn_id: _BoundedName
    reason_code: _ReasonCode


class ReadStarted(_LedgerEventBase):
    """Persist a read proposal before invoking its external adapter."""

    event_type: Literal["read_started"] = "read_started"
    turn_id: _BoundedName
    proposal_id: _BoundedName
    tool_name: _BoundedName
    redacted_command: JsonObject
    command_hash: _HashDigest

    @model_validator(mode="after")
    def require_command_digest(self) -> ReadStarted:
        if self.command_hash != sha256_json(self.redacted_command):
            raise ValueError("command_hash must equal the public command canonical hash")
        return self


class ReadObserved(_LedgerEventBase):
    """Capture a bounded, redacted observation returned by a read adapter."""

    event_type: Literal["read_observed"] = "read_observed"
    turn_id: _BoundedName
    proposal_id: _BoundedName
    tool_name: _BoundedName
    redacted_result: JsonObject
    result_hash: _HashDigest

    @model_validator(mode="after")
    def require_result_digest(self) -> ReadObserved:
        if self.result_hash != sha256_json(self.redacted_result):
            raise ValueError("result_hash must equal the public result canonical hash")
        return self


class ReadUnavailable(_LedgerEventBase):
    """Durable, detail-free evidence that an authorized read did not complete."""

    event_type: Literal["read_unavailable"] = "read_unavailable"
    turn_id: _BoundedName
    proposal_id: _BoundedName
    tool_name: _BoundedName
    reason_code: Literal["read_unavailable"] = "read_unavailable"


class EffectIntentRecorded(_EffectEventBase):
    """Make an authorized effect intent durable before provider dispatch."""

    event_type: Literal["effect_intent_recorded"] = "effect_intent_recorded"
    compensate_with: _BoundedName | None = None


class DispatchStarted(_EffectEventBase):
    """Record entry into the provider's effect boundary."""

    event_type: Literal["dispatch_started"] = "dispatch_started"


class DispatchAbortedBeforeEntry(_EffectEventBase):
    """Prove a claimed dispatch stopped before crossing the effect boundary."""

    event_type: Literal["dispatch_aborted_before_entry"] = "dispatch_aborted_before_entry"


class EffectOutcomeRecorded(_EffectEventBase):
    """Persist the authoritative outcome and evidence for a dispatched effect."""

    event_type: Literal["effect_outcome_recorded"] = "effect_outcome_recorded"
    outcome: EffectOutcome
    redacted_result: JsonObject
    result_hash: _HashDigest

    @model_validator(mode="after")
    def require_safe_outcome_proof(self) -> EffectOutcomeRecorded:
        if not effect_outcome_is_safe(self.outcome):
            raise ValueError("outcome must be normalized before persistence")
        representation = safe_outcome_json(self.outcome)
        if self.redacted_result != representation:
            raise ValueError("redacted_result must equal the safe outcome representation")
        if self.result_hash != sha256_json(representation):
            raise ValueError("result_hash must equal the safe outcome canonical hash")
        return self


class ReconciliationRecorded(_EffectEventBase):
    """Persist a safe resolution of an effect whose outcome was uncertain."""

    event_type: Literal["reconciliation_recorded"] = "reconciliation_recorded"
    reconciliation_attempt: int = Field(strict=True, ge=1)
    outcome: ReconciliationOutcome
    action: Literal["confirm", "retry_same_id", "wait", "human"]
    redacted_result: JsonObject
    result_hash: _HashDigest
    recovery_policy_digest: _HashDigest

    @model_validator(mode="after")
    def require_safe_reconciliation_proof(self) -> ReconciliationRecorded:
        _require_reconciliation_action(self.outcome, self.action)
        _require_safe_reconciliation_outcome(self.outcome)
        representation = safe_reconciliation_json(self.outcome)
        _require_reconciliation_representation(self, representation)
        return self


def _require_reconciliation_action(outcome: ReconciliationOutcome, action: str) -> None:
    allowed = {
        "reconcile_effect_confirmed": frozenset({"confirm"}),
        "reconcile_no_effect_confirmed": frozenset({"retry_same_id"}),
        "reconcile_pending": frozenset({"wait"}),
        "reconcile_conflict": frozenset({"human"}),
        "reconcile_unsupported": frozenset({"retry_same_id", "human"}),
    }
    if action not in allowed[outcome.kind]:
        raise ValueError("reconciliation action contradicts its evidence")


def _require_safe_reconciliation_outcome(outcome: ReconciliationOutcome) -> None:
    if not reconciliation_outcome_is_safe(outcome):
        raise ValueError("reconciliation outcome must be normalized before persistence")


def _require_reconciliation_representation(
    event: ReconciliationRecorded, representation: JsonObject
) -> None:
    if event.redacted_result != representation:
        raise ValueError("redacted_result must equal the safe reconciliation representation")
    if event.result_hash != sha256_json(representation):
        raise ValueError("result_hash must equal the safe reconciliation canonical hash")


class RecoveryPlanRequired(_LedgerEventBase):
    """Block autonomous progress until a recovery plan is authorized."""

    event_type: Literal["recovery_plan_required"] = "recovery_plan_required"
    reason_code: _ReasonCode


class RecoveryPlanAccepted(_LedgerEventBase):
    """Bind an accepted recovery plan to its canonical digest."""

    event_type: Literal["recovery_plan_accepted"] = "recovery_plan_accepted"
    plan_hash: _HashDigest


class RecoveryPlanRejected(_LedgerEventBase):
    """Record why a proposed recovery plan cannot be used."""

    event_type: Literal["recovery_plan_rejected"] = "recovery_plan_rejected"
    reason_code: _ReasonCode


class ProposalRejected(_LedgerEventBase):
    """Record deterministic policy rejection of an agent proposal."""

    event_type: Literal["proposal_rejected"] = "proposal_rejected"
    proposal_id: _BoundedName
    proposal_hash: _HashDigest
    reason_code: _ReasonCode


class ApprovalConsumed(_LedgerEventBase):
    """Prove a verified human approval was consumed for one proposal."""

    event_type: Literal["approval_consumed"] = "approval_consumed"
    decision_id: _BoundedName
    proposal_hash: _HashDigest
    verification_result: Literal[True]


class CompensationStarted(_LedgerEventBase):
    """Move a saga into deterministic compensation processing."""

    event_type: Literal["compensation_started"] = "compensation_started"
    proposal_id: _BoundedName | None = None
    proposal_hash: _HashDigest | None = None
    reason_code: _ReasonCode | None = None


class CompensationIntentRecorded(_EffectEventBase):
    """Durably bind a compensation attempt to its proven forward effect."""

    event_type: Literal["compensation_intent_recorded"] = "compensation_intent_recorded"
    compensates_operation_id: OperationId
    forward_receipts: tuple[JsonObject, ...] = Field(min_length=1)


class InvariantEvaluated(_LedgerEventBase):
    """Persist terminal-invariant evidence evaluated at an exact ledger sequence."""

    event_type: Literal["invariant_evaluated"] = "invariant_evaluated"
    evaluated_at_seq: int = Field(strict=True, ge=1)
    target_status: _TerminalStatus
    invariant_version: _BoundedName
    evidence_digest: _HashDigest
    results: JsonObject
    all_passed: bool


class HumanRequired(_LedgerEventBase):
    """Suspend autonomous execution with a stable operator-facing reason."""

    event_type: Literal["human_required"] = "human_required"
    reason_code: _ReasonCode


class HumanResolutionRecorded(_LedgerEventBase):
    """Record a verified operator decision used to resume or settle a saga."""

    event_type: Literal["human_resolution_recorded"] = "human_resolution_recorded"
    decision_id: _BoundedName
    proposal_hash: _HashDigest
    verification_result: bool
    action: Literal["approve", "reject", "reconcile"] = "approve"


class TerminalAssigned(_LedgerEventBase):
    """Commit a terminal status after its required evidence has passed."""

    event_type: Literal["terminal_assigned"] = "terminal_assigned"
    status: _TerminalStatus


class TerminalDenied(_LedgerEventBase):
    """Record why a requested terminal transition failed closed."""

    event_type: Literal["terminal_denied"] = "terminal_denied"
    proposal_id: _BoundedName
    proposal_hash: _HashDigest
    target_status: _TerminalStatus
    reason_code: _ReasonCode


type LedgerEvent = Annotated[
    SagaCreated
    | SagaStarted
    | AgentTurnReserved
    | AgentTurnFailed
    | ReadStarted
    | ReadObserved
    | ReadUnavailable
    | EffectIntentRecorded
    | DispatchStarted
    | DispatchAbortedBeforeEntry
    | EffectOutcomeRecorded
    | ReconciliationRecorded
    | RecoveryPlanRequired
    | RecoveryPlanAccepted
    | RecoveryPlanRejected
    | ProposalRejected
    | ApprovalConsumed
    | CompensationStarted
    | CompensationIntentRecorded
    | InvariantEvaluated
    | HumanRequired
    | HumanResolutionRecorded
    | TerminalAssigned
    | TerminalDenied,
    Field(discriminator="event_type"),
]


__all__ = [
    "AgentTurnFailed",
    "AgentTurnReserved",
    "ApprovalConsumed",
    "CompensationIntentRecorded",
    "CompensationStarted",
    "DispatchAbortedBeforeEntry",
    "DispatchStarted",
    "EffectIntentRecorded",
    "EffectOutcomeRecorded",
    "HumanRequired",
    "HumanResolutionRecorded",
    "InvariantEvaluated",
    "LedgerEvent",
    "ProposalRejected",
    "ReadObserved",
    "ReadStarted",
    "ReadUnavailable",
    "ReconciliationRecorded",
    "RecoveryPlanAccepted",
    "RecoveryPlanRejected",
    "RecoveryPlanRequired",
    "SagaCreated",
    "SagaStarted",
    "TerminalAssigned",
    "TerminalDenied",
]
