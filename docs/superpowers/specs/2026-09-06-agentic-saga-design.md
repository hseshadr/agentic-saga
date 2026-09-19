# Agentic Saga — Design Specification

> **Superseded implementation note (2026-09-18):** Earlier references to LangGraph/Deep Agents,
> LangChain-style structured responses, or a custom JSON action protocol describe the discarded
> adapter prototype. The maintained optional path pins `pydantic-deep==0.3.43` and
> `pydantic-ai-slim[openrouter]==2.45.0`, publishes current proposal schemas with a Pydantic AI
> `ExternalToolset`, and accepts exactly one native `DeferredToolRequests` call. See
> [`docs/agent-adapter.md`](../../agent-adapter.md). The kernel authority and Saga semantics remain
> current.

**Status:** Implemented private v0.1 development candidate; not released<br>
**Date:** 2026-09-06  
**Audience:** Python and agent-engineering OSS practitioners  
**Visibility:** Private during development; intended for a later public OSS release  

## TL;DR

Agentic Saga lets a smart agent execute a business goal by choosing and calling typed tools at runtime. Developers provide a declarative Saga Context Manifest, registered tools, policies, and invariants—not a hard-coded workflow graph or business `if/else` tree.

Every side-effecting tool call passes through a deterministic Saga kernel. The kernel records intent before execution, assigns stable idempotency keys, tracks compensation obligations, reconciles ambiguous outcomes, fences concurrent workers, and refuses to declare success or compensation until deterministic invariants prove the final state.

The shipped offline ecommerce reference makes this contract executable from the source checkout. Its
four BDD scenarios cover verified success, reverse-order compensation, restart reconciliation
without duplicate business effect, and unverifiable compensation requiring a human. The
source-checkout **Saga Flight Recorder** visualizes the same exported evidence through the packaged
CLI and its Story, Ledger, and Proof views. Final clean-current-commit and hosted-CI evidence remain
to be recorded; no package is published and no service is hosted.

> The agent decides what the transaction should do. Deterministic infrastructure guarantees what it is allowed to do and proves what actually happened.

## Why This Project Exists

Agent frameworks are good at interpreting goals and adapting to changing evidence. Transaction systems are good at durable state, concurrency, idempotency, and provable outcomes. Neither should impersonate the other.

Existing agent demos often hide side effects behind optimistic tool calls. Existing Saga implementations usually require developers to encode the orchestration graph. Agentic Saga demonstrates a third approach:

- A capable agent owns goal decomposition, sequencing, replanning, forward recovery, and the decision to compensate.
- A small deterministic kernel owns authorization, durable execution, compensation accounting, recovery, and terminal-state verification.
- A visible evidence trail shows exactly where probabilistic reasoning ends and deterministic authority begins.

This is a focused OSS project, not a general workflow platform.

## Product Shape

The private v0.1 development candidate contains three coherent artifacts:

1. **`agentic_saga` Python package** — provider-neutral contracts and a deterministic Saga kernel.
2. **Ecommerce reference application** — an offline reference plus optional planning adapter.
3. **Saga Flight Recorder** — a packaged, loopback-only, read-only evidence console with Story,
   Ledger, and Proof views.

The source-checkout reference is deterministic, offline, and free:

```bash
uv sync --group dev
uv run python -m examples.ecommerce.run
```

The shipped opt-in live evaluation uses OpenRouter from the source checkout:

```bash
export OPENROUTER_API_KEY=...
RUN_LIVE_MODEL_EVALS=1 uv run python -m examples.ecommerce.eval --live \
  --samples 3 --output .artifacts/eval
```

The console command serves one captured trace in the Flight Recorder:

```bash
uv run agentic-saga demo --scenario business-failure --open
```

## Lean, Composable Core

`agentic_saga` is a generic, domain-neutral library. Its core vocabulary is limited to goals,
typed actions and tool intents, effects and outcomes, reconciliation, compensation, recovery, and
human escalation. Ecommerce is a separate sample Saga workflow; its types, rules, prompts, and
branches do not belong in the core package.

The core owns only irreducible Saga semantics. Public boundaries are small typed interfaces that
applications can compose like Lego bricks. Agent-framework integrations, provider SDK glue,
business workflows, demos, and UI stay outside the core. Prefer maintained, industry-standard
libraries and normal Python or `asyncio` facilities over duplicated frameworks or custom
infrastructure.

## Core Principle: Smart Orchestrator, Deterministic Rails

### What the developer supplies

- A validated **Saga Context Manifest** (`saga.yaml`) that combines the objective, operating
  instructions, success criteria, invariants, escalation rules, autonomy level, four typed limits,
  tool references, and non-binding example paths.
- Registered typed read and effect tools referenced by name from the manifest.
- Optional named, registered deterministic policy and invariant checks for domain rules; the
  manifest validates their names while applications register their implementations outside the
  kernel.

The developer does **not** encode an ecommerce decision tree.

### Public authoring contract: Saga Context Manifest

The public authoring experience is a declarative Saga brief, not imperative workflow code. A
validated `saga.yaml` is the single author-written source for the agent's structured context and
the policy/limit references consumed by explicit application assembly. The loader does not install
checks or configure the kernel by itself. The manifest describes intent and limits; it does not
prescribe a state machine, encode tool order, or introduce business branches inside the kernel.

The manifest contains human-readable objective, instructions, success criteria, invariants,
escalation rules, autonomy boundaries, and example paths alongside four strictly typed limits and tool
references. Tool input and result schemas are not copied into YAML: tool names resolve against
versioned registered adapters and descriptors, whose schemas and effect, idempotency,
reconciliation, and compensation metadata remain authoritative. Live state, receipts, operation
identity, and recovery evidence remain exclusively in durable storage.

The manifest layer is a small Pydantic model plus maintained `ruamel.yaml` safe loading—not a
custom workflow language. It pins the selected authoritative descriptor catalog by digest and
validates named checks against application-supplied inventories before rendering canonical agent
context. Gherkin features remain executable examples and
evaluation artifacts; manifest example paths are guidance for the agent and never kernel control
flow. The agent receives the validated manifest, available tool descriptors, and current durable
projection, then chooses exactly one next action from the authoritative observation.

The schema must be domain-neutral. The ecommerce reference application will remain the polished
demo, while a small ticket-booking manifest/test fixture will prove portability across
search/hold/pay/issue and release/refund compensation without adding a second UI. Domain-specific
invariants are optional named registered checks, never a framework DSL or application-specific
`if/else` in core.

The low-level API exposes the necessary pieces separately: `SagaGoal` carries only an
objective and public context, `SagaDefinition` binds tools, policies, invariants, and budgets, and
runtime construction exposes kernel collaborators. `load_saga_context` returns authoring inputs for
those pieces; it does not build a `SagaDefinition` or enforce a runtime allowlist. Application
assembly remains explicit and durable state never moves into YAML.

### What the agent controls

- Decomposing the business goal.
- Selecting and ordering tools.
- Interpreting observations and partial failures.
- Deciding whether to retry, inspect, use an alternate provider, continue forward, or compensate.
- Replanning after each tool result.
- Requesting entry into compensation after a failed goal.
- Selecting among only the compensation actions in the kernel-computed safe frontier.
- Proposing that the Saga is complete.
- Providing a concise structured rationale for each proposal.

### What the kernel controls

- Tool allowlisting and strict argument validation.
- Durable intent-before-effect execution.
- Kernel-generated logical operation IDs and idempotency keys.
- Typed tool authorization, capability and reversibility checks, and durable limits.
- Append-only evidence and rebuildable state projection.
- Leases, compare-and-swap transitions, and fencing tokens.
- Compensation obligations and eligibility.
- Reconciliation of unknown external outcomes.
- Turn, tool-call, output-token, and agent-call-time allocations.
- Final invariant evaluation and terminal-state assignment.
- Deterministic emergency unwind when the agent is unavailable or exhausted.

The agent may propose. Only the kernel may authorize and execute a side effect or assign a terminal state.

For a positive turn limit, the runtime reserves the fixed floors `token_limit // turn_limit` and
`elapsed_ms_limit // turn_limit` before each agent call. The maintained OpenRouter adapter uses
those as a maximum-output setting and call deadline. They are not input-token, actual provider
usage, end-to-end Saga-time, or monetary meters. Agentic Saga does not enforce provider spend;
operators must configure provider-account controls before any explicitly authorized live call.

## Runtime Interaction

The agent operates incrementally instead of submitting an opaque, long-running plan:

```text
Goal + prompt + current projection + available typed tools
                         │
                         ▼
                Agent proposes action
                         │
                         ▼
              Kernel validates proposal
                  │ reject      │ accept
                  ▼             ▼
            Evidence only   Record intent + outbox
                                  │
                                  ▼
                             Execute tool
                                  │
                                  ▼
                        Record typed outcome
                                  │
                                  ▼
                       Return observation to agent
```

The public agent boundary is conceptually:

```python
class AgentDriver(Protocol):
    async def next_action(
        self,
        observation: SagaObservation,
        available_tools: tuple[ToolDescriptor, ...],
    ) -> ToolCall | Finish | BeginCompensation | Escalate: ...
```

The current v0.1 adapters are:

- `ScriptedProposalDriver` in the ecommerce example for deterministic tests and the default demo.
- `DeepAgentsDriver`, backed by Pydantic Deep native deferred tools, as the optional planning
  adapter used by the opt-in evaluation path.

Pydantic Deep and Pydantic AI are adapters, not the business record and not part of the core public
contract. On restart, agent context is rebuilt from the Saga kernel's authoritative projection and
previously accepted decisions.

### V0.1 trust boundary

LLM proposals, persisted inputs, and every network/provider result are untrusted data. The kernel
validates, bounds, redacts, and records them before they can affect policy or terminal state.
Installed `AgentDriver` and `EffectAdapter` implementations are trusted application code: v0.1
invokes them in process and requires their async methods to cooperate with cancellation. The core
library does not claim to sandbox arbitrary Python, native extensions, or deliberately hostile
plugins. Deploy those integrations behind a process, container, or service boundary owned by the
application.

Core JSON values are capped at depth 16, 4,096 nodes, 256 items per container, 16 KiB per UTF-8
string, and 64 KiB encoded. Every exact `SagaDefinition` owns the immutable `RedactionPolicy` used
across its runtime and historical trace export. Built-in credential and payment-secret rules are a
minimum floor, not a general PII detector: applications must explicitly list every ordinary PII
key they admit, and unlisted fields are public by contract. `saga.yaml` is public authoring context
and must contain no PII, credential, receipt, or private provider data.

This trust-boundary simplification is an unreleased-v0.1 development store-format cutover. Stores
that persist the abandoned factory capability fields or `AgentTurnReserved.agent_factory_digest`
are unsupported and must be reinitialized; no pre-release data migration is provided. Installed
driver or adapter behavior changes require a new pinned definition version.

## Typed Tools and Effect Registration

Read-only tools are logged but create no compensation obligation. Mutating tools must declare their operational capabilities and, where applicable, compensation:

```python
@saga.effect(
    compensate_with="refund_payment",
    reconcile_with="find_payment",
    reversibility="semantic",
)
async def charge_payment(command: ChargePayment) -> PaymentReceipt: ...
```

Each effect definition includes:

- Strict input and result schemas.
- Explicit independence and resource selectors for compensation groups where applicable.
- Idempotency scope and retention window.
- Reconciliation support.
- Cancellation and fencing support.
- Reversibility: exact, semantic mitigation, or irreversible.
- Possible partial effects.
- Compensation dependencies.

Free-form dictionaries are not accepted at the safety boundary. Pydantic discriminated unions use strict validation and reject extra fields.

Agent-supplied idempotency keys are ignored or rejected. The kernel derives a stable logical identity from Saga ID, step instance, direction, and semantic generation. Transport retries reuse that identity; a retry counter is never part of the business idempotency key.

## State Model

### Saga states

```text
CREATED
  → RUNNING
      → RECOVERY_PLAN_REQUIRED → RUNNING | COMPENSATING | HUMAN_REQUIRED
      → RETRY_WAIT → RUNNING
      → RECONCILING_UNKNOWN → RUNNING | COMPENSATING | HUMAN_REQUIRED
      → COMPENSATING → COMPENSATED_VERIFIED | HUMAN_REQUIRED
      → SUCCEEDED_VERIFIED
      → ABORTED_CLEAN

HUMAN_REQUIRED → RUNNING | COMPENSATING | RESOLVED_WITH_EXCEPTION
```

`RECONCILING_UNKNOWN` and `HUMAN_REQUIRED` are durable, quiescent states. The design deliberately avoids a vague `FAILED` terminal state: a timeout is not proof that an external effect failed.

### Operation states

```text
PLANNED
  → INTENT_DURABLE
  → DISPATCHED
  → EFFECT_CONFIRMED | NO_EFFECT_CONFIRMED | PARTIAL_EFFECT_CONFIRMED | OUTCOME_UNKNOWN
```

Exceptions, disconnects, timeouts, and worker death after dispatch default to `OUTCOME_UNKNOWN`. The runtime never guesses that no effect occurred.

### Terminal meanings

- `SUCCEEDED_VERIFIED` — all required forward effects and current success invariants are verified.
- `COMPENSATED_VERIFIED` — all confirmed effects requiring repair have verified compensations and compensation invariants pass.
- `ABORTED_CLEAN` — authoritative evidence proves no external effect remains.
- `RESOLVED_WITH_EXCEPTION` — an authenticated human explicitly accepted named residual effects; actor, reason, evidence, and invariant results are recorded.

No terminal transition is legal while an operation is unknown, a command remains runnable, a required approval is pending, or an invariant is stale or failing.

## Ledger, Outbox, and Storage

SQLite is the v0.1 single-host reference backend. It is deliberately not presented as a highly available distributed backend.

One durable database transaction:

1. Compares the expected Saga sequence and fencing token.
2. Appends the typed operation intent.
3. Arms its possible compensation.
4. Inserts an outbox command.
5. Updates the rebuildable current-state projection.

Only after commit may a dispatcher call an external system. The result is appended in a second fenced transaction. A crash between the external effect and its result record is expected and enters reconciliation.

Ledger events contain versioned recovery data, not only hashes:

- Event, Saga, sequence, trace, and actor identity.
- Definition and schema versions.
- Step instance, direction, and logical operation identity.
- Canonical redacted command and result.
- Durable external receipt or correlation reference.
- Delivery attempt and fence token.
- Policy, prompt, model, provider, and invariant versions.
- Structured agent rationale—never hidden chain-of-thought.
- Recorded time from an injectable clock.

Append-only database controls prevent ordinary mutation. An optional hash chain offers tamper evidence, not magical immutability. Projection rebuild from the ledger must reproduce the material state exactly.

SQLite uses durable transactions, `synchronous=FULL`, integrity checks, and tested backup/restore
procedures. It is POSIX-only: the immediate parent must be current-user-owned and not group/world
writable, and database, `-wal`/`-shm`, temporary, backup, and restored leaves are hardened to mode
`0600`. Backup and restore publish to fresh destinations only; no overwrite mode exists. This
assumes a truthful local filesystem. ACLs, root, same-UID processes, quotas, encryption, retention,
and deletion remain operator responsibilities. The storage protocol ships with a backend
conformance suite so future backends can be evaluated against the same kernel semantics.

## Ambiguous Outcomes and Reconciliation

Typed external outcomes are:

```text
EffectConfirmed(receipt)
NoEffectConfirmed(reason)
PartialEffectConfirmed(receipts)
OutcomeUnknown(correlation)
```

A mutating call may be retried only when the provider durably deduplicates the same operation ID for longer than the recovery horizon, or authoritative reconciliation proves no effect occurred. Otherwise the Saga becomes `HUMAN_REQUIRED` without a blind retry.

The honest execution guarantee is:

> At-least-once dispatch with provider-assisted effect deduplication and deterministic reconciliation.

The project never promises universal exactly-once side effects.

## Concurrency and Fencing

Every Saga has a monotonic sequence, renewable executor lease, and monotonic fencing token. State-changing appends use compare-and-swap on both sequence and fence.

Adapters pass fencing information to downstream services that support conditional writes. Without downstream fencing, the runtime serializes local dispatch, reuses the logical operation ID, and refuses to compensate while an earlier forward attempt could still complete. It must cancel, quiesce, and reconcile first; otherwise it escalates.

Per-Saga fencing cannot prevent two independent Sagas from racing over a shared SKU or account. Cross-Saga isolation remains the downstream service's responsibility through reservation tokens, optimistic versions, uniqueness constraints, or resource-level serialization.

## Recovery and Compensation

The agent normally owns recovery strategy:

1. Inspect additional evidence.
2. Retry when policy permits.
3. Select an alternate forward path.
4. Request eligible compensation.
5. Escalate when no safe action exists.

The kernel exposes only currently legal actions. Before executing each proposal it confirms that the proposal is based on the current Saga sequence, satisfies policy and resource boundaries, fits all budgets, has any required approval, and conflicts with no unknown operation.

Compensation is armed with the forward intent but becomes eligible only after a full or partial effect is confirmed. Unknown outcomes reconcile first. Compensation runs in reverse topological dependency order, not merely reverse declaration order. Independent repairs may run in parallel only when explicitly modeled as independent.

A compensation is itself an idempotent, fenced, intent-first Saga operation with its own retries and verification. Compensation means business repair invariants hold; it does not mean history was reversed.

If the agent is unavailable, loops, exceeds budget, or repeatedly produces invalid proposals, the kernel activates a deterministic emergency unwind using eligible registered compensations. It never compensates an unknown or unconfirmed effect.

## Human Intervention

Human intervention is a fail-closed exception path, not normal orchestration. It is required only when the kernel cannot safely continue or prove a valid outcome, including:

- An external effect remains unknown and cannot be reconciled.
- A provider lacks sufficient idempotency or lookup guarantees.
- An irreversible or policy-sensitive effect requires approval.
- Compensation repeatedly fails.
- Conflicting downstream evidence cannot be resolved.
- The agent or recovery budget is exhausted without a safe deterministic unwind.

`HUMAN_REQUIRED` is quiescent: no autonomous mutating work occurs. Human actions are authenticated, scoped to an exact Saga sequence and proposal, single-use, and durably recorded. Duplicate or stale approvals are rejected.

## Flagship Ecommerce Story

The business goal is to complete an order using typed tools such as:

- `create_order` / `cancel_order`
- `charge_payment` / `refund_payment`
- `check_inventory`
- `reserve_inventory` / `release_inventory`
- `schedule_fulfillment` / `cancel_fulfillment`

The prompt describes business behavior and preferences. It does not prescribe a fixed call sequence. In the main failure story:

1. The agent creates the order and captures payment.
2. Primary inventory is unavailable.
3. The agent inspects alternate stock and restock evidence.
4. Depending on policy and evidence, it dynamically selects alternate fulfillment or compensation.
5. The kernel validates and records every effect.
6. The final state is proved as fulfilled, compensated, or explicitly unresolved.

Durable fake order, payment, inventory, and fulfillment services use storage separate from the Saga database. This makes lost-response and process-crash scenarios realistic.

## Saga Flight Recorder

The console is a read-only incident inspector inspired by railway signal boxes and aircraft flight recorders. Its visual grammar makes authority explicit:

```text
Agent proposes → Policy decides → Saga acts → Invariants prove
```

- Dashed signal amber: agent observation or proposal.
- Gate symbol: deterministic policy decision.
- Solid cobalt: executed effect with receipt and idempotency identity.
- Reverse connector: compensation paired with its forward effect.
- Proof teal: validated invariant.
- Fault red plus text/icon: failure or rejected proposal.

The visual palette uses porcelain, ink, cobalt, signal amber, fault red, and proof teal. Atkinson Hyperlegible is the interface font; IBM Plex Mono is reserved for IDs and payloads. The design avoids the generic dark “AI terminal” aesthetic.

### Primary surfaces

1. **Trajectory** — select one of four captured, redacted reference runs.
2. **Flight Path** — outcome, causal event lanes, current proof, and replay controls.
3. **Story** — fixed human-readable causal descriptions.
4. **Ledger** — safe chronological identifiers and receipt references.
5. **Proof** — invariant rules, concrete evidence bindings, and results.

Selecting an event reveals its recorded input/output, structured reason, policy decision, before/after state, attempt, duration, operation identity, and hashes. The UI never exposes or implies private chain-of-thought.

The console consumes one stored `RunTrace` contract. It is not a workflow editor, monitoring
platform, or operations control plane. V0.1 uses a small Vite + TypeScript static application
bundled with the Python package; the CLI materializes one selected bundled trace and serves the
local assets without a general web API. Fixture generation remains an explicit repository command.

The implemented console is keyboard operable, responsive to 320 CSS pixels, reduced-motion aware,
and has no critical/serious axe violations in the current browser gate. Those claims require fresh
exact-commit release evidence before publication.

## Current BDD Vertical Slice and Lower-Level Expansion

The shipped `pytest-bdd` feature is executable documentation. It uses temporary real Saga and
provider SQLite databases, the real kernel/runtime, a proposal-only scripted driver, and durable
fake external services. It proves the happy path, reverse compensation, restart reconciliation,
and failed-compensation escalation. The broader behaviors below are covered by focused unit,
integration, crash, concurrency, property, and contract tests; additional BDD prose is optional,
not a substitute for those executable gates.

### `01_goal_fulfillment.feature`

- Direct fulfillment reaches `SUCCEEDED_VERIFIED` with one logical effect per step.
- Dynamic alternate inventory succeeds without a hard-coded recovery branch.
- A duplicate agent command returns the prior result and produces one business effect.

### `02_compensation.feature`

- Payment captured, inventory unavailable, agent elects compensation.
- Only confirmed steps enter the compensation frontier.
- A transient refund failure retries idempotently and succeeds.
- An unverifiable refund never yields `COMPENSATED_VERIFIED`.

### `03_crash_recovery.feature`

- Hard process exit at every commit/effect boundary converges or escalates without duplication.
- Concurrent resume allows one fenced transition path.
- A stale worker cannot overwrite current state.
- `HUMAN_REQUIRED` remains quiescent across restart.

### `04_unsafe_agent.feature`

- Unknown tools, malformed arguments, wrong resources, second charges, stale plans, and agent-supplied keys are rejected with zero forbidden effects.
- Prompt injection embedded in tool output cannot bypass policy.
- False completion is rejected when invariants fail.
- Free-form or malformed output cannot trigger an effect.

### `05_manual_escalation.feature`

- Exhausted budgets enter `HUMAN_REQUIRED` without later autonomous mutation.
- High-risk actions pause before execution.
- Exact, version-bound human approval executes once.
- Rejection selects deterministic compensation.
- Duplicate and stale decisions are rejected.
- A human reconciliation decision resumes without duplicating an external effect.

### `06_evidence_and_privacy.feature`

- The ledger explains every outcome.
- Terminal state follows invariant evidence.
- Prompts, arguments, results, and errors apply the exact definition-owned redaction policy;
  application-classified ordinary PII is redacted along with the built-in credential floor.

BDD assertions target business state, terminal state, logical side-effect counts, and semantic event ordering. They do not snapshot timestamps, serialization details, or free-form rationale.

## Verification Strategy

### Required offline release gate

- Pure reducer and policy unit tests.
- Tool-adapter conformance tests.
- SQLite persistence and replay tests.
- Subprocess hard-crash tests at durable failpoints.
- Barrier-based concurrency and stale-worker tests—never timing sleeps.
- A generic end-to-end kernel scenario plus the four shipped ecommerce BDD paths.
- Compact Hypothesis sequences over real runtime and storage boundaries.
- At least 90% branch coverage for kernel, policy, state, and invariant code.
- At least 85% selected safety-critical mutation score for a bounded, named function set; this is
  never described as whole-repository mutation coverage.
- Network disabled and no model credentials required.

Core properties include:

- A successful terminal state always satisfies fresh success invariants.
- A compensated terminal state has no unresolved compensation liability.
- `HUMAN_REQUIRED` performs no autonomous mutation.
- No unapproved capability reaches an adapter.
- One logical command creates at most one observable business effect under the adapter's declared guarantees.
- Every confirmed effect is verified, compensated, or explicitly unresolved.
- Replay yields the same material state.

### Optional live OpenRouter evaluation

Live evaluation measures agent usefulness, never kernel safety. It is non-gating for ordinary pull
requests and uses a fixed case corpus, prompt version, tool schemas, model ID, provider policy,
output-token/call-deadline limits, and a deterministic business-state oracle. Live use can cost
money, but the library does not meter or cap provider spend; explicit consent and provider-account
controls are required.

Initial configuration:

- Provider: OpenRouter.
- Default model: `openai/gpt-oss-20b`.
- Same-model provider failover only; no silent cross-model fallback.
- Low reasoning, zero temperature, no Pydantic Deep harness tools, and a required native tool call.
- Zero or multiple deferred calls are rejected locally before execution; unsupported
  `parallel_tool_calls` and `seed` parameters are not sent.
- No `openrouter/auto`, random free routing, or moving `latest` aliases.

The initial 24-case corpus contains six straightforward goals, six recoverable failures, six adversarial observations, and six escalation cases. Each runs three samples.

Targets:

- Structured decision validity: at least 98%.
- Recoverable episodes reaching an allowed valid outcome within budget: at least 90%.
- Critical irreversible or unverifiable cases escalated: 100%.
- Forbidden observable effects: zero.
- Kernel rejection of unsafe proposals: 100%.
- Secret or payment-data leakage: zero.

Provider failures are classified separately from model-quality failures. Accepted unsafe model output remains evidence; it is not silently erased and rerun.

## Dependencies and Packaging

Core runtime targets Python 3.12+ and keeps dependencies narrow:

- Pydantic for strict boundary models.
- Standard-library SQLite for the reference store.
- No Pydantic Deep, Pydantic AI, or provider package required by core users.

The maintained Pydantic Deep/Pydantic AI/OpenRouter integration is isolated in the optional
`agent` extra.
Contributors use the `dev` dependency group for pytest, Hypothesis, coverage, mutation testing,
Ruff, and mypy.

Use `uv` for contributor workflows and hatchling for packaging. Follow the portfolio's shared CI, security, provenance, governance, and trusted-publishing patterns. Package publication remains out of scope until separately authorized.

## Current Lean Repository Structure

```text
src/agentic_saga/
  contracts/          # Strict values, actions, outcomes, events, clocks, redaction
  kernel/             # Definitions, ports, policy, reducer, invariants, compensation
  storage/            # SQLite reference implementation
  execution/          # Runtime composition, dispatch, leases, reconciliation, unwind
  agents/             # Optional Pydantic Deep/Pydantic AI/OpenRouter planning adapter
  evidence/           # RunTrace projection/export
  demo/               # Flight Recorder materialization and loopback server
  cli/                # Demo command

examples/ecommerce/
  *.py                # Reference assembly, provider, evaluation, and trace export
  saga.yaml           # Public example context

web/flight-recorder/  # Vite + TypeScript static inspector

tests/
  unit/
  contract/
  integration/
  crash/
  concurrency/
  property/
  bdd/features/
  bdd/steps/
  live_model/
```

Canonical advanced homes are `kernel.definitions` for definitions/catalogs,
`contracts.common` for strict JSON and canonical hashing, `contracts.clock` for clocks,
`contracts.redaction` for redaction, `kernel.ports` for the storage protocol,
`storage.sqlite` for the POSIX store, and the small `agents` and `demo` facades for their maintained
integrations. Advanced pre-1.0 module paths and symbols are not compatibility promises.

## Non-Goals for V0.1

- General-purpose workflow designer or workflow runtime.
- Microservices, Kafka, Kubernetes, Redis, or Celery.
- A hosted operations platform.
- Universal exactly-once side effects.
- Automatic safety derived from prompts alone.
- Arbitrary shell, filesystem, database, or network access for the agent.
- Generic event-sourcing framework.
- Multi-agent delegation inside the transaction.
- Publishing to PyPI or another registry without explicit authorization.

## Security and Privacy

- Installed drivers and adapters are trusted application code, while their inputs and outputs are
  untrusted data. The core library enforces bounded async deadlines but not hostile-code
  containment; adapters must be cancellation-cooperative.
- The live agent receives only Saga-scoped typed tools; general filesystem, shell, and subagent tools are disabled.
- Tool output is untrusted data and cannot authorize actions.
- Prompt injection is contained by deterministic policy and strict schemas.
- Tool schemas validate commands. Manifests reference named application policy checks for
  additional domain constraints; the generic kernel contains no domain-specific branches.
- Every exact definition carries one redaction policy through ordinary ledger and exported evidence.
  Built-in credential/payment-secret rules cannot be weakened; applications explicitly classify
  ordinary PII keys, and unlisted fields are public by contract.
- Sensitive recovery payloads require an encrypted envelope and retention policy before real integrations are added.
- Structured reasons are recorded; private chain-of-thought is neither requested nor stored.

## Honest Claims

V0.1 may claim that it:

- Demonstrates mission-critical transaction safety patterns for agent-orchestrated tools.
- Makes probabilistic orchestration inspectable and deterministically bounded.
- Provides crash-recoverable single-host reference execution under documented assumptions.
- Proves its safety properties without a live model.

V0.1 must not claim that it:

- Is itself production-ready for every mission-critical workload.
- Provides universal exactly-once execution.
- Makes arbitrary agent tools safe.
- Sandboxes hostile Python or native adapter implementations inside the core library.
- Reverses history through compensation.
- Provides high availability with the SQLite backend.

## Definition of Done for V0.1

- A prompt-driven agent completes the ecommerce goal without a hard-coded workflow graph.
- Pydantic Deep can orchestrate the same typed native proposal contract through OpenRouter.
- The deterministic scripted agent runs all scenarios offline.
- Every mutating tool call is intent-first, idempotent, recorded, and compensation-aware.
- Happy path, alternate forward recovery, compensation, crash recovery, retry, unsafe proposal, and exhausted recovery are demonstrated.
- All required BDD scenarios pass.
- Crash and concurrency tests prove the documented SQLite guarantees.
- Terminal states require fresh invariant evidence.
- The Flight Recorder clearly separates proposal, policy, effect, compensation, and proof.
- A new contributor can run the flagship scenario within five minutes.
- The README includes a copy-pasteable quickstart, realistic walkthrough, extension guide, trust model, and limitations.
- CI, security scanning, provenance, governance, license, and contribution files follow the established OSS portfolio patterns.
- The repository remains private until a separate public-release decision.
- No package is published without explicit authorization.

## Naming Risk

`Agentic Saga` is the working project and repository name. A similarly named `agent-saga` package already exists, so the public package name and brand require a final collision and discoverability review before release. This does not block private implementation.
