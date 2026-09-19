# Provenance and release status

TL;DR: source commits are authoritative. A release candidate is one wheel and one source archive
built from a clean exact commit, installed with hash-pinned dependencies, tested through the
Temporal and browser gates, and matched to the hosted Dagger run for that commit. This project does
not publish a package automatically.

## What is frozen

- `uv.lock` freezes Python inputs, including the Temporal Python SDK.
- `web/flight-recorder/pnpm-lock.yaml` freezes frontend inputs.
- `package.json` pins pnpm 11.5.0.
- Dagger base images and shared CI modules use immutable digests or exact commits.
- `SHA256SUMS` binds the wheel, source archive, and runtime requirements to one build.
- `SOURCE_COMMIT` binds the artifact set to committed `HEAD`.

## Reproduce the evidence

```bash
uv run poe gate
uv run poe release-candidate
uv run python scripts/measure_release.py
```

The gate runs lint, formatting, strict types, Xenon grade A, ordinary tests, Temporal integration
tests, true branch-coverage floors, package checks, and release-measurement tests. The Temporal BDD
suite uses the isolated time-skipping test server; it does not pretend that test infrastructure is
a production Temporal Service.

The release-candidate command builds from `git archive HEAD`, creates exactly one wheel and one
source archive, verifies SHA-256 digests, installs hash-pinned runtime dependencies without an
index, installs the wheel without dependency resolution, checks the environment, and verifies the
CLI and Temporal public surface. It does not publish.

The measurement report proves these shipped surfaces:

- core, frontend, and release-script branch coverage;
- Python complexity and browser behavior gates;
- Temporal as the runtime dependency and absence of the retired runtime packages;
- typed public API imports and Temporal integration Legos;
- Temporal tests as part of the exact quality gate;
- wheel build/install; and
- packaged Flight Recorder startup.

A dirty-tree result is diagnostic only. Final evidence must come from the same clean commit as the
reviewed source and hosted Dagger run.

## Current cutover status

The pre-1.0 branch has cut over to Temporal as its sole durability engine. A previous unpublished
implementation contained a custom SQLite store, leases, dispatch recovery, and local replay. That
code and its compatibility surface were deliberately removed; there is no dual runtime and no
migration promise for those pre-release local files.

Historical CI links for the retired implementation are historical provenance only. They do not
prove the Temporal release candidate. The Temporal cutover becomes releasable only after its own
clean exact commit passes every local and hosted gate.

## Repository controls

`.github/workflows/dagger.yml` and `.github/workflows/dagger-security.yml` are narrow entry points
to the pinned Dagger graph. Dagger is the canonical CI Lego: it composes shared supply-chain and
Python-package controls with repository-specific Temporal, frontend, recorder, and release checks.
Checkout credentials remain disabled for untrusted build work.

The project is a library plus a local loopback viewer. It has no hosted application deployment.
Production users deploy their Workers and connect them to Temporal Cloud or an operated,
production-ready self-hosted Temporal Service.

## Publication boundary

Publishing to PyPI or another registry requires separate, fresh authorization and trusted
publishing from the reviewed immutable artifact. Live OpenRouter evaluation also requires separate
consent and provider-side spend controls. Model-quality evidence is not release-correctness
evidence.

See the [operations contract](docs/operations.md) and
[Temporal safety contract](docs/temporal-safety-contract.md).
