from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, field_validator

from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    OperationId,
    SagaId,
    sha256_json,
)
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.runtime import TerminalRequirement as _TerminalRequirement
from agentic_saga.kernel.state import (
    CompensationObligation,
    ObligationStatus,
    OperationRecord,
    OperationStatus,
    SagaSnapshot,
)

type _RuleId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _Version = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _Explanation = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
type _BoundedName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _HashDigest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]


class TerminalStateDenied(ValueError):
    """Raised when deterministic evidence cannot authorize a terminal state."""


class AuthoritativeInvariantInput(BaseModel):
    """Typed evidence collected from authoritative systems for one rule."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    saga_id: SagaId
    definition_version: _Version
    evaluated_at_seq: int = Field(strict=True, ge=1)
    target_status: SagaStatus
    values: JsonObject


class InvariantResult(BaseModel):
    """Capture one terminal rule's authoritative inputs, verdict, and explanation."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    rule_id: _RuleId
    passed: bool
    inputs: JsonObject
    explanation: _Explanation


class AcceptedResidual(BaseModel):
    """One explicitly accepted durable effect remaining after resolution."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    operation_id: OperationId
    reason: _Explanation
    evidence: JsonObject


class VerifiedHumanResolution(BaseModel):
    """Verified exception decision stripped of authentication material."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    decision_id: _BoundedName
    actor: _BoundedName
    proposal_hash: _HashDigest
    reason: _Explanation
    accepted_residuals: tuple[AcceptedResidual, ...] = Field(min_length=1)
    saga_id: SagaId
    resolved_at_seq: int = Field(strict=True, ge=1)
    verification_result: bool

    @field_validator("accepted_residuals")
    @classmethod
    def require_unique_residuals(
        cls, value: tuple[AcceptedResidual, ...]
    ) -> tuple[AcceptedResidual, ...]:
        operation_ids = tuple(item.operation_id for item in value)
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("accepted residual operation IDs must be unique")
        return value


class InvariantEvidence(BaseModel):
    """Bind terminal rule results to a saga, definition, and ledger sequence."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    saga_id: SagaId
    definition_version: _Version
    evaluated_at_seq: int = Field(strict=True, ge=1)
    target_status: SagaStatus
    invariant_version: _Version
    results: tuple[InvariantResult, ...] = Field(min_length=1)
    human_resolution: VerifiedHumanResolution | None = None


class InvariantProofEventFields(BaseModel):
    """Kernel-built fields binding an invariant event to its complete evidence."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    evaluated_at_seq: int = Field(strict=True, ge=1)
    target_status: SagaStatus
    invariant_version: _Version
    evidence_digest: _HashDigest
    results: JsonObject
    all_passed: bool


@runtime_checkable
class InvariantRule(Protocol):
    """Evaluate one named terminal condition from authoritative kernel input."""

    @property
    def rule_id(self) -> str: ...

    def __call__(self, evidence: AuthoritativeInvariantInput) -> InvariantResult: ...


_TERMINAL_STATUSES = frozenset(
    {
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    }
)
_TERMINAL_SOURCES: Mapping[SagaStatus, frozenset[SagaStatus]] = {
    SagaStatus.SUCCEEDED_VERIFIED: frozenset({SagaStatus.RUNNING}),
    SagaStatus.COMPENSATED_VERIFIED: frozenset({SagaStatus.COMPENSATING}),
    SagaStatus.ABORTED_CLEAN: frozenset({SagaStatus.RUNNING}),
    SagaStatus.RESOLVED_WITH_EXCEPTION: frozenset({SagaStatus.HUMAN_REQUIRED}),
}
_INFLIGHT_STATUSES = frozenset(
    {OperationStatus.PLANNED, OperationStatus.INTENT_DURABLE, OperationStatus.DISPATCHED}
)
_RESOLVED_OBLIGATIONS = frozenset({ObligationStatus.SATISFIED, ObligationStatus.NOT_REQUIRED})
_RESIDUAL_OPERATION_STATUSES = frozenset(
    {OperationStatus.EFFECT_CONFIRMED, OperationStatus.PARTIAL_EFFECT_CONFIRMED}
)
_RESIDUAL_OBLIGATION_STATUSES = frozenset(
    {ObligationStatus.ARMED, ObligationStatus.ELIGIBLE, ObligationStatus.IN_PROGRESS}
)
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def _canonical_digest(value: JsonObject) -> str:
    return sha256_json(value)


def invariant_evidence_digest(evidence: InvariantEvidence) -> str:
    """Return the canonical digest persisted beside an invariant event."""
    payload = _JSON_OBJECT_ADAPTER.validate_python(evidence.model_dump(mode="json"))
    return _canonical_digest(payload)


def _event_results(evidence: InvariantEvidence) -> JsonObject:
    _result_map(evidence)
    values = {item.rule_id: item.passed for item in evidence.results}
    return _JSON_OBJECT_ADAPTER.validate_python(values)


def build_invariant_event_fields(evidence: InvariantEvidence) -> InvariantProofEventFields:
    """Build the only evidence-derived fields Task 7 needs for its proof event."""
    return InvariantProofEventFields(
        evaluated_at_seq=evidence.evaluated_at_seq,
        target_status=evidence.target_status,
        invariant_version=evidence.invariant_version,
        evidence_digest=invariant_evidence_digest(evidence),
        results=_event_results(evidence),
        all_passed=all(item.passed for item in evidence.results),
    )


def _require_runnable_count(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("runnable_commands must be a non-negative integer")


def _require_source(target: SagaStatus, snapshot: SagaSnapshot) -> None:
    if snapshot.status not in _TERMINAL_SOURCES[target]:
        raise TerminalStateDenied("terminal target is not compatible with current Saga status")


def _require_settled_operations(snapshot: SagaSnapshot) -> None:
    statuses = tuple(item.status for item in snapshot.operations.values())
    if OperationStatus.OUTCOME_UNKNOWN in statuses:
        raise TerminalStateDenied("unknown operation blocks terminal state")
    if any(status in _INFLIGHT_STATUSES for status in statuses):
        raise TerminalStateDenied("in-flight operation blocks terminal state")


def _require_no_runnable_commands(runnable_commands: int) -> None:
    if runnable_commands:
        raise TerminalStateDenied("runnable command blocks terminal state")


def _require_no_pending_approval(snapshot: SagaSnapshot) -> None:
    if snapshot.pending_approval:
        raise TerminalStateDenied("pending approval blocks terminal state")


def _require_fresh_evidence(snapshot: SagaSnapshot, evidence: InvariantEvidence) -> None:
    _require_post_proof_snapshot(snapshot)
    evaluated_state_seq = snapshot.seq - 1
    if evidence.evaluated_at_seq < evaluated_state_seq:
        raise TerminalStateDenied("stale invariant evidence")
    if evidence.evaluated_at_seq > evaluated_state_seq:
        raise TerminalStateDenied("future invariant evidence")
    _require_current_proof(snapshot)


def _require_post_proof_snapshot(snapshot: SagaSnapshot) -> None:
    if snapshot.last_invariant_seq is None or snapshot.last_invariant_passed is None:
        raise TerminalStateDenied("a post-proof snapshot is required")


def _require_current_proof(snapshot: SagaSnapshot) -> None:
    if snapshot.last_invariant_seq != snapshot.seq:
        raise TerminalStateDenied("a current invariant event is required")
    if snapshot.last_invariant_passed is not True:
        raise TerminalStateDenied("a passing invariant event is required")


def _require_evidence_binding(
    target: SagaStatus, snapshot: SagaSnapshot, evidence: InvariantEvidence
) -> None:
    if evidence.saga_id != snapshot.saga_id:
        raise TerminalStateDenied("invariant evidence belongs to a different Saga")
    if evidence.definition_version != snapshot.definition_version:
        raise TerminalStateDenied("invariant evidence uses a different definition")
    if evidence.target_status is not target:
        raise TerminalStateDenied("invariant target does not match requested terminal state")


def _require_exact_proof(snapshot: SagaSnapshot, evidence: InvariantEvidence) -> None:
    markers = (
        snapshot.last_invariant_target,
        snapshot.last_invariant_version,
        snapshot.last_invariant_evidence_digest,
    )
    expected = (
        evidence.target_status,
        evidence.invariant_version,
        invariant_evidence_digest(evidence),
    )
    if markers != expected:
        raise TerminalStateDenied("invariant proof binding does not match evidence")


def _result_map(evidence: InvariantEvidence) -> dict[str, InvariantResult]:
    results = {item.rule_id: item for item in evidence.results}
    if len(results) != len(evidence.results):
        raise TerminalStateDenied("duplicate invariant rule result")
    return results


def _require_rule_set(
    requirement: _TerminalRequirement,
    results: Mapping[str, InvariantResult],
) -> None:
    required = frozenset(requirement.required_rule_ids)
    actual = frozenset(results)
    if required - actual:
        raise TerminalStateDenied("missing required invariant rule result")
    if actual != required:
        raise TerminalStateDenied("invariant rule set does not match terminal requirement")


def _require_passing_results(results: Mapping[str, InvariantResult]) -> None:
    if any(not item.passed for item in results.values()):
        raise TerminalStateDenied("invariant rule failed")


def _require_resolved_compensation(target: SagaStatus, snapshot: SagaSnapshot) -> None:
    if target is not SagaStatus.COMPENSATED_VERIFIED:
        return
    unresolved = any(
        item.status not in _RESOLVED_OBLIGATIONS for item in snapshot.obligations.values()
    )
    if unresolved:
        raise TerminalStateDenied("unresolved compensation obligation")


def _valid_human_resolution(evidence: InvariantEvidence) -> bool:
    resolution = evidence.human_resolution
    if resolution is None or resolution.verification_result is not True:
        return False
    if resolution.saga_id != evidence.saga_id:
        return False
    return resolution.resolved_at_seq == evidence.evaluated_at_seq


def _residual_is_relevant(snapshot: SagaSnapshot, operation_id: OperationId) -> bool:
    operation = snapshot.operations.get(operation_id)
    if not _operation_is_residual(operation, operation_id):
        return False
    obligation = snapshot.obligations.get(operation_id)
    return obligation is None or _obligation_is_residual(obligation, operation_id)


def _operation_is_residual(operation: OperationRecord | None, operation_id: OperationId) -> bool:
    if operation is None or operation.operation_id != operation_id:
        return False
    return (
        operation.direction is Direction.FORWARD
        and operation.status in _RESIDUAL_OPERATION_STATUSES
    )


def _obligation_is_residual(
    obligation: CompensationObligation | None, operation_id: OperationId
) -> bool:
    if obligation is None or obligation.forward_operation_id != operation_id:
        return False
    return obligation.status in _RESIDUAL_OBLIGATION_STATUSES


def _require_residual_references(
    snapshot: SagaSnapshot, resolution: VerifiedHumanResolution
) -> None:
    if any(
        not _residual_is_relevant(snapshot, item.operation_id)
        for item in resolution.accepted_residuals
    ):
        raise TerminalStateDenied("accepted residual operation is not relevant to this Saga")


def _require_human_exception(
    target: SagaStatus, snapshot: SagaSnapshot, evidence: InvariantEvidence
) -> None:
    if target is not SagaStatus.RESOLVED_WITH_EXCEPTION:
        if evidence.human_resolution is not None:
            raise TerminalStateDenied("irrelevant human resolution evidence")
        return
    if not _valid_human_resolution(evidence):
        raise TerminalStateDenied("verified human-exception evidence is required")
    resolution = evidence.human_resolution
    if resolution is not None:
        _require_residual_references(snapshot, resolution)


class TerminalGate:
    """Fail closed unless exact, current evidence satisfies a terminal contract."""

    def __init__(self, requirements: Mapping[SagaStatus, _TerminalRequirement]) -> None:
        if frozenset(requirements) != _TERMINAL_STATUSES:
            raise ValueError("terminal requirements must cover every terminal status exactly")
        self._requirements = MappingProxyType(dict(requirements))

    @property
    def requirements(self) -> Mapping[SagaStatus, _TerminalRequirement]:
        """Return the immutable terminal contract bound to this gate."""
        return self._requirements

    def evaluate(
        self,
        target: SagaStatus,
        snapshot: SagaSnapshot,
        evidence: InvariantEvidence,
        runnable_commands: int,
    ) -> SagaStatus:
        _require_runnable_count(runnable_commands)
        requirement = self._requirement(target)
        self._require_context(target, snapshot, runnable_commands)
        self._require_evidence(target, snapshot, evidence, requirement)
        _require_resolved_compensation(target, snapshot)
        _require_human_exception(target, snapshot, evidence)
        _require_exact_proof(snapshot, evidence)
        return target

    def _require_context(
        self, target: SagaStatus, snapshot: SagaSnapshot, runnable_commands: int
    ) -> None:
        _require_settled_operations(snapshot)
        _require_no_runnable_commands(runnable_commands)
        _require_no_pending_approval(snapshot)
        _require_source(target, snapshot)

    def _requirement(self, target: SagaStatus) -> _TerminalRequirement:
        requirement = self._requirements.get(target)
        if requirement is None:
            raise TerminalStateDenied("requested state is not a configured terminal target")
        return requirement

    def _require_evidence(
        self,
        target: SagaStatus,
        snapshot: SagaSnapshot,
        evidence: InvariantEvidence,
        requirement: _TerminalRequirement,
    ) -> None:
        _require_fresh_evidence(snapshot, evidence)
        _require_evidence_binding(target, snapshot, evidence)
        if evidence.invariant_version != requirement.invariant_version:
            raise TerminalStateDenied("invariant version does not match terminal requirement")
        results = _result_map(evidence)
        _require_rule_set(requirement, results)
        _require_passing_results(results)


__all__ = [
    "AcceptedResidual",
    "AuthoritativeInvariantInput",
    "InvariantEvidence",
    "InvariantProofEventFields",
    "InvariantResult",
    "InvariantRule",
    "TerminalGate",
    "TerminalStateDenied",
    "VerifiedHumanResolution",
    "build_invariant_event_fields",
    "invariant_evidence_digest",
]
