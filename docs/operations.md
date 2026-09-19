# Operations and release contract

TL;DR: Temporal is the only durability engine. Run Workers against Temporal Cloud or a
production-ready self-hosted Temporal Service. Keep Workflow code deterministic, make every
side-effecting Activity idempotent and reconcilable, compensate confirmed effects automatically in
reverse order, and ask a human only when automation cannot establish a safe outcome.

This is the operator contract for v0.1. It separates behavior supplied by Agentic Saga from
durability supplied by Temporal.

## Runtime modes

| Environment | Mode | What it means |
| --- | --- | --- |
| Production | Temporal Cloud or a production-ready self-hosted Temporal Service | Externally operated durable service; application-owned Workers run Workflow and Activity code. |
| Local development | `temporal server start-dev` | Disposable local service and Web UI; convenient, not production. |
| Integration tests | `WorkflowEnvironment.start_time_skipping()` | Isolated test-server process with virtual time; test-only. |
| Unit tests | No Temporal service | Test schemas, policies, ordering, and pure decision logic directly. |

Temporal's official [production guide](https://docs.temporal.io/production-deployment) makes the
same production/local distinction. Its
[Python testing guide](https://docs.temporal.io/develop/python/best-practices/testing-suite)
documents time skipping and history replay.

## Responsibility boundary

Agentic Saga owns:

- typed goals, observations, proposals, tools, receipts, and outcomes;
- deterministic eligibility and decision validation;
- the bounded agent-decision Activity;
- mapping forward effects to compensation tools;
- reverse compensation order and terminal proof rules;
- reconciliation contracts and human-escalation policy; and
- the redacted Flight Recorder projection.

Temporal owns:

- durable Event History and Workflow state recovery;
- task delivery, timers, retries, timeouts, and Worker coordination;
- long waits for external or human input;
- Queries and Updates; and
- replay and safe Worker rollout mechanisms.

There is no compatibility path or second runtime.

## Deterministic Workflow, bounded agent

The Workflow decides only from recorded state and Temporal APIs. It does not call a model, provider,
filesystem, secret manager, or wall clock directly. Those operations run as Activities.

The agent-decision Activity receives a public observation, the currently eligible tool schemas,
and a bounded execution budget. The agent proposes; it does not execute. The Workflow accepts only
a current, typed, eligible proposal.

## Effect-delivery contract

Activities execute at least once. A timeout can occur after the provider accepted an operation but
before the Worker recorded the response. Temporal durability cannot make a payment, reservation,
booking, or email exactly once.

Every side-effecting adapter must provide:

1. a stable idempotency key for the logical operation;
2. a typed receipt containing the provider reference needed to compensate;
3. reconciliation by that stable identity;
4. bounded Start-to-Close/Schedule-to-Close timeouts and retry policy; and
5. explicit retryable and non-retryable failures.

On ambiguity, reconcile before retrying or compensating. If the provider proves success, continue
from the confirmed receipt. If it proves no effect, a same-identity retry may be safe. If it cannot
prove either outcome, stop automatic work and escalate.

## Compensation and human escalation

When the forward goal becomes unreachable, the Workflow automatically compensates confirmed
effects in reverse dependency order. Compensation is another business operation; it does not erase
history.

Human escalation is reserved for unresolved cases: an unknown external outcome, missing authority,
or a compensation whose result cannot be proven. It is not the normal failure path.

- A Query returns a read-only, typed operator view.
- The application authenticates the operator and authorizes that Saga.
- A validated Update binds the decision to the current event sequence and operation.
- Stale, replayed, malformed, or unauthorized decisions are rejected.
- An accepted Update is recorded in history before execution resumes.

## Privacy and data handling

Workflow and Activity payloads may be stored in Temporal Event History. Search Attributes are
visible through the visibility layer. The default Agentic Saga converter is therefore restricted
to public-safe values: opaque IDs, minimized facts, and redacted projection fields. Credentials,
authorization tokens, private documents, and raw provider errors stay outside payloads.

Private production payloads require an application-supplied, client-side encrypted Data
Converter/Payload Codec on every Client and Worker, with keys held in an external KMS. Key
retention must cover every history that may still replay. The Namespace must enforce
least-privilege roles and namespace-scoped credentials. See Temporal's
[Python data-handling guide](https://docs.temporal.io/develop/python/data-handling) and
[Cloud security model](https://docs.temporal.io/evaluate/cloud/security).

Do not put sensitive values in Search Attributes. Secure any Codec Server separately; access to
the Temporal Web UI is not permission to decrypt private business payloads.

## Production runbook

1. **Provision Temporal.** Choose Temporal Cloud or operate a production-ready self-hosted service.
   Create a dedicated Namespace, retention policy, scoped identities, network controls, audit
   export, backup policy, and alerts.
2. **Deploy compatible Workers.** Pin the application artifact and task queue. Replay representative
   retained histories before rollout. Use Temporal's supported Worker/versioning strategy when a
   change would alter Workflow commands.
3. **Inject secrets into Activities.** Resolve OpenRouter and provider credentials from the
   deployment secret manager. Never pass them through Workflow history.
4. **Observe.** Alert on exhausted Activity retries, reconciliation waits, compensation failures,
   `human_required`, task-queue backlog, schedule-to-start latency, Workflow-task failures, and
   Namespace capacity.
5. **Recover provider ambiguity.** Reconcile by stable operation identity. Never infer success from
   a missing response and never create a new idempotency key for the same logical effect.
6. **Resolve a human pause.** Show minimized evidence through a Query-backed application view.
   Authenticate, authorize, validate, and submit the decision through an Update. Never edit
   history or persistence directly.
7. **Rotate credentials and encryption keys.** Keep decryption compatibility for retained history;
   test recovery before retiring an old key.
8. **Delete data deliberately.** Apply Namespace retention and application deletion rules to
   Temporal history, exported traces, logs, provider data, backups, and external object references.
9. **Stop local tools.** `temporal server start-dev` and the Flight Recorder are disposable local
   processes. Do not expose them as production services.

## OpenRouter setup

```bash
cp .env.example .env
chmod 600 .env
# Edit .env and set OPENROUTER_API_KEY to your own key.
```

`.env` is ignored by Git. The committed template contains no secret. The host application chooses
whether to load it. Ordinary tests, the deterministic BDD example, release measurement, and the
recorded demo make no paid model call.

## Release evidence

Run:

```bash
uv run poe gate
uv run poe release-candidate
uv run python scripts/measure_release.py
```

The published budgets are deliberately small and testable:

| Proof | Required result |
| --- | --- |
| Core branch coverage | at least 90% |
| Release-script branch coverage | at least 90% |
| Frontend branch coverage | at least 90% |
| Python complexity | Xenon grade A |
| Browser behavior | gate passes |
| Runtime dependency | Temporal present |
| Retired runtime | absent |
| Public API | typed imports pass |
| Temporal tests | exact quality gate passes |
| Package | wheel builds and installs |
| Flight Recorder | packaged CLI reaches ready state |

The candidate must come from a clean exact commit and match its hosted Dagger run. Release scripts
build one wheel and one source archive, verify SHA-256 manifests, install hash-pinned dependencies
offline, and do not publish. Live model evaluation is separate and never substitutes for these
deterministic checks.

## Known limits

- External effects are not atomic with Temporal history.
- Exactly-once business behavior depends on provider idempotency and reconciliation.
- A human pause has no automatic recovery-time objective.
- Agentic Saga does not meter provider money or enforce account spend.
- The Flight Recorder is loopback-only and not a multi-user operations console.
- The library does not operate Temporal or secure the host application's Activities.

See the [Temporal safety contract](temporal-safety-contract.md) for claim-to-test evidence.
