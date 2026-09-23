# Temporal safety contract

TL;DR: the agent may choose only among currently eligible tools. A deterministic Temporal Workflow
validates that choice, records state, executes I/O through Activities, compensates confirmed effects
in reverse order, and pauses for an authenticated human only when automation cannot prove a safe
next step.

## Exact v0.1 claims

Agentic Saga provides:

- one deterministic Temporal Workflow for Saga control flow;
- typed and bounded Workflow, Activity, Query, and Update contracts;
- a bounded agent-decision Activity;
- deterministic tool eligibility and proposal validation;
- at-least-once effect execution with stable operation identity;
- reconciliation before retry or compensation after ambiguity;
- reverse-order compensation for confirmed effects;
- authenticated, sequence-bound human resolution; and
- a redacted, domain-neutral trace projection.

It does not provide universal exactly-once effects, atomic commit across Temporal and an external
provider, a sandbox for installed code, automatic resolution of an opaque provider outcome, or a
production Temporal Service.

## Workflow versus Activities

Workflow code is deterministic. It can inspect its recorded state and use Temporal Workflow APIs.
It cannot perform network or filesystem I/O, call a model, read secrets, use unmanaged randomness,
or depend on wall-clock time.

Activities own all I/O:

- the agent-decision Activity calls a deterministic driver or model adapter;
- tool Activities call business providers;
- reconciliation Activities ask providers what happened; and
- the human-verification Activity validates an authorization reference through application-owned
  identity and policy systems.

The model proposes an action. It never receives an executable provider adapter, Temporal Client,
credential, idempotency key, or human authorization token.

## State machine

```text
forward work
  -> succeeded_verified                         when final proof succeeds
  -> compensation in reverse dependency order  when the goal becomes unreachable
      -> compensated_verified                   when every journaled undo has a confirmed outcome
      -> human_required                         when an undo remains unresolved
```

A confirmed outcome is a provider receipt, a reconciliation that confirms the undo, or a verified
human resolution. `compensated_verified` rests on those per-step outcomes; the
`compensation_completed` event that precedes it records that the journaled compensations ran in
reverse order and is not an invariant evaluation.

An ordinary business failure does not require a human. The Workflow compensates automatically.
Human escalation begins only after reconciliation cannot prove a safe automatic action.

## At-least-once boundary

Temporal may deliver an Activity more than once. The Activity request carries stable Saga, tool,
and operation identity. A provider adapter must treat the same identity plus the same command as
the same logical effect and reject identity reuse with a different command.

A successful effect returns a typed receipt. A lost response produces an unknown outcome, not a
guessed success or failure. Reconciliation can establish:

- the effect occurred, with the receipt needed to continue or compensate;
- no effect occurred, so a same-identity retry is safe; or
- the result remains unknown, so automatic work must stop.

Compensation is also at least once and needs the same idempotency and reconciliation discipline.

## Human boundary

`query_saga_state` reads the durable typed state and cannot mutate it.
`resolve_human_compensation` submits a typed Workflow Update.

Before an Update is accepted, the application-owned verifier must authenticate the person,
authorize access to the Saga, and bind the decision to the unresolved operation. The Workflow
rejects stale event sequences and state-incompatible decisions. An accepted decision is recorded
in history and resumes compensation.

## Data boundary

The default contracts are public-safe. Workflow inputs, Activity inputs/results, failures, and
Update values may remain in Temporal history for the Namespace retention period. Use opaque IDs and
minimum necessary facts. Never include credentials, authorization tokens, raw private documents,
or unbounded provider errors.

Private production payloads require the same client-side encrypted Data Converter/Payload Codec on
all Clients and Workers, keys managed in an external KMS, and least-privilege Namespace access.
Search Attributes must remain non-sensitive. See [Security](../SECURITY.md).

## Runtime modes

- Production: Temporal Cloud or a production-ready self-hosted Temporal Service.
- Local development: `temporal server start-dev` with its disposable local Web UI.
- Integration tests: `WorkflowEnvironment.start_time_skipping()` only.
- Pure unit tests: no service.

The test server is not an in-process production database and must never be presented as one.

## Executable evidence

| Claim | Evidence |
| --- | --- |
| Workflow determinism and typed state | `tests/integration/temporal/test_saga_workflow.py` |
| Compensation journal and reverse order | `tests/unit/temporal/test_journal.py` and `tests/integration/temporal/test_saga_workflow.py` |
| Activity identity and retry behavior | `tests/unit/temporal/test_activities.py` |
| Typed client Query and Update operations | `tests/unit/temporal/test_client.py` |
| Worker registration and sandbox | `tests/unit/temporal/test_worker.py` |
| Happy path, compensation, lost response, and human pause | `tests/bdd/features/ecommerce_saga.feature` and `tests/bdd/steps/test_ecommerce_saga.py` |
| Generic trace projection | `tests/unit/temporal/test_trace.py` |
| Public API and removal of retired runtime | `tests/test_public_api.py` and `tests/test_repository_contract.py` |

Run the Python and Temporal evidence gate:

```bash
uv run poe gate
```

The gate includes `pytest -m temporal --force-enable-socket`. Its release-contract tests verify that
Temporal is a runtime dependency, retired runtime packages are absent, and the installed-artifact
checks fail closed. Run `uv run poe release-candidate` from a clean exact commit to build and install
the real wheel before making a release claim.

## Replay and versioning contract

Workflow histories are durable compatibility inputs. Before a production rollout:

1. export representative recent open and closed histories for every affected task queue;
2. replay them against the candidate Workflow code;
3. fail CI on nondeterminism;
4. use a compatible Worker/versioning or patching strategy for changed control flow; and
5. retain old code and encryption keys while retained histories still need them.

Temporal's [Python testing guide](https://docs.temporal.io/develop/python/best-practices/testing-suite)
documents time-skipping tests and history replay.

## Supported imports

The root package exposes the curated typed client and contract surface: `SagaGoal`,
`SagaWorkflowInput`, `WorkflowState`, `WorkflowTool`, `start_saga`, `get_saga_handle`,
`query_saga_state`, and `resolve_human_compensation`, plus the manifest contracts and related typed
results.

The `agentic_saga.temporal` package additionally exposes the integration Legos:
`TemporalActivities`, `build_worker`, `connect_local_client`, `connect_cloud_client`,
`TemporalCloudConfig`, and `project_run_trace`. The compatibility name `connect_client` is
local-development only and rejects remote targets.

Advanced pre-1.0 module paths are not compatibility promises merely because repository code can
import them.

## Deliberate clean break

The unpublished pre-1.0 durability implementation and its compatibility APIs were removed.
Temporal is the sole durability engine. There is no dual write, migration utility, or fallback
runtime.

See [Operations](operations.md) for deployment, privacy, monitoring, incident handling, and release
evidence.
