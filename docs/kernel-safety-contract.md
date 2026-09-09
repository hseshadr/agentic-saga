# Kernel safety contract

TL;DR: Agentic Saga makes side-effecting agent work recoverable and auditable on one host. It
cannot make a remote provider transactional, and it does not sandbox installed application code.

## Exact v0.1 claims

Provides: deterministic authorization; intent-before-effect local durability; append-only ordered
evidence; at-least-once dispatch; provider-assisted deduplication; deterministic reconciliation;
verified business compensation; single-host crash recovery under documented SQLite/filesystem
assumptions.

Does not provide: universal exactly-once effects; atomic cross-service commit; serializable
cross-Saga isolation; automatic resolution when safe reconciliation cannot establish an opaque
external outcome; literal rollback of history; high availability with SQLite; safety for arbitrary
tools that fail the adapter contract.

The kernel additionally enforces durable turn, tool-call, output-token, and agent-call-time
reservations, lease/fence authority, authenticated single-use human resolution, replay-checked
projections, and deterministic redacted trace export. Those token/time values are fixed planning
allocations, not measurements of input tokens, actual provider usage, end-to-end elapsed time, or
money; the library provides no monetary meter or provider spend cap.

Every created Saga persists a definition fingerprint covering its tools, typed models, adapters,
policy, terminal invariants, budget, and redaction policy. A restart must supply that exact
definition through the same tool registry; same-version behavior drift fails closed. Stateful
adapters and transitive policy helpers therefore expose explicit public behavior versions.

Runtime reads are bounded by the turn budget. An unavailable durable read records a typed,
detail-free `ReadUnavailable` outcome instead of inventing state. Lease authority is renewed and
checked around awaited work; a stale owner cannot commit or release a successor's lease, and the
active owner releases its lease before returning or handing control to a human.

## Trust boundary

Model output, provider outcomes, and persisted input are untrusted data. The kernel validates these
at typed boundaries and fails closed when an outcome cannot be proven. Installed `AgentDriver` and
`EffectAdapter` implementations are trusted application code and must cooperate with asynchronous
cancellation. Hostile or native plugins require an external process, container, or service; this
library is not a Python sandbox.

Application-supplied policy-context and invariant-evidence providers are also trusted code. Their
transitive behavior is not introspected; changing it requires a definition or public behavior
version bump.

Every generic JSON value is capped at depth 16, 4,096 nodes, 256 items per container, 16 KiB per
UTF-8 string, and 64 KiB encoded. Each exact `SagaDefinition` owns the immutable redaction policy
used by the kernel, dispatcher, reconciler, observations, and trace exporter. Built-in credential
and payment-secret detection is only a floor: applications must explicitly classify every ordinary
PII key, and unlisted fields are treated as public. The optional `saga.yaml` is public authoring
context and must contain no PII, credentials, receipts, or private provider data.

## SQLite and filesystem assumptions

- One application host owns a database on a local filesystem.
- The filesystem truthfully implements SQLite locking, atomic rename, flush, and sync semantics.
- Network filesystems, shared-volume multi-host writers, and storage that acknowledges lost writes
  are outside this contract.
- The backend is POSIX-only. The database's immediate parent must be current-user-owned and not
  group/world writable. Database, `-wal`/`-shm`, temporary, backup, and restored files are regular
  current-user leaves hardened to mode `0600`.
- Backup and restore publish only to fresh destinations; existing destinations and sidecars are
  rejected rather than overwritten. Operators verify a restored store before serving work.
- SQLite provides local transaction durability, not atomic commit with an external provider.
  Adapters must honor stable idempotency keys and their declared resource-fence contract.
- Schema v1 is deliberately rejected in this unpublished v0.1 release. No migration promise exists
  yet; a future published schema change must ship an explicit migration or export path.
- Mode bits do not contain root, same-UID processes, or ACL grants. Operators own directory ACLs,
  encryption, quotas, retention, and deletion of the database, sidecars, and every backup copy.

## Executable evidence

| Claim | Named executable evidence |
| --- | --- |
| Deterministic authorization | `tests/unit/kernel/test_policy.py::test_should_authorize_typed_command_with_kernel_identity_and_fence` |
| Intent before effect | `tests/integration/execution/test_intent_before_effect.py::test_adapter_observes_durable_intent_before_effect` |
| Ordered evidence and replay | `tests/integration/storage/test_append_only.py::test_should_forbid_ledger_update_and_delete`; `tests/integration/storage/test_projection_rebuild.py::test_should_detect_projection_that_does_not_match_replay` |
| Stable identity and deduplication | `tests/property/test_idempotency_properties.py::test_same_operation_and_command_has_one_business_effect` |
| Lease/fence authority | `tests/concurrency/test_split_brain.py::test_stale_worker_cannot_append_after_takeover_or_duplicate_provider_effect` |
| Ambiguous outcome handling | `tests/integration/execution/test_lost_response.py::test_should_escalate_opaque_provider_without_blind_retry` |
| Reconciliation | `tests/unit/execution/test_reconciliation.py::test_should_follow_fail_closed_reconciliation_table` |
| Verified compensation | `tests/integration/execution/test_compensation_flow.py::test_compensation_uses_normal_dispatch_exact_receipts_and_fresh_terminal` |
| Durable budgets | `tests/property/test_ledger_replay.py::test_budget_reservations_are_exact_across_reopen` |
| Definition fingerprint and restart binding | `tests/integration/execution/test_runtime_resume.py::test_fresh_process_resume_rejects_same_version_with_changed_definition` |
| Bounded read failure evidence | `tests/unit/execution/test_runtime_loop.py::test_stalled_read_records_safe_unavailable_outcome_within_turn_budget` |
| Awaited-work lease lifecycle | `tests/unit/execution/test_runtime_loop.py::test_runtime_cannot_write_or_release_after_awaited_work_loses_authority` |
| Human resolution | `tests/integration/execution/test_human_quiescence.py::test_human_decision_is_authenticated_sequence_bound_single_use_and_private` |
| Redacted trace | `tests/integration/evidence/test_run_trace.py::test_export_redacts_nested_values_without_exporting_raw_digest` |
| Crash recovery | `tests/crash/test_crash_matrix.py::test_restart_converges_without_duplicate_business_effects` |

Run the complete evidence gate with `uv run poe gate`. Run the separately bounded mutation gate
with `uv run poe mutation`. A release claim requires a fresh clean-current-commit report with no
surviving, untested, suspicious, timed-out, interrupted, or crashing selected mutant. This is not a whole-repository mutation score:
it covers the named identity, authorization, terminal-proof, reconciliation, compensation,
redaction, and SQLite-authority functions frozen in the runner.
Mutation testing complements rather than replaces deterministic, crash, concurrency, contract,
and property suites.

## Supported imports

The root package exports eight named symbols: `SagaContext`, `SagaDefinition`, `SagaGoal`,
`SagaManifest`, `SagaRuntime`, `__version__`, `compose_runtime`, and `load_saga_context`.

| Concern | Canonical home |
| --- | --- |
| Definitions and historical catalog | `agentic_saga.kernel.definitions` |
| Strict JSON, canonical encoding, and hashes | `agentic_saga.contracts.common` |
| Clock protocol and system clock | `agentic_saga.contracts.clock` |
| Redaction policy and functions | `agentic_saga.contracts.redaction` |
| Kernel storage protocol | `agentic_saga.kernel.ports` |
| POSIX SQLite reference store | `agentic_saga.storage.sqlite` |
| Maintained planning adapters | `agentic_saga.agents` |
| Flight Recorder materialization/serving | `agentic_saga.demo` |

The three `agentic_saga.demo` exports are supported embedding APIs for materializing and serving
caller-supplied redacted traces; distribution reference-trace loading remains CLI-internal. Use the
canonical defining modules for advanced contracts. Advanced pre-1.0 module paths and symbols are
not compatibility promises merely because repository code can import them.

## Deliberate limitations

An at-least-once dispatcher can call a provider more than once after an ambiguous transport
failure. Business-effect deduplication therefore depends on the provider contract. An opaque
provider outcome remains unresolved and is reconciled or escalated; the kernel never guesses or
blindly repeats it. Compensation is a new verified business effect, not erased history. Cross-Saga
provider-resource serialization exists only when the adapter implements and truthfully declares
that fence.

The [operations and release contract](operations.md) adds the threat/privacy boundary, shared-state
inventory, recovery runbook, resource budgets, and release evidence required around these kernel
guarantees.
