# Kernel Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic, crash-recoverable Saga kernel that gates typed side effects, records intent before effect, reconciles ambiguity, fences concurrent workers, compensates verified effects, and exports trustworthy run evidence.

**Architecture:** Keep all safety decisions in pure contracts, reducers, policies, and invariant gates; keep SQLite as the single-host durable system of record; and route network/tool I/O through a fenced dispatcher. The ledger is append-only and authoritative, the projection is rebuildable, every external delivery reuses a stable logical operation ID, and ambiguous outcomes remain nonterminal until reconciled or explicitly escalated. Installed driver/adapter implementations are trusted, cancellation-cooperative application code invoked in process; their inputs and outputs remain untrusted.

**Tech Stack:** Python 3.12+, Pydantic v2, standard-library `sqlite3`, `asyncio`, pytest, pytest-asyncio, Hypothesis, coverage.py, mutmut, Ruff, mypy, uv, hatchling

**Spec:** `docs/superpowers/specs/2026-09-06-agentic-saga-design.md`

> **Historical plan and supersession notice (2026-09-08):** This file records the implementation
> path and original file map; it is not the current public API or operating contract. The original
> draft included a fifth synthetic monetary budget and compatibility facades that were later
> rejected. Shipped v0.1 has only `turn_limit`, `tool_call_limit`, `token_limit`, and
> `elapsed_ms_limit`; token/time are deterministic per-turn maximum-output and agent-call-deadline
> allocations, not actual usage or spend. There is no monetary meter or provider spend cap. Current
> canonical homes and security boundaries are documented in
> `docs/kernel-safety-contract.md` and `docs/operations.md`; those documents and source code
> supersede paths, examples, test names, and status checkboxes below.

## Global Constraints

- Core runtime targets Python 3.12+ and depends on Pydantic plus the standard library; LangGraph, Deep Agents, and provider packages must not be imported by core modules.
- V0.1 does not sandbox installed Python plugins. Hostile or native integrations require an
  application-owned process, container, or service boundary outside the core library.
- Keep the kernel generic, lean, and composable: core owns only irreducible Saga semantics through
  small typed interfaces. Ecommerce remains a separate sample workflow, and framework/provider
  integrations stay outside core. Prefer maintained libraries and standard Python facilities over
  custom infrastructure or framework duplication.
- SQLite is the v0.1 single-host reference backend and must not be described as highly available or universally production-ready.
- Use strict Pydantic models with `extra="forbid"`; free-form dictionaries, `TypedDict`, and `dict[str, Any]` are forbidden at safety boundaries.
- Generate logical operation IDs inside the kernel from Saga ID, step instance, direction, and semantic generation; delivery attempts never alter the business idempotency key.
- Record intent, compensation obligation, outbox command, and projection update in one durable transaction before any mutating adapter call.
- Exceptions, disconnects, timeouts, and worker death after dispatch become `OUTCOME_UNKNOWN`, never inferred no-effect failures.
- Never retry an unknown effect unless authoritative reconciliation proves absence or the adapter declares provider idempotency for longer than the configured recovery horizon.
- Never compensate an unknown or unconfirmed effect; quiesce and reconcile possible late forward work first.
- No terminal state is legal with unknown operations, runnable commands, pending approvals, stale invariant evidence, or failing invariants.
- `HUMAN_REQUIRED` is durable and quiescent: no autonomous mutating command may remain runnable or be enqueued.
- Ordinary ledger payloads must be redacted; secrets, raw payment data, and private chain-of-thought are never stored.
- All clocks, ID sources, and failpoints used by safety-critical code are injectable and deterministic in tests.
- Required offline gates run with network disabled and no model credentials.
- Kernel, policy, state, and invariant modules require at least 90% branch coverage; a bounded,
  explicitly named safety-critical function set requires at least an 85% mutation score.
- Use `uv` for contributor commands and hatchling for packaging; do not publish to PyPI or any registry.
- Follow the repository Python quality contract: typed boundaries, functions no longer than 15 lines, Radon Grade A, Ruff, strict mypy, and no hidden legacy exemptions.

## Locked File Map

The kernel plan owns the following implementation boundaries. Do not move agent adapters, ecommerce business logic, BDD feature text, CLI code, or Flight Recorder UI into these files.

```text
src/agentic_saga/
  contracts/
    common.py       # IDs, JSON value types, directions, reversibility
    actions.py      # untrusted agent proposals and validated internal calls
    outcomes.py     # effect and reconciliation result unions
    tools.py        # typed effect definitions and registry
    runtime.py      # AgentDriver, goal, observation, descriptor, result contracts
    events.py       # versioned append-only ledger event union
    trace.py        # stored RunTrace contract
  kernel/
    canonical.py    # canonical JSON bytes and command/result hashes
    identity.py     # stable kernel-generated logical operation IDs
    runtime.py      # proposal-to-intent and terminal-assignment facade
    state.py        # immutable Saga/operation/obligation projections
    reducer.py      # pure event-to-projection reducer and replay
    policy.py       # proposal authorization and budgets
    invariants.py   # invariant evaluation and terminal gate
    compensation.py # compensation frontier and reverse dependency ordering
  storage/
    base.py         # store command/result protocol
    schema.sql      # SQLite schema, constraints, append-only triggers
    sqlite.py       # atomic ledger/projection/outbox implementation
  execution/
    clock.py        # injectable UTC clock
    leases.py       # lease/fence service
    dispatcher.py   # intent-first tool delivery and result recording
    reconciliation.py # unknown-outcome resolution
    unwind.py       # deterministic emergency unwind and quiescence
    runtime.py      # bounded incremental smart-agent orchestration loop
  evidence/
    redaction.py    # recursive key/value redaction
    run_trace.py    # ledger-to-RunTrace export
```

Every new package directory also receives an `__init__.py` exporting only its intended public symbols.

---

### Task 1: Strict Value, Proposal, and Outcome Contracts

**Files:**
- Create: `src/agentic_saga/contracts/common.py`
- Create: `src/agentic_saga/contracts/actions.py`
- Create: `src/agentic_saga/contracts/outcomes.py`
- Create: `src/agentic_saga/contracts/__init__.py`
- Create: `tests/unit/contracts/test_actions.py`
- Create: `tests/unit/contracts/test_outcomes.py`

**Interfaces:**
- Consumes: Pydantic v2 from repository bootstrap.
- Produces: `SagaId`, `StepInstanceId`, `OperationId`, `FenceToken`, `JsonValue`, `JsonObject`, `Direction`, `Reversibility`, public `ToolCall`, `Finish`, and `Escalate` proposal models, `AgentProposal`, `AuthorizedToolCall[CommandT]`, `HumanDecision`, `EffectConfirmed`, `NoEffectConfirmed`, `PartialEffectConfirmed`, `OutcomeUnknown`, `EffectOutcome`, all five `Reconcile*` variants, and `ReconciliationOutcome`.

- [ ] **Step 1: Write strict proposal tests**

```python
def test_tool_call_rejects_agent_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        ToolCall.model_validate(
            {
                "kind": "tool_call",
                "tool_name": "charge_payment",
                "arguments": {"amount_minor": 14900},
                "based_on_saga_seq": 3,
                "idempotency_key": "attacker-choice",
            }
        )


def test_tool_call_keeps_json_without_coercion() -> None:
    proposal = ToolCall.model_validate(
        {
            "kind": "tool_call",
            "tool_name": "charge_payment",
            "arguments": {"amount_minor": 14900},
            "based_on_saga_seq": 3,
        }
    )
    assert proposal.arguments == {"amount_minor": 14900}
```

- [ ] **Step 2: Run the proposal tests and observe the red failure**

Run: `uv run pytest tests/unit/contracts/test_actions.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'agentic_saga.contracts'`.

- [ ] **Step 3: Implement the common and action contracts**

Use strict aliases and frozen models. `ToolCall.arguments` is an untrusted JSON envelope only; Task 2 must convert it into a tool-specific Pydantic command before it becomes `AuthorizedToolCall`.

```python
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

SagaId = Annotated[str, StringConstraints(pattern=r"^saga_[a-z0-9]{16,64}$")]
StepInstanceId = Annotated[str, StringConstraints(pattern=r"^step_[a-z0-9]{8,64}$")]
OperationId = Annotated[str, StringConstraints(pattern=r"^op_[a-f0-9]{64}$")]
FenceToken = Annotated[int, Field(ge=1)]


class Direction(StrEnum):
    FORWARD = "forward"
    COMPENSATION = "compensation"


class ToolCall(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    kind: Literal["tool_call"] = "tool_call"
    tool_name: str
    arguments: JsonObject
    based_on_saga_seq: int = Field(ge=0)
    rationale: str = Field(min_length=1, max_length=500)
```

Define `Finish` and `Escalate` with the same strict configuration and `based_on_saga_seq`. Define `AgentProposal` as an annotated discriminated union on `kind`. Define generic, frozen `AuthorizedToolCall[CommandT: BaseModel]` with kernel-generated `operation_id`, `step_instance_id`, `direction`, `semantic_generation`, and typed `command`. The canonical public names imported by the ecommerce plan are `ToolCall`, `Finish`, and `Escalate` from `agentic_saga.contracts.actions`; do not create a second proposal hierarchy.

Define `HumanDecision` with `decision_id`, `saga_id`, `based_on_saga_seq`, `action: Literal["approve", "reject", "reconcile"]`, `proposal_hash`, `actor`, aware `issued_at`, and opaque `auth_proof`. Authentication is injected through the policy layer; the ledger stores only the actor, decision identity, proposal hash, and verification result, never reusable credentials.

- [ ] **Step 4: Add and run typed outcome tests**

```python
def test_unknown_outcome_requires_correlation() -> None:
    outcome = OutcomeUnknown(correlation="provider-request-7")
    assert outcome.kind == "outcome_unknown"


def test_partial_effect_requires_at_least_one_receipt() -> None:
    with pytest.raises(ValidationError):
        PartialEffectConfirmed(receipts=())


def test_reconciliation_conflict_is_distinct_from_unknown() -> None:
    outcome = ReconcileConflict(reason="two provider records")
    assert outcome.kind == "reconcile_conflict"
```

Implement a strict discriminated union whose variants are:

```python
class EffectConfirmed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    kind: Literal["effect_confirmed"] = "effect_confirmed"
    receipt: JsonObject


class NoEffectConfirmed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    kind: Literal["no_effect_confirmed"] = "no_effect_confirmed"
    reason: str = Field(min_length=1, max_length=500)


class PartialEffectConfirmed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    kind: Literal["partial_effect_confirmed"] = "partial_effect_confirmed"
    receipts: tuple[JsonObject, ...] = Field(min_length=1)


class OutcomeUnknown(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    kind: Literal["outcome_unknown"] = "outcome_unknown"
    correlation: str = Field(min_length=1, max_length=500)
```

In the same module, define strict frozen `ReconcileEffectConfirmed`,
`ReconcileNoEffectConfirmed`, `ReconcilePending`, `ReconcileConflict`, and
`ReconcileUnsupported` models. `ReconcileEffectConfirmed` carries one typed JSON receipt,
`ReconcileNoEffectConfirmed` and the conflict/unsupported variants carry a bounded reason,
and `ReconcilePending` carries a correlation plus an aware UTC `check_after`. Export them as
the `ReconciliationOutcome` discriminated union. Defining these provider-boundary values now
lets Task 2 type its reconciler without a forward reference to a module that does not yet exist.

Run: `uv run pytest tests/unit/contracts/test_actions.py tests/unit/contracts/test_outcomes.py -q`

Expected: all tests pass; invalid extra fields and empty partial receipts are rejected.

- [ ] **Step 5: Run focused quality checks and commit**

Run: `uv run ruff check src/agentic_saga/contracts tests/unit/contracts && uv run mypy src/agentic_saga/contracts`

Expected: both commands exit 0.

```bash
git add src/agentic_saga/contracts tests/unit/contracts
git commit -m "feat(kernel): add strict action and outcome contracts"
```

---

### Task 2: Typed Effect Definitions, Registry, and Stable Operation Identity

**Files:**
- Create: `src/agentic_saga/contracts/tools.py`
- Create: `src/agentic_saga/kernel/identity.py`
- Create: `tests/unit/contracts/test_tools.py`
- Create: `tests/unit/kernel/test_identity.py`

**Interfaces:**
- Consumes: `AuthorizedToolCall`, `Direction`, `EffectOutcome`, `JsonObject`, `OperationId`, `SagaId`, and `StepInstanceId` from Task 1.
- Produces: `EffectContext`, `ReconcileContext`, `EffectAdapter[CommandT]`, `ReadToolDefinition`, `EffectToolDefinition[CommandT]`, `ToolCapabilities`, `ToolRegistry.register_read(...)`, `ToolRegistry.register_effect(...)`, `ToolRegistry.definition(...)`, `OperationIdentityFactory.create(...)`, and `UnknownToolError`.

- [ ] **Step 1: Write registry tests that prove strict conversion occurs before authorization**

```python
class ChargeCommand(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    amount_minor: int = Field(gt=0)
    currency: Literal["USD"]


def test_registry_rejects_extra_command_field() -> None:
    registry = registry_with_charge()
    with pytest.raises(ValidationError):
        registry.validate_command(
            "charge_payment",
            {"amount_minor": 14900, "currency": "USD", "admin": True},
        )


def test_registry_rejects_unknown_tool() -> None:
    with pytest.raises(UnknownToolError):
        ToolRegistry(()).definition("wire_money")
```

- [ ] **Step 2: Run the registry tests and observe the red failure**

Run: `uv run pytest tests/unit/contracts/test_tools.py -q`

Expected: collection fails because `EffectToolDefinition` and `ToolRegistry` do not exist.

- [ ] **Step 3: Implement effect capabilities and registry**

```python
class ToolCapabilities(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    idempotency_retention_seconds: int | None = Field(default=None, gt=0)
    reconciliation_supported: bool
    cancellation_supported: bool
    fencing_supported: bool
    reversibility: Reversibility
    partial_effects_possible: bool


@dataclass(frozen=True)
class EffectToolDefinition(Generic[CommandT]):
    name: str
    input_model: type[CommandT]
    adapter: EffectAdapter[CommandT]
    capabilities: ToolCapabilities
    compensate_with: str | None
    compensation_dependencies: tuple[str, ...] = ()


class ToolRegistry:
    def register_read(self, definition: ReadToolDefinition) -> None:
        self._register(definition)

    def register_effect(self, definition: EffectToolDefinition[BaseModel]) -> None:
        self._register(definition)

    def definition(self, name: str) -> ReadToolDefinition | EffectToolDefinition[BaseModel]:
        return self._definitions[name]

    def validate_command(self, name: str, raw: JsonObject) -> BaseModel:
        definition = self.definition(name)
        return definition.input_model.model_validate(raw)
```

Define the exact public adapter boundary consumed by the ecommerce plan:

```python
class EffectContext(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    saga_id: SagaId
    step_instance_id: StepInstanceId
    operation_id: OperationId
    fence_token: FenceToken
    delivery_attempt: int = Field(ge=1)


class ReconcileContext(EffectContext):
    correlation: str


class EffectAdapter(Protocol[CommandT]):
    async def execute(self, command: CommandT, context: EffectContext) -> EffectOutcome: ...
    async def reconcile(
        self,
        command: CommandT,
        context: ReconcileContext,
    ) -> EffectOutcome: ...
```

`ReadToolDefinition` has a strict `input_model`, strict result model, and async read adapter but no compensation or idempotency capabilities. Both definition types expose `input_model`, so registry validation is uniform. Use `EffectToolDefinition` as the single public effect-definition name.
The public adapter signature deliberately matches the ecommerce plan: reconciliation reports the
same four observed external outcomes as execution. Task 9 normalizes those outcomes plus missing
lookup capability, contradictory receipts, and provider protocol errors into the internal
`ReconciliationOutcome` decision union.

- [ ] **Step 4: Write and implement stable identity tests**

```python
def test_delivery_attempt_does_not_change_operation_id() -> None:
    factory = OperationIdentityFactory(namespace=b"test-namespace")
    first = factory.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    retry = factory.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    assert first == retry


def test_semantic_generation_changes_operation_id() -> None:
    factory = OperationIdentityFactory(namespace=b"test-namespace")
    first = factory.create(SAGA_ID, STEP_ID, Direction.FORWARD, 0)
    second = factory.create(SAGA_ID, STEP_ID, Direction.FORWARD, 1)
    assert first != second
```

Implement `OperationIdentityFactory.create` as SHA-256 over a length-delimited canonical byte sequence containing namespace, Saga ID, step instance ID, direction, and semantic generation. Prefix the 64-character lowercase hex digest with `op_`. Do not accept a delivery attempt parameter.

Run: `uv run pytest tests/unit/contracts/test_tools.py tests/unit/kernel/test_identity.py -q`

Expected: all tests pass, including duplicate-name registry rejection and stable identity tests.

- [ ] **Step 5: Commit the tool boundary**

```bash
git add src/agentic_saga/contracts/tools.py src/agentic_saga/kernel/identity.py tests/unit/contracts/test_tools.py tests/unit/kernel/test_identity.py
git commit -m "feat(kernel): register typed effects and stable identities"
```

---

### Task 3: Append-Only Event Contract and Pure Saga Reducer

**Files:**
- Create: `src/agentic_saga/contracts/events.py`
- Create: `src/agentic_saga/kernel/state.py`
- Create: `src/agentic_saga/kernel/reducer.py`
- Create: `src/agentic_saga/kernel/__init__.py`
- Create: `tests/unit/kernel/test_reducer.py`
- Create: `tests/unit/kernel/test_replay.py`

**Interfaces:**
- Consumes: IDs, directions, effect outcomes, and JSON values from Tasks 1–2.
- Produces: `SagaStatus`, `OperationStatus`, `ObligationStatus`, `OperationRecord`, `CompensationObligation`, `SagaSnapshot`, `LedgerEvent` union, `reduce_event(snapshot, event)`, `rebuild_projection(events)`, `InvalidTransition`, and `SequenceGap`.

- [ ] **Step 1: Write reducer transition tests**

```python
def test_intent_arms_compensation_before_dispatch() -> None:
    created = reduce_event(None, saga_created(seq=1))
    running = reduce_event(created, saga_started(seq=2))
    intended = reduce_event(running, effect_intent(seq=3, compensate_with="refund"))
    assert intended.operations[OP_ID].status is OperationStatus.INTENT_DURABLE
    assert intended.obligations[OP_ID].status is ObligationStatus.ARMED


def test_timeout_is_recorded_as_unknown_not_failure() -> None:
    snapshot = replay_to_dispatched_operation()
    unknown = reduce_event(snapshot, outcome_unknown_event(seq=snapshot.seq + 1))
    assert unknown.operations[OP_ID].status is OperationStatus.OUTCOME_UNKNOWN
    assert unknown.status is SagaStatus.RECONCILING_UNKNOWN


def test_sequence_gap_fails_closed() -> None:
    with pytest.raises(SequenceGap):
        reduce_event(snapshot_at_seq(4), saga_started(seq=6))
```

- [ ] **Step 2: Run reducer tests and verify the intended failure**

Run: `uv run pytest tests/unit/kernel/test_reducer.py -q`

Expected: collection fails because the state and reducer modules are absent.

- [ ] **Step 3: Implement immutable state and discriminated events**

Create strict frozen event models for:

```text
SagaCreated, SagaStarted, EffectIntentRecorded, DispatchStarted,
EffectOutcomeRecorded, RecoveryPlanRequired, RecoveryPlanAccepted,
RecoveryPlanRejected, CompensationStarted, CompensationIntentRecorded,
InvariantEvaluated, HumanRequired, HumanResolutionRecorded, TerminalAssigned
```

Every event includes `event_id`, `saga_id`, positive `saga_seq`, `schema_version`, `definition_version`, `fence_token | None`, `actor`, `trace_id`, and `recorded_at`. Effect events additionally carry `operation_id`, `step_instance_id`, `direction`, `semantic_generation`, `delivery_attempt`, `tool_name`, a redacted canonical command/result, hashes, and receipt/correlation data.

Model `SagaSnapshot` as a frozen value with `operations` and `obligations` mappings copied on write, never mutated in place:

```python
class SagaSnapshot(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    saga_id: SagaId
    seq: int = Field(ge=1)
    status: SagaStatus
    definition_version: str
    operations: dict[OperationId, OperationRecord]
    obligations: dict[OperationId, CompensationObligation]
    last_invariant_seq: int | None = None
    pending_approval: bool = False
```

- [ ] **Step 4: Implement a total reducer and replay**

Use one small handler per event type and an explicit handler table. Reject unknown event types, sequence gaps, Saga ID changes, stale definition changes, dispatch before intent, terminal-to-nonterminal transitions, and compensation eligibility for absent/unknown effects.

```python
def reduce_event(
    snapshot: SagaSnapshot | None,
    event: LedgerEvent,
) -> SagaSnapshot:
    if snapshot is None:
        return _create_snapshot(event)
    _validate_common_transition(snapshot, event)
    return _HANDLERS[type(event)](snapshot, event)


def rebuild_projection(events: Iterable[LedgerEvent]) -> SagaSnapshot:
    snapshot: SagaSnapshot | None = None
    for event in events:
        snapshot = reduce_event(snapshot, event)
    if snapshot is None:
        raise SequenceGap("a Saga requires SagaCreated at sequence 1")
    return snapshot
```

Run: `uv run pytest tests/unit/kernel/test_reducer.py tests/unit/kernel/test_replay.py -q`

Expected: all legal transitions pass; malformed sequences and false terminal transitions fail closed.

- [ ] **Step 5: Commit the state machine**

```bash
git add src/agentic_saga/contracts/events.py src/agentic_saga/kernel/state.py src/agentic_saga/kernel/reducer.py src/agentic_saga/kernel/__init__.py tests/unit/kernel/test_reducer.py tests/unit/kernel/test_replay.py
git commit -m "feat(kernel): add append-only events and pure reducer"
```

---

### Task 4: Deterministic Proposal Policy and Terminal Invariant Gate

**Files:**
- Create: `src/agentic_saga/kernel/policy.py`
- Create: `src/agentic_saga/kernel/invariants.py`
- Create: `tests/unit/kernel/test_policy.py`
- Create: `tests/unit/kernel/test_invariants.py`

**Interfaces:**
- Consumes: `AgentProposal`, `AuthorizedToolCall`, `ToolRegistry`, `SagaSnapshot`, and operation identity from Tasks 1–3.
- Produces: `PolicyContext`, `ExecutionBudget`, `PolicyDecision`, `PolicyEngine.authorize(...)`, `InvariantRule`, `InvariantResult`, `InvariantEvidence`, `TerminalGate.evaluate(...)`, `StaleProposal`, and `TerminalStateDenied`.

- [ ] **Step 1: Write policy tests for stale, malformed, over-budget, and duplicate effects**

```python
def test_stale_proposal_is_rejected_without_authorized_call() -> None:
    decision = policy.authorize(tool_proposal(seq=4), snapshot_at_seq(5), context())
    assert decision.allowed is False
    assert decision.code == "stale_proposal"
    assert decision.authorized_call is None


def test_second_charge_requires_new_policy_authorization() -> None:
    snapshot = snapshot_with_confirmed_charge()
    decision = policy.authorize(charge_proposal(snapshot.seq), snapshot, context())
    assert decision.allowed is False
    assert decision.code == "duplicate_business_effect"


def test_raw_arguments_are_converted_to_typed_command() -> None:
    decision = policy.authorize(charge_proposal(2), snapshot_at_seq(2), context())
    assert decision.allowed is True
    assert isinstance(decision.authorized_call.command, ChargeCommand)
```

- [ ] **Step 2: Run policy tests and observe the red failure**

Run: `uv run pytest tests/unit/kernel/test_policy.py -q`

Expected: collection fails because `PolicyEngine` is not defined.

- [ ] **Step 3: Implement ordered fail-closed policy checks**

`PolicyEngine.authorize` must check, in this order: Saga is mutable; proposal sequence is current;
tool is registered; raw arguments validate into the tool-specific strict model; no conflicting
unknown or in-flight operation exists; no duplicate semantic effect exists; approval requirements
pass; and turn/tool/output-token/agent-call-time allocations remain. Return a reason code and redacted explanation
for every rejection.

```python
class PolicyDecision(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    allowed: bool
    code: str
    explanation: str
    authorized_call: AuthorizedToolCall[BaseModel] | None
```

Task 18 supersedes the original domain callback slots. Current domain constraints belong in strict
tool schemas and adapters; the planned manifest integration may register named application policy
checks outside the generic kernel. Tool output is data in `PolicyContext`; it never adds
capabilities or approval.

- [ ] **Step 4: Write and implement fresh invariant gating**

```python
def test_success_requires_evidence_at_current_sequence() -> None:
    evidence = passing_evidence(evaluated_at_seq=8)
    with pytest.raises(TerminalStateDenied, match="stale invariant evidence"):
        TerminalGate().evaluate(SagaStatus.SUCCEEDED_VERIFIED, snapshot_at_seq(9), evidence, 0)


def test_unknown_operation_blocks_every_terminal_state() -> None:
    snapshot = snapshot_with_unknown_operation()
    with pytest.raises(TerminalStateDenied, match="unknown operation"):
        TerminalGate().evaluate(
            SagaStatus.RESOLVED_WITH_EXCEPTION,
            snapshot,
            passing_evidence(snapshot.seq),
            runnable_commands=0,
        )
```

Define `InvariantRule` as a named synchronous callable over typed authoritative evidence, not only the local projection. `InvariantEvidence` includes `evaluated_at_seq`, `invariant_version`, per-rule inputs, and `InvariantResult` values. `TerminalGate.evaluate` denies terminal assignment when any operation is unknown, runnable command count is nonzero, approval is pending, evidence sequence is stale, a required rule failed, a compensation obligation is unresolved, or human exception evidence is missing.

Run: `uv run pytest tests/unit/kernel/test_policy.py tests/unit/kernel/test_invariants.py -q`

Expected: all tests pass; a false finish proposal cannot create a terminal event.

- [ ] **Step 5: Commit the deterministic gates**

```bash
git add src/agentic_saga/kernel/policy.py src/agentic_saga/kernel/invariants.py tests/unit/kernel/test_policy.py tests/unit/kernel/test_invariants.py
git commit -m "feat(kernel): enforce policy and terminal invariants"
```

---

### Task 5: SQLite Ledger, Projection, and Outbox Transaction

**Files:**
- Create: `src/agentic_saga/storage/base.py`
- Create: `src/agentic_saga/storage/schema.sql`
- Create: `src/agentic_saga/storage/sqlite.py`
- Create: `src/agentic_saga/storage/__init__.py`
- Create: `tests/integration/storage/test_sqlite_store.py`
- Create: `tests/integration/storage/test_projection_rebuild.py`
- Create: `tests/integration/storage/test_append_only.py`
- Create: `tests/integration/storage/test_backup_restore.py`

**Interfaces:**
- Consumes: `LedgerEvent`, `SagaSnapshot`, and `rebuild_projection` from Task 3.
- Produces: `OutboxCommand`, `TransitionBatch`, `ClaimedCommand`, `StoreConflict`, `StaleFence`, `KernelStore` protocol, and `SQLiteKernelStore` with `initialize`, `create_saga`, `commit_transition`, `load_snapshot`, `read_events`, `claim_outbox`, `complete_outbox`, `runnable_count`, and `rebuild_and_verify`.

- [ ] **Step 1: Write the atomic intent/outbox/projection integration test**

```python
def test_transition_atomically_appends_event_projection_and_outbox(
    store: SQLiteKernelStore,
) -> None:
    snapshot = store.create_saga(saga_created(seq=1))
    batch = intent_batch(snapshot, effect_intent(seq=2), outbox_command())
    updated = store.commit_transition(batch)
    assert updated.seq == 2
    assert [event.saga_seq for event in store.read_events(SAGA_ID)] == [1, 2]
    assert store.runnable_count(SAGA_ID) == 1
    assert store.rebuild_and_verify(SAGA_ID) == updated
```

- [ ] **Step 2: Run the storage test and observe the red failure**

Run: `uv run pytest tests/integration/storage/test_sqlite_store.py -q`

Expected: collection fails because `SQLiteKernelStore` does not exist.

- [ ] **Step 3: Implement the schema with database-enforced invariants**

Create tables:

```sql
CREATE TABLE sagas (
  saga_id TEXT PRIMARY KEY,
  saga_seq INTEGER NOT NULL CHECK (saga_seq >= 1),
  status TEXT NOT NULL,
  definition_version TEXT NOT NULL,
  projection_json TEXT NOT NULL,
  fence_token INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at TEXT
);

CREATE TABLE ledger_events (
  event_id TEXT PRIMARY KEY,
  saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
  saga_seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  event_json TEXT NOT NULL,
  event_hash TEXT NOT NULL,
  prior_hash TEXT,
  UNIQUE (saga_id, saga_seq)
);

CREATE TABLE outbox_commands (
  command_id TEXT PRIMARY KEY,
  saga_id TEXT NOT NULL REFERENCES sagas(saga_id),
  operation_id TEXT NOT NULL,
  command_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('runnable','claimed','completed','parked')),
  available_at TEXT NOT NULL,
  claim_owner TEXT,
  claim_expires_at TEXT,
  delivery_attempt INTEGER NOT NULL DEFAULT 0,
  UNIQUE (saga_id, operation_id)
);
```

Add `BEFORE UPDATE` and `BEFORE DELETE` triggers on `ledger_events` that abort with `ledger_events is append-only`. Add indexes for runnable outbox scans and Saga event replay. Enable `foreign_keys=ON`, WAL mode, `synchronous=FULL`, and a bounded `busy_timeout` on every connection.

- [ ] **Step 4: Implement one-transaction compare-and-swap commits**

`TransitionBatch` contains the exact expected Saga sequence, expected fence token, ordered new events, resulting projection, and zero or more outbox commands. `commit_transition` uses `BEGIN IMMEDIATE`, verifies sequence/fence, appends hash-chained events, inserts commands, updates the projection, and commits. Any exception rolls the entire transaction back.

Add a failpoint after each statement in tests and prove rollback leaves no partial event, projection, or outbox row:

```python
@pytest.mark.parametrize("failpoint", ["after_event", "after_outbox", "after_projection"])
def test_failed_transition_is_fully_rolled_back(store_factory, failpoint: str) -> None:
    store = store_factory(failpoint=failpoint)
    before = store.load_snapshot(SAGA_ID)
    with pytest.raises(InjectedStoreFailure):
        store.commit_transition(intent_batch(before))
    assert store.load_snapshot(SAGA_ID) == before
    assert store.runnable_count(SAGA_ID) == 0
```

Implement `SQLiteKernelStore.backup_to(destination: Path)` with SQLite's online backup API,
then open the destination through a new store instance, run `PRAGMA integrity_check`, verify the
event hash chain, and compare `rebuild_and_verify` with the source projection:

```python
def test_online_backup_restores_authoritative_projection(store, tmp_path: Path) -> None:
    expected = populate_saga_with_confirmed_effect(store)
    backup_path = tmp_path / "restored.db"
    store.backup_to(backup_path)
    restored = SQLiteKernelStore.open(backup_path)
    assert restored.integrity_check() == "ok"
    assert restored.rebuild_and_verify(SAGA_ID) == expected
```

Run: `uv run pytest tests/integration/storage -q`

Expected: all tests pass; direct event update/delete fails; replay matches the stored material projection exactly.

- [ ] **Step 5: Commit atomic storage**

```bash
git add src/agentic_saga/storage tests/integration/storage
git commit -m "feat(storage): add atomic SQLite ledger and outbox"
```

---

### Task 6: Renewable Leases and Monotonic Fencing

**Files:**
- Create: `src/agentic_saga/execution/clock.py`
- Create: `src/agentic_saga/execution/leases.py`
- Create: `src/agentic_saga/execution/__init__.py`
- Create: `tests/unit/execution/test_clock.py`
- Create: `tests/concurrency/test_leases.py`

**Interfaces:**
- Consumes: `SQLiteKernelStore`, Saga sequence/fence fields, and `FenceToken`.
- Produces: `Clock`, `SystemClock`, `FakeClock`, `Lease`, `LeaseService.acquire`, `LeaseService.renew`, `LeaseService.release`, `LeaseUnavailable`, and `LeaseLost`.

- [ ] **Step 1: Write barrier-based lease takeover tests**

```python
def test_takeover_mints_higher_fence_and_stale_owner_cannot_commit(store, fake_clock) -> None:
    leases = LeaseService(store, fake_clock)
    first = leases.acquire(SAGA_ID, "worker-a", timedelta(seconds=30))
    fake_clock.advance(timedelta(seconds=31))
    second = leases.acquire(SAGA_ID, "worker-b", timedelta(seconds=30))
    assert second.fence_token > first.fence_token
    with pytest.raises(StaleFence):
        store.commit_transition(batch_for(first.fence_token))
```

Use `threading.Barrier` for simultaneous acquisition tests; do not use sleeps.

- [ ] **Step 2: Run the lease tests and observe the red failure**

Run: `uv run pytest tests/concurrency/test_leases.py -q`

Expected: collection fails because `LeaseService` is absent.

- [ ] **Step 3: Implement database-clock leases and fencing**

Acquire with `BEGIN IMMEDIATE`; permit acquisition only when the lease is absent, already owned by the caller, or expired. A takeover increments `fence_token` monotonically in the same transaction. Renewal must match `(saga_id, owner, fence_token)` and must not revive an expired lease. Release clears owner/expiry but never decrements the token.

```python
class Lease(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    saga_id: SagaId
    owner: str
    fence_token: FenceToken
    expires_at: datetime
```

Use the injected UTC `Clock` for tests. For production SQLite, read `CURRENT_TIMESTAMP` inside the transaction so competing processes share one time source.

- [ ] **Step 4: Prove one winner and non-resurrection after expiry**

Run: `uv run pytest tests/unit/execution/test_clock.py tests/concurrency/test_leases.py -q -x`

Expected: exactly one simultaneous acquirer succeeds; stale renewal, stale release, and stale state commit all fail.

- [ ] **Step 5: Commit leases and fences**

```bash
git add src/agentic_saga/execution/clock.py src/agentic_saga/execution/leases.py src/agentic_saga/execution/__init__.py tests/unit/execution/test_clock.py tests/concurrency/test_leases.py
git commit -m "feat(execution): add renewable leases and fencing"
```

---

### Task 7: Saga Kernel Proposal-to-Intent Facade

**Files:**
- Create: `src/agentic_saga/kernel/canonical.py`
- Create: `src/agentic_saga/kernel/runtime.py`
- Create: `src/agentic_saga/evidence/redaction.py`
- Create: `src/agentic_saga/evidence/__init__.py`
- Create: `tests/unit/evidence/test_redaction.py`
- Create: `tests/unit/kernel/test_runtime.py`
- Create: `tests/integration/kernel/test_proposal_to_outbox.py`
- Create: `tests/integration/kernel/test_terminal_assignment.py`

**Interfaces:**
- Consumes: proposals, effect registry, identity factory, policy/invariant gates, SQLite store, leases, events, and reducer state from Tasks 1–6.
- Produces: `canonical_json(value: JsonValue) -> bytes`, `sha256_json(value: JsonValue) -> str`, `RedactionPolicy`, `redact_json(value, policy)`, `InvariantEvidenceProvider`, `ProposalResult`, `SagaKernel.submit_proposal(...)`, and `SagaKernel.assign_terminal(...)`.

- [ ] **Step 1: Write tests proving the facade is the only proposal-to-effect path**

```python
def test_authorized_proposal_atomically_creates_intent_and_outbox(kernel, store) -> None:
    result = kernel.submit_proposal(SAGA_ID, charge_proposal(seq=2), current_lease())
    snapshot = store.load_snapshot(SAGA_ID)
    assert result.accepted is True
    assert snapshot.operations[result.operation_id].status is OperationStatus.INTENT_DURABLE
    assert store.runnable_count(SAGA_ID) == 1


def test_rejected_proposal_records_evidence_without_outbox(kernel, store) -> None:
    result = kernel.submit_proposal(SAGA_ID, unknown_tool_proposal(seq=2), current_lease())
    assert result.accepted is False
    assert store.read_events(SAGA_ID)[-1].event_type == "recovery_plan_rejected"
    assert store.runnable_count(SAGA_ID) == 0
```

- [ ] **Step 2: Run the facade tests and observe the red failure**

Run: `uv run pytest tests/unit/kernel/test_runtime.py tests/integration/kernel/test_proposal_to_outbox.py -q`

Expected: collection fails because `SagaKernel` and canonical/redaction helpers do not exist.

- [ ] **Step 3: Implement canonicalization, redaction, and proposal submission**

`canonical_json` uses UTF-8 JSON with sorted keys, compact separators, `allow_nan=False`, and no implicit string conversion. `sha256_json` hashes those exact bytes. `RedactionPolicy` normalizes key names and replaces authorization, API key, cookie, card number, CVV, and bearer-token values with `[REDACTED]` before canonical command/result JSON enters a ledger event.

`SagaKernel.submit_proposal` must:

1. Load the current snapshot and validate the supplied lease.
2. Call `PolicyEngine.authorize` with the current sequence.
3. For rejection, append `RecoveryPlanRejected` without an outbox command.
4. For an authorized tool call, derive the logical operation ID, redact/canonicalize/hash the typed command, create `EffectIntentRecorded`, arm compensation metadata, create exactly one `OutboxCommand`, reduce the resulting projection, and call `commit_transition` once.
5. For `Escalate`, atomically park autonomous commands and append `HumanRequired`.
6. Never call a tool adapter directly.

```python
class ProposalResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    accepted: bool
    code: str
    saga_seq: int
    operation_id: OperationId | None = None


class SagaKernel:
    def submit_proposal(
        self,
        saga_id: SagaId,
        proposal: AgentProposal,
        lease: Lease,
    ) -> ProposalResult:
        snapshot = self._store.load_snapshot(saga_id)
        decision = self._policy.authorize(proposal, snapshot, self._policy_context(snapshot))
        return self._commit_decision(snapshot, proposal, decision, lease)
```

Split each numbered responsibility into a typed private function no longer than 15 lines.

- [ ] **Step 4: Implement terminal assignment through fresh authoritative evidence**

```python
def test_finish_rechecks_authoritative_invariants_at_current_sequence(
    kernel, evidence_provider
) -> None:
    evidence_provider.return_passing(evaluated_at_seq=kernel.snapshot.seq)
    result = kernel.assign_terminal(SAGA_ID, SagaStatus.SUCCEEDED_VERIFIED, current_lease())
    assert result.accepted is True


def test_finish_with_stale_or_failing_proof_creates_no_terminal_event(
    kernel, evidence_provider
) -> None:
    evidence_provider.return_failing(evaluated_at_seq=kernel.snapshot.seq - 1)
    result = kernel.assign_terminal(SAGA_ID, SagaStatus.SUCCEEDED_VERIFIED, current_lease())
    assert result.accepted is False
    assert kernel.snapshot.status is not SagaStatus.SUCCEEDED_VERIFIED
```

Define `InvariantEvidenceProvider.evaluate(saga_id, snapshot) -> InvariantEvidence`. `assign_terminal` obtains new authoritative evidence, appends `InvariantEvaluated`, reduces to the proof sequence, asks `TerminalGate` to authorize the requested terminal state, then appends `TerminalAssigned` in the same local transaction. If evaluation or gating fails, append deterministic denial evidence and remain nonterminal.

Run: `uv run pytest tests/unit/evidence/test_redaction.py tests/unit/kernel/test_runtime.py tests/integration/kernel -q`

Expected: all tests pass; raw secrets never appear in ledger JSON; only accepted effect proposals produce outbox work.

- [ ] **Step 5: Commit the kernel facade**

```bash
git add src/agentic_saga/kernel/canonical.py src/agentic_saga/kernel/runtime.py src/agentic_saga/evidence tests/unit/evidence/test_redaction.py tests/unit/kernel/test_runtime.py tests/integration/kernel
git commit -m "feat(kernel): turn safe proposals into durable intent"
```

---

### Task 8: Intent-First Fenced Dispatcher

**Files:**
- Create: `src/agentic_saga/execution/dispatcher.py`
- Create: `tests/unit/execution/test_dispatcher.py`
- Create: `tests/integration/execution/test_intent_before_effect.py`
- Create: `tests/integration/execution/test_idempotent_delivery.py`
- Create: `tests/support/durable_tool.py`
- Create: `tests/support/kernel_harness.py`

**Interfaces:**
- Consumes: `KernelStore`, `ClaimedCommand`, `Lease`, `ToolRegistry`, `EffectOutcome`, and reducer events.
- Produces: `Dispatcher.dispatch_one(worker_id) -> DispatchResult`, `DispatchResult`, `DispatchFailure`, the durable dispatch lifecycle, and the initial `DurableFakeTool`/`KernelHarness` used by all later integration tests.

- [ ] **Step 1: Write a test proving durable intent precedes adapter entry**

```python
@pytest.mark.asyncio
async def test_adapter_observes_durable_intent_before_effect(store, registry) -> None:
    observed: list[OperationStatus] = []

    async def execute(command, context):
        snapshot = store.load_snapshot(SAGA_ID)
        observed.append(snapshot.operations[context.operation_id].status)
        return EffectConfirmed(receipt={"payment_id": "pay_1"})

    registry = registry_with_charge_adapter(execute)
    await Dispatcher(store, registry).dispatch_one("worker-a")
    assert observed == [OperationStatus.DISPATCHED]
```

- [ ] **Step 2: Run dispatcher tests and observe the red failure**

Run: `uv run pytest tests/unit/execution/test_dispatcher.py tests/integration/execution/test_intent_before_effect.py -q`

Expected: collection fails because `Dispatcher` is absent.

- [ ] **Step 3: Implement the dispatcher in explicit durable phases**

`dispatch_one` must:

1. Claim one due outbox row with a bounded claim lease.
2. Verify the Saga executor lease/fence is current.
3. Append `DispatchStarted` and increment `delivery_attempt` before external I/O.
4. Rehydrate and revalidate the typed command from stored canonical JSON.
5. Call the registered executor with the stable `operation_id` and current fence.
6. Convert every uncaught adapter exception, cancellation after adapter entry, and timeout into `OutcomeUnknown` with a non-secret correlation.
7. Append `EffectOutcomeRecorded` using compare-and-swap and complete or park the outbox row atomically.

```python
async def _call_effect(
    definition: EffectToolDefinition[BaseModel],
    claimed: ClaimedCommand,
) -> EffectOutcome:
    try:
        return await definition.adapter.execute(claimed.command, _effect_context(claimed))
    except asyncio.CancelledError as error:
        return OutcomeUnknown(correlation=_safe_correlation(error))
    except Exception as error:
        return OutcomeUnknown(correlation=_safe_correlation(error))
```

Keep cancellation before adapter entry safe to release/requeue. Once adapter entry occurs, cancellation must persist unknown outcome under `asyncio.shield` before propagating.

V0.1 invokes the registered trusted adapter in process under a bounded async timeout. Adapters must
be cancellation-cooperative. A timeout or cancellation after durable dispatch is still ambiguous
and records `OutcomeUnknown`; it never licenses a blind retry. Deliberately hostile or native code
requires an application-owned external isolation boundary and is outside this library contract.

Create the initial test support at this point, rather than relying on a later task. `DurableFakeTool`
uses a SQLite file separate from the Saga ledger, uniquely stores `(tool_name, operation_id)`, and
returns the prior receipt for a repeated command hash. `KernelHarness` assembles a real store,
registry, lease, kernel facade, dispatcher, and durable fake tool; later tasks extend this same
harness without creating a second runtime.

- [ ] **Step 4: Prove 100 deliveries reuse one business identity**

```python
@pytest.mark.asyncio
async def test_one_hundred_deliveries_create_one_provider_effect(harness) -> None:
    for _ in range(100):
        await harness.redeliver_same_operation()
    assert harness.provider.effect_count(OP_ID) == 1
    assert {call.operation_id for call in harness.provider.calls} == {OP_ID}
```

Run: `uv run pytest tests/unit/execution/test_dispatcher.py tests/integration/execution/test_intent_before_effect.py tests/integration/execution/test_idempotent_delivery.py -q`

Expected: all tests pass; exceptions and lost responses yield `RECONCILING_UNKNOWN`, not a retry or terminal state.

- [ ] **Step 5: Commit the dispatcher**

```bash
git add src/agentic_saga/execution/dispatcher.py tests/unit/execution/test_dispatcher.py tests/integration/execution tests/support
git commit -m "feat(execution): dispatch fenced effects intent first"
```

---

### Task 9: Unknown-Outcome Reconciliation

**Files:**
- Create: `src/agentic_saga/execution/reconciliation.py`
- Create: `tests/unit/execution/test_reconciliation.py`
- Create: `tests/integration/execution/test_lost_response.py`

**Interfaces:**
- Consumes: unknown operations, tool capabilities/reconciler, outbox, leases, and policy limits.
- Produces: `ReconcileEffectConfirmed`, `ReconcileNoEffectConfirmed`, `ReconcilePending`, `ReconcileConflict`, `ReconcileUnsupported`, `ReconciliationOutcome`, `ReconciliationPlanner.decide(...)`, and `Reconciler.reconcile_one(...)`.

- [ ] **Step 1: Write the reconciliation decision table tests**

```python
@pytest.mark.parametrize(
    ("outcome", "idempotency_covers_horizon", "decision"),
    [
        (ReconcileEffectConfirmed(receipt={"payment_id": "pay_1"}), False, "confirm"),
        (ReconcileNoEffectConfirmed(reason="not found"), False, "retry_same_id"),
        (ReconcilePending(correlation="req_1"), True, "wait"),
        (ReconcileConflict(reason="two provider records"), True, "human"),
        (ReconcileUnsupported(reason="no lookup"), False, "human"),
        (ReconcileUnsupported(reason="no lookup"), True, "retry_same_id"),
    ],
)
def test_reconciliation_is_fail_closed(outcome, idempotency_covers_horizon, decision) -> None:
    actual = ReconciliationPlanner().decide(outcome, idempotency_covers_horizon)
    assert actual.action == decision
```

- [ ] **Step 2: Run reconciliation tests and observe the red failure**

Run: `uv run pytest tests/unit/execution/test_reconciliation.py -q`

Expected: collection fails because `ReconciliationPlanner` and `Reconciler` are missing.

- [ ] **Step 3: Implement reconciliation outcomes and planner**

Use the strict frozen `ReconciliationOutcome` discriminated union from Task 1. `Reconciler.reconcile_one` acquires the current Saga lease, calls the registered reconciler, records its evidence, and executes only the pure planner decision:

```text
confirmed effect -> append confirmed outcome; do not redeliver
confirmed absence -> requeue the same logical operation ID
pending -> park until the returned check-after instant
conflict -> HUMAN_REQUIRED
unsupported + retention covers recovery horizon -> requeue same ID
unsupported + insufficient retention -> HUMAN_REQUIRED
```

The recovery horizon calculation includes maximum retry delay, operator response window, and clock skew allowance. Reject configurations whose idempotency retention is shorter.

- [ ] **Step 4: Prove lost-response recovery and no blind retry**

```python
@pytest.mark.asyncio
async def test_lost_success_response_reconciles_without_second_charge(harness) -> None:
    await harness.dispatch_with_response_loss("charge_payment")
    assert harness.snapshot.status is SagaStatus.RECONCILING_UNKNOWN
    await harness.restart_and_reconcile()
    assert harness.snapshot.operations[OP_ID].status is OperationStatus.EFFECT_CONFIRMED
    assert harness.provider.effect_count(OP_ID) == 1


@pytest.mark.asyncio
async def test_opaque_provider_escalates_without_retry(harness) -> None:
    harness.provider.disable_reconciliation_and_deduplication()
    await harness.dispatch_with_response_loss("charge_payment")
    await harness.restart_and_reconcile()
    assert harness.snapshot.status is SagaStatus.HUMAN_REQUIRED
    assert harness.provider.call_count(OP_ID) == 1
```

Run: `uv run pytest tests/unit/execution/test_reconciliation.py tests/integration/execution/test_lost_response.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit reconciliation**

```bash
git add src/agentic_saga/execution/reconciliation.py tests/unit/execution/test_reconciliation.py tests/integration/execution/test_lost_response.py
git commit -m "feat(execution): reconcile ambiguous external outcomes"
```

---

### Task 10: Compensation Eligibility and Reverse Dependency Frontier

**Files:**
- Create: `src/agentic_saga/kernel/compensation.py`
- Create: `tests/unit/kernel/test_compensation.py`
- Create: `tests/integration/execution/test_compensation_flow.py`

**Interfaces:**
- Consumes: confirmed/partial/unknown operations, armed obligations, effect registry dependencies, stable operation identity, dispatcher, and terminal gate.
- Produces: `CompensationItem`, `CompensationFrontier`, `CompensationPlanner.plan(snapshot, registry)`, `CompensationBlocked`, and `DependencyCycle`.

- [ ] **Step 1: Write eligibility and order tests**

```python
def test_only_confirmed_effects_are_eligible() -> None:
    frontier = CompensationPlanner().plan(snapshot_with_confirmed_and_unknown(), registry())
    assert frontier.blocked_by_unknown == (UNKNOWN_OP_ID,)
    assert frontier.runnable == ()


def test_frontier_uses_reverse_topological_order() -> None:
    snapshot = confirmed_order_payment_inventory()
    frontier = CompensationPlanner().plan(snapshot, dependent_registry())
    assert [item.tool_name for item in frontier.ordered] == [
        "release_inventory",
        "refund_payment",
        "cancel_order",
    ]


def test_partial_effect_compensates_exact_receipts() -> None:
    item = CompensationPlanner().plan(partial_reservation(), registry()).ordered[0]
    assert item.receipts == ({"reservation_id": "r1"}, {"reservation_id": "r2"})
```

- [ ] **Step 2: Run compensation tests and observe the red failure**

Run: `uv run pytest tests/unit/kernel/test_compensation.py -q`

Expected: collection fails because `CompensationPlanner` is absent.

- [ ] **Step 3: Implement deterministic frontier planning**

Build a dependency DAG from confirmed obligations and registered compensation dependencies. Reject cycles at registry construction and again during replay. Reverse the topological order. Default every item to serial; expose a parallel group only when every pair is explicitly marked independent and resource selectors do not overlap.

Each compensation gets a stable operation ID derived from the forward step instance, `Direction.COMPENSATION`, and the same semantic generation. It carries exact forward receipt(s), never reconstructed guesses. Unknown forward effects block the frontier and transition to reconciliation rather than compensation.

- [ ] **Step 4: Exercise transient and unverifiable compensation outcomes**

```python
@pytest.mark.asyncio
async def test_refund_retry_reuses_one_compensation_identity(harness) -> None:
    harness.refunds.fail_transiently_once()
    await harness.compensate()
    assert harness.refunds.effect_count(harness.refund_operation_id) == 1
    assert harness.snapshot.status is SagaStatus.COMPENSATED_VERIFIED


@pytest.mark.asyncio
async def test_unverifiable_refund_cannot_terminally_compensate(harness) -> None:
    harness.refunds.lose_response_without_lookup()
    await harness.compensate()
    assert harness.snapshot.status is SagaStatus.HUMAN_REQUIRED
    assert harness.snapshot.status is not SagaStatus.COMPENSATED_VERIFIED
```

Run: `uv run pytest tests/unit/kernel/test_compensation.py tests/integration/execution/test_compensation_flow.py -q`

Expected: all tests pass; `COMPENSATED_VERIFIED` requires fresh repair invariants and zero unresolved obligations.

- [ ] **Step 5: Commit compensation planning**

```bash
git add src/agentic_saga/kernel/compensation.py tests/unit/kernel/test_compensation.py tests/integration/execution/test_compensation_flow.py
git commit -m "feat(kernel): compensate verified effects in dependency order"
```

---

### Task 11: Late-Completion Safety and Deterministic Emergency Unwind

**Files:**
- Create: `src/agentic_saga/execution/unwind.py`
- Create: `tests/unit/execution/test_unwind.py`
- Create: `tests/concurrency/test_late_completion.py`
- Create: `tests/integration/execution/test_human_quiescence.py`

**Interfaces:**
- Consumes: reconciliation, compensation frontier, leases, tool cancellation/fencing capabilities, policy budgets, and store outbox control.
- Produces: `UnwindTrigger`, `UnwindDecision`, `EmergencyUnwinder.plan(...)`, `EmergencyUnwinder.execute(...)`, and `QuiescenceVerifier.verify(...)`.

- [ ] **Step 1: Write the emergency-unwind decision tests**

```python
@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        (UnwindTrigger.AGENT_UNAVAILABLE, "compensate"),
        (UnwindTrigger.BUDGET_EXHAUSTED, "compensate"),
        (UnwindTrigger.INVALID_PROPOSAL_LIMIT, "compensate"),
    ],
)
def test_safe_confirmed_effects_choose_compensation(trigger, expected) -> None:
    decision = EmergencyUnwinder().plan(confirmed_effect_snapshot(), registry(), trigger)
    assert decision.action == expected


def test_unknown_effect_chooses_reconciliation_not_compensation() -> None:
    decision = EmergencyUnwinder().plan(
        snapshot_with_unknown_operation(), registry(), UnwindTrigger.BUDGET_EXHAUSTED
    )
    assert decision.action == "reconcile"
```

- [ ] **Step 2: Run unwind tests and observe the red failure**

Run: `uv run pytest tests/unit/execution/test_unwind.py -q`

Expected: collection fails because `EmergencyUnwinder` is absent.

- [ ] **Step 3: Implement fail-closed unwind and quiescence**

The pure planner chooses exactly one action:

```text
unknown/potentially live forward work -> reconcile
confirmed eligible reversible effects -> compensate
irreversible effect, dependency conflict, or opaque ambiguity -> human
no effect and no pending command -> abort_clean candidate
```

Before enqueuing compensation, `QuiescenceVerifier` must prove no forward outbox row is runnable/claimed, no forward adapter call lacks a reconciled outcome, and the Saga lease is current. For `HUMAN_REQUIRED`, atomically park every autonomous command and append the transition event. Resuming requires an authenticated, exact-sequence, single-use human command handled by the policy layer.

Add exact-sequence approval tests to the integration file:

```python
def test_human_approval_is_sequence_bound_and_single_use(harness) -> None:
    approval = harness.sign_approval(saga_seq=harness.snapshot.seq)
    assert harness.apply_human_decision(approval).accepted is True
    assert harness.apply_human_decision(approval).code == "decision_already_used"


def test_stale_human_approval_cannot_resume(harness) -> None:
    approval = harness.sign_approval(saga_seq=harness.snapshot.seq)
    harness.append_operator_note()
    assert harness.apply_human_decision(approval).code == "stale_human_decision"
    assert harness.autonomous_adapter_call_count == 0
```

- [ ] **Step 4: Prove a late forward completion cannot resurrect state**

Use events and barriers rather than sleeps:

```python
@pytest.mark.asyncio
async def test_compensation_waits_for_timed_out_forward_call(harness) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    harness.inventory.block_after_provider_entry(entered, release)
    dispatch = asyncio.create_task(harness.dispatch_inventory())
    await entered.wait()
    await harness.expire_delivery_and_request_unwind()
    assert harness.refunds.call_count == 0
    release.set()
    await dispatch
    await harness.reconcile_then_unwind()
    assert harness.authoritative_invariants_hold()
```

Add a fenced-provider variant where a stale forward completion is rejected and a non-fencing provider variant where the Saga remains reconciling until the provider reports a stable result.

Run: `uv run pytest tests/unit/execution/test_unwind.py tests/concurrency/test_late_completion.py tests/integration/execution/test_human_quiescence.py -q`

Expected: all tests pass; restarting a `HUMAN_REQUIRED` Saga produces zero autonomous adapter calls.

- [ ] **Step 5: Commit unwind and quiescence**

```bash
git add src/agentic_saga/execution/unwind.py tests/unit/execution/test_unwind.py tests/concurrency/test_late_completion.py tests/integration/execution/test_human_quiescence.py
git commit -m "feat(execution): unwind safely after agent failure"
```

---

### Task 12: Bounded Incremental Agent Runtime

**Files:**
- Create: `src/agentic_saga/contracts/runtime.py`
- Create: `src/agentic_saga/execution/runtime.py`
- Create: `tests/unit/contracts/test_runtime_contracts.py`
- Create: `tests/unit/execution/test_runtime_loop.py`
- Create: `tests/integration/execution/test_runtime_resume.py`
- Create: `tests/integration/execution/test_runtime_unwind.py`

**Interfaces:**
- Consumes: tool/action contracts, registry, `SagaKernel`, dispatcher, reconciler, emergency unwinder, terminal gate, store, and leases from Tasks 1–11.
- Produces the exact upstream imports required by the ecommerce plan:

```python
# agentic_saga.contracts.actions
ToolCall
Finish
Escalate

# agentic_saga.contracts.runtime
AgentDriver
SagaObservation
ToolDescriptor
SagaGoal
SagaDefinition
SagaResult
SagaState
DefinitionCatalog

# agentic_saga.contracts.tools
ReadToolDefinition
EffectToolDefinition
EffectAdapter
EffectContext
ReconcileContext

# agentic_saga.execution.runtime
SagaRuntime
```

- [ ] **Step 1: Write strict public runtime-contract tests**

```python
def test_goal_and_observation_are_strict_and_sequence_bound() -> None:
    goal = SagaGoal(
        goal_id="goal_order_1",
        text="Fulfill this order or restore a valid state.",
        context={"order_id": "order_1"},
    )
    observation = SagaObservation(
        saga_id=SAGA_ID,
        saga_seq=4,
        state=SagaState.RUNNING,
        goal=goal,
        last_action=None,
        projection={"order_status": "created"},
        remaining_budget=remaining_budget(),
    )
    assert observation.saga_seq == 4


def test_tool_descriptor_contains_schema_but_no_callable() -> None:
    descriptor = ToolDescriptor.from_definition(charge_definition())
    assert descriptor.name == "charge_payment"
    assert descriptor.kind == "effect"
    assert "amount_minor" in descriptor.input_schema["properties"]
    assert "adapter" not in descriptor.model_dump(mode="json")
```

- [ ] **Step 2: Run contract and runtime-loop tests and observe the red failure**

Run: `uv run pytest tests/unit/contracts/test_runtime_contracts.py tests/unit/execution/test_runtime_loop.py -q`

Expected: collection fails because `AgentDriver`, `SagaObservation`, and `SagaRuntime` do not exist.

- [ ] **Step 3: Implement the exact public contracts**

```python
class AgentDriver(Protocol):
    async def next_action(
        self,
        observation: SagaObservation,
        available_tools: Sequence[ToolDescriptor],
    ) -> ToolCall | Finish | BeginCompensation | Escalate: ...


class SagaRuntime:
    def __init__(
        self,
        *,
        kernel: SagaKernel,
        dispatcher: Dispatcher,
        reconciler: Reconciler,
        unwinder: EmergencyUnwinder,
        leases: LeaseService,
        definitions: DefinitionCatalog,
    ) -> None: ...

    async def start(
        self,
        *,
        definition: SagaDefinition,
        goal: SagaGoal,
        agent: AgentDriver,
    ) -> SagaResult: ...

    async def resume(self, *, saga_id: SagaId, agent: AgentDriver) -> SagaResult: ...
```

`SagaGoal` is a strict frozen model with bounded `goal_id`, bounded plain-language `text`, and
redacted JSON `context`. `SagaDefinition` is a frozen dataclass containing `name`, immutable
`version`, `ToolRegistry`, `PolicyEngine`, success/compensation/clean-abort invariant sets, and
`ExecutionBudget`. `DefinitionCatalog.register(definition)` keys immutable definitions by
`(name, version)` and `DefinitionCatalog.resolve(name, version)` fails closed when historical code
is unavailable; this is how `resume(saga_id=...)` obtains the pinned definition without accepting
one from the caller. `SagaState` is the public alias of `SagaStatus`, not a second state machine.
`SagaObservation` contains the exact current ledger sequence, current state, goal, last recorded
action/outcome, redacted material projection, and remaining budget. `SagaResult` contains Saga ID,
final/current state, current sequence, whether autonomous execution is quiescent, and an optional
human-required reason; it never declares a model-selected terminal outcome.

`ToolDescriptor.from_definition` exposes only name, read/effect kind, description, strict JSON
input schema, reversibility, and currently relevant policy constraints. It never contains adapter
objects, database handles, credentials, idempotency keys, or tools currently ineligible by policy.

`SagaRuntime` accepts a trusted in-process `AgentDriver` implementation. Calls run under bounded
async timeouts and drivers must cooperate with cancellation. The durable turn reservation remains
authoritative: an exception, timeout, cancellation, or missing proposal never refunds the reserved
budget and never permits replay of an unresolved turn. Hostile driver containment belongs to the
embedding application, not the core runtime.

- [ ] **Step 4: Implement one-proposal-per-turn orchestration without business branches**

Write the red tests first:

```python
@pytest.mark.asyncio
async def test_runtime_requests_one_action_after_each_observation(runtime, recording_agent) -> None:
    result = await runtime.start(
        definition=generic_definition(), goal=generic_goal(), agent=recording_agent
    )
    assert recording_agent.concurrent_calls == 0
    assert [item.saga_seq for item in recording_agent.observations] == sorted(
        item.saga_seq for item in recording_agent.observations
    )
    assert result.state is SagaState.SUCCEEDED_VERIFIED


@pytest.mark.asyncio
async def test_runtime_has_no_tool_name_or_domain_specific_branch(runtime_source) -> None:
    forbidden = {"charge_payment", "reserve_inventory", "order_status", "out_of_stock"}
    assert forbidden.isdisjoint(runtime_source.identifiers_and_string_literals())
```

The loop is generic and performs this exact sequence:

```text
load authoritative projection -> acquire/renew lease -> build observation and currently
eligible descriptors -> await exactly one proposal -> validate current sequence -> apply
deterministic policy -> record rejection or durable intent -> execute a read once or dispatch
the effect outbox -> record typed observation -> repeat
```

`start` registers the immutable definition, creates `SagaCreated` containing the definition
name/version plus redacted goal, acquires the first lease, and enters the loop. Reject reuse of an
existing Saga ID with a different goal or definition rather than treating it as a resume.

Read tools are strict and policy-gated but create no compensation obligation. Record `ReadStarted`
before execution and `ReadObserved` afterward; a crash may safely repeat a read. Effect tools always
go through `SagaKernel.submit_proposal` and the outbox dispatcher. `Finish` always goes through
`SagaKernel.assign_terminal`; a false finish becomes rejection evidence and another observation.
`Escalate` atomically enters quiescent `HUMAN_REQUIRED`.

The runtime never switches on tool names, business error codes, ecommerce fields, or a prescribed
tool sequence. It may switch only on proposal kind, deterministic kernel state, and typed outcome.

- [ ] **Step 5: Implement bounded failures, invalid output handling, and emergency unwind**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["agent_exception", "invalid_limit", "turn_limit", "token_limit"]
)
async def test_agent_failure_uses_deterministic_unwind_or_human(
    runtime_harness, failure: str
) -> None:
    runtime_harness.inject_agent_failure(failure)
    result = await runtime_harness.run()
    assert result.state in {
        SagaState.COMPENSATED_VERIFIED,
        SagaState.ABORTED_CLEAN,
        SagaState.HUMAN_REQUIRED,
    }
    assert runtime_harness.unapproved_effect_count == 0
```

Count turns, accepted/rejected tool calls, invalid outputs, agent-call time, and output tokens from
durable events so restart cannot reset a budget. Agent exceptions, unavailable agent, malformed
output, repeated invalid proposals, or any exhausted limit stop agent calls and invoke
`EmergencyUnwinder`. Execute its deterministic reconciliation/compensation commands only while
the lease and budgets permit; otherwise enter `HUMAN_REQUIRED`. An unknown effect can never be
converted into clean abort or compensation merely because the agent failed.

- [ ] **Step 6: Prove resume rebuilds context and never duplicates an accepted effect**

```python
@pytest.mark.asyncio
async def test_resume_rebuilds_observation_from_ledger(runtime_harness) -> None:
    await runtime_harness.crash_after_confirmed_provider_effect()
    agent = runtime_harness.fresh_agent()
    result = await runtime_harness.fresh_runtime().resume(
        saga_id=runtime_harness.saga_id,
        agent=agent,
    )
    assert runtime_harness.provider.effect_count(runtime_harness.operation_id) == 1
    assert result.state is SagaState.SUCCEEDED_VERIFIED
    assert agent.received_checkpoint_state is False
```

`resume` loads the pinned definition version from the Saga record, refuses to run when that
definition is unavailable, rebuilds observation solely from ledger/projection, acquires a newer
fence if needed, reconciles dispatched-without-outcome operations before asking the agent, and
preserves every durable budget counter. It never consumes a LangGraph or provider checkpoint as
business truth.

Run: `uv run pytest tests/unit/contracts/test_runtime_contracts.py tests/unit/execution/test_runtime_loop.py tests/integration/execution/test_runtime_resume.py tests/integration/execution/test_runtime_unwind.py -q`

Expected: all tests pass; the runtime can execute arbitrary registered tool names in agent-selected order, and every unsafe stop converges deterministically or remains explicitly human-required.

- [ ] **Step 7: Run focused quality checks and commit**

Run: `uv run ruff check src/agentic_saga/contracts/runtime.py src/agentic_saga/execution/runtime.py tests/unit/contracts/test_runtime_contracts.py tests/unit/execution/test_runtime_loop.py tests/integration/execution/test_runtime_resume.py tests/integration/execution/test_runtime_unwind.py && uv run mypy src/agentic_saga/contracts/runtime.py src/agentic_saga/execution/runtime.py`

Expected: lint and strict typing exit 0.

```bash
git add src/agentic_saga/contracts/runtime.py src/agentic_saga/execution/runtime.py tests/unit/contracts/test_runtime_contracts.py tests/unit/execution/test_runtime_loop.py tests/integration/execution/test_runtime_resume.py tests/integration/execution/test_runtime_unwind.py
git commit -m "feat(runtime): orchestrate one bounded agent action per turn"
```

---

### Task 13: Redacted, Deterministic RunTrace Export

**Files:**
- Create: `src/agentic_saga/contracts/trace.py`
- Create: `src/agentic_saga/evidence/run_trace.py`
- Modify: `src/agentic_saga/evidence/__init__.py`
- Modify: `tests/unit/evidence/test_redaction.py`
- Create: `tests/integration/evidence/test_run_trace.py`

**Interfaces:**
- Consumes: ordered ledger events, material Saga snapshot, invariant evidence, and hashes.
- Produces: `TraceAuthority`, `TraceEvent`, `TraceProof`, canonical core `RunTrace`, and `RunTraceExporter.export(saga_id)`; consumes the existing `RedactionPolicy` and `redact_json(...)` from Task 7.

- [ ] **Step 1: Write the failing trace contract test and extend redaction coverage**

```python
def test_trace_contract_rejects_unknown_schema_version() -> None:
    payload = valid_trace_payload()
    payload["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        RunTrace.model_validate(payload)


def test_redaction_removes_nested_secrets_and_payment_data() -> None:
    value = {
        "authorization": "Bearer secret",
        "customer": {"card_number": "4242424242424242", "name": "Asha"},
        "items": [{"cvv": "123", "sku": "SHOE-123"}],
    }
    assert redact_json(value, default_policy()) == {
        "authorization": "[REDACTED]",
        "customer": {"card_number": "[REDACTED]", "name": "Asha"},
        "items": [{"cvv": "[REDACTED]", "sku": "SHOE-123"}],
    }
```

- [ ] **Step 2: Run evidence tests and observe the red failure**

Run: `uv run pytest tests/unit/evidence/test_redaction.py tests/integration/evidence/test_run_trace.py -q`

Expected: collection fails because the `RunTrace` contract and exporter are absent.

- [ ] **Step 3: Implement the stored trace contract and reuse kernel redaction**

Map every ledger event to one of `agent`, `policy`, `kernel`, `effect`, `compensation`, `proof`, or `human`. `TraceEvent` includes sequence, authority, event type, operation identity, step identity, attempt, fence, redacted input/output, structured rationale, policy decision, before/after status, receipt/correlation, hashes, and recorded time. `TraceProof` includes invariant version, exact evaluated Saga sequence, rule inputs, result, and explanation.

```python
class RunTrace(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    saga_id: SagaId
    definition_version: str
    started_at: datetime
    finished_at: datetime | None
    outcome: SagaStatus
    events: tuple[TraceEvent, ...]
    proofs: tuple[TraceProof, ...]
    final_projection_hash: str
```

Redact both by normalized sensitive key names and value detectors for bearer tokens and payment-card-like digit strings. Preserve hashes of canonical redacted values, never hashes that enable verification of low-entropy secrets.

This model is the one canonical causal trace. The ecommerce demo must wrap/export it rather than
define a competing event or proof schema: its `DemoRunTraceExport` adds `scenario_id`,
`scenario_name`, `mode`, `fault_config`, `initial_state`, and `required_invariant_ids`, then emits
those scenario fields alongside the core `RunTrace` fields for Flight Recorder schema `1.0`.
`TraceEvent` and `TraceProof` are imported unchanged from `agentic_saga.contracts.trace`; browser-only
events are forbidden. The Flight Recorder therefore consumes a scenario-enriched serialization of
the real core trace, not a parallel trace source.

- [ ] **Step 4: Prove deterministic ordering and complete causal evidence**

```python
def test_export_is_sequence_ordered_and_materially_deterministic(exporter) -> None:
    first = exporter.export(SAGA_ID)
    second = exporter.export(SAGA_ID)
    assert [event.saga_seq for event in first.events] == sorted(
        event.saga_seq for event in first.events
    )
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_terminal_trace_contains_current_proof(exporter) -> None:
    trace = exporter.export(SAGA_ID)
    assert trace.proofs[-1].evaluated_at_seq == trace.events[-2].saga_seq
    assert trace.events[-1].event_type == "terminal_assigned"
```

Run: `uv run pytest tests/unit/evidence/test_redaction.py tests/integration/evidence/test_run_trace.py -q`

Expected: all tests pass; fixture secrets do not appear anywhere in serialized trace JSON.

- [ ] **Step 5: Commit evidence export**

```bash
git add src/agentic_saga/contracts/trace.py src/agentic_saga/evidence tests/unit/evidence tests/integration/evidence
git commit -m "feat(evidence): export redacted deterministic run traces"
```

---

### Task 14: Reusable Backend and Tool-Adapter Conformance Suites

**Files:**
- Create: `tests/contract/storage.py`
- Create: `tests/contract/tools.py`
- Create: `tests/contract/test_sqlite_backend.py`
- Create: `tests/contract/test_durable_fake_tool.py`
- Modify: `tests/support/durable_tool.py`
- Modify: `tests/support/kernel_harness.py`

**Interfaces:**
- Consumes: every public contract from Tasks 1–11.
- Produces: `StorageContract`, `ToolAdapterContract`, `DurableFakeTool`, and `KernelHarness` reused by BDD, crash, concurrency, and future backend adapters.

- [ ] **Step 1: Write the conformance contracts as executable base classes**

```python
class StorageContract:
    def make_store(self, path: Path) -> KernelStore:
        raise NotImplementedError

    def test_atomic_transition(self, tmp_path: Path) -> None:
        store = self.make_store(tmp_path / "saga.db")
        snapshot = store.create_saga(saga_created(seq=1))
        updated = store.commit_transition(intent_batch(snapshot))
        assert store.rebuild_and_verify(SAGA_ID) == updated

    def test_compare_and_swap_rejects_stale_writer(self, tmp_path: Path) -> None:
        store = self.make_store(tmp_path / "saga.db")
        snapshot = store.create_saga(saga_created(seq=1))
        store.commit_transition(intent_batch(snapshot))
        with pytest.raises(StoreConflict):
            store.commit_transition(intent_batch(snapshot))
```

`ToolAdapterContract` verifies stable-key deduplication, same-key/different-command rejection, reconciliation of a lost response, fencing behavior when declared, explicit partial receipts, and idempotent compensation.

The conformance suite treats installed adapters as trusted application code and provider data as
untrusted. It covers malformed results, ordinary exceptions, cooperative timeouts, lost responses,
privacy normalization, and recovery semantics. It does not claim containment of deliberately
hostile Python/native implementations; applications needing that boundary must host the adapter in
an external process, container, or service.

- [ ] **Step 2: Run contract tests and observe missing harness failures**

Run: `uv run pytest tests/contract -q`

Expected: collection fails until `DurableFakeTool` and concrete SQLite contract subclasses are implemented.

- [ ] **Step 3: Implement durable fake services separate from the Saga database**

`DurableFakeTool` uses its own SQLite file and a unique `(tool_name, operation_id)` constraint. In one transaction it rejects command-hash mismatch, rejects stale fences when configured, or returns the prior receipt for a duplicate identity. Fault modes are deterministic enum values: `NO_EFFECT_FAILURE`, `PARTIAL_EFFECT`, `EFFECT_THEN_LOSE_RESPONSE`, `BLOCK_AFTER_EFFECT`, and `COMPENSATION_FAILURE_ONCE`.

The fake's observation API exposes business effect count, call count, stored command hash, receipt, and current resource state without reading the Saga database.

- [ ] **Step 4: Run both concrete conformance suites**

Run: `uv run pytest tests/contract/test_sqlite_backend.py tests/contract/test_durable_fake_tool.py -q`

Expected: all storage and adapter contract cases pass. A tool that declares fencing but accepts an old fence must fail its contract test.

- [ ] **Step 5: Commit reusable conformance infrastructure**

```bash
git add tests/contract tests/support
git commit -m "test(kernel): add backend and adapter conformance suites"
```

---

### Task 15: Real Subprocess Crash Matrix

**Files:**
- Create: `tests/crash/worker.py`
- Create: `tests/crash/test_crash_matrix.py`
- Create: `tests/crash/test_sqlite_recovery.py`
- Modify: `tests/support/kernel_harness.py`

**Interfaces:**
- Consumes: SQLite kernel, durable fake tool, dispatcher, reconciler, unwinder, and
  `AGENTIC_SAGA_FAILPOINT`, which the parent test sets and only the subprocess worker reads and
  interprets.
- Produces: a subprocess crash harness that terminates with `os._exit(91)` at named durability boundaries and returns only persisted evidence after restart.

This task kills the whole application worker; it does not isolate trusted adapters. The environment
variable is set by the parent harness, but only `tests/crash/worker.py` reads it or calls
`os._exit`. Production objects accept only a typed
failpoint dependency whose default implementation is a no-op. Checkpoints are injected at their
actual durability owners: SQLite for transaction pre-commit, the kernel after intent commit, the
dispatcher after dispatch/outcome commit, and the external durable fake after its effect commit.
The reconciler and unwinder are exercised during fresh-process recovery but do not receive unused
failpoint dependencies because none of the eight named checkpoints occurs inside them.

- [ ] **Step 1: Write the parametrized crash matrix before adding failpoints**

```python
@pytest.mark.parametrize(
    "failpoint",
    [
        "before_intent_commit",
        "after_intent_commit",
        "after_dispatch_record",
        "after_provider_effect",
        "after_outcome_commit",
        "after_compensation_intent",
        "after_compensation_effect",
        "before_terminal_commit",
    ],
)
def test_restart_converges_or_escalates_without_duplication(crash_harness, failpoint: str) -> None:
    result = crash_harness.run_until_killed(failpoint)
    assert result.returncode == 91
    recovered = crash_harness.restart_and_recover()
    assert recovered.status in {
        SagaStatus.SUCCEEDED_VERIFIED,
        SagaStatus.COMPENSATED_VERIFIED,
        SagaStatus.HUMAN_REQUIRED,
    }
    assert recovered.duplicate_business_effects == 0
```

- [ ] **Step 2: Run the crash matrix and observe the red failure**

Run: `uv run pytest tests/crash/test_crash_matrix.py -q -x`

Expected: tests fail because named failpoints are not yet wired to the worker/store/dispatcher.

- [ ] **Step 3: Add test-only failpoint injection without production branching**

Define a typed failpoint protocol and inject it only into components that own a named durability
boundary. Production uses `NoOpFailpoint`; the subprocess worker uses
`ExitFailpoint.from_environment()` and calls `os._exit(91)` on an exact name. Do not call
`os._exit` or inspect the environment from library modules.

Persist scenario configuration and initialize external fake-service state before spawning. After
death, open fresh processes and connections; never reuse in-memory objects from the killed run.

- [ ] **Step 4: Run crash recovery and SQLite integrity checks**

Run: `uv run pytest tests/crash -q`

Expected: every cut point yields a valid `PRAGMA integrity_check`, contiguous event sequence, projection/replay equality, zero duplicate business effects, and either convergence or explicit human escalation.

- [ ] **Step 5: Commit the crash suite**

```bash
git add tests/crash tests/support/kernel_harness.py
git commit -m "test(kernel): verify recovery across hard crash boundaries"
```

---

### Task 16: Split-Brain, Stale-Worker, and Cross-Saga Scope Tests

**Files:**
- Create: `tests/concurrency/test_split_brain.py`
- Create: `tests/concurrency/test_stale_dispatch.py`
- Create: `tests/concurrency/test_cross_saga_scope.py`
- Modify: `tests/support/durable_tool.py`

**Interfaces:**
- Consumes: lease/fence service, dispatcher, durable fake provider, and barriers.
- Produces: executable evidence for exactly what per-Saga fencing does and does not guarantee.

- [ ] **Step 1: Write stale-worker tests with deterministic barriers**

```python
@pytest.mark.asyncio
async def test_stale_worker_cannot_record_or_apply_after_takeover(harness) -> None:
    old_entered, release_old = harness.provider.blocking_barrier()
    old = asyncio.create_task(harness.dispatch_as("worker-old"))
    await old_entered.wait()
    harness.expire_lease_and_take_over("worker-new")
    release_old.set()
    await old
    assert harness.provider.accepted_fences == [harness.new_fence]
    assert harness.store.load_snapshot(SAGA_ID).fence_token == harness.new_fence
```

- [ ] **Step 2: Run concurrency tests and observe the red failure**

Run: `uv run pytest tests/concurrency/test_split_brain.py tests/concurrency/test_stale_dispatch.py -q`

Expected: at least the stale provider-effect assertion fails until the fake provider enforces conditional fences and the dispatcher rejects stale result commits.

- [ ] **Step 3: Complete downstream fence enforcement and stale-result reconciliation**

The durable fake provider stores the highest accepted fence per resource and rejects lower fences
before mutation. A stale dispatcher discards its local result and cannot write after takeover. The
current live dispatcher observes the prior durable dispatch, atomically records the unknown outcome,
and schedules reconciliation because a nonconforming provider could still have changed externally.

For a provider declaring `fencing_supported=False`, prove that the old and new deliveries share one operation ID and that compensation remains blocked until lookup reports a stable outcome.

- [ ] **Step 4: Document the cross-Saga boundary in an executable test**

```python
def test_per_saga_lease_does_not_claim_cross_saga_inventory_isolation(harness) -> None:
    first, second = harness.reserve_last_unit_from_two_sagas_without_resource_version()
    assert first.provider_observed_race is True
    assert second.provider_observed_race is True
    assert harness.kernel_claims.cross_saga_isolation is False
```

Add the paired provider-version test showing an optimistic inventory version permits only one reservation. This is a downstream guarantee surfaced in evidence, not a kernel claim.

Run: `uv run pytest tests/concurrency -q`

Expected: all tests pass without timing sleeps.

- [ ] **Step 5: Commit concurrency evidence**

```bash
git add tests/concurrency tests/support/durable_tool.py
git commit -m "test(kernel): prove fencing and concurrency boundaries"
```

---

### Task 17: Hypothesis Stateful Model and Safety Property Gate

**Files:**
- Create: `tests/property/reference_model.py`
- Create: `tests/property/test_saga_state_machine.py`
- Create: `tests/property/test_ledger_replay.py`
- Create: `tests/property/test_idempotency_properties.py`

**Interfaces:**
- Consumes: public contracts, pure reducer, SQLite store, compensation planner, and fake tool.
- Produces: a small independent reference model and randomized safety evidence across legal and adversarial event sequences.

- [ ] **Step 1: Write the stateful rule machine**

```python
class SagaRuleMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.reference = ReferenceSaga()
        self.actual = KernelHarness.in_memory_for_property_test()

    @rule(action=sampled_from(tuple(ModelAction)))
    def apply_action(self, action: ModelAction) -> None:
        self.reference.apply(action)
        self.actual.apply(action)

    @invariant()
    def terminal_states_have_fresh_proof_and_no_pending_work(self) -> None:
        if self.actual.snapshot.status.is_terminal:
            assert self.actual.snapshot.last_invariant_seq == self.actual.snapshot.seq - 1
            assert self.actual.runnable_count == 0
            assert not self.actual.snapshot.has_unknown_operations

    @invariant()
    def implementation_matches_reference(self) -> None:
        assert self.actual.material_state() == self.reference.material_state()
```

- [ ] **Step 2: Run properties and observe the first minimized counterexample**

Run: `uv run pytest tests/property -q --hypothesis-show-statistics`

Expected: the initial run exposes any missing transition or reference-model implementation; save no example database in the repository.

- [ ] **Step 3: Implement the independent reference model and complete rules**

The model supports: create/start, authorize/reject, intent, dispatch, full/no/partial/unknown outcome, reconcile present/absent/conflict, lease takeover, retry, compensation intent/outcome, human escalation, invariant pass/fail, and terminal proposal. It tracks only abstract sets and statuses; it must not call the production reducer.

Add explicit properties:

```text
replay(events) equals stored material projection
sequence is contiguous and append-only
terminal implies fresh passing proof and empty runnable outbox
human-required implies zero autonomous runnable commands
unknown implies neither blind retry nor compensation
same logical operation plus same command has at most one fake-provider effect
same logical operation plus changed command is rejected
every confirmed effect is verified, compensated, or explicitly unresolved
```

- [ ] **Step 4: Run deterministic CI profiles**

Run: `uv run pytest tests/property -q --hypothesis-profile=ci`

Expected: all property tests pass with the repository CI profile and print reproducible seeds on failure.

- [ ] **Step 5: Commit property tests**

```bash
git add tests/property
git commit -m "test(kernel): model Saga safety properties with Hypothesis"
```

---

### Task 18: Full Kernel Quality, Mutation, and Claim Audit

**Files:**
- Modify: dependency/gate metadata, package facades, safety-critical runtime functions and their tests
- Modify: `README.md`, `QUICKSTART.md`, `CHANGELOG.md`, `PROVENANCE.md`, and this design/plan
- Create: `tests/integration/test_kernel_end_to_end.py`
- Create: `scripts/run_kernel_mutation_gate.py`
- Create: `docs/kernel-safety-contract.md`

**Interfaces:**
- Consumes: all prior tasks and the repository bootstrap configuration.
- Produces: the kernel release command, enforceable coverage/mutation thresholds, and an honest public safety-contract document consumed by README/BDD/UI work.

- [ ] **Step 1: Add one realistic end-to-end kernel test**

```python
@pytest.mark.asyncio
async def test_generic_effect_compensates_and_reopens_with_verified_evidence(tmp_path) -> None:
    harness = _initialize_harness(tmp_path)
    harness.ensure_forward_intent()
    await harness.settle(Direction.FORWARD)
    harness.ensure_compensation_started()
    harness.ensure_compensation_intent()
    await harness.settle(Direction.COMPENSATION)
    harness.finish(SagaStatus.COMPENSATED_VERIFIED)
    _assert_reopened(harness, tmp_path / "backup.db")
```

- [ ] **Step 2: Run the complete offline suite before changing thresholds**

Run: `env -u OPENROUTER_API_KEY NO_PROXY='*' HTTPS_PROXY='http://127.0.0.1:9' HTTP_PROXY='http://127.0.0.1:9' uv run pytest tests/unit tests/contract tests/integration tests/crash tests/concurrency tests/property -q`

Expected: all tests pass without network access or model credentials.

- [ ] **Step 3: Configure exact safety gates and write the claim contract**

Add mutmut to the development group and refresh the lock:

Run: `uv add --dev "mutmut>=3.6,<4"`

The authoritative current configuration is `[tool.mutmut]` in `pyproject.toml`; repository tests
lock its source files, covered-line behavior, test selection, timeout, and process settings.
`scripts/run_kernel_mutation_gate.py` owns the exact selected function patterns across identity,
authorization, terminal proof, effect outcomes, reconciliation, compensation, redaction, and
SQLite authority. The plan deliberately does not duplicate that executable list.

Configure coverage to require 90% branches for `src/agentic_saga/kernel`, including `policy.py`,
`state.py`, and `invariants.py`. Exclude only abstract protocol bodies and defensive unreachable
version guards, each with a line-specific rationale. The separate mutation gate reads mutmut's
machine-readable CI statistics and fails below 85%, outside 300–500 executed mutants, or for any
survivor, no-test, skipped, suspicious, timeout, interruption, or crash result:

```bash
uv run poe mutation
```

Write `docs/kernel-safety-contract.md` with these exact claims:

```text
Provides: deterministic authorization; intent-before-effect local durability;
append-only ordered evidence; at-least-once dispatch; provider-assisted
deduplication; deterministic reconciliation; verified business compensation;
single-host crash recovery under documented SQLite/filesystem assumptions.

Does not provide: universal exactly-once effects; atomic cross-service commit;
serializable cross-Saga isolation; automatic recovery from opaque external
outcomes; literal rollback of history; high availability with SQLite; safety for
arbitrary tools that fail the adapter contract.
```

- [ ] **Step 4: Run every local release gate**

Run:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src tests
uv run pytest --cov=agentic_saga --cov-branch --cov-report=term-missing --cov-fail-under=90
uv run poe mutation
uv run python -m compileall -q src
```

Expected: lint, format, strict typing, compilation, and tests exit 0; branch coverage is at least
90%; the selected safety-critical mutation score is at least 85% with no blocking result; no
live-model suite runs. This bounded score is not a whole-repository mutation claim.

- [ ] **Step 5: Review the diff and commit the kernel gate**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only the audited runtime/test, gate metadata, and claim-document
files named above are changed.

```bash
git add pyproject.toml uv.lock src/agentic_saga tests docs/kernel-safety-contract.md
git commit -m "test(kernel): enforce deterministic safety gates"
```

## Plan Completion Checklist

- [ ] Every public model rejects extra fields and unintended coercion.
- [ ] Every mutating delivery has durable intent, armed compensation metadata, an outbox row, and a projection update before adapter entry.
- [ ] Every retry reuses the same logical operation ID and rejects command-hash mismatch.
- [ ] Every ambiguous outcome reconciles, waits, or escalates; none becomes inferred failure.
- [ ] Every lease takeover increments the fence; stale ledger commits and supported downstream effects are rejected.
- [ ] Every compensation is receipt-based, idempotent, ordered by reverse dependencies, and verified before terminal assignment.
- [ ] Emergency unwind never compensates unknown work and parks all autonomous work in `HUMAN_REQUIRED`.
- [ ] RunTrace contains complete causal evidence without secrets, payment data, or private chain-of-thought.
- [ ] Storage/tool conformance, real process-crash, barrier concurrency, and Hypothesis suites pass offline.
- [ ] Replay exactly reproduces the stored material projection and detects sequence/hash corruption.
- [ ] Kernel claim language matches measured guarantees and names all excluded distributed-system guarantees.
- [ ] No registry publication or public-release action occurred.
