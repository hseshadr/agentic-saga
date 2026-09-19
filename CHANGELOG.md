# Changelog

All notable changes to Agentic Saga are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project intends to use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) after its first public release.

## [Unreleased]

### Added

- Temporal Python SDK integration as the sole durable Saga engine.
- One deterministic Workflow for tool eligibility, prerequisites, budgets, success proof,
  reverse compensation, and verified human recovery.
- Typed Activities for bounded agent decisions, business tools, reconciliation, and application-
  authenticated human authorization.
- Stable operation identities for provider idempotency and lost-response reconciliation.
- Four Temporal `pytest-bdd` scenarios covering verified success, automatic compensation,
  one-effect lost-response recovery, and a human-required compensation outcome.
- Pydantic Deep with a pinned OpenRouter tool-calling route for optional agentic decisions.
- Optional TypeSafe AI Jev and OpenRouter Decisions adapters for bounded selection among
  application-built candidates.
- Public `saga.yaml` context manifests with bounded parsing, registered-name validation, catalog
  digests, budgets, safety guidance, and example paths.
- An accessible Flight Recorder that replays four redacted ecommerce traces with a perceptible
  forward and rollback animation.
- Interactive Archify architecture documentation, exact-package release checks, and Dagger CI.

### Changed

- Limited the model surface to currently eligible forward business tools plus
  `finish_saga(succeeded_verified)` when deterministic proof permits it.
- Assigned reconciliation, compensation order, escalation, and final state exclusively to the
  Temporal Workflow.
- Made ecommerce a standalone example of the generic library rather than domain logic in core.
- Documented distinct test, local-development, and production Temporal modes.
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
