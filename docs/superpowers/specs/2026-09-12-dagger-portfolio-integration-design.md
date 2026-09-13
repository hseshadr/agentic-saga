# Agentic Saga Dagger Portfolio Integration Design

**Date:** 2026-09-12
**Status:** Implemented; corrected after final equivalence audit
**Repositories:** `hseshadr/ci`, `hseshadr/agentic-saga`, `project-ideas`

## TL;DR

Agentic Saga will reuse the portfolio's proven EdgeProc Dagger pattern: one 24-line, two-action
GitHub workflow and one small typed adapter pinned to the shared `portfolio-foundation` module.
The adapter does not rebuild Agentic Saga's CI logic. Under each pinned Python 3.12 and 3.13
runtime it calls `uv run poe gate`, `uv run poe release-candidate`, and
`uv run python scripts/measure_release.py`; it also calls the Flight Recorder `pnpm gate` once.
A similarly thin scheduled entry point runs the existing locked dependency audits. The central
fleet then verifies eligible repositories, while `portfolio.json` records both the Dagger control
plane and Agentic Saga as current facts.

This is portfolio integration, not a product-runtime change. The Saga API, deterministic kernel,
examples, and optional model adapters do not change.

## Why this change exists

Agentic Saga was released with pinned reusable GitHub workflows from `hseshadr/ci` at
`ci-v3.3.0`. The portfolio control plane subsequently moved its execution layer into Dagger.
Agentic Saga therefore has valid Northstar evidence but does not conform to the newer portfolio
execution standard, is absent from the central fleet, and is absent from the canonical portfolio
manifest.

Dagger is not itself a Northstar criterion. Northstar evaluates outcomes: exact-source identity,
correctness, security, recovery, performance, clarity, and production truth. Dagger is the
portfolio's selected mechanism for making those outcomes portable and centrally inspectable.

## Goals

1. Make every Agentic Saga repository-authored workflow job a thin, immutable Dagger ingress.
2. Preserve every existing release-critical check and threshold by delegating to the commands
   that already own them, rather than translating those checks into Dagger code.
3. Use the shared `portfolio-foundation` rather than duplicating identity, full-history secret
   scanning, action validation, or artifact-boundary logic.
4. Extend the shared foundation so private repositories receive authenticated Git history through
   a typed secret without exposing credentials to commands or logs.
5. Require one exact GitHub Dagger check on Agentic Saga `main` and verify it from the central
   fleet.
6. Register Agentic Saga and the current Dagger control-plane truth in `portfolio.json`, then
   regenerate all owned portfolio views.

## Non-goals

- No change to Saga runtime behavior, policy, storage, compensation, or escalation semantics.
- No package publication, registry credential, OIDC publisher, deployment, or public visibility
  change.
- No generic user-supplied image, command, argument vector, path, or package-install API.
- No migration of unrelated portfolio repositories.
- No use of Dagger Cloud or new hosted credentials.
- No replacement of GitHub's event, permission, environment, or branch-protection boundaries.

## Considered approaches

### A. Shared foundation plus a small local product graph — selected

Reuse EdgeProc's exact shape: add private-history authentication to the shared Foundation, compose
that module at an immutable commit, and keep the Agentic Saga adapter limited to runtime setup and
calls to existing repository-owned commands. GitHub workflows contain checkout plus the pinned
Dagger action.

This is the leanest approach that satisfies the fleet contract and preserves specialized proof.

### B. Standalone Agentic Saga Dagger wrapper — rejected

A local wrapper could invoke the current commands quickly, but it would duplicate full-history
secret scanning, source binding, and workflow validation. It would not satisfy the shared-module
fleet contract and would create another security implementation to maintain.

### C. Keep current workflows and document an exception — rejected

The current workflows are correct and pinned, but a permanent exception would leave Agentic Saga
outside the portfolio control plane. It would also make the fleet claim incomplete.

## Architecture

```text
GitHub event and permissions
        |
        v
.github/workflows/dagger.yml          two pinned actions only
        |
        v
Agentic Saga local Dagger module      thin command adapter
        |
        +--> shared Foundation        source identity + history guard
        +--> Python 3.12 + 3.13       one immutable container per runtime
        |    +--> uv run poe gate     quality + tests + coverage
        |    +--> release-candidate   exact wheel + offline install
        |    +--> measure_release.py  packaged browser + budgets
        +--> pnpm gate                frontend quality + browser proof
        +--> existing audits          locked Python + pnpm graphs
        |
        v
single exact `Dagger` check
        |
        v
central fleet + branch protection
        |
        v
canonical portfolio manifest and generated views
```

The local module is an adapter, not a second task runner. It contains no copy of a coverage floor,
performance budget, package recipe, test selection, or browser scenario. Product behavior and
thresholds remain in their current authoritative files.

## Repository boundaries

### `hseshadr/ci`

The shared Foundation gains an optional typed Git HTTP authorization header for operations that
need exact private history. Existing public callers remain source-compatible. The secret is
forwarded only to Dagger's Git client; it is never revealed, mounted as a file, or passed to a
container command.

After Agentic Saga is green and protected, the fleet allowlist adds `agentic-saga` with the normal
contract and no grandfathering. Documentation reports nine total repositories: eight consumers
plus central CI.

### `hseshadr/agentic-saga`

The repository adds a Python Dagger module with a narrow public API:

```python
@object_type
class AgenticSaga:
    @function
    @check
    async def ci(
        self,
        source: dagger.Directory,
        commit_sha: str,
        git_auth_header: dagger.Secret,
    ) -> str: ...

    @function
    async def security(
        self,
        source: dagger.Directory,
        commit_sha: str,
        git_auth_header: dagger.Secret,
    ) -> str: ...
```

The public API accepts the exact source snapshot, exact commit identity, and one typed secret. It
does not accept arbitrary execution configuration. Repository identity, toolchain versions,
commands, cache namespaces, paths, and performance thresholds are fixed internal constants.

`ci` first completes the shared source/history guard. Under each pinned Python 3.12 and 3.13
runtime it then runs three fixed repository-owned commands: the Python Poe gate, the Poe
release-candidate gate, and `scripts/measure_release.py`. The release container imports the pinned
Node toolchain and Playwright browser dependencies, so the measurement command exercises the
packaged Flight Recorder against the installed CLI and enforces the existing performance budgets.
The Flight Recorder `pnpm gate` runs once in its pinned Node container. Dagger supplies reproducible
containers and readable step boundaries; it does not duplicate repository control flow.

`security` is scheduled/manual and repeats the shared guard plus the repository's existing locked
dependency-audit commands. It is not a second required branch-protection check.

The implementation follows the EdgeProc precedent but omits its release-envelope and publishing
APIs. The target is no more than two public functions and roughly 100-150 hand-written adapter
lines, excluding generated SDK code, the lock file, and tests. If the adapter grows beyond that,
the default response is to move behavior back behind an existing repository command, not to add
another Dagger abstraction.

### `project-ideas`

`portfolio.json` remains the only owner of current status. The existing `ci` object is updated
from the pre-Dagger reusable-workflow story to the current Dagger control plane. A new private,
unranked `agentic-saga` object links to an owned `oss/agentic-saga.md` design/status document.
The renderer regenerates `PORTFOLIO-STATUS.md`, `oss/README.md`, and `ALL-PROJECT-IDEAS.md`.

Dagger is represented by the existing `ci` project, not duplicated as a fictional standalone
repository.

## Private-repository authentication

GitHub checks out Agentic Saga at `${{ github.sha }}` with `persist-credentials: false` and full
history. The workflow reads the repository Actions secret `DAGGER_GIT_HTTP_AUTH_HEADER`, whose
masked value is the value-only `Basic <base64(x-access-token:TOKEN)>` Git authorization header.
It passes that value to Dagger as a typed secret; the local module forwards it to the shared
Foundation without exposing it in logs or ordinary command arguments.

Foundation binds the supplied source directory to `hseshadr/agentic-saga@<full-sha>` and fetches
the canonical Git tree/history at the same commit using Dagger's authenticated Git API. The guard
compares the supplied source to that canonical identity and scans complete history.

Failure to authenticate, resolve the commit, compare the source, or scan history fails closed.
There is no anonymous fallback for the private consumer path.

Tests must prove that:

- public callers still use no auth header;
- private callers forward a `dagger.Secret` to the Git API;
- the secret value is absent from errors and container arguments;
- authentication or history failure prevents every product gate;
- malformed repository or commit identities fail before network execution.

## Preserved Agentic Saga evidence

The migration is incomplete if any of these disappear:

| Evidence | Preserved contract |
|---|---|
| Python quality | `uv run poe gate` remains authoritative and runs with the frozen lock |
| Kernel coverage | At least 90% total coverage and the existing kernel branch floor |
| Complexity | Xenon Grade A and the existing function-size repository contract |
| Frontend | The existing `pnpm gate` retains Node/pnpm pins, coverage, build, assets, and Playwright |
| Packaged demo | `uv run poe release-candidate` runs in each pinned runtime and retains the exact wheel |
| Installation integrity | Hash-required dependencies, no-index install, `uv pip check` |
| User journey | Per-runtime `scripts/measure_release.py` exercises the packaged recorder against the installed CLI |
| Performance | Per-runtime measurement preserves the existing cold-start and 20-fresh-store p95 budgets |
| Supply chain | Foundation guard plus the repository's existing locked Python and pnpm audits |
| Mutation | Existing mutation command remains available; its current release status is documented |

The Dagger result must retain readable sub-gate names and surface command output on failure.

## Workflow contract

The required workflow contains one job named `Dagger`. Its only steps are:

1. `actions/checkout` pinned to a full commit, with full history, exact `${{ github.sha }}`, and
   persisted credentials disabled.
2. `dagger/dagger-for-github` pinned to a full commit and Dagger `0.21.8`, calling the closed `ci`
   function with exact source, commit identity, and the masked Git authorization secret.

The scheduled security workflow has the same two-step shape and calls `security`.

The migration deletes the retired `hseshadr/ci` reusable-workflow references only when the Dagger
PR check proves equivalent behavior. No shell execution remains in workflow YAML.

## Branch protection and fleet sequencing

1. Merge the shared private-history Foundation change after its exact CI and security gates pass.
2. Pin Agentic Saga to that immutable central commit.
3. Run local Foundation tests, the complete Agentic Saga gate, and a real local Dagger call.
4. Open the Agentic Saga PR and require all existing checks plus the new Dagger check for the
   transition.
5. Merge only after the exact PR head is green.
6. Protect Agentic Saga `main` with strict status checks requiring only `Dagger` from GitHub
   Actions, conversation resolution, admin enforcement, no force pushes, and no deletions. Zero
   approvals is appropriate for the private solo-maintainer repository.
7. Verify the exact current `main` Dagger check.
8. Add `expectation("agentic-saga")` to the central fleet with no grandfathering, update central
   counts/docs, and prove the authoritative fleet scan on the exact central `main` commit.
9. Update and verify the portfolio manifest from the final immutable identities.

Unreadable private CodeQL metadata is resolved before fleet onboarding. The chosen state must be
explicitly readable by the fleet token; a 403 cannot be treated as “disabled.” Dependabot checks
are not required and Dependabot pull requests are never merged automatically.

## TDD and verification

Every behavioral change starts with a failing test and a witnessed expected failure.

### Shared Foundation

- Public schema test for the optional typed secret.
- Git-adapter test proving authenticated private history and anonymous public history.
- Failure/redaction test proving auth errors do not reveal the secret.
- Complete central Python quality, module-fixture, actionlint, Zizmor, dependency, and real Dagger
  gates.

### Agentic Saga adapter

- Contract tests for the immutable Foundation pin and the three fixed repository command calls.
- Orchestration tests proving guard-before-commands and no duplicated thresholds or release logic.
- Failure tests proving one failed sub-gate fails the Dagger result.
- Workflow tests executing controlled fixtures rather than merely grepping source text.
- Existing full Python, frontend, packaged E2E, measurement, mutation, and security contracts.
- Real local `dagger call ci` against the exact candidate commit.

### Fleet and portfolio

- Fleet name/expectation tests fail before allowlisting Agentic Saga.
- Real authoritative private-repository fleet scan after main protection is configured.
- Portfolio manifest validation fails before the new project/corpus entry.
- Renderer check, link validation, stale-language probes, and the complete portfolio Poe gate.

## Rollback

Before branch protection switches, rollback is closing the Agentic Saga PR. After merge, rollback
is a reviewed revert that restores the exact prior workflows and required checks together; branch
protection is never left requiring a check the reverted workflow cannot produce.

The Foundation change is backward compatible. Reverting Agentic Saga does not require reverting
the central optional-auth API. If fleet onboarding fails, Agentic Saga is not added to the fleet
until the consumer is conforming; the scanner is never weakened to force a green result.

## Acceptance criteria

- Shared Foundation proves fail-closed authenticated private history without secret disclosure.
- Agentic Saga has no live legacy reusable-workflow reference.
- Every repository-authored workflow job is two-step pinned Dagger ingress.
- The exact Agentic Saga PR and merged `main` commits pass the complete preserved evidence set.
- Branch protection requires the exact GitHub Actions `Dagger` check and blocks direct bypass.
- The central fleet reads private Agentic Saga metadata and reports zero findings on exact `main`.
- `portfolio.json` contains current `ci`/Dagger truth and a complete private Agentic Saga entry.
- All generated portfolio views match the canonical manifest.
- Both repositories remain at their original visibility; no package or release is published.
- All temporary branches and worktrees are removed after verified merges.
