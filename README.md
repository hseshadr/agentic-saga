# Agentic Saga

[![Dagger](https://github.com/hseshadr/agentic-saga/actions/workflows/dagger.yml/badge.svg)](https://github.com/hseshadr/agentic-saga/actions/workflows/dagger.yml)

TL;DR: **Under private development.** Agentic Saga is a generic Python library for agent-directed,
side-effecting work. A smart agent chooses the next typed action; a deterministic Saga kernel
authorizes it, records intent before execution, reconciles uncertainty, compensates verified
effects, and refuses to call an unproven outcome “done.”

Run the candidate console command from this source checkout and open its read-only Flight Recorder:

```bash
uv run --no-dev agentic-saga demo --scenario business-failure --open
```

The first dependency installation may access the package registry. The demo itself needs no key,
model, or external runtime service: it serves one captured, redacted ecommerce trace on
`127.0.0.1`, waits for Ctrl-C, and removes its temporary site on shutdown. Use `happy-path`,
`lost-response`, or `compensation-failure` to inspect another real captured outcome.

## The idea

An application may build typed context directly or load the optional `saga.yaml` authoring format.
Both give the agent an objective, bounded planning allocations, eligible tool descriptions, proof
checks, and escalation guidance; neither defines a fixed workflow. The agent may propose a read,
effect, finish, compensation phase, or escalation; only the kernel may turn a proposal into a
durable external action.

Delivery is deliberately honest. Attempts are at least once. Durable intent, stable operation
identity, provider deduplication and fencing where declared, and reconciliation make ambiguity
recoverable. If safe reconciliation still cannot prove what happened, the Saga becomes durably
quiescent in `HUMAN_REQUIRED`.

## Architecture

[Open the architecture signal board](docs/architecture.html) for the visual lifecycle and exact
source/test map.

| Surface | One responsibility |
| --- | --- |
| `src/agentic_saga/manifest.py` | Validate domain-neutral `saga.yaml` context and registered names. |
| `src/agentic_saga/contracts/` | Define strict, serializable values at every public boundary. |
| `src/agentic_saga/cli/` | Expose the packaged demo command and no kernel authority. |
| `src/agentic_saga/agents/` | Return one strict proposal; never receive business-tool authority. |
| `src/agentic_saga/kernel/` | Own policy, identity, budgets, compensation frontier, and terminal proof. |
| `src/agentic_saga/execution/` | Dispatch, reconcile, recover, and coordinate leases. |
| `src/agentic_saga/storage/` | Provide append-only SQLite evidence, replay, backup, and restore. |
| `src/agentic_saga/evidence/` | Export deterministic redacted traces. |
| `src/agentic_saga/demo/` | Materialize and serve the generic loopback Flight Recorder. |
| `examples/ecommerce/` | Exercise the public Lego pieces as one realistic application. |
| `web/flight-recorder/` | Validate and replay stored evidence without mutating Saga state. |

Ecommerce is an example, not a workflow hidden in the core. The ticket-booking manifest uses the
same authoring shape; applications register their own typed tools, adapters, policies, and
invariants.

## Compose the supported runtime

The root package provides one supported composition path. Applications construct the typed Lego
pieces, then pass all eight collaborators and identity values explicitly:

```python
from agentic_saga import SagaGoal, compose_runtime

runtime = compose_runtime(
    store=store,
    definition=definition,
    policy_context_provider=policy_context_provider,
    terminal_gate=terminal_gate,
    invariant_evidence_provider=invariant_evidence_provider,
    clock=clock,
    worker_id="orders-worker",
    id_namespace=b"acme-orders-v1",
)

goal = SagaGoal(goal_id="order-123", text="Complete the order safely.", context={})
result = await runtime.start(definition=definition, goal=goal, agent=agent)
```

`SagaGoal` enters `runtime.start(...)` separately; it is transaction input, not a hidden ninth
composition setting. See the complete working assembly in
[`examples/ecommerce/demo.py`](examples/ecommerce/demo.py).

## Shipped private v0.1 behavior

- Generic typed contracts and a single-host SQLite reference kernel with deterministic policy,
  intent-before-effect dispatch, recovery, reconciliation, compensation, human escalation, backup,
  restore, and redacted evidence export.
- Strict, bounded, deliberately public `saga.yaml` loading with authoritative registered
  descriptors and named checks.
- Optional Deep Agents + OpenRouter planning adapter that returns one strict proposal while the
  kernel retains every side-effect decision.
- Four executable `pytest-bdd` ecommerce paths: verified success, reverse compensation, lost-response
  restart reconciliation, and unverifiable compensation requiring a human.
- A versioned 24-case deterministic evaluation corpus plus a separately opt-in, credential-gated
  OpenRouter evaluator. Ordinary validation makes no live model call.
- A keyboard-operable Flight Recorder implementation with four distribution-bound captured traces,
  bounded strict loading, user-controlled replay, and Story, Ledger, and Proof views.
- An offline release-measurement harness, dual-Python hosted workflow, exact package-content checks,
  and packaged-browser gate. Exact main commit
  `635974d51f87aa802886914be6a46bfd28518c66` passed the hosted
  [Dagger gate](https://github.com/hseshadr/agentic-saga/actions/runs/34727077866) and independent
  [security gate](https://github.com/hseshadr/agentic-saga/actions/runs/34727111885).

## Prove it locally

```bash
uv sync --group dev
uv run poe gate
uv run python scripts/measure_release.py
```

The quality gate and implemented measurement harness stay offline and credential-free. The
measurement command runs the Python and frontend gates, builds and installs a wheel from locked
inputs, exercises all four scenarios and the packaged recorder, and enforces the published resource
and latency budgets. It reports the exact commit and environment; a dirty-tree result is diagnostic
only. The hosted Dagger gate above verified the clean exact main commit, including both supported
Python versions, the frontend gate, packaged-browser checks, dependency audits, and release budgets.

## Boundaries

This is a private, unpublished, non-hosted, single-host v0.1. The example provider is deterministic
simulation, not a production commerce integration. The library does not provide universal
exactly-once effects, atomic cross-service commit, high availability, serializable cross-Saga
isolation, or containment for hostile installed Python/native code. Pre-release storage has no
migration guarantee.

Every generic JSON boundary is capped at depth 16, 4,096 nodes, 256 items per container, 16 KiB per
UTF-8 string, and 64 KiB encoded. Each exact `SagaDefinition` owns the redaction policy used by its
runtime and exported evidence. The built-in policy is a credential and payment-secret floor; the
application must explicitly add every ordinary personal-data key it permits into Saga inputs.
Unlisted fields are public by contract, and `saga.yaml` must contain public authoring context only.

`turn_limit`, `tool_call_limit`, `token_limit`, and `elapsed_ms_limit` are deterministic kernel
limits. Tokens and elapsed milliseconds are fixed per-turn planning allocations: the maintained
OpenRouter adapter uses them only as an output-token cap and an agent-call deadline. They do not
measure input tokens, actual provider usage, end-to-end Saga time, money, or provider spend. Set
provider-account spend limits separately before any opt-in live call.

The SQLite reference store is POSIX-only and requires an owner-controlled, non-shared-writable
local parent directory. It keeps database, sidecar, temporary, backup, and restored files at mode
`0600`; backup and restore publish only to fresh destinations and never overwrite. Read the
operations contract before handling real data: same-UID/root access, ACLs, local-filesystem truth,
retention, and deletion remain operator responsibilities.

Start with the [Quickstart](QUICKSTART.md). Before integrating a real provider, read the
[Kernel safety contract](docs/kernel-safety-contract.md),
[operations and release contract](docs/operations.md),
[Saga Context Manifest guide](docs/context-manifest.md), and
[agent adapter guide](docs/agent-adapter.md).
