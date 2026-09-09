from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
)

from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    OperationId,
    SagaId,
    StepInstanceId,
)
from agentic_saga.contracts.runtime import SagaStatus as _SagaStatus

type _BoundedName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _BoundedText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
type _HashDigest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]


class OperationStatus(StrEnum):
    """Describe the strongest durable knowledge about one effect operation."""

    PLANNED = "planned"
    INTENT_DURABLE = "intent_durable"
    DISPATCHED = "dispatched"
    EFFECT_CONFIRMED = "effect_confirmed"
    NO_EFFECT_CONFIRMED = "no_effect_confirmed"
    PARTIAL_EFFECT_CONFIRMED = "partial_effect_confirmed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ObligationStatus(StrEnum):
    """Describe whether a forward effect still requires compensation."""

    ARMED = "armed"
    ELIGIBLE = "eligible"
    IN_PROGRESS = "in_progress"
    SATISFIED = "satisfied"
    NOT_REQUIRED = "not_required"


class OperationRecord(BaseModel):
    """Project one forward or compensation operation from durable ledger evidence."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    operation_id: OperationId
    step_instance_id: StepInstanceId
    direction: Direction
    semantic_generation: int = Field(strict=True, ge=0)
    delivery_attempt: int = Field(strict=True, ge=1)
    tool_name: _BoundedName
    status: OperationStatus
    redacted_command: JsonObject
    command_hash: _HashDigest
    redacted_result: JsonObject | None = None
    result_hash: _HashDigest | None = None
    receipts: tuple[JsonObject, ...] = ()
    correlation: _BoundedText | None = None
    reconciliation_retry: bool = False
    compensates_operation_id: OperationId | None = None


class CompensationObligation(BaseModel):
    """Track the compensation required for one proven forward effect."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    forward_operation_id: OperationId
    compensation_tool_name: _BoundedName
    status: ObligationStatus
    receipts: tuple[JsonObject, ...] = ()
    compensation_operation_id: OperationId | None = None


class SagaSnapshot(BaseModel):
    """Represent the immutable deterministic projection at one ledger sequence."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    saga_id: SagaId
    seq: int = Field(strict=True, ge=1)
    status: _SagaStatus
    definition_version: _BoundedName
    operations: Mapping[OperationId, OperationRecord]
    obligations: Mapping[OperationId, CompensationObligation]
    last_invariant_seq: int | None = Field(default=None, strict=True, ge=1)
    last_invariant_passed: bool | None = None
    last_invariant_target: _SagaStatus | None = None
    last_invariant_version: _BoundedName | None = None
    last_invariant_evidence_digest: _HashDigest | None = None
    pending_approval: bool = False
    resume_status: _SagaStatus | None = None
    consumed_approval_ids: tuple[_BoundedName, ...] = ()

    @field_validator("consumed_approval_ids")
    @classmethod
    def require_unique_approval_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("consumed approval IDs must be unique")
        return value

    @field_validator("operations", mode="after")
    @classmethod
    def freeze_operations(
        cls, value: Mapping[OperationId, OperationRecord]
    ) -> Mapping[OperationId, OperationRecord]:
        return MappingProxyType(dict(value))

    @field_validator("obligations", mode="after")
    @classmethod
    def freeze_obligations(
        cls, value: Mapping[OperationId, CompensationObligation]
    ) -> Mapping[OperationId, CompensationObligation]:
        return MappingProxyType(dict(value))

    @field_serializer("operations")
    def serialize_operations(
        self, value: Mapping[OperationId, OperationRecord]
    ) -> dict[OperationId, OperationRecord]:
        return dict(value)

    @field_serializer("obligations")
    def serialize_obligations(
        self, value: Mapping[OperationId, CompensationObligation]
    ) -> dict[OperationId, CompensationObligation]:
        return dict(value)


__all__ = [
    "CompensationObligation",
    "ObligationStatus",
    "OperationRecord",
    "OperationStatus",
    "SagaSnapshot",
]
