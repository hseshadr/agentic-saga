# Changelog

All notable changes to Agentic Saga will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Generic durable Saga kernel with typed actions, intent-before-effect dispatch, reconciliation,
  compensation, human escalation, crash recovery, and redacted evidence export.
- SQLite reference storage with lease fencing, replay verification, backup, and restore contracts.
- Offline unit, integration, crash-process, concurrency, contract, and Hypothesis safety suites.
- Domain-neutral Saga Context Manifest loading with bounded safe YAML, deterministic agent context,
  authoritative tool-catalog resolution, named check references, and ecommerce/ticket-booking
  examples.
- Optional planning-only Deep Agents/OpenRouter adapter with strict proposals, ordered model
  fallback, deterministic offline injection, and no business-tool execution authority.
- Executable offline ecommerce reference with a separate durable provider, proposal-only agent,
  exact payment policy, four pytest-bdd Saga paths, restart recovery, and redacted evidence.
- Typed `BeginCompensation` proposals that let an agent request the kernel-owned compensation phase
  without choosing arbitrary rollback execution or bypassing the deterministic frontier.
- Versioned 24-case evaluation corpus, executable ecommerce fixtures, deterministic scoring, and
  an opt-in resumable OpenRouter runner with atomic redacted evidence and provider separation.
- Secret-free typed agent planning failures that distinguish trusted provider exhaustion from
  invalid model responses and internal adapter failures without exposing raw exception material.
- Saga Flight Recorder package resources with bounded strict `RunTrace` ingestion, digest-bound
  scenario loading, pure causal replay, responsive Story/Ledger/Proof inspection, and four
  reproducible real ecommerce evidence fixtures.
- Loopback-only, read-only recorder materialization and serving with anchored POSIX paths, exclusive
  destinations, bounded files and requests, explicit security headers, and fail-closed cleanup.
- Packaged `agentic-saga demo` command for one selected captured trace, with deterministic
  offline defaults, optional browser launch, bounded safe failures, and clean Ctrl-C shutdown.
- Source-grounded architecture signal board and one operational contract for threat, privacy,
  recovery, operator, resource, performance, and release boundaries.
- Frozen browser quality gate covering all four traces, keyboard and reduced-motion behavior,
  required responsive widths, 200% zoom, accessibility, console errors, and external requests.
- Fail-closed offline release measurement, exact wheel/source-archive contracts, and a hosted
  Python 3.12/3.13 packaged-browser matrix. Final clean-current-commit and hosted evidence remain
  required before release.

### Changed

- Removed pre-release domain-specific policy slots. Application policies, strict schemas, and
  adapters enforce domain constraints; manifests reference application-registered checks by name.
- Reduced package facades to current consumers; advanced types are imported from defining modules.
- Cut over the unreleased v0.1 store format after removing adapter-factory capability fields and
  `AgentTurnReserved.agent_factory_digest`; stores created before `4e947cb` must be recreated.
- Made exhausted agent compensation fail closed to durable human escalation instead of attempting
  to restart an already-active compensation phase.
- Consolidated byte-framed stable identity hashing behind one kernel helper while preserving all
  previously generated identifiers and deliberately distinct identity domains.
- Added one supported `compose_runtime` assembly path with eight explicit inputs, while keeping each
  `SagaGoal` as separate transaction input to `runtime.start(...)`.
