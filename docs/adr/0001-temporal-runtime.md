# ADR 0001: Temporal is the sole durability engine

- Status: Accepted
- Date: 2026-09-19
- Scope: Pre-1.0 runtime cutover

## TL;DR

Agentic Saga will use Temporal as its only durable execution engine. We will remove the custom SQLite runtime instead of maintaining a compatibility layer or two execution paths.

Temporal will persist workflow history, resume after failure, schedule retries and timers, and coordinate workers. Agentic Saga will remain a small, generic layer for agent planning, tool policy, compensation semantics, human escalation, and a readable execution view.

This is a clean pre-1.0 break. Existing local SQLite runs will not be migrated.

## Why

A saga is a long-running transaction made of ordinary operations plus compensating operations that undo completed work when a later step fails. Durable execution is difficult infrastructure: crashes, retries, timers, replay, concurrent workers, and deployment compatibility all have subtle failure modes.

Temporal already solves that infrastructure problem and is designed to resume application workflows after failures. It can be run through [Temporal Cloud or a self-hosted Temporal Service](https://docs.temporal.io/production-deployment). Rebuilding those capabilities inside Agentic Saga adds code and risk without improving the project's core idea.

The useful OSS contribution is narrower: let an agent choose among approved business tools while deterministic policy controls what may execute, what compensates, and when a human must decide.

## Decision

Use one runtime architecture:

1. A deterministic Temporal Workflow owns saga state and control flow.
2. Model calls and business I/O run as Temporal Activities, never directly in Workflow code.
3. Successful forward Activities append typed compensation receipts to Workflow state.
4. Failure starts deterministic compensation in reverse dependency order.
5. A human reads pending work through a Query and responds through an authenticated, authorized, validated Update.
6. The UI and flight recorder project from durable Workflow state/history; they are not a second source of truth.

The Pydantic agent integration may use Pydantic AI's [Temporal durable-execution integration](https://ai.pydantic.dev/durable_execution/temporal/). That integration is an adapter, not another durability engine.

## Runtime modes

| Environment | Temporal mode | Contract |
| --- | --- | --- |
| Production | Temporal Cloud or a production self-hosted Temporal Service | External durable service, separately operated and monitored; application workers contain Workflow and Activity code. |
| Local development and demo | `temporal server start-dev` | Disposable local server and Web UI. State is ephemeral by default; `--db-filename` may preserve local state. The dev server is never a production deployment. See the [Temporal CLI development-server command](https://docs.temporal.io/cli/server#start-dev). |
| Automated integration tests | `WorkflowEnvironment.start_time_skipping()` | Isolated test server with virtual-time skipping. It is test infrastructure, not an embedded production mode. See the [Python SDK testing guide](https://docs.temporal.io/develop/python/testing-suite). |
| Pure unit tests | No server | Test policy, schemas, compensation ordering, and deterministic decision functions as ordinary Python. |

## Responsibility boundary

### Agentic Saga retains

- the generic context manifest and prompt contract;
- typed tool, input, result, failure, and compensation-receipt contracts;
- the allowlist and deterministic eligibility checks applied before execution;
- the planner/agent adapter and model-provider adapters;
- mapping each forward tool to its compensation tool;
- deterministic compensation ordering and terminal-state rules;
- classification of ambiguous outcomes that require reconciliation;
- human-escalation policy and validated decision schemas;
- domain-neutral events and the human-readable execution projection;
- examples such as ecommerce, which remain examples rather than runtime assumptions.

### Temporal owns

- durable Workflow history and crash recovery;
- worker coordination and task delivery;
- Activity retries, backoff, timeouts, cancellation, and timers;
- durable Workflow state transitions and replay;
- long waits for human decisions;
- Workflow Queries and Updates;
- operational history, visibility, and the Temporal Web UI;
- safe rollout mechanisms for changed Workflow code.

Agentic Saga will not wrap these capabilities in lookalike storage abstractions.

## Delivery semantics: at least once, not exactly once

Temporal can retry an Activity after a timeout even when the external provider completed the request but the result was lost. Therefore every side-effecting tool must define:

- a stable idempotency key derived from the saga and logical step;
- a provider request identifier when the provider supports one;
- a typed receipt containing the identifiers needed to compensate;
- a reconciliation operation for an unknown outcome;
- retryable and non-retryable failure classes;
- bounded retry and timeout policy.

Temporal durability does not make a payment, booking, email, or inventory API exactly once. Agentic Saga must never compensate an unknown outcome by guessing. It reconciles first; if reconciliation cannot establish the outcome safely, it escalates to a human.

See Temporal's distinction between durable Workflows and failure-prone [Activities](https://docs.temporal.io/activities).

## Human decision boundary

- A Query returns a read-only, typed view of the pending decision and evidence.
- The application/API authenticates the human and authorizes access to that saga. Temporal messaging is not the authentication layer.
- A Workflow Update carries a typed decision, actor reference, reason, and expected decision version.
- An Update validator rejects malformed, stale, unauthorized-by-policy, or state-incompatible requests before the Workflow accepts them.
- The Update handler records the accepted decision in durable Workflow state and resumes execution.
- Queries never mutate state. Raw prompts, provider credentials, and authorization tokens never appear in Query results or Update payloads.

This follows Temporal's [message-passing model for Queries and Updates](https://docs.temporal.io/develop/python/message-passing).

## Security and privacy

Workflow inputs, Activity inputs/results, errors, memo fields, and search attributes may be persisted in Temporal history or visibility storage. We therefore assume payloads are durable records.

- Store references and minimum necessary facts, not API keys, access tokens, or full sensitive documents.
- Resolve secrets inside Activities from the deployment's secret manager.
- Redact exception messages before they cross the Activity boundary.
- Never place sensitive data in Search Attributes; they are intended for visibility and lookup.
- Configure namespace retention to the product's data-retention policy.
- Use a client-side payload codec/encryption where sensitive payload persistence is unavoidable, following Temporal's [data-converter and encryption guidance](https://docs.temporal.io/develop/python/converters-and-encryption).
- Keep logs and the Agentic Saga projection subject to the same minimization rules; they must not become shadow history stores.

## Determinism, testing, replay, and versioning

Workflow code must be deterministic. It may inspect Workflow state and call Temporal Workflow APIs, but it must not perform network/file I/O, read wall-clock time directly, generate unmanaged randomness, or call a model directly. Those operations belong in Activities.

The pre-alpha source release contract is:

1. Unit-test policy, compensation order, schema validation, and state transitions without Temporal.
2. Integration-test happy path, forward failure, reverse compensation, Activity retry, unknown outcome, and human wait/resume with the time-skipping environment.

Before a production rollout, cancellation, Worker restart, and representative open/closed history
replay fixtures become required gates. The Python SDK documents
[Workflow replay testing](https://docs.temporal.io/develop/python/testing-suite#replay). Deployed
Workflow histories must then be treated as an API: use Temporal's supported
[Python Workflow versioning](https://docs.temporal.io/develop/python/workflows/versioning) or an
explicit patching/Worker rollout strategy when control flow changes, and retain old Workflow code
until every retained execution can replay through the replacement or has been migrated by a
documented Temporal mechanism.

## Clean-cutover deletion intent

The cutover deletes code, tests, configuration, and documentation whose only job is to reproduce Temporal:

- SQLite schemas, migrations, repositories, and run persistence;
- lease, claim, heartbeat, and worker-ownership machinery;
- local inbox/outbox delivery and recovery loops;
- custom retry scheduling, timers, and timeout recovery;
- custom crash recovery and history replay;
- SQLite backup/restore and compatibility code;
- CLI flags and public APIs that select the legacy runtime;
- tests that assert the deleted implementation rather than saga behavior.

Useful domain contracts, safety-policy tests, agent adapters, examples, BDD scenarios, and the execution projection are retained and adapted to Temporal. No dual-write, import bridge, migration utility, feature flag, or SQLite compatibility package will be shipped.

## Rejected alternatives

### Keep the custom SQLite runtime

Rejected because it duplicates hard infrastructure, increases security and correctness surface area, and distracts from agentic orchestration.

### Support SQLite and Temporal side by side

Rejected because two runtimes create two semantic contracts, double the test matrix, and encourage drift in retry, compensation, and recovery behavior.

### Hide both behind a generic durability interface

Rejected because the lowest-common-denominator abstraction would obscure Temporal's Workflow, Activity, Query, Update, history, and versioning guarantees while preserving unnecessary legacy concepts.

### Use the in-process time-skipping test server in production

Rejected because `WorkflowEnvironment.start_time_skipping()` is a test harness. Production requires Temporal Cloud or a production self-hosted Temporal Service; local manual development uses `temporal server start-dev`.

## Consequences

The project becomes smaller and more focused, with fewer correctness claims implemented in local code. Operators must run or buy Temporal, and contributors must understand Workflow determinism and replay compatibility. That dependency is intentional: durable saga execution is infrastructure, while Agentic Saga is the composable agent-orchestration layer built on top of it.
