# Agentic Saga

[![Dagger](https://github.com/hseshadr/agentic-saga/actions/workflows/dagger.yml/badge.svg)](https://github.com/hseshadr/agentic-saga/actions/workflows/dagger.yml)

Let an agent choose the next useful step. Let Temporal guarantee that the transaction can recover.

Agentic Saga is a small Python library for long-running, multi-system transactions. It combines:

- **Temporal** for durable execution, retries, timers, recovery, and history;
- **Pydantic Deep** or **Jev** for bounded, tool-based decisions; and
- your typed business tools for effects, checks, reconciliation, and compensation.

The agent is the smart orchestrator. It is not the transaction authority. Deterministic workflow
code still controls which tools are eligible, how often they may run, what must be proven, and how
confirmed effects are compensated.

> Status: pre-alpha source release. The API may change. The ecommerce provider is a simulation,
> not a production commerce integration.

## What is a Saga?

Suppose checkout must reserve stock, charge a card, and schedule fulfillment. Those systems cannot
share one database transaction. A Saga treats each successful step as a fact and pairs it with an
undo action:

| Forward action | Compensation if a later step fails |
| --- | --- |
| Reserve inventory | Release inventory |
| Charge payment | Refund payment |
| Schedule fulfillment | Cancel fulfillment |

If fulfillment fails after the first two actions succeeded, the Saga compensates the confirmed
effects in safe reverse order. If a provider response is lost, it first reconciles with that
provider instead of guessing or repeating the effect blindly.

## Why make the orchestrator agentic?

A static orchestrator accumulates branches for alternate suppliers, stale facts, partial progress,
retries, and every new exception. An agent can inspect fresh public evidence and choose the best
currently eligible capability without encoding every route as nested `if/else` logic.

The model does **not** receive unrestricted functions. On each turn it sees a small, typed set of
eligible tools. Temporal then validates and executes the proposal using deterministic rules.

```text
public goal + current evidence + eligible tools
                       |
                       v
              bounded agent decision
                       |
                       v
        deterministic Temporal Workflow validates
              /                    \
     business Activity       reject invalid proposal
              |
      receipt or reconciliation
              |
      prove success, continue, or compensate
```

No probability model or Markov decision process is required. The LLM only needs to choose a tool
and produce arguments within a constrained schema. The hard transaction guarantees remain ordinary,
testable code.

## The ecommerce example

The example is one application of the generic library, not a workflow baked into Agentic Saga.

```text
reserve inventory -> charge payment -> schedule fulfillment -> verify order
                                                              |
                              +-------------------------------+------------------+
                              |                                                  |
                       verified success                                   proof fails
                              |                                                  |
                   SUCCEEDED_VERIFIED                          cancel -> refund -> release
                                                                                 |
                                                                    COMPENSATED_VERIFIED
```

The agent chooses among forward tools. It never chooses rollback order. The Workflow derives that
order from confirmed effects and registered compensation pairs. A human enters only when automatic
reconciliation or compensation cannot prove a safe result. Human resolutions are authenticated by
an application-owned verification Activity before the Workflow accepts them.

The executable behavior includes four scenarios:

- verified happy path;
- fulfillment rejection with automatic `cancel -> refund -> release`;
- lost payment response recovered by stable-ID reconciliation without a duplicate charge; and
- unresolved refund compensation that pauses for a verified human resolution.

## Watch it in 60 seconds

The packaged Flight Recorder replays captured, redacted traces. It needs no API key or running
Temporal server:

```bash
uv run --no-dev agentic-saga demo --scenario business-failure --open
```

Choose **Watch from start**. The UI reveals the forward path and reverse compensation at a pace a
person can follow. Try every captured outcome:

```bash
uv run --no-dev agentic-saga demo --scenario happy-path --open
uv run --no-dev agentic-saga demo --scenario lost-response --open
uv run --no-dev agentic-saga demo --scenario compensation-failure --open
```

The recorder is read-only. It cannot call a business tool or alter a Saga.

## Run the real workflow locally

Install the locked development environment and start Temporal's single-binary development server:

```bash
uv sync --group dev
temporal server start-dev
```

In another terminal:

```bash
uv run python -m examples.ecommerce.run happy-path
uv run python -m examples.ecommerce.run business-failure
uv run python -m examples.ecommerce.run lost-response
uv run python -m examples.ecommerce.run compensation-failure
```

`temporal server start-dev` is convenient local infrastructure, not a production topology. The
test suite uses Temporal's time-skipping test server. Production applications should connect the
same Worker and client code to Temporal Cloud or an operated Temporal Service.

See [QUICKSTART.md](QUICKSTART.md) for the complete copy-paste path.

## The Lego model

Agentic Saga deliberately does not reimplement a durable workflow engine.

| Lego | Responsibility |
| --- | --- |
| Temporal | Durable history, crash recovery, Activity retries, timers, Queries, and Updates |
| Agentic Saga Workflow | Eligibility, prerequisites, global budgets, proof gates, reverse compensation, escalation |
| Pydantic Deep | OpenRouter-backed native tool choice from bounded public context |
| Jev adapter | Optional probability-bearing choice among application-built candidates |
| Your integrations | Typed provider calls, idempotency, authorization, reconciliation, receipts |
| Flight Recorder | Redacted, read-only explanation of the recorded execution |

This keeps the library focused on the seam between probabilistic planning and deterministic
transaction safety.

## Describe intent with `saga.yaml`

`saga.yaml` is an agent context template, not an executable workflow language. It explains the
public objective, use cases, available capability names, safety guidance, and decision budgets.
Your application still registers the real typed tools in Python.

```yaml
schema_version: "1.0"
name: ecommerce-checkout
version: "1.0"
objective: Complete an order safely or compensate every confirmed effect.
instructions:
  - Choose one eligible forward tool from fresh public evidence.
success_criteria:
  - Authoritative proof confirms the completed order.
autonomy:
  mode: guarded
  instructions:
    - Never guess when a provider outcome is unknown.
budgets:
  turn_limit: 8
  tool_call_limit: 6
  elapsed_ms_limit: 60000
  token_limit: 4000
tools:
  catalog_sha256: "<digest of the registered descriptors>"
  allowed:
    - reserve_inventory
    - charge_payment
    - schedule_fulfillment
    - verify_order
checks:
  policy: []
  success: [order_verified]
  compensation: [effects_compensated]
  clean_abort: [no_external_effects]
escalation:
  conditions:
    - Compensation remains unresolved after safe retries and reconciliation.
  instructions:
    - Present redacted evidence to an authorized operator.
```

Compensation tools are registered with their forward effects; they are not offered to the agent as
forward choices. The Workflow invokes them when recovery is required. The same manifest shape can
describe ticket booking, travel reservations, provisioning, or any other Saga.

Read [the context-manifest guide](docs/context-manifest.md).

## Choose a decision adapter

### Deterministic driver

Start here. The included deterministic driver exercises the real Temporal Workflow and is the
fastest way to prove provider semantics, compensation, and recovery without an LLM.

### Pydantic Deep through OpenRouter

Pydantic Deep supplies the model/tool loop. Agentic Saga disables its unrelated filesystem,
subagent, shell, memory, and web features, advertises only current native proposal tools, sets
temperature to zero, and allows one bounded decision per Workflow turn.

```bash
uv sync --extra agent --group dev
cp .env.example .env
# Add OPENROUTER_API_KEY to .env, then explicitly opt in to a live evaluation.
```

Ordinary tests and release gates never make a paid model call. Read
[the agent adapter guide](docs/agent-adapter.md) for the opt-in command and data boundary.

### Jev

Use Jev when the application can build a closed set of valid candidates and wants a compact
decision engine to rank them. The adapter preserves the returned probabilities and confidence;
Jev cannot invent arguments or execute an effect.

```bash
uv sync --extra jev --group dev             # TypeSafe API
uv sync --extra jev-openrouter --group dev  # OpenRouter Decisions API
```

## Minimal integration shape

An application supplies a typed `ToolRegistry`, an `AgentDriver`, a bounded `ExecutionBudget`, and
an authenticated human-resolution Activity. Agentic Saga supplies the Temporal Activities,
Workflow, client helpers, and Worker assembly.

```python
activities = TemporalActivities(agent, registry, budget)
worker = build_worker(
    client,
    task_queue="orders",
    activities=activities,
    human_resolution_activity=verify_operator_resolution,
)

handle = await start_saga(client, workflow_input, task_queue="orders")
result = await handle.result()
```

See [`examples/ecommerce`](examples/ecommerce) for complete tool definitions, provider-side
idempotency, reconciliation, a Worker, and all four outcomes.

## What the library guarantees—and what it cannot

- Workflow decisions, prerequisites, budgets, compensation order, and terminal proof are
  deterministic and replayable.
- Activities are **at least once**. Every effect integration must use the stable operation identity
  for provider idempotency or fencing and implement authoritative reconciliation.
- A lost response is not success or failure. The Workflow reconciles it before continuing.
- `SUCCEEDED_VERIFIED` requires fresh proof. Failed proof starts compensation.
- Unresolved compensation stops at `HUMAN_REQUIRED`; it never guesses.
- A human Update is accepted only after an application-owned verification Activity authenticates
  its opaque authorization reference.

The library cannot create atomic commits across external services, make a non-idempotent provider
safe, contain hostile installed Python, or decide your organization's authorization policy.

Workflow inputs and history must be public/redacted under the default converter. Applications that
need private production payloads must configure a Temporal payload encryption codec backed by
their key-management system and enforce namespace access controls. Never place credentials in
`saga.yaml`, Workflow inputs, trace files, or receipts.

Read the [Temporal safety contract](docs/temporal-safety-contract.md),
[operations guide](docs/operations.md), and [security policy](SECURITY.md) before connecting real
providers.

## Architecture and source map

[Explore the interactive architecture](docs/architecture/index.html) to follow a decision through
Temporal, an external provider, reconciliation, compensation, and verified human recovery.

| Surface | Responsibility |
| --- | --- |
| `src/agentic_saga/temporal/` | Workflow, Activities, typed client/Worker helpers, journal, trace projection |
| `src/agentic_saga/contracts/` | Strict serializable values and public payload limits |
| `src/agentic_saga/agents/` | Pydantic Deep, Jev, and OpenRouter decision adapters |
| `src/agentic_saga/manifest.py` | Bounded domain-neutral context manifests |
| `examples/ecommerce/` | Realistic provider, Worker, scenarios, BDD features, and evaluation |
| `web/flight-recorder/` | Accessible animated trace explorer |

## Prove it locally

```bash
uv sync --group dev
uv run poe gate
cd web/flight-recorder
npx --yes pnpm@11.5.0 install --frozen-lockfile
npx --yes pnpm@11.5.0 gate
cd ../..

# After committing, from a clean exact source tree:
uv run poe artifacts
uv run poe release-candidate
```

`poe gate` runs Python formatting, lint, strict typing, complexity, offline tests, Temporal
time-skipping tests, coverage, and release-contract unit tests. The pinned `pnpm` gate separately
proves the recorder's tests, accessibility, build, packaged-asset parity, and browser behavior. The
final two commands build and install the package from a clean exact commit. Core branch coverage
must stay at or above 90%. Dagger combines those proofs again from clean, exact source.

No release command publishes to PyPI. This repository does not claim a released package until a
fresh exact-commit gate and an explicit publish decision exist. See [PROVENANCE.md](PROVENANCE.md).

## License

Apache-2.0. Bundled frontend dependencies and notices are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
