from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter

from agentic_saga import execution
from agentic_saga.contracts.actions import AgentProposal, Finish, HumanDecision, ToolCall
from agentic_saga.contracts.clock import Clock, FakeClock
from agentic_saga.contracts.common import Direction, JsonObject, Reversibility
from agentic_saga.contracts.events import (
    EffectOutcomeRecorded,
    ReadObserved,
    ReconciliationRecorded,
    SagaCreated,
)
from agentic_saga.contracts.outcomes import (
    EffectConfirmed,
    EffectOutcome,
    OutcomeUnknown,
    ReconcileEffectConfirmed,
)
from agentic_saga.contracts.redaction import RedactionPolicy
from agentic_saga.contracts.runtime import (
    ExecutionBudget,
    SagaGoal,
    SagaObservation,
    SagaStatus,
    TerminalRequirement,
    ToolDescriptor,
)
from agentic_saga.contracts.tools import (
    EffectAdapter,
    EffectContext,
    EffectToolDefinition,
    ReadToolDefinition,
    ReconcileContext,
    ToolCapabilities,
    ToolRegistry,
)
from agentic_saga.evidence.run_trace import RunTraceExporter
from agentic_saga.execution import composition
from agentic_saga.execution.runtime import SagaRuntime
from agentic_saga.kernel.definitions import DefinitionCatalog, SagaDefinition
from agentic_saga.kernel.identity import OperationIdentityFactory
from agentic_saga.kernel.invariants import (
    InvariantEvidence,
    InvariantResult,
    TerminalGate,
)
from agentic_saga.kernel.policy import (
    PolicyContext,
    PolicyEngine,
    PolicyRules,
    ProposedEffectIdentity,
)
from agentic_saga.kernel.ports import KernelStore, Lease
from agentic_saga.kernel.runtime import InvariantEvidenceProvider, PolicyContextProvider
from agentic_saga.kernel.state import SagaSnapshot
from agentic_saga.storage import SQLiteKernelStore
from tests.support.durable_agent import DurableFakeAgent

_NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
_JSON_OBJECT: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)
_NAMESPACE = b"composition-test-v1"
_BUDGET = ExecutionBudget(
    turn_limit=2,
    tool_call_limit=2,
    elapsed_ms_limit=2_000,
    token_limit=200,
)
_TERMINAL_STATUSES = (
    SagaStatus.SUCCEEDED_VERIFIED,
    SagaStatus.COMPENSATED_VERIFIED,
    SagaStatus.ABORTED_CLEAN,
    SagaStatus.RESOLVED_WITH_EXCEPTION,
)
_PRIVATE_KEYS = ("email", "name", "address")
_PRIVATE_VALUES = ("customer@example.com", "Private Customer", "1 Private Lane")
_EFFECT_CAPABILITIES = ToolCapabilities(
    idempotency_retention_seconds=3_600,
    reconciliation_supported=True,
    cancellation_supported=False,
    fencing_supported=True,
    reversibility=Reversibility.SEMANTIC,
    partial_effects_possible=False,
)


def _not_duplicate(
    identity: ProposedEffectIdentity, snapshot: SagaSnapshot, context: PolicyContext
) -> bool:
    del identity, snapshot, context
    return False


def _approval_required(command: BaseModel, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del command, snapshot, context
    return False


def _verify_approval(
    decision: HumanDecision,
    proposal: ToolCall,
    snapshot: SagaSnapshot,
    context: PolicyContext,
) -> bool:
    del decision, proposal, snapshot, context
    return False


def _allow_compensation(proposal: object, snapshot: SagaSnapshot, context: PolicyContext) -> bool:
    del proposal, snapshot, context
    return True


def _deny_tool_advertisement(tool_name: str, snapshot: SagaSnapshot) -> None:
    del tool_name, snapshot


def _advertise_private(tool_name: str, snapshot: SagaSnapshot) -> JsonObject:
    del tool_name, snapshot
    return {"email": "customer@example.com", "public_constraint": True}


@dataclass(frozen=True)
class _Contexts(PolicyContextProvider):
    def build(self, snapshot: SagaSnapshot, proposal: AgentProposal, lease: Lease) -> PolicyContext:
        del snapshot, proposal
        return PolicyContext(
            step_instance_id="step_compositiontest",
            direction=Direction.FORWARD,
            semantic_generation=0,
            fence_token=lease.fence_token,
            budget=_BUDGET,
            turns_used=0,
            tool_calls_used=0,
            elapsed_ms=0,
            tokens_used=0,
            policy_evidence={"source": "composition-test"},
            resource_identity={"resource": "composition-test"},
        )


@dataclass(frozen=True)
class _Evidence(InvariantEvidenceProvider):
    def evaluate(
        self, saga_id: str, snapshot: SagaSnapshot, target_status: SagaStatus
    ) -> InvariantEvidence:
        result = InvariantResult(
            rule_id="durable_completion",
            passed=True,
            inputs={"source": "composition-test"},
            explanation="The minimal Saga reached its authoritative terminal state.",
        )
        return InvariantEvidence(
            saga_id=saga_id,
            definition_version=snapshot.definition_version,
            evaluated_at_seq=snapshot.seq,
            target_status=target_status,
            invariant_version="composition-invariants-v1",
            results=(result,),
        )


@dataclass(frozen=True)
class _FinishingAgent:
    async def next_action(
        self, observation: SagaObservation, available_tools: Sequence[ToolDescriptor]
    ) -> AgentProposal:
        del available_tools
        return Finish(
            proposal_id="proposal_composition_test",
            based_on_saga_seq=observation.saga_seq,
            rationale="The authoritative completion evidence is ready.",
            target_status="succeeded_verified",
        )


@dataclass(frozen=True)
class _DefinitionOnlyPolicy:
    def advertised_constraints(self, tool_name: str, snapshot: object) -> None:
        del tool_name, snapshot


class _StringSubclass(str):
    """A value rejected by an exact-type boundary despite string behavior."""


class _BytesSubclass(bytes):
    """A value rejected by an exact-type boundary despite bytes behavior."""


def _definition(registry: ToolRegistry | None = None) -> SagaDefinition:
    selected = ToolRegistry() if registry is None else registry
    policy = PolicyEngine(
        registry=selected,
        identity_factory=OperationIdentityFactory(_NAMESPACE),
        rules=PolicyRules(
            _not_duplicate,
            _approval_required,
            _verify_approval,
            compensation_allowed=_allow_compensation,
            tool_advertisement=_deny_tool_advertisement,
        ),
    )
    requirement = TerminalRequirement(
        invariant_version="composition-invariants-v1",
        required_rule_ids=("durable_completion",),
    )
    return SagaDefinition(
        name="composition_test",
        version="composition-v1",
        registry=selected,
        policy=policy,
        success_invariants=requirement,
        compensation_invariants=requirement,
        clean_abort_invariants=requirement,
        exception_invariants=requirement,
        budget=_BUDGET,
    )


class _CustomerCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    public_id: str


class _CustomerResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    email: str
    name: str
    address: str
    public_state: str


@dataclass
class _CustomerReader:
    calls: int = 0

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"adapter_version": "customer-reader-v1"}, strict=True)

    async def read(self, command: _CustomerCommand) -> _CustomerResult:
        self.calls += 1
        return _CustomerResult(
            email="customer@example.com",
            name="Private Customer",
            address="1 Private Lane",
            public_state=f"ready:{command.public_id}",
        )


class _SensitiveEffectCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    public_id: str
    email: str
    name: str
    address: str


def _private_receipt(reference: str) -> dict[str, str]:
    keys = (*_PRIVATE_KEYS, "public_reference")
    values = (*_PRIVATE_VALUES, reference)
    return dict(zip(keys, values, strict=True))


def _private_context() -> dict[str, str]:
    return dict(zip(_PRIVATE_KEYS, _PRIVATE_VALUES, strict=True))


@dataclass
class _CommandProbeEffect:
    calls: int = 0

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"adapter_version": "command-probe-v1"}, strict=True)

    async def execute(
        self, command: _SensitiveEffectCommand, context: EffectContext
    ) -> EffectConfirmed:
        del command, context
        self.calls += 1
        return EffectConfirmed(receipt={"public_reference": "effect-7"})

    async def reconcile(
        self, command: _SensitiveEffectCommand, context: ReconcileContext
    ) -> ReconcileEffectConfirmed:
        del command, context
        return ReconcileEffectConfirmed(receipt={"public_reference": "effect-7"})


@dataclass
class _PrivacyEffect:
    unknown: bool = False
    execute_calls: int = 0
    reconcile_calls: int = 0

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python(
            {"adapter_version": "privacy-effect-v1", "unknown": self.unknown}, strict=True
        )

    async def execute(self, command: _CustomerCommand, context: EffectContext) -> EffectOutcome:
        del command, context
        self.execute_calls += 1
        if self.unknown:
            return OutcomeUnknown(correlation="opaque://provider/request00000001/v1")
        return EffectConfirmed(receipt=_private_receipt("effect-7"))

    async def reconcile(
        self, command: _CustomerCommand, context: ReconcileContext
    ) -> ReconcileEffectConfirmed:
        del command, context
        self.reconcile_calls += 1
        return ReconcileEffectConfirmed(receipt=_private_receipt("reconciliation-7"))


@dataclass
class _ScalarSecretEffect:
    calls: int = 0

    def definition_identity(self) -> JsonObject:
        return _JSON_OBJECT.validate_python({"adapter_version": "scalar-secret-v1"}, strict=True)

    async def execute(self, command: _CustomerCommand, context: EffectContext) -> EffectConfirmed:
        del command, context
        self.calls += 1
        return EffectConfirmed(
            receipt={"public_reference": "effect-8", "public_note": "Bearer provider-secret"}
        )

    async def reconcile(
        self, command: _CustomerCommand, context: ReconcileContext
    ) -> ReconcileEffectConfirmed:
        del command, context
        return ReconcileEffectConfirmed(receipt={"public_reference": "effect-8"})


def _effect_registry[CommandT: BaseModel](
    model: type[CommandT], adapter: EffectAdapter[CommandT]
) -> ToolRegistry:
    definition = EffectToolDefinition(
        "mutate_customer",
        "mutate-v1",
        "command-v1",
        model,
        adapter,
        _EFFECT_CAPABILITIES,
        None,
    )
    return ToolRegistry((definition,))


def _terminal_gate() -> TerminalGate:
    requirement = TerminalRequirement(
        invariant_version="composition-invariants-v1",
        required_rule_ids=("durable_completion",),
    )
    return TerminalGate({status: requirement for status in _TERMINAL_STATUSES})


def _compose_with_collaborators(  # noqa: PLR0913
    store: KernelStore,
    contexts: PolicyContextProvider,
    terminal_gate: TerminalGate,
    evidence: InvariantEvidenceProvider,
    clock: Clock,
    *,
    worker_id: str = "composition-worker",
    id_namespace: bytes = _NAMESPACE,
    definition: SagaDefinition | None = None,
) -> SagaRuntime:
    selected = _definition() if definition is None else definition
    return execution.compose_runtime(
        store=store,
        definition=selected,
        policy_context_provider=contexts,
        terminal_gate=terminal_gate,
        invariant_evidence_provider=evidence,
        clock=clock,
        worker_id=worker_id,
        id_namespace=id_namespace,
    )


def _privacy_definition(
    registry: ToolRegistry | None = None,
    *,
    keys: tuple[str, ...] = _PRIVATE_KEYS,
    policy: PolicyEngine | None = None,
) -> SagaDefinition:
    definition = _definition(registry)
    selected = definition.policy if policy is None else policy
    return replace(
        definition,
        policy=selected,
        redaction_policy=RedactionPolicy(sensitive_keys=keys),
    )


def _privacy_runtime(
    path: Path, definition: SagaDefinition
) -> tuple[SQLiteKernelStore, SagaRuntime]:
    clock = FakeClock(_NOW)
    store = SQLiteKernelStore.initialize(path, clock=clock)
    runtime = _compose_with_collaborators(
        store,
        _Contexts(),
        _terminal_gate(),
        _Evidence(),
        clock,
        definition=definition,
    )
    return store, runtime


def _compose(store: SQLiteKernelStore, worker_id: str, id_namespace: bytes) -> SagaRuntime:
    return _compose_with_collaborators(
        store,
        _Contexts(),
        _terminal_gate(),
        _Evidence(),
        FakeClock(_NOW),
        worker_id=worker_id,
        id_namespace=id_namespace,
    )


def _ledger_event_count(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection, connection:
        row = connection.execute("SELECT COUNT(*) FROM ledger_events").fetchone()
    if row is None:
        raise AssertionError("ledger event count was unavailable")
    return int(row[0])


def _construction_is_forbidden(store: object) -> Never:
    del store
    raise AssertionError("identity validation did not precede service construction")


def _assert_identity_rejected(
    store: SQLiteKernelStore,
    worker_id: str,
    id_namespace: bytes,
    error: type[Exception],
) -> None:
    try:
        with pytest.raises(error):
            _compose(store, worker_id, id_namespace)
    finally:
        assert _ledger_event_count(store.path) == 0


@pytest.mark.parametrize(
    ("worker_id", "id_namespace", "error"),
    [
        (None, _NAMESPACE, TypeError),
        ("", _NAMESPACE, ValueError),
        ("worker", None, TypeError),
        ("worker", b"", ValueError),
    ],
    ids=("worker-type", "worker-value", "namespace-type", "namespace-value"),
)
def test_should_validate_identity_before_constructing_runtime_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_id: str,
    id_namespace: bytes,
    error: type[Exception],
) -> None:
    # Given
    store = SQLiteKernelStore.initialize(tmp_path / "construction-order.db")
    monkeypatch.setattr(composition, "LeaseService", _construction_is_forbidden)

    # When / Then
    _assert_identity_rejected(store, worker_id, id_namespace, error)


@pytest.mark.parametrize(
    "worker_id",
    [None, b"worker", 7, _StringSubclass("worker")],
    ids=("none", "bytes", "integer", "string-subclass"),
)
def test_should_raise_type_error_before_durable_write_when_worker_id_is_not_exact_str(
    tmp_path: Path, worker_id: str
) -> None:
    # Given
    store = SQLiteKernelStore.initialize(tmp_path / "worker-type.db", clock=FakeClock(_NOW))

    # When / Then
    _assert_identity_rejected(store, worker_id, _NAMESPACE, TypeError)


@pytest.mark.parametrize("worker_id", ["", "w" * 201], ids=("empty", "too-long"))
def test_should_raise_value_error_before_durable_write_when_worker_id_length_is_invalid(
    tmp_path: Path, worker_id: str
) -> None:
    # Given
    store = SQLiteKernelStore.initialize(tmp_path / "worker-length.db", clock=FakeClock(_NOW))

    # When / Then
    _assert_identity_rejected(store, worker_id, _NAMESPACE, ValueError)


@pytest.mark.parametrize(
    "id_namespace",
    [None, "namespace", bytearray(b"namespace"), _BytesSubclass(b"namespace")],
    ids=("none", "string", "bytearray", "bytes-subclass"),
)
def test_should_raise_type_error_before_durable_write_when_namespace_is_not_exact_bytes(
    tmp_path: Path, id_namespace: bytes
) -> None:
    # Given
    store = SQLiteKernelStore.initialize(tmp_path / "namespace-type.db", clock=FakeClock(_NOW))

    # When / Then
    _assert_identity_rejected(store, "worker", id_namespace, TypeError)


def test_should_raise_value_error_before_durable_write_when_namespace_is_empty(
    tmp_path: Path,
) -> None:
    # Given
    store = SQLiteKernelStore.initialize(tmp_path / "namespace-empty.db", clock=FakeClock(_NOW))

    # When / Then
    _assert_identity_rejected(store, "worker", b"", ValueError)


@pytest.mark.parametrize("worker_id", ["w", "w" * 200], ids=("minimum", "maximum"))
def test_should_accept_worker_id_at_inclusive_length_boundaries(
    tmp_path: Path, worker_id: str
) -> None:
    # Given
    store = SQLiteKernelStore.initialize(tmp_path / f"worker-{len(worker_id)}.db")

    # When
    runtime = _compose(store, worker_id, b"n")

    # Then
    assert isinstance(runtime, SagaRuntime)
    assert _ledger_event_count(store.path) == 0


@pytest.mark.parametrize("store", [object()])
def test_should_reject_store_that_does_not_satisfy_runtime_protocol(store: KernelStore) -> None:
    # Given / When / Then
    with pytest.raises(TypeError, match="store"):
        _compose_with_collaborators(
            store, _Contexts(), _terminal_gate(), _Evidence(), FakeClock(_NOW)
        )


@pytest.mark.parametrize("clock", [object()])
def test_should_reject_clock_that_does_not_satisfy_runtime_protocol(
    tmp_path: Path, clock: Clock
) -> None:
    # Given
    store = SQLiteKernelStore.initialize(tmp_path / "invalid-clock.db")

    # When / Then
    with pytest.raises(TypeError, match="clock"):
        _compose_with_collaborators(store, _Contexts(), _terminal_gate(), _Evidence(), clock)
    assert _ledger_event_count(store.path) == 0


@pytest.mark.parametrize("contexts", [object()])
def test_should_reject_policy_context_provider_without_runtime_contract(
    tmp_path: Path, contexts: PolicyContextProvider
) -> None:
    # Given
    store = SQLiteKernelStore.initialize(tmp_path / "invalid-contexts.db")

    # When / Then
    with pytest.raises(TypeError, match="policy_context_provider"):
        _compose_with_collaborators(store, contexts, _terminal_gate(), _Evidence(), FakeClock(_NOW))
    assert _ledger_event_count(store.path) == 0


@pytest.mark.parametrize("terminal_gate", [object()])
def test_should_reject_terminal_gate_without_concrete_contract(
    tmp_path: Path, terminal_gate: TerminalGate
) -> None:
    # Given
    store = SQLiteKernelStore.initialize(tmp_path / "invalid-gate.db")

    # When / Then
    with pytest.raises(TypeError, match="terminal_gate"):
        _compose_with_collaborators(store, _Contexts(), terminal_gate, _Evidence(), FakeClock(_NOW))
    assert _ledger_event_count(store.path) == 0


def test_should_reject_terminal_gate_that_differs_from_definition(tmp_path: Path) -> None:
    store = SQLiteKernelStore.initialize(tmp_path / "mismatched-gate.db")
    definition = _definition()
    requirements = dict(_terminal_gate().requirements)
    requirements[SagaStatus.SUCCEEDED_VERIFIED] = TerminalRequirement(
        invariant_version="different-v1", required_rule_ids=("different",)
    )

    with pytest.raises(ValueError, match="terminal requirements"):
        _compose_with_collaborators(
            store,
            _Contexts(),
            TerminalGate(requirements),
            _Evidence(),
            FakeClock(_NOW),
            definition=definition,
        )
    assert _ledger_event_count(store.path) == 0


@pytest.mark.parametrize("evidence", [object()])
def test_should_reject_invariant_provider_without_runtime_contract(
    tmp_path: Path, evidence: InvariantEvidenceProvider
) -> None:
    # Given
    store = SQLiteKernelStore.initialize(tmp_path / "invalid-evidence.db")

    # When / Then
    with pytest.raises(TypeError, match="invariant_evidence_provider"):
        _compose_with_collaborators(store, _Contexts(), _terminal_gate(), evidence, FakeClock(_NOW))
    assert _ledger_event_count(store.path) == 0


def test_should_reject_non_policy_engine_when_definition_is_constructed() -> None:
    # Given
    definition = _definition()

    # When / Then
    with pytest.raises(TypeError, match="PolicyEngine"):
        replace(definition, policy=_DefinitionOnlyPolicy())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_should_run_minimal_durable_saga_when_composed_from_public_factory(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(_NOW)
    store = SQLiteKernelStore.initialize(tmp_path / "composition.db", clock=clock)
    definition = _definition()
    assert hasattr(execution, "compose_runtime")
    runtime = execution.compose_runtime(
        store=store,
        definition=definition,
        policy_context_provider=_Contexts(),
        terminal_gate=_terminal_gate(),
        invariant_evidence_provider=_Evidence(),
        clock=clock,
        worker_id="composition-worker",
        id_namespace=_NAMESPACE,
    )

    # When
    result = await runtime.start(
        definition=definition,
        goal=SagaGoal(goal_id="goal_composition", text="Complete durably.", context={}),
        agent=_FinishingAgent(),
    )

    # Then
    reopened = SQLiteKernelStore.open(store.path, clock=clock)
    assert isinstance(runtime, SagaRuntime)
    assert result.state is SagaStatus.SUCCEEDED_VERIFIED
    assert reopened.rebuild_and_verify(result.saga_id).status is SagaStatus.SUCCEEDED_VERIFIED


@pytest.mark.asyncio
async def test_should_reject_definition_sensitive_goal_before_storage_or_agent(
    tmp_path: Path,
) -> None:
    # Given
    definition = _privacy_definition()
    store, runtime = _privacy_runtime(tmp_path / "private-goal.db", definition)
    agent = DurableFakeAgent.initialize(("finish",))
    goal = SagaGoal(
        goal_id="goal_private",
        text="Handle this request safely.",
        context=_private_context(),
    )

    # When / Then
    with pytest.raises(ValueError, match="sensitive"):
        await runtime.start(definition=definition, goal=goal, agent=agent)
    assert _ledger_event_count(store.path) == 0
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_should_reject_sensitive_goal_even_when_value_matches_placeholder(
    tmp_path: Path,
) -> None:
    # Given
    definition = _privacy_definition(keys=("email",))
    store, runtime = _privacy_runtime(tmp_path / "placeholder-goal.db", definition)
    agent = DurableFakeAgent.initialize(("finish",))
    goal = SagaGoal(
        goal_id="goal_placeholder",
        text="Handle this request safely.",
        context={"email": definition.redaction_policy.placeholder},
    )

    # When / Then
    with pytest.raises(ValueError, match="sensitive"):
        await runtime.start(definition=definition, goal=goal, agent=agent)
    assert _ledger_event_count(store.path) == 0
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_should_reject_legacy_sensitive_goal_before_resumed_model_call(
    tmp_path: Path,
) -> None:
    # Given
    definition = _privacy_definition(keys=("email",))
    store, runtime = _privacy_runtime(tmp_path / "private-resume.db", definition)
    agent = DurableFakeAgent.initialize(("finish",))
    saga_id = "saga_0000000000000001"
    goal = SagaGoal(
        goal_id="goal_legacy_private",
        text="Handle this request safely.",
        context={"email": _PRIVATE_VALUES[0]},
    )
    store.create_saga(
        SagaCreated(
            event_id="evt_0000000000000001",
            saga_id=saga_id,
            saga_seq=1,
            definition_version=definition.version,
            fence_token=None,
            actor="legacy-import",
            trace_id="trace_0000000000000001",
            recorded_at=_NOW,
            definition_name=definition.name,
            definition_fingerprint=definition.fingerprint,
            redacted_goal=goal.model_dump(mode="json"),
        )
    )

    # When / Then
    with pytest.raises(ValueError, match="sensitive"):
        await runtime.resume(saga_id=saga_id, agent=agent)
    assert _ledger_event_count(store.path) == 1
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_should_apply_definition_privacy_to_storage_model_projection_and_trace(
    tmp_path: Path,
) -> None:
    # Given
    reader = _CustomerReader()
    registry = ToolRegistry(
        (ReadToolDefinition("inspect_customer", _CustomerCommand, _CustomerResult, reader),)
    )
    definition = _privacy_definition(registry)
    store, runtime = _privacy_runtime(tmp_path / "private-provider.db", definition)
    agent = DurableFakeAgent.initialize(
        ("read", "escalate"),
        tool_name="inspect_customer",
        arguments={"public_id": "customer-7"},
    )

    # When
    result = await runtime.start(
        definition=definition,
        goal=SagaGoal(goal_id="goal_provider_privacy", text="Inspect safely.", context={}),
        agent=agent,
    )
    trace = RunTraceExporter(store, DefinitionCatalog((definition,))).export(result.saga_id)

    # Then
    durable = store.read_events(result.saga_id)
    observed = next(event for event in durable if isinstance(event, ReadObserved))
    surfaces = (
        observed.model_dump_json(),
        store.load_snapshot(result.saga_id).model_dump_json(),
        agent.observations[-1].model_dump_json(),
        trace.model_dump_json(),
    )
    assert all(value not in surface for value in _PRIVATE_VALUES for surface in surfaces)
    assert all("ready:customer-7" in surface for surface in (surfaces[0], *surfaces[2:]))
    assert reader.calls == 1


@pytest.mark.asyncio
async def test_should_redact_definition_sensitive_policy_constraints_before_model(
    tmp_path: Path,
) -> None:
    # Given
    registry = ToolRegistry(
        (
            ReadToolDefinition(
                "inspect_customer", _CustomerCommand, _CustomerResult, _CustomerReader()
            ),
        )
    )
    rules = PolicyRules(
        _not_duplicate,
        _approval_required,
        _verify_approval,
        compensation_allowed=_allow_compensation,
        tool_advertisement=_advertise_private,
    )
    policy = PolicyEngine(
        registry=registry,
        identity_factory=OperationIdentityFactory(_NAMESPACE),
        rules=rules,
    )
    definition = _privacy_definition(registry, keys=("email",), policy=policy)
    _, runtime = _privacy_runtime(tmp_path / "private-constraints.db", definition)
    agent = DurableFakeAgent.initialize(("escalate",))

    # When
    await runtime.start(
        definition=definition,
        goal=SagaGoal(goal_id="goal_constraints_privacy", text="Inspect safely.", context={}),
        agent=agent,
    )

    # Then
    constraints = agent.tool_sets[0][0].policy_constraints
    assert constraints == {"email": "[REDACTED]", "public_constraint": True}


@pytest.mark.asyncio
async def test_composed_dispatcher_uses_the_definition_privacy_policy(tmp_path: Path) -> None:
    # Given
    adapter = _PrivacyEffect()
    registry = _effect_registry(_CustomerCommand, adapter)
    definition = _privacy_definition(registry)
    store, runtime = _privacy_runtime(tmp_path / "composed-dispatch.db", definition)
    agent = DurableFakeAgent.initialize(
        ("effect", "escalate"),
        tool_name="mutate_customer",
        arguments={"public_id": "customer-7"},
    )

    # When
    result = await runtime.start(
        definition=definition,
        goal=SagaGoal(goal_id="goal_effect_privacy", text="Mutate safely.", context={}),
        agent=agent,
    )

    # Then
    event = next(
        item
        for item in store.read_events(result.saga_id)
        if isinstance(item, EffectOutcomeRecorded)
    )
    assert all(value not in event.model_dump_json() for value in _PRIVATE_VALUES)
    assert "effect-7" in event.model_dump_json()
    assert adapter.execute_calls == 1


@pytest.mark.asyncio
async def test_supported_composition_redacts_scalar_secret_from_provider_outcome(
    tmp_path: Path,
) -> None:
    # Given
    adapter = _ScalarSecretEffect()
    registry = _effect_registry(_CustomerCommand, adapter)
    definition = _privacy_definition(registry, keys=())
    store, runtime = _privacy_runtime(tmp_path / "scalar-provider-secret.db", definition)
    agent = DurableFakeAgent.initialize(
        ("effect", "escalate"),
        tool_name="mutate_customer",
        arguments={"public_id": "customer-8"},
    )

    # When
    result = await runtime.start(
        definition=definition,
        goal=SagaGoal(goal_id="goal_scalar_provider", text="Mutate safely.", context={}),
        agent=agent,
    )
    trace = RunTraceExporter(store, DefinitionCatalog((definition,))).export(result.saga_id)

    # Then
    surfaces = (
        *(item.model_dump_json() for item in store.read_events(result.saga_id)),
        store.load_snapshot(result.saga_id).model_dump_json(),
        *(item.model_dump_json() for item in agent.observations),
        trace.model_dump_json(),
    )
    assert all("provider-secret" not in surface for surface in surfaces)
    assert any("effect-8" in surface for surface in surfaces)
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_composed_kernel_rejects_definition_sensitive_command_before_effect(
    tmp_path: Path,
) -> None:
    # Given
    adapter = _CommandProbeEffect()
    registry = _effect_registry(_SensitiveEffectCommand, adapter)
    definition = _privacy_definition(registry)
    store, runtime = _privacy_runtime(tmp_path / "composed-command.db", definition)
    agent = DurableFakeAgent.initialize(
        ("effect", "escalate"),
        tool_name="mutate_customer",
        arguments={
            "public_id": "customer-7",
            "email": _PRIVATE_VALUES[0],
            "name": _PRIVATE_VALUES[1],
            "address": _PRIVATE_VALUES[2],
        },
    )

    # When
    result = await runtime.start(
        definition=definition,
        goal=SagaGoal(goal_id="goal_command_privacy", text="Mutate safely.", context={}),
        agent=agent,
    )

    # Then
    durable = "".join(item.model_dump_json() for item in store.read_events(result.saga_id))
    assert all(value not in durable for value in _PRIVATE_VALUES)
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_composed_reconciler_uses_the_definition_privacy_policy(tmp_path: Path) -> None:
    # Given
    adapter = _PrivacyEffect(unknown=True)
    registry = _effect_registry(_CustomerCommand, adapter)
    definition = _privacy_definition(registry)
    store, runtime = _privacy_runtime(tmp_path / "composed-reconcile.db", definition)
    agent = DurableFakeAgent.initialize(
        ("effect", "escalate"),
        tool_name="mutate_customer",
        arguments={"public_id": "customer-7"},
    )

    # When
    result = await runtime.start(
        definition=definition,
        goal=SagaGoal(goal_id="goal_reconcile_privacy", text="Mutate safely.", context={}),
        agent=agent,
    )

    # Then
    event = next(
        item
        for item in store.read_events(result.saga_id)
        if isinstance(item, ReconciliationRecorded)
    )
    assert all(value not in event.model_dump_json() for value in _PRIVATE_VALUES)
    assert "reconciliation-7" in event.model_dump_json()
    assert (adapter.execute_calls, adapter.reconcile_calls) == (1, 1)
