"""Typed contracts for durable kernel storage adapters."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol, runtime_checkable

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from agentic_saga.contracts.actions import HumanDecision
from agentic_saga.contracts.common import (
    Direction,
    FenceToken,
    JsonObject,
    OperationId,
    SagaId,
    StepInstanceId,
    sha256_json,
)
from agentic_saga.contracts.events import LedgerEvent, SagaCreated
from agentic_saga.contracts.tools import ToolCapabilities
from agentic_saga.kernel.state import SagaSnapshot

type _CommandId = Annotated[str, StringConstraints(strict=True, pattern=r"^cmd_[a-z0-9]{16,64}$")]
type _ClaimId = Annotated[str, StringConstraints(strict=True, pattern=r"^claim_[a-f0-9]{32}$")]
type _TransitionId = Annotated[
    str, StringConstraints(strict=True, pattern=r"^txn_[a-z0-9]{16,64}$")
]
type _BoundedName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
type _HashDigest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
type _JobId = Annotated[str, StringConstraints(strict=True, pattern=r"^recon_[a-f0-9]{64}$")]


class StoreError(RuntimeError):
    """Base class for durable store failures."""


class StoreConflict(StoreError):
    """Raised when an identity, claim, or compare-and-swap conflicts."""


class ProposalIdentityConflict(StoreConflict):
    """Raised when one proposal identity is reused for different content."""


class StaleFence(StoreConflict):
    """Raised when a writer presents an obsolete Saga fence token."""


class LeaseUnavailable(StoreConflict):
    """Raised when another live owner holds the Saga lease."""


class LeaseLost(StaleFence):
    """Raised when a lease operation presents an expired or stale identity."""


class StoreCorruption(StoreError):
    """Raised when durable bytes do not verify against their proofs."""


class RecoveryProofExpired(StoreConflict):
    """Raised when a retry proof expires before its commit transaction."""


class HumanResolutionInapplicable(StoreConflict):
    """Raised when an authenticated action has no exact durable recovery target."""


class HumanResolutionAuthenticationFailed(StoreConflict):
    """Raised when the trusted store verifier rejects an ephemeral human decision."""


class HumanResolutionVerifier(Protocol):
    """Authenticate and validate a human decision against current saga state."""

    def __call__(self, decision: HumanDecision, snapshot: SagaSnapshot) -> bool: ...


class StoreFailpoint(StrEnum):
    """Name transaction boundaries available to storage crash tests."""

    BEFORE_EVENT_INSERT = "before_event_insert"
    AFTER_EVENT_INSERT = "after_event_insert"
    BEFORE_OUTBOX_INSERT = "before_outbox_insert"
    AFTER_OUTBOX_INSERT = "after_outbox_insert"
    BEFORE_PROJECTION_UPDATE = "before_projection_update"
    AFTER_PROJECTION_UPDATE = "after_projection_update"
    BEFORE_RECEIPT_INSERT = "before_receipt_insert"
    AFTER_RECEIPT_INSERT = "after_receipt_insert"
    BEFORE_OUTBOX_UPDATE = "before_outbox_update"
    AFTER_OUTBOX_UPDATE = "after_outbox_update"
    BEFORE_RECONCILIATION_JOB_UPDATE = "before_reconciliation_job_update"
    AFTER_RECONCILIATION_JOB_UPDATE = "after_reconciliation_job_update"
    BEFORE_LEASE_UPDATE = "before_lease_update"
    AFTER_LEASE_UPDATE = "after_lease_update"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_RETURN = "after_commit_before_return"


class InjectedStoreFailure(StoreError):
    """Signal an intentional storage failure at a named transaction boundary."""

    def __init__(self, point: StoreFailpoint) -> None:
        super().__init__(f"injected store failure at {point.value}")
        self.point = point


@runtime_checkable
class Failpoint(Protocol):
    """Inject a controlled failure at a storage transaction boundary."""

    def hit(self, point: StoreFailpoint) -> None: ...


@runtime_checkable
class ClaimIdFactory(Protocol):
    """Create a unique identifier for one durable work claim."""

    def __call__(self) -> str: ...


class NoOpFailpoint:
    """Allow every storage transaction boundary to proceed normally."""

    def hit(self, point: StoreFailpoint) -> None:
        del point


class OutboxState(StrEnum):
    """Describe the durable delivery state of an effect command."""

    RUNNABLE = "runnable"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    PARKED = "parked"
    SUSPENDED_FOR_HUMAN = "suspended_for_human"


class ReconciliationJobState(StrEnum):
    """Describe the durable lifecycle of an uncertain-outcome recovery job."""

    DUE = "due"
    WAITING = "waiting"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    REQUEUED = "requeued"
    HUMAN_REQUIRED = "human_required"


class RecoveryPolicy(BaseModel):
    """Persist the exact retry, operator, and clock-skew recovery horizon."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    maximum_retry_delay_microseconds: int = Field(strict=True, gt=0)
    operator_response_window_microseconds: int = Field(strict=True, gt=0)
    clock_skew_allowance_microseconds: int = Field(strict=True, ge=0)

    @property
    def total(self) -> timedelta:
        return timedelta(
            microseconds=self.maximum_retry_delay_microseconds
            + self.operator_response_window_microseconds
            + self.clock_skew_allowance_microseconds
        )


class EffectCapabilityProof(BaseModel):
    """Bind dispatch semantics to the exact capabilities declared by a tool."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    capabilities: ToolCapabilities
    capability_digest: _HashDigest

    @model_validator(mode="after")
    def require_exact_digest(self) -> EffectCapabilityProof:
        payload = self.capabilities.model_dump(mode="json")
        if sha256_json(payload) != self.capability_digest:
            raise ValueError("capability digest does not match capabilities")
        return self


class ReconciliationJob(BaseModel):
    """Track fenced, claimable recovery work for one uncertain operation."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    job_id: _JobId
    command_id: _CommandId
    saga_id: SagaId
    operation_id: OperationId
    state: ReconciliationJobState
    due_at: AwareDatetime
    first_dispatch_at: AwareDatetime
    claim_id: _ClaimId | None = None
    claim_owner: _BoundedName | None = None
    claim_expires_at: AwareDatetime | None = None
    claim_generation: int = Field(strict=True, ge=0)
    claim_fence_token: int = Field(strict=True, ge=0)
    recovery_policy: RecoveryPolicy | None = None
    recovery_policy_digest: _HashDigest | None = None
    claimed_at: AwareDatetime | None = None
    lookup_attempt: int = Field(default=0, strict=True, ge=0)
    lookup_started_at: AwareDatetime | None = None

    @field_validator(
        "due_at", "first_dispatch_at", "claim_expires_at", "claimed_at", "lookup_started_at"
    )
    @classmethod
    def require_job_time_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, "reconciliation job time")


class TransitionKind(StrEnum):
    """Classify atomic ledger transitions for idempotent receipt handling."""

    STANDARD = "standard"
    PROPOSAL = "proposal"
    HUMAN_SUSPENSION = "human_suspension"
    HUMAN_RESOLUTION = "human_resolution"
    TERMINAL = "terminal"
    RECONCILIATION = "reconciliation"


class OutboxCommand(BaseModel):
    """Persist a canonical effect command for fenced, retryable delivery."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    command_id: _CommandId
    saga_id: SagaId
    operation_id: OperationId
    tool_name: _BoundedName
    definition_version: _BoundedName
    command_schema_version: _BoundedName
    step_instance_id: StepInstanceId
    direction: Direction
    semantic_generation: int = Field(strict=True, ge=0)
    command: JsonObject
    command_hash: _HashDigest
    available_at: AwareDatetime
    capability_proof: EffectCapabilityProof | None = None

    @field_validator("available_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "available_at")


class ClaimMetadata(BaseModel):
    """Carry the owner, expiry, generation, and fence for a work claim."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    claim_id: _ClaimId
    claim_owner: _BoundedName
    claim_expires_at: AwareDatetime
    claim_generation: int = Field(strict=True, ge=1)
    delivery_attempt: int = Field(strict=True, ge=1)
    saga_fence_token: int = Field(strict=True, ge=0)

    @field_validator("claim_expires_at")
    @classmethod
    def require_claim_expiry_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "claim_expires_at")


class ClaimedCommand(BaseModel):
    """Pair an outbox command with the authority required to dispatch it."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    envelope: OutboxCommand
    claim: ClaimMetadata

    @property
    def command_id(self) -> str:
        return self.envelope.command_id

    @property
    def saga_id(self) -> SagaId:
        return self.envelope.saga_id

    @property
    def operation_id(self) -> OperationId:
        return self.envelope.operation_id

    @property
    def tool_name(self) -> str:
        return self.envelope.tool_name

    @property
    def definition_version(self) -> str:
        return self.envelope.definition_version

    @property
    def command_schema_version(self) -> str:
        return self.envelope.command_schema_version

    @property
    def step_instance_id(self) -> StepInstanceId:
        return self.envelope.step_instance_id

    @property
    def direction(self) -> Direction:
        return self.envelope.direction

    @property
    def semantic_generation(self) -> int:
        return self.envelope.semantic_generation

    @property
    def command(self) -> JsonObject:
        return self.envelope.command

    @property
    def command_hash(self) -> str:
        return self.envelope.command_hash

    @property
    def available_at(self) -> datetime:
        return self.envelope.available_at

    @property
    def claim_id(self) -> str:
        return self.claim.claim_id

    @property
    def claim_owner(self) -> str:
        return self.claim.claim_owner

    @property
    def claim_expires_at(self) -> datetime:
        return self.claim.claim_expires_at

    @property
    def claim_generation(self) -> int:
        return self.claim.claim_generation

    @property
    def delivery_attempt(self) -> int:
        return self.claim.delivery_attempt

    @property
    def saga_fence_token(self) -> int:
        return self.claim.saga_fence_token


class TransitionBatch(BaseModel):
    """Atomically commit ledger events, projection state, and outbox commands."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    transition_id: _TransitionId
    saga_id: SagaId
    expected_seq: int = Field(strict=True, ge=0)
    expected_fence_token: int = Field(strict=True, ge=0)
    lease_owner: _BoundedName | None = None
    events: tuple[LedgerEvent, ...] = Field(min_length=1, max_length=100)
    projection: SagaSnapshot
    outbox_commands: tuple[OutboxCommand, ...] = Field(default=(), max_length=100)


class TransitionReceipt(BaseModel):
    """Return durable evidence for an idempotently committed transition."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    transition_id: _TransitionId
    saga_id: SagaId
    resulting_seq: int = Field(strict=True, ge=1)
    request_digest: _HashDigest | None
    kind: TransitionKind
    events: tuple[LedgerEvent, ...]
    projection: SagaSnapshot


class Lease(BaseModel):
    """The exact durable identity of a live Saga execution lease."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    saga_id: SagaId
    owner: _BoundedName
    fence_token: FenceToken
    expires_at: AwareDatetime

    @field_validator("expires_at")
    @classmethod
    def require_expiry_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "expires_at")


class UnwindToolEvidence(BaseModel):
    """Public historical effect identity needed to decide safe unwind work."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    operation_id: OperationId
    tool_name: _BoundedName
    definition_version: _BoundedName
    command_schema_version: _BoundedName
    capability_digest: _HashDigest | None = None


class UnwindQuiescence(BaseModel):
    """A lease-bound snapshot of durable forward-work blockers."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    saga_id: SagaId
    saga_seq: int = Field(strict=True, ge=1)
    fence_token: FenceToken
    forward_outbox_blockers: tuple[OperationId, ...] = ()
    unresolved_forward_blockers: tuple[OperationId, ...] = ()
    reconciliation_blockers: tuple[OperationId, ...] = ()
    tool_evidence: tuple[UnwindToolEvidence, ...] = ()
    historical_tool_blockers: tuple[OperationId, ...] = ()

    @property
    def safe(self) -> bool:
        return not (
            self.forward_outbox_blockers
            or self.unresolved_forward_blockers
            or self.reconciliation_blockers
            or self.historical_tool_blockers
        )


class ConnectionSettings(BaseModel):
    """Report the SQLite safety settings validated for a kernel store."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    foreign_keys: bool
    journal_mode: str
    synchronous: int
    busy_timeout_ms: int
    schema_version: int


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must use UTC")
    return value


@runtime_checkable
class SagaStorage(Protocol):
    """Persist saga transitions and projections with sequence and fence checks."""

    def create_saga(self, first_event: SagaCreated) -> SagaSnapshot: ...
    def commit_transition(self, batch: TransitionBatch) -> SagaSnapshot: ...
    def commit_proposal(self, batch: TransitionBatch, request_digest: str) -> SagaSnapshot: ...
    def suspend_for_human(self, batch: TransitionBatch, request_digest: str) -> SagaSnapshot: ...
    def commit_human_resolution(
        self, batch: TransitionBatch, decision: HumanDecision
    ) -> SagaSnapshot: ...
    def commit_terminal(self, batch: TransitionBatch, request_digest: str) -> SagaSnapshot: ...
    def lookup_transition_receipt(self, transition_id: str) -> TransitionReceipt | None: ...
    def load_snapshot(self, saga_id: SagaId) -> SagaSnapshot: ...
    def read_events(self, saga_id: SagaId) -> tuple[LedgerEvent, ...]: ...


@runtime_checkable
class OutboxStorage(Protocol):
    """Claim and settle effect commands while enforcing dispatch authority."""

    def claim_outbox(self, owner: str, lease_duration: timedelta) -> ClaimedCommand | None: ...
    def claim_outbox_for_saga(
        self, saga_id: SagaId, owner: str, lease_duration: timedelta
    ) -> ClaimedCommand | None: ...
    def release_outbox(self, claimed: ClaimedCommand) -> None: ...
    def validate_dispatch_authority(self, claimed: ClaimedCommand, lease: Lease) -> None: ...
    def start_dispatch(self, claimed: ClaimedCommand, batch: TransitionBatch) -> SagaSnapshot: ...
    def abort_dispatch(self, claimed: ClaimedCommand, batch: TransitionBatch) -> SagaSnapshot: ...
    def complete_outbox(self, claimed: ClaimedCommand, batch: TransitionBatch) -> SagaSnapshot: ...
    def park_outbox(self, claimed: ClaimedCommand, batch: TransitionBatch) -> SagaSnapshot: ...
    def runnable_count(self, saga_id: SagaId) -> int: ...
    def load_outbox(self, command_id: str) -> OutboxCommand: ...


@runtime_checkable
class ReconciliationStorage(Protocol):
    """Claim and commit fenced recovery work for uncertain effects."""

    def claim_reconciliation(
        self,
        owner: str,
        lease_duration: timedelta,
        recovery_policy: RecoveryPolicy,
        recovery_policy_digest: str,
    ) -> ReconciliationJob | None: ...
    def claim_reconciliation_frozen(
        self, owner: str, lease_duration: timedelta
    ) -> ReconciliationJob | None: ...
    def reconciliation_job(self, operation_id: OperationId) -> ReconciliationJob | None: ...
    def bind_reconciliation_claim(
        self, job: ReconciliationJob, lease: Lease
    ) -> ReconciliationJob: ...
    def validate_reconciliation_authority(self, job: ReconciliationJob, lease: Lease) -> None: ...
    def reconciliation_requires_human_followup(
        self, job: ReconciliationJob, lease: Lease
    ) -> bool: ...
    def begin_reconciliation_lookup(
        self, job: ReconciliationJob, lease: Lease
    ) -> ReconciliationJob: ...
    def release_reconciliation(self, job: ReconciliationJob) -> None: ...
    def commit_reconciliation(
        self,
        job: ReconciliationJob,
        lease: Lease,
        batch: TransitionBatch,
        action: str,
        check_after: datetime | None,
    ) -> SagaSnapshot: ...


@runtime_checkable
class LeaseStorage(Protocol):
    """Issue, renew, release, and inspect fenced saga execution leases."""

    def acquire_lease(self, saga_id: SagaId, owner: str, duration: timedelta) -> Lease: ...
    def renew_lease(self, lease: Lease, duration: timedelta) -> Lease: ...
    def release_lease(self, lease: Lease) -> None: ...
    def lease_state(self, saga_id: SagaId) -> Lease | None: ...


@runtime_checkable
class RecoveryStorage(Protocol):
    """Verify durable state integrity and create consistent recovery backups."""

    def rebuild_and_verify(self, saga_id: SagaId) -> SagaSnapshot: ...
    def integrity_check(self) -> str: ...
    def foreign_key_check(self) -> Sequence[str]: ...
    def backup_to(self, destination: Path) -> None: ...


@runtime_checkable
class UnwindStorage(Protocol):
    """Provide fenced quiescence evidence before emergency unwind decisions."""

    def inspect_unwind_quiescence(self, saga_id: SagaId, lease: Lease) -> UnwindQuiescence: ...


@runtime_checkable
class KernelStore(
    SagaStorage,
    OutboxStorage,
    ReconciliationStorage,
    LeaseStorage,
    RecoveryStorage,
    UnwindStorage,
    Protocol,
):
    """Composed durable kernel storage boundary."""


__all__ = [
    "ClaimIdFactory",
    "ClaimMetadata",
    "ClaimedCommand",
    "ConnectionSettings",
    "EffectCapabilityProof",
    "Failpoint",
    "HumanResolutionAuthenticationFailed",
    "HumanResolutionInapplicable",
    "HumanResolutionVerifier",
    "InjectedStoreFailure",
    "KernelStore",
    "Lease",
    "LeaseLost",
    "LeaseStorage",
    "LeaseUnavailable",
    "NoOpFailpoint",
    "OutboxCommand",
    "OutboxState",
    "OutboxStorage",
    "ReconciliationJob",
    "ReconciliationJobState",
    "ReconciliationStorage",
    "RecoveryPolicy",
    "RecoveryProofExpired",
    "RecoveryStorage",
    "SagaStorage",
    "StaleFence",
    "StoreConflict",
    "StoreCorruption",
    "StoreError",
    "StoreFailpoint",
    "TransitionBatch",
    "TransitionKind",
    "TransitionReceipt",
    "UnwindQuiescence",
    "UnwindStorage",
    "UnwindToolEvidence",
]
