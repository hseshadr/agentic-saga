from _thread import LockType
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Lock, get_ident
from typing import Literal

import pytest
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, TypeAdapter

from agentic_saga.contracts.actions import (
    AgentProposal,
    BeginCompensation,
    Escalate,
    HumanDecision,
    ToolCall,
)
from agentic_saga.contracts.clock import Clock, FakeClock
from agentic_saga.contracts.common import (
    Direction,
    JsonObject,
    OperationId,
    Reversibility,
    sha256_json,
    thaw_json_object,
)
from agentic_saga.contracts.events import (
    ApprovalConsumed,
    CompensationIntentRecorded,
    CompensationStarted,
    DispatchStarted,
    EffectOutcomeRecorded,
    LedgerEvent,
    ProposalRejected,
    SagaCreated,
    SagaStarted,
)
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    ReconcileEffectConfirmed,
    ReconciliationOutcome,
    safe_outcome_json,
)
from agentic_saga.contracts.redaction import RedactionPolicy
from agentic_saga.contracts.runtime import ExecutionBudget
from agentic_saga.contracts.tools import (
    EffectContext,
    EffectToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.execution.leases import LeaseService
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.invariants import TerminalGate
from agentic_saga.kernel.policy import (
    PolicyContext,
    PolicyEngine,
    PolicyRules,
    ProposedEffectIdentity,
)
from agentic_saga.kernel.ports import (
    ClaimedCommand,
    Lease,
    LeaseLost,
    OutboxState,
    ProposalIdentityConflict,
    StoreConflict,
    TransitionBatch,
    TransitionReceipt,
)
from agentic_saga.kernel.reducer import reduce_event
from agentic_saga.kernel.runtime import (
    EventMetadataFactory,
    InvariantEvidenceProvider,
    PolicyContextProvider,
    ProposalResult,
    SagaKernel,
    StableIdFactory,
)
from agentic_saga.kernel.state import OperationStatus, SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore

SAGA_ID = "saga_0000000000007001"
NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


@dataclass
class AdvancingClock(Clock):
    current: datetime
    lock: LockType = field(default_factory=Lock)

    def now(self) -> datetime:
        with self.lock:
            self.current += timedelta(milliseconds=1)
            return self.current


class ChargeCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    amount_minor: int = Field(gt=0)
    currency: Literal["USD"]
    credential_ref: str


class PasswordCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    password: str


class SecretFamilyCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    client_secret: str
    private_key: str
    session_token: str
    id_token: str


class DescriptorSecretCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    client_secret_value: str
    api_key_value: str
    password_hash: str
    private_key_pem: str


class DescriptorReferenceCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    client_secret_value_ref: str


class UppercaseCredentialCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    resource_id: str
    supplied_value: str = Field(
        validation_alias=AliasChoices(
            "CLIENT_SECRET_VALUE", "API-KEY-VALUE", "CLIENT_SECRET_VALUE_REF"
        )
    )


class Adapter:
    def __init__(self) -> None:
        self.calls = 0

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"adapter_version": "outbox-test-v1"}, strict=True)

    async def execute(self, command: ChargeCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        self.calls += 1
        return EffectConfirmed(receipt={"payment_id": "pay_1"})

    async def reconcile(
        self, command: ChargeCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"payment_id": "pay_1"})


class PasswordAdapter:
    async def execute(self, command: PasswordCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        return EffectConfirmed(receipt={"unreachable": True})

    async def reconcile(
        self, command: PasswordCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"unreachable": True})


class SecretFamilyAdapter:
    async def execute(self, command: SecretFamilyCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        return EffectConfirmed(receipt={"unreachable": True})

    async def reconcile(
        self, command: SecretFamilyCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"unreachable": True})


class DescriptorSecretAdapter:
    async def execute(
        self, command: DescriptorSecretCommand, context: EffectContext
    ) -> EffectOutcome:
        del command, context
        return EffectConfirmed(receipt={"unreachable": True})

    async def reconcile(
        self, command: DescriptorSecretCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"unreachable": True})


class DescriptorReferenceAdapter:
    async def execute(
        self, command: DescriptorReferenceCommand, context: EffectContext
    ) -> EffectOutcome:
        del command, context
        return EffectConfirmed(receipt={"unreachable": True})

    async def reconcile(
        self, command: DescriptorReferenceCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"unreachable": True})


class UppercaseCredentialAdapter:
    async def execute(
        self, command: UppercaseCredentialCommand, context: EffectContext
    ) -> EffectOutcome:
        del command, context
        return EffectConfirmed(receipt={"unreachable": True})

    async def reconcile(
        self, command: UppercaseCredentialCommand, context: ReconcileContext
    ) -> ReconciliationOutcome:
        del command, context
        return ReconcileEffectConfirmed(receipt={"unreachable": True})


def allow(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return True


def deny(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return False


def no_duplicate(
    identity: ProposedEffectIdentity, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    del identity, snapshot, context
    return False


def verify(
    decision: HumanDecision,
    proposal: ToolCall,
    snapshot: SagaSnapshot,
    context: PolicyContext,
) -> bool:
    del proposal, snapshot, context
    return decision.auth_proof == "verified-proof"


@dataclass
class Contexts(PolicyContextProvider):
    approval: HumanDecision | None = None
    direction: Direction = Direction.FORWARD
    semantic_generation: int = 0
    compensates_operation_id: OperationId | None = None

    def build(self, snapshot: SagaSnapshot, proposal: AgentProposal, lease: Lease) -> PolicyContext:
        del proposal
        values = context_identity(self, snapshot, lease) | context_usage()
        return PolicyContext.model_validate(values)


@dataclass(frozen=True)
class FailingContexts(PolicyContextProvider):
    auth_material: str

    def build(self, snapshot: SagaSnapshot, proposal: AgentProposal, lease: Lease) -> PolicyContext:
        del snapshot, proposal, lease
        raise RuntimeError(f"context leaked {self.auth_material}")


def context_identity(contexts: Contexts, snapshot: SagaSnapshot, lease: Lease) -> dict[str, object]:
    return {
        "step_instance_id": "step_00007001",
        "direction": contexts.direction,
        "semantic_generation": contexts.semantic_generation,
        "fence_token": lease.fence_token,
        "budget": budget(),
        "policy_evidence": {"source": "authoritative"},
        "resource_identity": {"resource_id": "order_1"},
        "compensates_operation_id": contexts.compensates_operation_id,
        "approval": contexts.approval,
        "used_approval_ids": snapshot.consumed_approval_ids,
    }


def context_usage() -> dict[str, object]:
    return {
        "turns_used": 1,
        "tool_calls_used": 1,
        "elapsed_ms": 1,
        "tokens_used": 1,
    }


def budget() -> ExecutionBudget:
    return ExecutionBudget(
        turn_limit=10,
        tool_call_limit=10,
        elapsed_ms_limit=10_000,
        token_limit=10_000,
    )


def rules(
    *,
    is_duplicate_effect: Callable[
        [ProposedEffectIdentity, SagaSnapshot, PolicyContext], bool
    ] = no_duplicate,
    approval_required: Callable[[BaseModel, SagaSnapshot, PolicyContext], bool] = deny,
    approval_verifier: Callable[
        [HumanDecision, ToolCall, SagaSnapshot, PolicyContext], bool
    ] = verify,
) -> PolicyRules:
    return PolicyRules(
        is_duplicate_effect=is_duplicate_effect,
        approval_required=approval_required,
        approval_verifier=approval_verifier,
    )


def created_event() -> SagaCreated:
    return SagaCreated(
        event_id="evt_0000000000007001",
        saga_id=SAGA_ID,
        saga_seq=1,
        definition_version="checkout-v1",
        fence_token=None,
        actor="kernel",
        trace_id="trace_0000000000007001",
        recorded_at=NOW,
        definition_name="checkout",
        definition_fingerprint="f" * 64,
        redacted_goal={"order_id": "order_1"},
    )


def started_event(lease: Lease) -> SagaStarted:
    return SagaStarted(
        event_id="evt_0000000000007002",
        saga_id=SAGA_ID,
        saga_seq=2,
        definition_version="checkout-v1",
        fence_token=lease.fence_token,
        actor="kernel",
        trace_id="trace_0000000000007001",
        recorded_at=NOW,
    )


def event_batch(
    snapshot: SagaSnapshot, lease: Lease, event: LedgerEvent, transition_id: str
) -> TransitionBatch:
    return TransitionBatch(
        transition_id=transition_id,
        saga_id=SAGA_ID,
        expected_seq=snapshot.seq,
        expected_fence_token=lease.fence_token,
        lease_owner=lease.owner,
        events=(event,),
        projection=reduce_event(snapshot, event),
    )


def started_store(path: Path, clock: Clock) -> tuple[SQLiteKernelStore, Lease]:
    store = SQLiteKernelStore.initialize(path, clock=clock)
    snapshot = store.create_saga(created_event())
    lease = LeaseService(store).acquire(SAGA_ID, "worker-a", timedelta(minutes=5))
    started = started_event(lease)
    store.commit_transition(event_batch(snapshot, lease, started, "txn_0000000000007001"))
    return store, lease


def capabilities() -> ToolCapabilities:
    return ToolCapabilities(
        idempotency_retention_seconds=86_400,
        reconciliation_supported=True,
        cancellation_supported=False,
        fencing_supported=True,
        reversibility=Reversibility.SEMANTIC,
        partial_effects_possible=False,
    )


def registry(adapter: Adapter) -> ToolRegistry:
    charge = charge_definition(adapter)
    definitions = (
        charge,
        refund_definition(adapter, charge),
        password_definition(charge),
        secret_family_definition(charge),
        descriptor_secret_definition(charge),
        descriptor_reference_definition(charge),
        uppercase_credential_definition(charge),
    )
    return ToolRegistry(definitions)


def charge_definition(adapter: Adapter) -> EffectToolDefinition[ChargeCommand]:
    return EffectToolDefinition(
        name="charge_payment",
        definition_version="charge-payment-v1",
        command_schema_version="charge-command-v1",
        input_model=ChargeCommand,
        adapter=adapter,
        capabilities=capabilities(),
        compensate_with="refund_payment",
    )


def refund_definition(
    adapter: Adapter, charge: EffectToolDefinition[ChargeCommand]
) -> EffectToolDefinition[ChargeCommand]:
    return EffectToolDefinition(
        name="refund_payment",
        definition_version="refund-payment-v1",
        command_schema_version="refund-command-v1",
        input_model=ChargeCommand,
        adapter=adapter,
        capabilities=charge.capabilities,
        compensate_with=None,
    )


def password_definition(
    charge: EffectToolDefinition[ChargeCommand],
) -> EffectToolDefinition[PasswordCommand]:
    return EffectToolDefinition(
        name="password_effect",
        definition_version="password-effect-v1",
        command_schema_version="password-command-v1",
        input_model=PasswordCommand,
        adapter=PasswordAdapter(),
        capabilities=charge.capabilities,
        compensate_with=None,
    )


def secret_family_definition(
    charge: EffectToolDefinition[ChargeCommand],
) -> EffectToolDefinition[SecretFamilyCommand]:
    return EffectToolDefinition(
        name="secret_family_effect",
        definition_version="secret-family-effect-v1",
        command_schema_version="secret-family-command-v1",
        input_model=SecretFamilyCommand,
        adapter=SecretFamilyAdapter(),
        capabilities=charge.capabilities,
        compensate_with=None,
    )


def descriptor_secret_definition(
    charge: EffectToolDefinition[ChargeCommand],
) -> EffectToolDefinition[DescriptorSecretCommand]:
    return EffectToolDefinition(
        name="descriptor_secret_effect",
        definition_version="descriptor-secret-effect-v1",
        command_schema_version="descriptor-secret-command-v1",
        input_model=DescriptorSecretCommand,
        adapter=DescriptorSecretAdapter(),
        capabilities=charge.capabilities,
        compensate_with=None,
    )


def descriptor_reference_definition(
    charge: EffectToolDefinition[ChargeCommand],
) -> EffectToolDefinition[DescriptorReferenceCommand]:
    return EffectToolDefinition(
        name="descriptor_reference_effect",
        definition_version="descriptor-reference-effect-v1",
        command_schema_version="descriptor-reference-command-v1",
        input_model=DescriptorReferenceCommand,
        adapter=DescriptorReferenceAdapter(),
        capabilities=charge.capabilities,
        compensate_with=None,
    )


def uppercase_credential_definition(
    charge: EffectToolDefinition[ChargeCommand],
) -> EffectToolDefinition[UppercaseCredentialCommand]:
    return EffectToolDefinition(
        name="uppercase_credential_effect",
        definition_version="uppercase-credential-effect-v1",
        command_schema_version="uppercase-credential-command-v1",
        input_model=UppercaseCredentialCommand,
        adapter=UppercaseCredentialAdapter(),
        capabilities=charge.capabilities,
        compensate_with=None,
    )


@dataclass(frozen=True)
class RuntimeOptions:
    contexts: PolicyContextProvider | None
    terminal_gate: TerminalGate | None
    invariant_provider: InvariantEvidenceProvider | None
    redaction_policy: RedactionPolicy | None


def saga_kernel(
    store: SQLiteKernelStore,
    clock: Clock,
    tools: ToolRegistry,
    policy: PolicyEngine,
    options: RuntimeOptions,
) -> SagaKernel:
    return SagaKernel(
        store=store,
        policy=policy,
        registry=tools,
        policy_context_provider=options.contexts or Contexts(),
        event_metadata_factory=EventMetadataFactory(clock),
        id_factory=StableIdFactory(namespace=b"runtime-tests"),
        redaction_policy=options.redaction_policy,
        terminal_gate=options.terminal_gate,
        invariant_evidence_provider=options.invariant_provider,
    )


def policy_engine(
    tools: ToolRegistry,
    is_duplicate_effect: Callable[[ProposedEffectIdentity, SagaSnapshot, PolicyContext], bool],
    approval_required: Callable[[BaseModel, SagaSnapshot, PolicyContext], bool],
    approval_verifier: Callable[[HumanDecision, ToolCall, SagaSnapshot, PolicyContext], bool],
) -> PolicyEngine:
    return PolicyEngine(
        registry=tools,
        identity_factory=OperationIdentityFactory(namespace=b"runtime-tests"),
        rules=rules(
            is_duplicate_effect=is_duplicate_effect,
            approval_required=approval_required,
            approval_verifier=approval_verifier,
        ),
    )


def kernel(  # noqa: PLR0913
    path: Path,
    *,
    terminal_gate: TerminalGate | None = None,
    invariant_provider: InvariantEvidenceProvider | None = None,
    contexts: PolicyContextProvider | None = None,
    is_duplicate_effect: Callable[
        [ProposedEffectIdentity, SagaSnapshot, PolicyContext], bool
    ] = no_duplicate,
    approval_required: Callable[[BaseModel, SagaSnapshot, PolicyContext], bool] = deny,
    approval_verifier: Callable[
        [HumanDecision, ToolCall, SagaSnapshot, PolicyContext], bool
    ] = verify,
    clock: Clock | None = None,
    redaction_policy: RedactionPolicy | None = None,
) -> tuple[SagaKernel, SQLiteKernelStore, Lease, Adapter]:
    active_clock = clock or FakeClock(NOW)
    store, lease = started_store(path, active_clock)
    adapter = Adapter()
    tools = registry(adapter)
    policy = policy_engine(tools, is_duplicate_effect, approval_required, approval_verifier)
    options = RuntimeOptions(contexts, terminal_gate, invariant_provider, redaction_policy)
    runtime = saga_kernel(store, active_clock, tools, policy, options)
    return runtime, store, lease, adapter


def proposal(**changes: object) -> ToolCall:
    values: dict[str, object] = {
        "proposal_id": "proposal_00007001",
        "tool_name": "charge_payment",
        "arguments": {
            "resource_id": "order_1",
            "amount_minor": 14900,
            "currency": "USD",
            "credential_ref": "vault://payments/v1",
        },
        "based_on_saga_seq": 2,
        "rationale": "Capture payment for the order.",
    }
    return ToolCall.model_validate(values | changes)


def approval_hash(call: ToolCall, snapshot: SagaSnapshot, context: PolicyContext) -> str:
    command = ChargeCommand.model_validate(thaw_json_object(call.arguments))
    identity = approval_identity(call, command, context)
    return sha256_json(approval_material(call, command, snapshot, context, identity))


def approval_identity(
    call: ToolCall, command: ChargeCommand, context: PolicyContext
) -> ProposedEffectIdentity:
    return ProposedEffectIdentity(
        tool_name=call.tool_name,
        direction=context.direction,
        step_instance_id=context.step_instance_id,
        semantic_generation=context.semantic_generation,
        command_digest=sha256_json(command.model_dump(mode="json")),
        resource_identity=context.resource_identity,
    )


def approval_material(
    call: ToolCall,
    command: ChargeCommand,
    snapshot: SagaSnapshot,
    context: PolicyContext,
    identity: ProposedEffectIdentity,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "saga_id": snapshot.saga_id,
        "definition_version": snapshot.definition_version,
        "proposal": call.model_dump(mode="json"),
        "step_instance_id": context.step_instance_id,
        "direction": context.direction.value,
        "semantic_generation": context.semantic_generation,
        "command": command.model_dump(mode="json"),
        "effect_identity": identity.model_dump(mode="json"),
    }


def test_authorized_proposal_atomically_creates_intent_and_outbox(tmp_path: Path) -> None:
    runtime, store, lease, adapter = kernel(tmp_path / "saga.db")

    result = runtime.submit_proposal(SAGA_ID, proposal(), lease)

    snapshot = store.load_snapshot(SAGA_ID)
    assert result.accepted is True
    assert result.operation_id is not None
    assert snapshot.operations[result.operation_id].status is OperationStatus.INTENT_DURABLE
    assert store.runnable_count(SAGA_ID) == 1
    assert adapter.calls == 0


def accepted_operation(path: Path, proposal_id: str, generation: int = 0) -> OperationId:
    contexts = Contexts(semantic_generation=generation)
    runtime, _, lease, _ = kernel(path, contexts=contexts)
    result = runtime.submit_proposal(SAGA_ID, proposal(proposal_id=proposal_id), lease)
    assert result.operation_id is not None
    return result.operation_id


def test_request_identity_does_not_change_semantic_operation_id(tmp_path: Path) -> None:
    first = accepted_operation(tmp_path / "first.db", "proposal_00007001")
    second = accepted_operation(tmp_path / "second.db", "proposal_00007002")

    assert second == first


def test_semantic_generation_changes_operation_id(tmp_path: Path) -> None:
    first = accepted_operation(tmp_path / "first.db", "proposal_00007001")
    next_generation = accepted_operation(tmp_path / "second.db", "proposal_00007001", 1)

    assert next_generation != first


def test_rejected_proposal_records_evidence_without_outbox(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db", approval_required=allow)

    result = runtime.submit_proposal(SAGA_ID, proposal(), lease)

    assert result.accepted is False
    assert isinstance(store.read_events(SAGA_ID)[-1], ProposalRejected)
    assert store.runnable_count(SAGA_ID) == 0


def test_secret_command_is_rejected_without_persisting_secret_or_its_digest(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    unsafe_value = "Bearer reusable-secret-value"
    unsafe = proposal(arguments=command_arguments(unsafe_value))

    result = runtime.submit_proposal(SAGA_ID, unsafe, lease)

    durable = store.path.read_bytes()
    assert result.code == "unsafe_command"
    assert unsafe_value.encode() not in durable
    assert store.runnable_count(SAGA_ID) == 0


def command_arguments(credential_ref: str) -> dict[str, object]:
    return {
        "resource_id": "order_1",
        "amount_minor": 14900,
        "currency": "USD",
        "credential_ref": credential_ref,
    }


def test_unversioned_credential_reference_is_not_executable(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    unsafe = proposal(arguments=command_arguments("payments-production"))

    result = runtime.submit_proposal(SAGA_ID, unsafe, lease)

    assert result.code == "unsafe_command"
    assert store.runnable_count(SAGA_ID) == 0


def test_strict_command_password_is_rejected_before_sqlite(tmp_path: Path) -> None:
    policy = RedactionPolicy(sensitive_keys=("private-note",))
    runtime, store, lease, _ = kernel(tmp_path / "saga.db", redaction_policy=policy)
    credential = "raw-password-must-not-persist"
    unsafe = proposal(
        tool_name="password_effect", arguments={"resource_id": "order_1", "password": credential}
    )

    result = runtime.submit_proposal(SAGA_ID, unsafe, lease)

    durable = store.path.read_bytes()
    assert result.code == "unsafe_command"
    assert credential.encode() not in durable
    assert sha256(credential.encode()).hexdigest().encode() not in durable
    assert store.runnable_count(SAGA_ID) == 0


def compound_credentials() -> tuple[str, ...]:
    return (
        "oauth-client-secret-value",
        "private-signing-key-value",
        "session-token-value",
        "identity-token-value",
    )


def compound_secret_arguments(credentials: tuple[str, ...]) -> dict[str, object]:
    return {
        "resource_id": "order_1",
        "client_secret": credentials[0],
        "private_key": credentials[1],
        "session_token": credentials[2],
        "id_token": credentials[3],
    }


def test_strict_compound_secret_fields_never_reach_sqlite(tmp_path: Path) -> None:
    policy = RedactionPolicy(sensitive_keys=("private-note",))
    runtime, store, lease, _ = kernel(tmp_path / "saga.db", redaction_policy=policy)
    credentials = compound_credentials()
    unsafe = proposal(
        tool_name="secret_family_effect", arguments=compound_secret_arguments(credentials)
    )
    result = runtime.submit_proposal(SAGA_ID, unsafe, lease)

    assert result.code == "unsafe_command"
    assert_no_raw_or_digest(store, credentials)


def assert_no_raw_or_digest(store: SQLiteKernelStore, values: tuple[str, ...]) -> None:
    durable = store.path.read_bytes()
    for value in values:
        assert value.encode() not in durable
        assert sha256(value.encode()).hexdigest().encode() not in durable


def descriptor_credentials() -> tuple[str, ...]:
    return (
        "oauth-client-secret-value",
        "api-key-sensitive-value",
        sha256(b"supplied-password-source").hexdigest(),
        "private-key-pem-material",
    )


def descriptor_arguments(values: tuple[str, ...]) -> dict[str, object]:
    return {
        "resource_id": "order_1",
        "client_secret_value": values[0],
        "api_key_value": values[1],
        "password_hash": values[2],
        "private_key_pem": values[3],
    }


def test_strict_descriptor_secret_fields_never_reach_sqlite(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    values = descriptor_credentials()
    unsafe = proposal(tool_name="descriptor_secret_effect", arguments=descriptor_arguments(values))

    result = runtime.submit_proposal(SAGA_ID, unsafe, lease)

    assert result.code == "unsafe_command"
    assert_private_failure(result, store, values)


def test_invalid_descriptor_secret_reference_never_reaches_sqlite(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    raw_reference = "raw-reference-value"
    arguments = {"resource_id": "order_1", "client_secret_value_ref": raw_reference}
    unsafe = proposal(tool_name="descriptor_reference_effect", arguments=arguments)

    result = runtime.submit_proposal(SAGA_ID, unsafe, lease)

    assert result.code == "unsafe_command"
    assert_private_failure(result, store, (raw_reference,))


def assert_uppercase_credential_rejected(tmp_path: Path, key: str, value: str) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    arguments = {"resource_id": "order_1", key: value}
    parsed = UppercaseCredentialCommand.model_validate(arguments)
    assert parsed.supplied_value == value

    result = runtime.submit_proposal(
        SAGA_ID, proposal(tool_name="uppercase_credential_effect", arguments=arguments), lease
    )

    assert result.code == "unsafe_command"
    assert_private_failure(result, store, (value,))


def test_strict_uppercase_snake_descriptor_never_reaches_sqlite(tmp_path: Path) -> None:
    assert_uppercase_credential_rejected(tmp_path, "CLIENT_SECRET_VALUE", "uppercase-snake-secret")


def test_strict_uppercase_kebab_descriptor_never_reaches_sqlite(tmp_path: Path) -> None:
    assert_uppercase_credential_rejected(tmp_path, "API-KEY-VALUE", "uppercase-kebab-secret")


def test_invalid_uppercase_final_reference_never_reaches_sqlite(tmp_path: Path) -> None:
    assert_uppercase_credential_rejected(
        tmp_path, "CLIENT_SECRET_VALUE_REF", "uppercase-raw-reference"
    )


def test_valid_uppercase_final_reference_remains_executable(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    reference = "vault://production/credential/v4"
    arguments = {"resource_id": "order_1", "CLIENT_SECRET_VALUE_REF": reference}

    result = runtime.submit_proposal(
        SAGA_ID, proposal(tool_name="uppercase_credential_effect", arguments=arguments), lease
    )

    assert result.code == "authorized"
    assert store.runnable_count(SAGA_ID) == 1


def raising_approval(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    raise RuntimeError("raw-policy-secret")


def test_unsafe_command_never_reaches_policy_callback(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db", approval_required=raising_approval)
    unsafe = proposal(
        tool_name="password_effect",
        arguments={"resource_id": "order_1", "password": "raw-password"},
    )

    result = runtime.submit_proposal(SAGA_ID, unsafe, lease)

    assert result.code == "unsafe_command"
    assert b"raw-policy-secret" not in store.path.read_bytes()


def test_approval_callback_exception_is_sanitized(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db", approval_required=raising_approval)

    result = runtime.submit_proposal(SAGA_ID, proposal(), lease)

    assert result.code == "policy_callback_error"
    assert b"raw-policy-secret" not in store.path.read_bytes()
    assert store.runnable_count(SAGA_ID) == 0


def test_exact_retry_uses_durable_receipt_but_changed_content_conflicts(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    first = runtime.submit_proposal(SAGA_ID, proposal(), lease)

    retried = runtime.submit_proposal(SAGA_ID, proposal(), lease)

    assert retried == first
    assert len(store.read_events(SAGA_ID)) == 3
    with pytest.raises(ProposalIdentityConflict, match="proposal identity"):
        runtime.submit_proposal(SAGA_ID, proposal(rationale="Changed request."), lease)


def test_exact_retry_requires_current_lease(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    runtime.submit_proposal(SAGA_ID, proposal(), lease)
    LeaseService(store).release(lease)
    LeaseService(store).acquire(SAGA_ID, "worker-b", timedelta(minutes=5))

    with pytest.raises(LeaseLost):
        runtime.submit_proposal(SAGA_ID, proposal(), lease)

    assert len(store.read_events(SAGA_ID)) == 3


def approval_context(
    contexts: Contexts, store: SQLiteKernelStore, call: ToolCall, lease: Lease
) -> PolicyContext:
    return contexts.build(store.load_snapshot(SAGA_ID), call, lease)


def verified_decision(
    call: ToolCall, store: SQLiteKernelStore, context: PolicyContext
) -> HumanDecision:
    return HumanDecision(
        decision_id="decision_00007001",
        saga_id=SAGA_ID,
        based_on_saga_seq=2,
        action="approve",
        proposal_hash=approval_hash(call, store.load_snapshot(SAGA_ID), context),
        actor="operator@example.test",
        issued_at=NOW,
        auth_proof="verified-proof",
    )


def assert_no_auth_material(store: SQLiteKernelStore, auth_material: str) -> None:
    durable = store.path.read_bytes()
    assert auth_material.encode() not in durable
    assert sha256(auth_material.encode()).hexdigest().encode() not in durable


def approval_kernel(
    path: Path,
    contexts: Contexts,
    verifier: Callable[[HumanDecision, ToolCall, SagaSnapshot, PolicyContext], bool] = verify,
) -> tuple[SagaKernel, SQLiteKernelStore, Lease, Adapter]:
    return kernel(
        path,
        contexts=contexts,
        approval_required=allow,
        approval_verifier=verifier,
    )


def test_verified_approval_is_consumed_without_persisting_auth_proof(tmp_path: Path) -> None:
    contexts = Contexts()
    runtime, store, lease, _ = approval_kernel(tmp_path / "saga.db", contexts)
    call = proposal()
    auth_material = "verified-proof"
    context = approval_context(contexts, store, call, lease)
    contexts.approval = verified_decision(call, store, context)

    result = runtime.submit_proposal(SAGA_ID, call, lease)

    assert result.accepted is True
    assert store.load_snapshot(SAGA_ID).consumed_approval_ids == ("decision_00007001",)
    assert isinstance(store.read_events(SAGA_ID)[-2], ApprovalConsumed)
    assert_no_auth_material(store, auth_material)


def raising_verifier(
    decision: HumanDecision, proposal: ToolCall, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    del proposal, snapshot, context
    raise RuntimeError(f"verifier leaked {decision.auth_proof}")


def raising_duplicate(
    identity: ProposedEffectIdentity, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    del identity, snapshot
    assert context.approval is not None
    raise RuntimeError(f"duplicate leaked {context.approval.auth_proof}")


def raising_approval_required(
    command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    del command, snapshot
    assert context.approval is not None
    raise RuntimeError(f"requirement leaked {context.approval.auth_proof}")


def install_approval(
    contexts: Contexts, store: SQLiteKernelStore, call: ToolCall, lease: Lease
) -> str:
    policy_context = approval_context(contexts, store, call, lease)
    contexts.approval = verified_decision(call, store, policy_context)
    return contexts.approval.auth_proof


def assert_private_failure(
    result: ProposalResult, store: SQLiteKernelStore, materials: tuple[str, ...]
) -> None:
    blobs = (result.model_dump_json().encode(), store.path.read_bytes())
    for blob in blobs:
        for material in materials:
            assert material.encode() not in blob
            assert sha256(material.encode()).hexdigest().encode() not in blob


def test_duplicate_callback_exception_never_leaks_approval_proof(tmp_path: Path) -> None:
    contexts = Contexts()
    runtime, store, lease, _ = kernel(
        tmp_path / "saga.db", contexts=contexts, is_duplicate_effect=raising_duplicate
    )
    call = proposal()
    auth_material = install_approval(contexts, store, call, lease)

    result = runtime.submit_proposal(SAGA_ID, call, lease)

    assert result.code == "policy_callback_error"
    assert_private_failure(result, store, (auth_material, f"duplicate leaked {auth_material}"))


def test_approval_required_exception_never_leaks_approval_proof(tmp_path: Path) -> None:
    contexts = Contexts()
    runtime, store, lease, _ = kernel(
        tmp_path / "saga.db", contexts=contexts, approval_required=raising_approval_required
    )
    call = proposal()
    auth_material = install_approval(contexts, store, call, lease)

    result = runtime.submit_proposal(SAGA_ID, call, lease)

    assert result.code == "policy_callback_error"
    assert_private_failure(result, store, (auth_material, f"requirement leaked {auth_material}"))


def test_context_provider_exception_is_a_private_policy_denial(tmp_path: Path) -> None:
    auth_material = "context-provider-proof"
    runtime, store, lease, _ = kernel(tmp_path / "saga.db", contexts=FailingContexts(auth_material))

    result = runtime.submit_proposal(SAGA_ID, proposal(), lease)

    assert result.code == "policy_callback_error"
    assert_private_failure(result, store, (auth_material, f"context leaked {auth_material}"))


def test_approval_verifier_exception_is_sanitized(tmp_path: Path) -> None:
    contexts = Contexts()
    runtime, store, lease, _ = approval_kernel(tmp_path / "saga.db", contexts, raising_verifier)
    call = proposal()
    context = approval_context(contexts, store, call, lease)
    contexts.approval = verified_decision(call, store, context)
    auth_material = contexts.approval.auth_proof

    result = runtime.submit_proposal(SAGA_ID, call, lease)

    assert result.code == "policy_callback_error"
    assert b"verifier leaked" not in store.path.read_bytes()
    assert_no_auth_material(store, auth_material)


def settle_forward(
    store: SQLiteKernelStore, lease: Lease, transition_number: int = 7002
) -> OperationId:
    claimed = store.claim_outbox(lease.owner, timedelta(minutes=1))
    assert claimed is not None
    snapshot = store.load_snapshot(SAGA_ID)
    dispatch = DispatchStarted.model_validate(effect_event_fields(claimed, snapshot.seq + 1, lease))
    transition_id = f"txn_{transition_number:016d}"
    store.start_dispatch(claimed, event_batch(snapshot, lease, dispatch, transition_id))
    return finish_forward(store, claimed, lease, transition_number + 1)


def effect_event_fields(claimed: ClaimedCommand, seq: int, lease: Lease) -> dict[str, object]:
    return ledger_event_fields(seq, lease) | effect_identity_fields(claimed)


def ledger_event_fields(seq: int, lease: Lease) -> dict[str, object]:
    return {
        "event_id": f"evt_{seq:016d}",
        "saga_id": SAGA_ID,
        "saga_seq": seq,
        "definition_version": "checkout-v1",
        "fence_token": lease.fence_token,
        "actor": "kernel",
        "trace_id": "trace_0000000000007002",
        "recorded_at": NOW,
    }


def effect_identity_fields(claimed: ClaimedCommand) -> dict[str, object]:
    return {
        "operation_id": claimed.operation_id,
        "step_instance_id": claimed.step_instance_id,
        "direction": claimed.direction,
        "semantic_generation": claimed.semantic_generation,
        "delivery_attempt": claimed.delivery_attempt,
        "tool_name": claimed.tool_name,
        "redacted_command": claimed.command,
        "command_hash": claimed.command_hash,
    }


def finish_forward(
    store: SQLiteKernelStore, claimed: ClaimedCommand, lease: Lease, transition_number: int
) -> OperationId:
    snapshot = store.load_snapshot(SAGA_ID)
    event = confirmed_event(claimed, snapshot.seq + 1, lease)
    batch = event_batch(snapshot, lease, event, f"txn_{transition_number:016d}")
    store.complete_outbox(claimed, batch)
    return claimed.operation_id


def confirmed_event(claimed: ClaimedCommand, seq: int, lease: Lease) -> EffectOutcomeRecorded:
    outcome = EffectConfirmed(receipt={"payment_id": "pay_1"})
    result = safe_outcome_json(outcome)
    fields = {
        "outcome": outcome,
        "redacted_result": result,
        "result_hash": sha256_json(result),
    }
    return EffectOutcomeRecorded.model_validate(effect_event_fields(claimed, seq, lease) | fields)


def start_compensation(store: SQLiteKernelStore, lease: Lease) -> None:
    snapshot = store.load_snapshot(SAGA_ID)
    event = compensation_started(snapshot, lease)
    store.commit_transition(event_batch(snapshot, lease, event, "txn_0000000000007004"))


def compensation_started(snapshot: SagaSnapshot, lease: Lease) -> CompensationStarted:
    return CompensationStarted(
        event_id="evt_0000000000007006",
        saga_id=SAGA_ID,
        saga_seq=snapshot.seq + 1,
        definition_version=snapshot.definition_version,
        fence_token=lease.fence_token,
        actor="kernel",
        trace_id="trace_0000000000007003",
        recorded_at=NOW,
    )


def prepare_compensation(
    runtime: SagaKernel, store: SQLiteKernelStore, lease: Lease, contexts: Contexts
) -> None:
    forward = runtime.submit_proposal(SAGA_ID, proposal(), lease)
    assert forward.operation_id is not None
    contexts.compensates_operation_id = settle_forward(store, lease)
    start_compensation(store, lease)
    contexts.direction = Direction.COMPENSATION


def test_begin_compensation_records_agent_request_without_new_outbox_work(
    tmp_path: Path,
) -> None:
    # Given
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    runtime.submit_proposal(SAGA_ID, proposal(), lease)
    settle_forward(store, lease)
    request = BeginCompensation(
        proposal_id="proposal_00007004",
        based_on_saga_seq=5,
        reason_code="goal_unreachable",
        rationale="Use the deterministic frontier; password=raw-agent-secret.",
    )

    # When
    result = runtime.submit_proposal(SAGA_ID, request, lease)

    # Then
    event = store.read_events(SAGA_ID)[-1]
    assert result.accepted is True
    assert isinstance(event, CompensationStarted)
    assert event.proposal_id == request.proposal_id
    assert event.reason_code == request.reason_code
    assert event.proposal_hash is not None
    assert store.load_snapshot(SAGA_ID).status.value == "compensating"
    assert store.runnable_count(SAGA_ID) == 0
    assert b"raw-agent-secret" not in store.path.read_bytes()
    reopened = SQLiteKernelStore.open(store.path)
    assert reopened.rebuild_and_verify(SAGA_ID).status.value == "compensating"


def compensation_proposal() -> ToolCall:
    return proposal(
        proposal_id="proposal_00007003",
        tool_name="refund_payment",
        based_on_saga_seq=6,
        rationale="Repair the confirmed payment.",
    )


def test_compensation_proposal_records_matching_intent_and_outbox(tmp_path: Path) -> None:
    contexts = Contexts()
    runtime, store, lease, _ = kernel(tmp_path / "saga.db", contexts=contexts)
    prepare_compensation(runtime, store, lease, contexts)

    result = runtime.submit_proposal(SAGA_ID, compensation_proposal(), lease)

    assert result.accepted is True
    assert isinstance(store.read_events(SAGA_ID)[-1], CompensationIntentRecorded)
    assert store.runnable_count(SAGA_ID) == 1


def escalation(seq: int = 3) -> Escalate:
    return Escalate(
        proposal_id="proposal_00007002",
        based_on_saga_seq=seq,
        reason_code="needs_operator",
        rationale="Autonomous progress is unsafe.",
    )


def test_escalation_atomically_suspends_runnable_commands(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    accepted = runtime.submit_proposal(SAGA_ID, proposal(), lease)

    result = runtime.submit_proposal(SAGA_ID, escalation(), lease)

    assert result.accepted is True
    assert store.load_snapshot(SAGA_ID).status.value == "human_required"
    assert store.runnable_count(SAGA_ID) == 0
    assert accepted.operation_id is not None
    command_id = StableIdFactory(namespace=b"runtime-tests").command_id(accepted.operation_id)
    assert store.outbox_state(command_id) is OutboxState.SUSPENDED_FOR_HUMAN


def test_escalation_with_claimed_command_is_non_mutating_conflict(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    runtime.submit_proposal(SAGA_ID, proposal(), lease)
    claimed = store.claim_outbox(lease.owner, timedelta(minutes=1))
    before = store.read_events(SAGA_ID)

    with pytest.raises(StoreConflict, match="claimed command"):
        runtime.submit_proposal(SAGA_ID, escalation(), lease)

    assert store.read_events(SAGA_ID) == before
    assert claimed is not None
    assert store.outbox_state(claimed.command_id) is OutboxState.CLAIMED


def test_human_suspension_survives_open_backup_and_restore(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    accepted = runtime.submit_proposal(SAGA_ID, proposal(), lease)
    runtime.submit_proposal(SAGA_ID, escalation(), lease)
    backup = tmp_path / "backup.db"

    reopened = SQLiteKernelStore.open(store.path)
    reopened.backup_to(backup)
    restored = SQLiteKernelStore.restore_from(backup, tmp_path / "restored.db")

    assert accepted.operation_id is not None
    command_id = StableIdFactory(b"runtime-tests").command_id(accepted.operation_id)
    assert restored.outbox_state(command_id) is OutboxState.SUSPENDED_FOR_HUMAN
    assert restored.load_snapshot(SAGA_ID).status.value == "human_required"


def synchronized_receipt_lookup(
    barrier: Barrier,
    original: Callable[[SQLiteKernelStore, str], TransitionReceipt | None],
) -> Callable[[SQLiteKernelStore, str], TransitionReceipt | None]:
    waited_threads: set[int] = set()
    guard = Lock()

    def synchronized_lookup(
        self: SQLiteKernelStore, transition_id: str
    ) -> TransitionReceipt | None:
        receipt = original(self, transition_id)
        thread_id = get_ident()
        with guard:
            should_wait = receipt is None and thread_id not in waited_threads
            waited_threads.add(thread_id)
        if should_wait:
            barrier.wait()
        return receipt

    return synchronized_lookup


def submit_concurrently(
    runtime: SagaKernel, lease: Lease, monkeypatch: pytest.MonkeyPatch
) -> tuple[ProposalResult, ...]:
    original = SQLiteKernelStore.lookup_transition_receipt
    lookup = synchronized_receipt_lookup(Barrier(2, timeout=5), original)
    monkeypatch.setattr(SQLiteKernelStore, "lookup_transition_receipt", lookup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        return tuple(
            pool.map(lambda _: runtime.submit_proposal(SAGA_ID, proposal(), lease), range(2))
        )


def test_synchronized_receipt_lookup_waits_once_per_worker(tmp_path: Path) -> None:
    _, store, _, _ = kernel(tmp_path / "saga.db")
    barrier = Barrier(2)
    lookup = synchronized_receipt_lookup(barrier, lambda _store, _transition_id: None)
    pool = ThreadPoolExecutor(max_workers=2)
    repeated = pool.submit(lambda: (lookup(store, "first"), lookup(store, "retry")))
    peer = pool.submit(lookup, store, "peer")
    try:
        peer.result(timeout=1)
        repeated.result(timeout=0.1)
    finally:
        barrier.abort()
        pool.shutdown(wait=True)


def test_concurrent_duplicate_submission_creates_one_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")

    results = submit_concurrently(runtime, lease, monkeypatch)

    assert results[0] == results[1]
    assert len(store.read_events(SAGA_ID)) == 3
    assert store.runnable_count(SAGA_ID) == 1


def test_advancing_clock_concurrent_duplicate_converges_to_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db", clock=AdvancingClock(NOW))

    results = submit_concurrently(runtime, lease, monkeypatch)

    assert results[0] == results[1]
    assert len(store.read_events(SAGA_ID)) == 3
    assert store.runnable_count(SAGA_ID) == 1


def assert_replaced_lease_is_rejected(
    runtime: SagaKernel, store: SQLiteKernelStore, lease: Lease
) -> None:
    LeaseService(store).release(lease)
    replacement = LeaseService(store).acquire(SAGA_ID, "worker-b", timedelta(minutes=5))
    assert replacement.fence_token > lease.fence_token
    with pytest.raises(LeaseLost):
        call = proposal(proposal_id="proposal_00007009")
        runtime.submit_proposal(SAGA_ID, call, lease)


def test_stale_unrelated_proposal_and_lease_are_non_mutating(tmp_path: Path) -> None:
    runtime, store, lease, _ = kernel(tmp_path / "saga.db")
    before = store.read_events(SAGA_ID)

    stale = runtime.submit_proposal(SAGA_ID, proposal(based_on_saga_seq=1), lease)

    assert stale.code == "stale_proposal"
    assert store.read_events(SAGA_ID) == before
    assert_replaced_lease_is_rejected(runtime, store, lease)
    assert store.read_events(SAGA_ID) == before
