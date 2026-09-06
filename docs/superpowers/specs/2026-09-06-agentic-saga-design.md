# Agentic Saga — Design Specification

**Status:** Proposed for implementation  
**Date:** 2026-09-06  
**Audience:** Python and agent-engineering OSS practitioners  
**Visibility:** Private during development; intended for a later public OSS release  

## TL;DR

Agentic Saga lets a smart agent execute a business goal by choosing and calling typed tools at runtime. Developers provide a prompt, tools, policies, and invariants—not a hard-coded workflow graph or business `if/else` tree.

Every side-effecting tool call passes through a deterministic Saga kernel. The kernel records intent before execution, assigns stable idempotency keys, tracks compensation obligations, reconciles ambiguous outcomes, fences concurrent workers, and refuses to declare success or compensation until deterministic invariants prove the final state.

The flagship ecommerce demo makes this contract visible in a browser-based **Saga Flight Recorder**. Executable `pytest-bdd` feature files prove happy paths, alternate forward recovery, compensation, crash recovery, unsafe-plan rejection, and the rare cases that require human intervention.

> The agent decides what the transaction should do. Deterministic infrastructure guarantees what it is allowed to do and proves what actually happened.

## Why This Project Exists

Agent frameworks are good at interpreting goals and adapting to changing evidence. Transaction systems are good at durable state, concurrency, idempotency, and provable outcomes. Neither should impersonate the other.

Existing agent demos often hide side effects behind optimistic tool calls. Existing Saga implementations usually require developers to encode the orchestration graph. Agentic Saga demonstrates a third approach:

- A capable agent owns goal decomposition, sequencing, replanning, forward recovery, and the decision to compensate.
- A small deterministic kernel owns authorization, durable execution, compensation accounting, recovery, and terminal-state verification.
- A visible evidence trail shows exactly where probabilistic reasoning ends and deterministic authority begins.

This is a focused OSS project, not a general workflow platform.

## Product Shape

The repository ships three coherent artifacts:

1. **`agentic_saga` Python package** — provider-neutral contracts and a deterministic Saga kernel.
2. **Ecommerce reference application** — Deep Agents + OpenRouter orchestrating realistic typed tools.
3. **Saga Flight Recorder** — a local browser console that replays agent proposals, policy gates, effects, compensation, ledger events, and invariant proofs.

The default quickstart is deterministic, offline, and free:

```bash
uv sync
uv run agentic-saga demo --scenario inventory-exhausted --open
```

An optional live mode uses OpenRouter:

```bash
export OPENROUTER_API_KEY=...
uv run agentic-saga demo --live --scenario inventory-exhausted --open
```

The offline and live modes are always labeled distinctly in the UI and evidence.

## Core Principle: Smart Orchestrator, Deterministic Rails

### What the developer supplies

- A system prompt describing behavior and recovery preferences.
- A business goal and initial context.
- Typed read and effect tools.
- Deterministic authorization policies.
- Deterministic success and compensation invariants.
- Explicit limits for turns, tool calls, elapsed time, retries, tokens, and cost.

The developer does **not** encode an ecommerce decision tree.

### What the agent controls

- Decomposing the business goal.
- Selecting and ordering tools.
- Interpreting observations and partial failures.
- Deciding whether to retry, inspect, use an alternate provider, continue forward, or compensate.
- Replanning after each tool result.
- Selecting among currently eligible recovery and compensation actions.
- Proposing that the Saga is complete.
- Providing a concise structured rationale for each proposal.

### What the kernel controls

- Tool allowlisting and strict argument validation.
- Durable intent-before-effect execution.
- Kernel-generated logical operation IDs and idempotency keys.
- Authorization, tenant/resource boundaries, and amount limits.
- Append-only evidence and rebuildable state projection.
- Leases, compare-and-swap transitions, and fencing tokens.
- Compensation obligations and eligibility.
- Reconciliation of unknown external outcomes.
- Retry, time, token, and cost budgets.
- Final invariant evaluation and terminal-state assignment.
- Deterministic emergency unwind when the agent is unavailable or exhausted.

The agent may propose. Only the kernel may authorize and execute a side effect or assign a terminal state.

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
    ) -> ToolCall | Finish | Escalate: ...
```

The v0.1 adapters are:

- `ScriptedAgentDriver` for deterministic tests and the default demo.
- `DeepAgentsDriver` for the flagship live experience.

Deep Agents and LangGraph are adapters, not the business record and not part of the core public contract. On restart, agent context is rebuilt from the Saga kernel's authoritative projection and previously accepted decisions.

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
- Resource and tenant selectors.
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

SQLite uses durable transactions, `synchronous=FULL`, integrity checks, documented filesystem assumptions, and tested backup/restore procedures. The storage protocol ships with a backend conformance suite so future PostgreSQL or Temporal adapters can be evaluated against the same safety claims.

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

1. **Scenario workbench** — seven compact replayable scenarios and their fault controls.
2. **Run inspector** — outcome, causal flight path, current proof, and replay controls.
3. **Story** — human-readable causal sequence.
4. **Ledger** — filterable chronological evidence.
5. **Proof** — invariant rules, concrete inputs, and results.

Selecting an event reveals its recorded input/output, structured reason, policy decision, before/after state, attempt, duration, operation identity, and hashes. The UI never exposes or implies private chain-of-thought.

The console consumes one stored `RunTrace` contract. It is not a workflow editor, monitoring platform, or operations control plane. V0.1 uses a small Vite + TypeScript static application bundled with the Python package; the CLI generates trace data and serves the local assets without a general web API.

The console must be keyboard operable, WCAG AA, responsive, reduced-motion aware, and provide semantic list/table alternatives for every visualization.

## BDD Feature Contract

`pytest-bdd` feature files are both executable documentation and integration tests. Every scenario uses a temporary real SQLite database, the real kernel, a scripted agent, and durable fake external services.

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

- Hard process exit at every journal/effect boundary converges or escalates without duplication.
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
- Prompts, arguments, results, and errors redact secrets and sensitive payment data.

BDD assertions target business state, terminal state, logical side-effect counts, and semantic event ordering. They do not snapshot timestamps, serialization details, or free-form rationale.

## Verification Strategy

### Required offline release gate

- Pure reducer and policy unit tests.
- Tool-adapter conformance tests.
- SQLite persistence and replay tests.
- Subprocess hard-crash tests at durable failpoints.
- Barrier-based concurrency and stale-worker tests—never timing sleeps.
- `pytest-bdd` end-to-end scenarios.
- Hypothesis stateful tests against a small reference model.
- At least 90% branch coverage for kernel, policy, state, and invariant code.
- At least 85% mutation score for those safety-critical modules.
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

Live evaluation measures agent usefulness, never kernel safety. It is non-gating for ordinary pull requests and uses a fixed case corpus, prompt version, tool schemas, model ID, provider policy, token/cost limits, and deterministic business-state oracle.

Initial configuration:

- Provider: OpenRouter.
- Default model: `openai/gpt-oss-20b`.
- Fallback: `qwen/qwen3-30b-a3b-instruct-2507`.
- Low reasoning, near-zero temperature, sequential tool calls, strict structured output.
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
- No LangGraph, Deep Agents, or provider package required by core users.

Optional extras:

- `agentic-saga[deepagents]` for the Deep Agents/LangGraph adapter.
- `agentic-saga[openrouter]` for the reference model provider.
- `agentic-saga[demo]` for the packaged Flight Recorder assets.
- `agentic-saga[dev]` for pytest, pytest-bdd, Hypothesis, coverage, mutation testing, Ruff, and mypy.

Use `uv` for contributor workflows and hatchling for packaging. Follow the portfolio's shared CI, security, provenance, governance, and trusted-publishing patterns. Package publication remains out of scope until separately authorized.

## Proposed Repository Structure

```text
src/agentic_saga/
  contracts/          # Strict commands, outcomes, observations, plans
  kernel/             # Reducer, policy, invariants, compensation frontier
  storage/            # Store protocol and SQLite reference backend
  execution/          # Outbox, dispatcher, leases, recovery
  agents/             # Scripted and optional Deep Agents adapters
  evidence/           # Ledger projection, redaction, RunTrace export
  cli/                # Demo and inspection commands

examples/ecommerce/
  tools/              # Durable fake business services
  prompts/            # Versioned agent behavior prompt
  policies/           # Deterministic authorization policy
  invariants/         # Success and compensation proofs
  scenarios/          # Deterministic fixture definitions

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

- The live agent receives only Saga-scoped typed tools; general filesystem, shell, and subagent tools are disabled.
- Tool output is untrusted data and cannot authorize actions.
- Prompt injection is contained by deterministic policy and strict schemas.
- Tenant, resource, currency, and amount constraints are validated independently of the prompt.
- Ordinary ledger fields contain redacted data; secrets and raw payment data are never recorded.
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
- Reverses history through compensation.
- Provides high availability with the SQLite backend.

## Definition of Done for V0.1

- A prompt-driven agent completes the ecommerce goal without a hard-coded workflow graph.
- Deep Agents can orchestrate the same typed tool contract through OpenRouter.
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
