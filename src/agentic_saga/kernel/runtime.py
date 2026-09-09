from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from agentic_saga.contracts.actions import (
    AgentProposal,
    AuthorizedToolCall,
    BeginCompensation,
    Escalate,
    Finish,
    HumanDecision,
    ToolCall,
)
from agentic_saga.contracts.clock import Clock
from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    OperationId,
    SagaId,
    canonical_json,
    sha256_json,
)
from agentic_saga.contracts.events import (
    ApprovalConsumed,
    CompensationIntentRecorded,
    CompensationStarted,
    EffectIntentRecorded,
    HumanRequired,
    HumanResolutionRecorded,
    InvariantEvaluated,
    LedgerEvent,
    ProposalRejected,
    TerminalAssigned,
    TerminalDenied,
)
from agentic_saga.contracts.redaction import (
    RedactionPolicy,
    _is_secret_reference_key,
    _is_versioned_secret_reference,
    redact_json,
)
from agentic_saga.contracts.runtime import SagaStatus
from agentic_saga.contracts.tools import EffectToolDefinition, ToolRegistry
from agentic_saga.kernel.failpoints import (
    DurabilityFailpoint,
    DurabilityPoint,
    NoOpDurabilityFailpoint,
)
from agentic_saga.kernel.identity import framed_sha256
from agentic_saga.kernel.invariants import (
    InvariantEvidence,
    TerminalGate,
    TerminalStateDenied,
    build_invariant_event_fields,
)
from agentic_saga.kernel.policy import (
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    human_resolution_digest,
)
from agentic_saga.kernel.ports import (
    EffectCapabilityProof,
    HumanResolutionAuthenticationFailed,
    HumanResolutionInapplicable,
    KernelStore,
    Lease,
    LeaseLost,
    OutboxCommand,
    StoreConflict,
    TransitionBatch,
    TransitionReceipt,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.state import SagaSnapshot

type _Code = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]*$")]
_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_TERMINAL_STATUSES = frozenset(
    {
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.ABORTED_CLEAN,
        SagaStatus.RESOLVED_WITH_EXCEPTION,
    }
)


class ProposalResult(BaseModel):
    """Durable outcome of submitting one proposal to the kernel."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    accepted: bool
    code: _Code
    saga_seq: int = Field(strict=True, ge=1)
    operation_id: OperationId | None = None


@runtime_checkable
class PolicyContextProvider(Protocol):
    """Build trusted policy context for a proposed transition."""

    def build(
        self, snapshot: SagaSnapshot, proposal: AgentProposal, lease: Lease
    ) -> PolicyContext: ...


@runtime_checkable
class InvariantEvidenceProvider(Protocol):
    """Evaluate application evidence for a requested terminal state."""

    def evaluate(
        self, saga_id: SagaId, snapshot: SagaSnapshot, target_status: SagaStatus
    ) -> InvariantEvidence: ...


class EventMetadata(BaseModel):
    """Record the trusted actor and UTC time for a kernel event."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    actor: str
    recorded_at: datetime


@dataclass(frozen=True)
class EventMetadataFactory:
    """Create UTC event metadata from an injected clock."""

    clock: Clock
    actor: str = "kernel"

    def create(self) -> EventMetadata:
        now = self.clock.now()
        if now.utcoffset() != UTC.utcoffset(now):
            raise ValueError("event clock must return UTC")
        return EventMetadata(actor=self.actor, recorded_at=now)


def _stable_digest(namespace: bytes, *values: str) -> str:
    components = (value.encode("utf-8") for value in values)
    return framed_sha256(namespace, *components)


@dataclass(frozen=True)
class StableIdFactory:
    """Derive deterministic identifiers inside an application namespace."""

    namespace: bytes

    def __post_init__(self) -> None:
        if not self.namespace:
            raise ValueError("stable ID namespace must not be empty")

    def transition_id(self, saga_id: SagaId, proposal_id: str) -> str:
        return f"txn_{_stable_digest(self.namespace, saga_id, proposal_id)}"

    def event_id(self, transition_id: str, index: int) -> str:
        return f"evt_{_stable_digest(self.namespace, transition_id, str(index))}"

    def command_id(self, operation_id: OperationId) -> str:
        return f"cmd_{_stable_digest(self.namespace, operation_id)}"

    def trace_id(self, transition_id: str) -> str:
        return f"trace_{_stable_digest(self.namespace, transition_id)}"


@dataclass(frozen=True)
class _Request:
    saga_id: SagaId
    proposal: AgentProposal
    lease: Lease
    snapshot: SagaSnapshot
    request_digest: str
    transition_id: str
    trace_id: str
    metadata: EventMetadata
    ids: StableIdFactory


def _proposal_json(proposal: AgentProposal) -> JsonObject:
    return _JSON_OBJECT_ADAPTER.validate_python(proposal.model_dump(mode="json"))


def _safe_digest(proposal: AgentProposal, policy: RedactionPolicy) -> str:
    redacted = redact_json(_proposal_json(proposal), policy)
    return sha256_json(redacted)


def _invalid_secret_reference(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_invalid_member(key, item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_invalid_secret_reference(item) for item in value)
    return False


def _invalid_member(key: object, value: object) -> bool:
    if isinstance(key, str) and _is_secret_reference_key(key):
        return not _is_versioned_secret_reference(value)
    return _invalid_secret_reference(value)


def _contains_placeholder(value: object, placeholder: str) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_placeholder(item, placeholder) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_placeholder(item, placeholder) for item in value)
    return value == placeholder


def _effect_capability_proof(
    definition: EffectToolDefinition[BaseModel],
) -> EffectCapabilityProof:
    capabilities = definition.capabilities
    digest = sha256_json(capabilities.model_dump(mode="json"))
    return EffectCapabilityProof(capabilities=capabilities, capability_digest=digest)


def _effect_definition_fields(
    definition: EffectToolDefinition[BaseModel],
) -> dict[str, object]:
    return {
        "definition_version": definition.definition_version,
        "command_schema_version": definition.command_schema_version,
    }


def _event_base(request: _Request, seq: int, index: int) -> dict[str, object]:
    return {
        "event_id": request.ids.event_id(request.transition_id, index),
        "saga_id": request.saga_id,
        "saga_seq": seq,
        "definition_version": request.snapshot.definition_version,
        "fence_token": request.lease.fence_token,
        "actor": request.metadata.actor,
        "trace_id": request.trace_id,
        "recorded_at": request.metadata.recorded_at,
    }


def _reduce_all(snapshot: SagaSnapshot, events: tuple[LedgerEvent, ...]) -> SagaSnapshot:
    projected = snapshot
    for event in events:
        projected = reduce_event(projected, event)
    return projected


def _result_from_receipt(receipt: TransitionReceipt) -> ProposalResult:
    operation = next(
        (event.operation_id for event in receipt.events if hasattr(event, "operation_id")), None
    )
    last = receipt.events[-1]
    accepted = not isinstance(last, (ProposalRejected, TerminalDenied))
    code = _receipt_code(last)
    return ProposalResult(
        accepted=accepted,
        code=code,
        saga_seq=receipt.resulting_seq,
        operation_id=cast(OperationId | None, operation),
    )


def _receipt_code(event: LedgerEvent) -> str:
    if isinstance(event, HumanRequired):
        return "human_required"
    if isinstance(event, TerminalAssigned):
        return "terminal_assigned"
    if isinstance(event, (ProposalRejected, TerminalDenied)):
        return event.reason_code
    return "authorized"


def _human_decision_failure(
    snapshot: SagaSnapshot, saga_id: SagaId, decision: HumanDecision
) -> str | None:
    checks = (
        _used_human_decision(snapshot, decision),
        _missing_human_request(snapshot),
        _stale_human_decision(snapshot, saga_id, decision),
        _invalid_human_decision(decision),
    )
    return next((code for code in checks if code is not None), None)


def _used_human_decision(snapshot: SagaSnapshot, decision: HumanDecision) -> str | None:
    if decision.decision_id in snapshot.consumed_approval_ids:
        return "decision_already_used"
    return None


def _missing_human_request(snapshot: SagaSnapshot) -> str | None:
    pending = snapshot.status is SagaStatus.HUMAN_REQUIRED and snapshot.pending_approval
    return None if pending else "human_decision_not_pending"


def _stale_human_decision(
    snapshot: SagaSnapshot, saga_id: SagaId, decision: HumanDecision
) -> str | None:
    current = decision.saga_id == saga_id and decision.based_on_saga_seq == snapshot.seq
    return None if current else "stale_human_decision"


def _invalid_human_decision(decision: HumanDecision) -> str | None:
    valid = decision.proposal_hash == human_resolution_digest(decision)
    return None if valid else "invalid_human_decision"


def _human_resolution_event(  # noqa: PLR0913, PLR0917
    snapshot: SagaSnapshot,
    decision: HumanDecision,
    lease: Lease,
    transition_id: str,
    metadata: EventMetadata,
    ids: StableIdFactory,
) -> HumanResolutionRecorded:
    event = _human_resolution_payload(snapshot, decision, lease)
    identifiers = {
        "event_id": ids.event_id(transition_id, 0),
        "trace_id": ids.trace_id(transition_id),
        "recorded_at": metadata.recorded_at,
    }
    return HumanResolutionRecorded.model_validate(event | identifiers)


def _human_resolution_payload(
    snapshot: SagaSnapshot, decision: HumanDecision, lease: Lease
) -> dict[str, object]:
    return {
        "saga_id": snapshot.saga_id,
        "saga_seq": snapshot.seq + 1,
        "definition_version": snapshot.definition_version,
        "fence_token": lease.fence_token,
        "actor": decision.actor,
        "decision_id": decision.decision_id,
        "proposal_hash": decision.proposal_hash,
        "verification_result": True,
        "action": decision.action,
    }


def _human_resolution_batch(
    snapshot: SagaSnapshot,
    event: HumanResolutionRecorded,
    lease: Lease,
    transition_id: str,
) -> TransitionBatch:
    return TransitionBatch(
        transition_id=transition_id,
        saga_id=snapshot.saga_id,
        expected_seq=snapshot.seq,
        expected_fence_token=lease.fence_token,
        lease_owner=lease.owner,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )


def _commit_authenticated_resolution(
    store: KernelStore,
    batch: TransitionBatch,
    decision: HumanDecision,
) -> ProposalResult:
    try:
        resolved = store.commit_human_resolution(batch, decision)
    except HumanResolutionAuthenticationFailed:
        return ProposalResult(
            accepted=False,
            code="human_decision_verification_failed",
            saga_seq=batch.expected_seq,
        )
    except HumanResolutionInapplicable:
        return ProposalResult(
            accepted=False,
            code="human_decision_action_inapplicable",
            saga_seq=batch.expected_seq,
        )
    return ProposalResult(accepted=True, code="human_decision_applied", saga_seq=resolved.seq)


class SagaKernel:
    """Authorize and atomically persist one deterministic Saga transition."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        store: KernelStore,
        policy: PolicyEngine,
        registry: ToolRegistry,
        policy_context_provider: PolicyContextProvider,
        event_metadata_factory: EventMetadataFactory,
        id_factory: StableIdFactory,
        redaction_policy: RedactionPolicy | None = None,
        terminal_gate: TerminalGate | None = None,
        invariant_evidence_provider: InvariantEvidenceProvider | None = None,
        failpoint: DurabilityFailpoint | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._registry = registry
        self._contexts = policy_context_provider
        self._metadata = event_metadata_factory
        self._ids = id_factory
        self._redaction = redaction_policy or RedactionPolicy()
        self._terminal_gate = terminal_gate
        self._invariants = invariant_evidence_provider
        self._failpoint = failpoint or NoOpDurabilityFailpoint()

    @property
    def store(self) -> KernelStore:
        return self._store

    @property
    def policy(self) -> PolicyEngine:
        return self._policy

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def redaction_policy(self) -> RedactionPolicy:
        return self._redaction

    def submit_proposal(
        self, saga_id: SagaId, proposal: AgentProposal, lease: Lease
    ) -> ProposalResult:
        digest = _safe_digest(proposal, self._redaction)
        transition_id = self._ids.transition_id(saga_id, proposal.proposal_id)
        snapshot = self._store.load_snapshot(saga_id)
        request = self._request(saga_id, proposal, lease, snapshot, digest, transition_id)
        self._validate_lease(request)
        prior = self._prior_result(transition_id, digest)
        if prior is not None:
            return prior
        return self._submit_new(request)

    def assign_terminal(
        self,
        saga_id: SagaId,
        proposal: Finish,
        lease: Lease,
    ) -> ProposalResult:
        if not isinstance(proposal, Finish):
            return self._invalid_terminal_proposal(saga_id, lease)
        return self.submit_proposal(saga_id, proposal, lease)

    def authorize_read(self, saga_id: SagaId, proposal: ToolCall, lease: Lease) -> ProposalResult:
        digest = _safe_digest(proposal, self._redaction)
        snapshot = self._store.load_snapshot(saga_id)
        transition_id = self._ids.transition_id(saga_id, proposal.proposal_id)
        request = self._request(saga_id, proposal, lease, snapshot, digest, transition_id)
        self._validate_lease(request)
        if proposal.based_on_saga_seq != snapshot.seq:
            return self._reject(request, "stale_proposal")
        if self._proposal_is_unsafe(proposal):
            return self._reject(request, "unsafe_command")
        return self._authorize_read(request, proposal)

    def _authorize_read(self, request: _Request, proposal: ToolCall) -> ProposalResult:
        try:
            context = self._context(request)
        except Exception:
            return self._reject(request, "policy_callback_error")
        decision = self._policy.authorize_read(proposal, request.snapshot, context)
        if not decision.allowed:
            return self._reject(request, decision.code)
        return ProposalResult(accepted=True, code="read_authorized", saga_seq=request.snapshot.seq)

    def apply_human_decision(
        self, saga_id: SagaId, decision: HumanDecision, lease: Lease
    ) -> ProposalResult:
        snapshot = self._store.load_snapshot(saga_id)
        self._validate_lease_identity(saga_id, lease, self._metadata.create().recorded_at)
        failure = _human_decision_failure(snapshot, saga_id, decision)
        if failure is not None:
            return ProposalResult(accepted=False, code=failure, saga_seq=snapshot.seq)
        return self._commit_human_decision(snapshot, decision, lease)

    def _commit_human_decision(
        self, snapshot: SagaSnapshot, decision: HumanDecision, lease: Lease
    ) -> ProposalResult:
        transition_id = self._ids.transition_id(snapshot.saga_id, decision.decision_id)
        metadata = self._metadata.create()
        event = _human_resolution_event(
            snapshot, decision, lease, transition_id, metadata, self._ids
        )
        batch = _human_resolution_batch(snapshot, event, lease, transition_id)
        return _commit_authenticated_resolution(self._store, batch, decision)

    def _invalid_terminal_proposal(self, saga_id: SagaId, lease: Lease) -> ProposalResult:
        snapshot = self._store.load_snapshot(saga_id)
        self._validate_lease_identity(saga_id, lease, self._metadata.create().recorded_at)
        return ProposalResult(
            accepted=False, code="invalid_terminal_proposal", saga_seq=snapshot.seq
        )

    def _prior_result(self, transition_id: str, digest: str) -> ProposalResult | None:
        receipt = self._store.lookup_transition_receipt(transition_id)
        if receipt is None:
            return None
        if receipt.request_digest != digest:
            raise StoreConflict("proposal identity was reused with changed content")
        return _result_from_receipt(receipt)

    def _submit_new(self, request: _Request) -> ProposalResult:
        if request.proposal.based_on_saga_seq != request.snapshot.seq:
            committed = self._prior_result(request.transition_id, request.request_digest)
            if committed is not None:
                return committed
            return ProposalResult(
                accepted=False, code="stale_proposal", saga_seq=request.snapshot.seq
            )
        if request.snapshot.status in _TERMINAL_STATUSES:
            return self._immutable_result(request.snapshot)
        return self._decide(request)

    @staticmethod
    def _immutable_result(snapshot: SagaSnapshot) -> ProposalResult:
        return ProposalResult(accepted=False, code="immutable_saga", saga_seq=snapshot.seq)

    def _request(  # noqa: PLR0913, PLR0917
        self,
        saga_id: SagaId,
        proposal: AgentProposal,
        lease: Lease,
        snapshot: SagaSnapshot,
        digest: str,
        transition_id: str,
    ) -> _Request:
        trace_id = self._ids.trace_id(transition_id)
        return _Request(
            saga_id,
            proposal,
            lease,
            snapshot,
            digest,
            transition_id,
            trace_id,
            self._metadata.create(),
            self._ids,
        )

    def _validate_lease(self, request: _Request) -> None:
        self._validate_lease_identity(request.saga_id, request.lease, request.metadata.recorded_at)

    def _validate_lease_identity(
        self, saga_id: SagaId, lease: Lease, recorded_at: datetime
    ) -> None:
        current = self._store.lease_state(saga_id)
        identity = (
            None if current is None else (current.owner, current.fence_token, current.expires_at)
        )
        supplied = (lease.owner, lease.fence_token, lease.expires_at)
        if lease.saga_id != saga_id or identity != supplied:
            raise LeaseLost("proposal lease identity is stale")
        if lease.expires_at <= recorded_at:
            raise LeaseLost("proposal lease has expired")

    def _context(self, request: _Request) -> PolicyContext:
        context = self._contexts.build(request.snapshot, request.proposal, request.lease)
        values = context.model_dump() | {
            "fence_token": request.lease.fence_token,
            "used_approval_ids": request.snapshot.consumed_approval_ids,
        }
        return PolicyContext.model_validate(values)

    def _decide(self, request: _Request) -> ProposalResult:
        if self._proposal_is_unsafe(request.proposal):
            return self._reject(request, "unsafe_command")
        try:
            context = self._context(request)
        except Exception:
            return self._reject(request, "policy_callback_error")
        decision = self._policy.authorize(request.proposal, request.snapshot, context)
        if not decision.allowed:
            return self._reject(request, decision.code)
        return self._accept(request, decision)

    def _accept(self, request: _Request, decision: PolicyDecision) -> ProposalResult:
        if isinstance(request.proposal, ToolCall):
            return self._accept_tool(request, decision)
        if isinstance(request.proposal, BeginCompensation):
            return self._begin_compensation(request)
        if isinstance(request.proposal, Escalate):
            return self._escalate(request)
        if isinstance(request.proposal, Finish):
            return self._assign_terminal(request, SagaStatus(request.proposal.target_status))
        return self._reject(request, "unsupported_control")

    def _begin_compensation(self, request: _Request) -> ProposalResult:
        proposal = cast(BeginCompensation, request.proposal)
        evidence = {
            "proposal_id": proposal.proposal_id,
            "proposal_hash": request.request_digest,
            "reason_code": proposal.reason_code,
        }
        payload = _event_base(request, request.snapshot.seq + 1, 0) | evidence
        event = CompensationStarted.model_validate(payload)
        batch = self._batch(request, (event,), ())
        snapshot = self._store.commit_proposal(batch, request.request_digest)
        return ProposalResult(accepted=True, code="compensation_started", saga_seq=snapshot.seq)

    def _proposal_is_unsafe(self, proposal: AgentProposal) -> bool:
        return isinstance(proposal, ToolCall) and self._unsafe_command(proposal.arguments)

    def _assign_terminal(self, request: _Request, target: SagaStatus) -> ProposalResult:
        try:
            evidence = self._evaluate_invariants(request, target)
        except Exception:
            return self._terminal_denied(request, target, "invariant_provider_error", None)
        if not self._evidence_matches(request, target, evidence):
            return self._terminal_denied(request, target, "invalid_invariant_evidence", None)
        try:
            return self._gate_terminal(request, target, evidence)
        except TerminalStateDenied:
            return self._terminal_denied(request, target, "invalid_invariant_evidence", None)

    def _evaluate_invariants(self, request: _Request, target: SagaStatus) -> InvariantEvidence:
        if self._invariants is None:
            raise RuntimeError("invariant evidence provider is not configured")
        evidence = self._invariants.evaluate(request.saga_id, request.snapshot, target)
        return InvariantEvidence.model_validate(evidence)

    @staticmethod
    def _evidence_matches(
        request: _Request, target: SagaStatus, evidence: InvariantEvidence
    ) -> bool:
        return (
            evidence.saga_id == request.saga_id
            and evidence.definition_version == request.snapshot.definition_version
            and evidence.evaluated_at_seq == request.snapshot.seq
            and evidence.target_status is target
        )

    def _gate_terminal(
        self, request: _Request, target: SagaStatus, evidence: InvariantEvidence
    ) -> ProposalResult:
        proof = self._invariant_event(request, evidence)
        proven = reduce_event(request.snapshot, proof)
        try:
            self._authorize_terminal(target, proven, evidence, request.saga_id)
        except TerminalStateDenied:
            return self._terminal_denied(request, target, "terminal_gate_denied", proof)
        return self._commit_terminal(request, target, proof, proven)

    def _authorize_terminal(
        self,
        target: SagaStatus,
        snapshot: SagaSnapshot,
        evidence: InvariantEvidence,
        saga_id: SagaId,
    ) -> None:
        if self._terminal_gate is None:
            raise TerminalStateDenied("terminal gate is not configured")
        runnable = self._store.runnable_count(saga_id)
        self._terminal_gate.evaluate(target, snapshot, evidence, runnable)

    def _invariant_event(
        self, request: _Request, evidence: InvariantEvidence
    ) -> InvariantEvaluated:
        fields = build_invariant_event_fields(evidence)
        payload = _event_base(request, request.snapshot.seq + 1, 0) | fields.model_dump()
        return InvariantEvaluated.model_validate(payload)

    def _commit_terminal(
        self,
        request: _Request,
        target: SagaStatus,
        proof: InvariantEvaluated,
        proven: SagaSnapshot,
    ) -> ProposalResult:
        event = TerminalAssigned.model_validate(
            _event_base(request, proven.seq + 1, 1) | {"status": target.value}
        )
        batch = self._batch(request, (proof, event), ())
        snapshot = self._store.commit_terminal(batch, request.request_digest)
        return ProposalResult(accepted=True, code="terminal_assigned", saga_seq=snapshot.seq)

    def _terminal_denied(
        self,
        request: _Request,
        target: SagaStatus,
        code: str,
        proof: InvariantEvaluated | None,
    ) -> ProposalResult:
        index = int(proof is not None)
        event = self._terminal_denial_event(request, target, code, index)
        events: tuple[LedgerEvent, ...] = (event,) if proof is None else (proof, event)
        batch = self._batch(request, events, ())
        snapshot = self._store.commit_proposal(batch, request.request_digest)
        return ProposalResult(accepted=False, code=code, saga_seq=snapshot.seq)

    def _terminal_denial_event(
        self, request: _Request, target: SagaStatus, code: str, index: int
    ) -> TerminalDenied:
        payload = _event_base(request, request.snapshot.seq + index + 1, index) | {
            "proposal_id": request.proposal.proposal_id,
            "proposal_hash": request.request_digest,
            "target_status": target.value,
            "reason_code": code,
        }
        return TerminalDenied.model_validate(payload)

    def _escalate(self, request: _Request) -> ProposalResult:
        proposal = cast(Escalate, request.proposal)
        payload = _event_base(request, request.snapshot.seq + 1, 0) | {
            "reason_code": proposal.reason_code,
        }
        event = HumanRequired.model_validate(payload)
        batch = self._batch(request, (event,), ())
        snapshot = self._store.suspend_for_human(batch, request.request_digest)
        return ProposalResult(accepted=True, code="human_required", saga_seq=snapshot.seq)

    def _reject(self, request: _Request, code: str) -> ProposalResult:
        event = self._rejection_event(request, code)
        batch = self._batch(request, (event,), ())
        snapshot = self._store.commit_proposal(batch, request.request_digest)
        return ProposalResult(accepted=False, code=code, saga_seq=snapshot.seq)

    def _rejection_event(self, request: _Request, code: str) -> ProposalRejected:
        payload = _event_base(request, request.snapshot.seq + 1, 0) | {
            "proposal_id": request.proposal.proposal_id,
            "proposal_hash": request.request_digest,
            "reason_code": code,
        }
        return ProposalRejected.model_validate(payload)

    def _accept_tool(self, request: _Request, decision: PolicyDecision) -> ProposalResult:
        call = decision.authorized_call
        if call is None:
            return self._reject(request, "missing_authorized_call")
        command = _JSON_OBJECT_ADAPTER.validate_python(call.command.model_dump(mode="json"))
        if self._unsafe_command(command):
            return self._reject(request, "unsafe_command")
        return self._commit_tool(request, decision, command)

    def _unsafe_command(self, command: JsonObject) -> bool:
        if _invalid_secret_reference(command):
            return True
        if _contains_placeholder(command, self._redaction.placeholder):
            return True
        redacted = redact_json(command, self._redaction)
        return canonical_json(redacted) != canonical_json(command)

    def _commit_tool(
        self, request: _Request, decision: PolicyDecision, command: JsonObject
    ) -> ProposalResult:
        events = self._tool_events(request, decision, command)
        outbox = (self._outbox(request, decision, command),)
        batch = self._batch(request, events, outbox)
        call = decision.authorized_call
        if call is None:
            raise StoreConflict("authorized decision has no typed call")
        snapshot = self._store.commit_proposal(batch, request.request_digest)
        self._after_intent(call.direction)
        operation_id = decision.authorized_call.operation_id if decision.authorized_call else None
        return ProposalResult(
            accepted=True, code="authorized", saga_seq=snapshot.seq, operation_id=operation_id
        )

    def _after_intent(self, direction: Direction) -> None:
        point = DurabilityPoint.AFTER_INTENT_COMMIT
        if direction is Direction.COMPENSATION:
            point = DurabilityPoint.AFTER_COMPENSATION_INTENT
        self._failpoint.hit(point)

    def _tool_events(
        self, request: _Request, decision: PolicyDecision, command: JsonObject
    ) -> tuple[LedgerEvent, ...]:
        approval = self._approval_event(request, decision)
        index = int(approval is not None)
        intent = self._intent_event(request, decision, command, index)
        return (intent,) if approval is None else (approval, intent)

    def _approval_event(
        self, request: _Request, decision: PolicyDecision
    ) -> ApprovalConsumed | None:
        consumed = decision.consumed_approval
        if consumed is None:
            return None
        payload = _event_base(request, request.snapshot.seq + 1, 0) | consumed.model_dump()
        return ApprovalConsumed.model_validate(payload)

    def _intent_event(
        self, request: _Request, decision: PolicyDecision, command: JsonObject, index: int
    ) -> LedgerEvent:
        call = decision.authorized_call
        if call is None:
            raise StoreConflict("authorized decision has no typed call")
        payload = self._intent_payload(request, decision, command, index)
        if call.direction.value == "compensation":
            return CompensationIntentRecorded.model_validate(payload)
        return EffectIntentRecorded.model_validate(payload)

    def _intent_payload(
        self, request: _Request, decision: PolicyDecision, command: JsonObject, index: int
    ) -> dict[str, object]:
        call = decision.authorized_call
        if call is None:
            raise StoreConflict("authorized decision has no typed call")
        definition = self._effect_definition(request.proposal)
        fields = self._effect_fields(request, call, command, index)
        if call.direction.value == "compensation":
            return fields | self._compensation_intent_fields(request, decision)
        return fields | {"compensate_with": definition.compensate_with}

    def _compensation_intent_fields(
        self, request: _Request, decision: PolicyDecision
    ) -> dict[str, object]:
        identity = decision.effect_identity
        target = None if identity is None else identity.compensates_operation_id
        receipts = self._compensation_receipts(request.snapshot, target)
        return {"compensates_operation_id": target, "forward_receipts": receipts}

    @staticmethod
    def _compensation_receipts(
        snapshot: SagaSnapshot, target: OperationId | None
    ) -> tuple[JsonObject, ...]:
        if target is None or target not in snapshot.obligations:
            raise StoreConflict("compensation target has no durable obligation")
        receipts = snapshot.obligations[target].receipts
        if not receipts:
            raise StoreConflict("compensation target has no exact receipts")
        return receipts

    def _effect_fields(
        self,
        request: _Request,
        call: AuthorizedToolCall[BaseModel],
        command: JsonObject,
        index: int,
    ) -> dict[str, object]:
        base = _event_base(request, request.snapshot.seq + index + 1, index)
        proposal = cast(ToolCall, request.proposal)
        return base | {
            "operation_id": call.operation_id,
            "step_instance_id": call.step_instance_id,
            "direction": call.direction,
            "semantic_generation": call.semantic_generation,
            "delivery_attempt": 1,
            "tool_name": proposal.tool_name,
            "redacted_command": command,
            "command_hash": sha256_json(command),
        }

    def _effect_definition(self, proposal: AgentProposal) -> EffectToolDefinition[BaseModel]:
        if not isinstance(proposal, ToolCall):
            raise StoreConflict("control proposal has no effect definition")
        definition = self._registry.definition(proposal.tool_name)
        if not isinstance(definition, EffectToolDefinition):
            raise StoreConflict("authorized tool is not an effect")
        return definition

    def _outbox(
        self, request: _Request, decision: PolicyDecision, command: JsonObject
    ) -> OutboxCommand:
        call = decision.authorized_call
        if call is None:
            raise StoreConflict("authorized decision has no typed call")
        proposal = cast(ToolCall, request.proposal)
        payload = self._outbox_payload(request, call, proposal, command)
        return OutboxCommand.model_validate(payload)

    def _outbox_payload(
        self,
        request: _Request,
        call: AuthorizedToolCall[BaseModel],
        proposal: ToolCall,
        command: JsonObject,
    ) -> dict[str, object]:
        definition = self._effect_definition(proposal)
        return {
            "command_id": self._ids.command_id(call.operation_id),
            "saga_id": request.saga_id,
            "operation_id": call.operation_id,
            "tool_name": proposal.tool_name,
            **_effect_definition_fields(definition),
            "step_instance_id": call.step_instance_id,
            "direction": call.direction,
            "semantic_generation": call.semantic_generation,
            "command": command,
            "command_hash": sha256_json(command),
            "available_at": request.metadata.recorded_at,
            "capability_proof": _effect_capability_proof(definition),
        }

    def _batch(
        self,
        request: _Request,
        events: tuple[LedgerEvent, ...],
        outbox: tuple[OutboxCommand, ...],
    ) -> TransitionBatch:
        return TransitionBatch(
            transition_id=request.transition_id,
            saga_id=request.saga_id,
            expected_seq=request.snapshot.seq,
            expected_fence_token=request.lease.fence_token,
            lease_owner=request.lease.owner,
            events=events,
            projection=_reduce_all(request.snapshot, events),
            outbox_commands=outbox,
        )


__all__ = [
    "EventMetadata",
    "EventMetadataFactory",
    "InvariantEvidenceProvider",
    "PolicyContextProvider",
    "ProposalResult",
    "SagaKernel",
    "StableIdFactory",
]
