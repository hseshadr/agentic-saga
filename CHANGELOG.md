# Changelog

All notable changes to Agentic Saga are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project intends to use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) after its first public release.

## [Unreleased]

### Added

- A four-scenario recorder catalog with stable outcome summaries, explicit agent provenance,
  replay status, and `python -m examples.ecommerce.run all --open` for fresh local runs.
- Automatic recording refresh, reconnect status, and page updates when a rebuilt UI is served.
- Evidence-checked use-case pass indicators and a labeled story table with per-event results.
- Temporal Python SDK integration as the sole durable Saga engine.
- One deterministic Workflow for tool eligibility, prerequisites, budgets, success proof,
  reverse compensation, and verified human recovery.
- Typed Activities for bounded agent decisions, business tools, reconciliation, and application-
  authenticated human authorization.
- Stable operation identities for provider idempotency and lost-response reconciliation.
- Four Temporal `pytest-bdd` scenarios covering verified success, automatic compensation,
  one-effect lost-response recovery, and a human-required compensation outcome.
- Pydantic AI with a pinned OpenRouter tool-calling route for optional agentic decisions.
- Optional TypeSafe AI Jev and OpenRouter Decisions adapters for bounded selection among
  application-built candidates.
- Public `saga.yaml` context manifests with bounded parsing, registered-name validation, catalog
  digests, budgets, safety guidance, and example paths.
- An accessible Flight Recorder that replays four redacted ecommerce traces with a perceptible
  forward and rollback animation.
- Interactive Archify architecture documentation, exact-package release checks, and Dagger CI.

### Changed

- Compensation no longer records a hardcoded invariant result. The Workflow previously appended
  a `compensation_verified` event claiming `obligations_reversed` passed without evaluating
  anything, and the Flight Recorder showed it as a valid proof. It now records
  `compensation_completed` (the compensated operation IDs, newest first, and the target status)
  with no `all_passed`/`verified` claim, and traces no longer carry a compensation `TraceProof`.
  Legacy `compensation_verified` events project as `compensation_completed` with the claims
  stripped. The Flight Recorder verifies a compensated run from each undo step's own confirmed
  outcome instead. The event is workflow-local state (no Temporal command), so existing
  histories replay without a patch; the regenerated `business-failure` trace changed accordingly.
- Documented manifest `checks` as declared agent-context labels that the runtime does not
  evaluate, alongside the gates it does enforce; marked `TerminalRequirement`, `HumanDecision`,
  `AuthorizedToolCall`, and `ToolDescriptor.policy_constraints` as reserved and not enforced; and
  stated that traces and receipts are hash-checked but unsigned.
- Replaced the Pydantic Deep wrapper with a bare Pydantic AI `Agent`: the adapter had disabled
  every deep-agent capability, so the dependency is dropped and `DeepAgentsDriver` is now
  `PydanticAIDriver` in `agentic_saga.agents.pydanticai`.
- Replaced custom prerequisite traversal with Python's `graphlib` cycle validation and removed
  unused agent helpers and obsolete ecommerce test models.
- Limited the model surface to currently eligible forward business tools plus
  `finish_saga(succeeded_verified)` when deterministic proof permits it.
- Assigned reconciliation, compensation order, escalation, and final state exclusively to the
  Temporal Workflow.
- Made ecommerce a standalone example of the generic library rather than domain logic in core.
- Documented distinct test, local-development, and production Temporal modes.

### Fixed

- Failed compensation now waits for verified human resolution instead of claiming successful
  recovery with unresolved obligations.
- Trace projection distinguishes confirmed no-effect failures, uncertain outcomes, and reads.
- The ecommerce human-review wait accommodates real Temporal retry timing.
- Declared Workflow history public/redacted by default; private production payloads require an
  application-configured encryption codec, external KMS, and least-privilege Namespace access.

### Removed

- The unpublished custom SQLite store, append-only ledger, dispatcher, lease/fence coordinator,
  crash-recovery runtime, backup/restore layer, and compatibility surface.
- Agent-controlled `begin_compensation`, rollback tools, non-success finish targets, and
  `escalate_to_human` proposals.
- Stale design plans and tests for the retired runtime. There is no dual-write path or migration
  promise for prototype local databases.

No package has been published from this cutover. A release requires a clean exact-commit gate,
matching hosted Dagger evidence, and a separate explicit publication decision.
