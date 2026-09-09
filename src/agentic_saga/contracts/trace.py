from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
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
from agentic_saga.contracts.runtime import SagaStatus

type _BoundedName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _BoundedText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
type _Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
type _EventId = Annotated[str, StringConstraints(strict=True, pattern=r"^evt_[a-z0-9]{16,64}$")]
type _TraceId = Annotated[str, StringConstraints(strict=True, pattern=r"^trace_[a-z0-9]{16,64}$")]
type _ProofResult = Literal["valid", "invalid"]
_TERMINAL = frozenset(
    {
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    }
)


class TraceAuthority(StrEnum):
    """Identify the authority responsible for each exported trace event."""

    AGENT = "agent"
    POLICY = "policy"
    KERNEL = "kernel"
    EFFECT = "effect"
    COMPENSATION = "compensation"
    PROOF = "proof"
    HUMAN = "human"


class TraceEvent(BaseModel):
    """Represent one redacted, integrity-bound ledger event for audit output."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    event_id: _EventId
    saga_seq: int = Field(strict=True, ge=1)
    recorded_at: AwareDatetime
    authority: TraceAuthority
    event_type: _BoundedName
    actor: _BoundedName
    trace_id: _TraceId
    definition_version: _BoundedName
    fence_token: FenceToken | None
    before_status: SagaStatus | None
    after_status: SagaStatus
    operation_id: OperationId | None = None
    step_instance_id: StepInstanceId | None = None
    direction: Direction | None = None
    semantic_generation: int | None = Field(default=None, strict=True, ge=0)
    attempt: int | None = Field(default=None, strict=True, ge=1)
    tool_name: _BoundedName | None = None
    compensates_operation_id: OperationId | None = None
    redacted_input: JsonObject | None = None
    redacted_output: JsonObject | None = None
    rationale: JsonObject
    policy_decision: JsonObject | None = None
    receipt: JsonObject | None = None
    correlation: _BoundedText | None = None
    input_hash: _Digest | None = None
    output_hash: _Digest | None = None

    @field_validator("recorded_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("trace event time must use UTC")
        return value

    @model_validator(mode="after")
    def require_public_hashes(self) -> TraceEvent:
        _require_hash(self.redacted_input, self.input_hash, "input")
        _require_hash(self.redacted_output, self.output_hash, "output")
        return self


def _require_hash(value: JsonObject | None, digest: str | None, role: str) -> None:
    if value is None and digest is not None:
        raise ValueError(f"{role} hash requires a redacted value")
    if value is not None and digest != sha256_json(value):
        raise ValueError(f"{role} hash must bind the canonical redacted value")


class TraceProof(BaseModel):
    """Reference one ledger-recorded invariant result supporting an outcome."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    source_event_id: _EventId
    source_event_seq: int = Field(strict=True, ge=1)
    invariant_version: _BoundedName
    evaluated_at_seq: int = Field(strict=True, ge=1)
    target_status: SagaStatus
    rule_id: _BoundedName
    inputs: None = None
    result: _ProofResult
    explanation: Literal["ledger_recorded_invariant_result"]


class RunTrace(BaseModel):
    """Package a verified saga history, proofs, and final projection digest."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: _TraceId
    saga_id: SagaId
    definition_version: _BoundedName
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    outcome: SagaStatus
    events: tuple[TraceEvent, ...] = Field(min_length=1, max_length=10_000)
    proofs: tuple[TraceProof, ...] = Field(max_length=10_000)
    final_projection_hash: _Digest

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("trace time must use UTC")
        return value

    @model_validator(mode="after")
    def require_event_sequence(self) -> RunTrace:
        expected = tuple(range(1, len(self.events) + 1))
        if tuple(event.saga_seq for event in self.events) != expected:
            raise ValueError("trace events must be contiguous and ordered")
        _require_trace_identity(self)
        _require_trace_times(self)
        _require_status_chain(self.events)
        _require_proof_sources(self)
        return self


def _require_trace_identity(trace: RunTrace) -> None:
    first, last = trace.events[0], trace.events[-1]
    if (trace.run_id, trace.started_at) != (first.trace_id, first.recorded_at):
        raise ValueError("trace header does not match its first ledger event")
    if any(event.definition_version != trace.definition_version for event in trace.events):
        raise ValueError("trace definition changed within one run")
    if trace.outcome is not last.after_status:
        raise ValueError("trace outcome does not match its final projection")


def _require_trace_times(trace: RunTrace) -> None:
    times = tuple(event.recorded_at for event in trace.events)
    if times != tuple(sorted(times)):
        raise ValueError("trace event timestamps must be monotonic")
    expected = trace.events[-1].recorded_at if trace.outcome in _TERMINAL else None
    if trace.finished_at != expected:
        raise ValueError("trace finish time does not match terminal evidence")


def _require_status_chain(events: tuple[TraceEvent, ...]) -> None:
    if events[0].before_status is not None:
        raise ValueError("first trace event must start without a prior status")
    if any(
        current.after_status is not following.before_status
        for current, following in pairwise(events)
    ):
        raise ValueError("trace before and after statuses do not form one causal chain")


def _require_proof_sources(trace: RunTrace) -> None:
    events = {event.saga_seq: event for event in trace.events}
    for proof in trace.proofs:
        _require_proof_source(proof, events.get(proof.source_event_seq))


def _require_proof_source(proof: TraceProof, source: TraceEvent | None) -> None:
    if source is None or source.event_id != proof.source_event_id:
        raise ValueError("trace proof source event is absent")
    _require_invariant_source(source)
    _require_prior_sequence(proof)


def _require_invariant_source(source: TraceEvent) -> None:
    if source.event_type != "invariant_evaluated":
        raise ValueError("trace proof source is not invariant evidence")


def _require_prior_sequence(proof: TraceProof) -> None:
    if proof.evaluated_at_seq != proof.source_event_seq - 1:
        raise ValueError("trace proof does not bind the prior Saga sequence")


__all__ = ["RunTrace", "TraceAuthority", "TraceEvent", "TraceProof"]
