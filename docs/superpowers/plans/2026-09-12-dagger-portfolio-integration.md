# Agentic Saga Dagger Portfolio Integration Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task by task. Apply `superpowers:test-driven-development` to every behavior change and `python-quality` after every Python edit.

**Goal:** Move private `hseshadr/agentic-saga` onto the portfolio's established Dagger CI pattern, add it to the central fleet, and register Agentic Saga plus the current Dagger control-plane truth in the canonical portfolio.

**Architecture:** GitHub Actions remains a two-action event and permission boundary. A small Agentic Saga Python Dagger adapter composes immutable shared `portfolio-foundation` and `python-package` modules, verifies exact private history using a typed secret, and delegates product proof to existing repository commands. The central fleet validates the protected merged commit. `project-ideas/portfolio.json` remains the source of portfolio truth.

**Tech Stack:** Dagger 0.21.8, Python 3.12/3.13, uv, Poe, pytest, Ruff, mypy, Xenon, Node 24, pnpm 11.5.0, Playwright, GitHub Actions, GitHub CLI.

---

## Task 1: Establish isolated worktrees and clean baselines

**Files:**
- Verify only; no product files change.

**Step 1: Verify repository state and existing worktrees**

Run:

```bash
git -C /Users/harish/dev/oss/ci status --short --branch
git -C /Users/harish/dev/oss/ci worktree list
git -C /Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration status --short --branch
git -C /Users/harish/dev/project-ideas status --short --branch
git -C /Users/harish/dev/project-ideas log -1 --oneline
```

Expected: Agentic Saga design worktree is clean; unrelated changes are identified and preserved.

**Step 2: Create the central Foundation worktree**

Run:

```bash
git -C /Users/harish/dev/oss/ci fetch origin main
git -C /Users/harish/dev/oss/ci worktree add \
  /Users/harish/dev/oss/ci/.worktrees/agentic-saga-private-history \
  -b feat/agentic-saga-private-history origin/main
```

**Step 3: Prove baselines before editing**

Run the focused Foundation suite in the central worktree and the already-established Agentic Saga gate. Record exact pass counts and any environmental preconditions; do not weaken tests to obtain green.

## Task 2: Add typed private-history authentication to Foundation

**Files:**
- Modify: `/Users/harish/dev/oss/ci/.worktrees/agentic-saga-private-history/modules/portfolio-foundation/.dagger/src/portfolio_foundation/main.py`
- Modify: `/Users/harish/dev/oss/ci/.worktrees/agentic-saga-private-history/modules/portfolio-foundation/.dagger/src/portfolio_foundation/source.py`
- Modify if required by the existing call boundary: `/Users/harish/dev/oss/ci/.worktrees/agentic-saga-private-history/modules/portfolio-foundation/.dagger/src/portfolio_foundation/guard.py`
- Modify: `/Users/harish/dev/oss/ci/.worktrees/agentic-saga-private-history/modules/portfolio-foundation/.dagger/tests/test_public_schema.py`
- Modify: `/Users/harish/dev/oss/ci/.worktrees/agentic-saga-private-history/modules/portfolio-foundation/.dagger/tests/test_source.py`
- Modify: `/Users/harish/dev/oss/ci/.worktrees/agentic-saga-private-history/modules/portfolio-foundation/.dagger/tests/test_guard.py`

**Step 1: Write failing public-contract tests**

Add tests proving:

- `source` and `guard` accept an optional typed `dagger.Secret` HTTP authorization header.
- Existing public callers can omit the secret unchanged.
- No plaintext string credential is accepted by the public schema.

Run the focused tests and witness failure because the secret parameter does not exist.

**Step 2: Write failing Git-adapter tests**

Add tests proving:

- The private path passes `http_auth_header` to Dagger's Git client.
- The public path invokes the Git client without an auth header.
- Authentication/history failure aborts before product execution.
- Secret material never appears in arguments, results, or exceptions.

Run the focused tests and witness the expected forwarding/redaction failures.

**Step 3: Implement the smallest typed forwarding change**

Thread `dagger.Secret | None` through the existing source/history boundary. Pass it only to Dagger's Git API. Do not mount it into a container, reveal it, or add a fallback from authenticated to anonymous access.

**Step 4: Refactor under green**

Keep functions at 15 lines or fewer, preserve existing public behavior, and avoid adding a generic credential abstraction.

**Step 5: Run Foundation and central quality gates**

Run:

```bash
DAGGER_NO_NAG=1 dagger develop
uv run --directory .dagger pytest \
  modules/portfolio-foundation/.dagger/tests/test_public_schema.py \
  modules/portfolio-foundation/.dagger/tests/test_source.py \
  modules/portfolio-foundation/.dagger/tests/test_guard.py -q
uv run --directory modules/portfolio-foundation/.dagger poe gate
uv run --directory modules/portfolio-foundation/.dagger poe audit
uv run --directory .dagger poe gate
uv run --directory .dagger poe audit
DAGGER_NO_NAG=1 dagger call module-fixtures
actionlint .github/workflows/*.yml
```

Expected: all tests and quality gates pass with no lowered threshold.

**Step 6: Commit, push, open PR, and merge only when green**

Commit message:

```text
feat: support private Foundation history
```

Verify the exact PR head checks, merge normally, verify central `main`, delete the remote branch, and remove the Foundation worktree and local branch.

## Task 3: Write Agentic Saga's failing Dagger contracts

**Files:**
- Create: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/.dagger/tests/test_public_contracts.py`
- Modify: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/tests/test_repository_contract.py`

**Step 1: Add the public Dagger contract tests**

Test only behavior and stable boundaries:

- Exactly two public functions: `ci` and `security`.
- Typed source, commit SHA, and secret boundaries.
- Exact immutable shared-module pins.
- The guard completes before any product command.
- `ci` runs `uv run poe gate`, `uv run poe release-candidate`, and
  `uv run python scripts/measure_release.py` under both pinned Python 3.12 and 3.13 runtimes, then
  runs the Flight Recorder `pnpm gate` once.
- `security` delegates to shared locked Python audit and pnpm audit.
- No public arbitrary image, command, argv, path, or package-install parameter.
- No copied coverage floor, performance budget, or release algorithm in the adapter.

Run these tests and witness failure because the module does not exist.

**Step 2: Replace workflow expectations with failing Dagger ingress tests**

Update repository contract tests to require:

- A 24-line-class, two-action pinned workflow.
- Full-history checkout of `${{ github.sha }}`.
- `persist-credentials: false`.
- Pinned `dagger/dagger-for-github` and Dagger 0.21.8.
- Typed masked secret forwarding.
- No shell steps, setup actions, mutable refs, or retired `hseshadr/ci` reusable workflows.

Run the focused tests and witness failure against the old workflows.

## Task 4: Implement the lean Agentic Saga Dagger adapter

**Files:**
- Create: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/dagger.json`
- Create: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/.dagger/pyproject.toml`
- Create: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/.dagger/uv.lock`
- Create: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/.dagger/src/agentic_saga_ci/__init__.py`
- Create: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/.dagger/src/agentic_saga_ci/main.py`
- Modify: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/.gitignore`

**Step 1: Scaffold from the EdgeProc precedent**

Use the same Dagger 0.21.8 Python module layout and generated-SDK ignore policy as EdgeProc. Pin `portfolio-foundation` and `python-package` to the exact merged central commit from Task 2. Never copy an old central SHA.

**Step 2: Implement reproducible fixed containers**

Create small private helpers for:

- Verified source/history through Foundation.
- Python/uv execution from the frozen lock.
- Node 24, pnpm 11.5.0, and Playwright execution from the frozen lock.
- Shared Python dependency audit and local pnpm audit.

No helper accepts user-controlled commands or image names.

**Step 3: Implement only `ci` and `security`**

`ci` performs the Foundation guard and then runs the three existing product-owned commands. `security` performs the guard and both locked dependency audits. Preserve command output on failure.

Target 100-150 hand-written implementation lines. If code exceeds that range, move orchestration back behind an existing repository command rather than adding a Dagger framework.

**Step 4: Generate SDK and lock deterministically**

Run:

```bash
DAGGER_NO_NAG=1 dagger develop
uv lock --directory .dagger
```

Keep generated SDK ignored and commit only deterministic module metadata and lock files.

**Step 5: Run red tests to green**

Run:

```bash
uv run pytest .dagger/tests/test_public_contracts.py tests/test_repository_contract.py -q
uv run --directory .dagger poe gate
uv run --directory .dagger poe audit
```

Expected: all new contracts pass.

**Step 6: Commit the adapter**

Commit message:

```text
feat: add lean Dagger execution adapter
```

## Task 5: Replace GitHub workflow ingress

**Files:**
- Delete: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/.github/workflows/ci.yml`
- Delete: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/.github/workflows/security-audit.yml`
- Create: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/.github/workflows/dagger.yml`
- Create: `/Users/harish/dev/oss/agentic-saga/.worktrees/dagger-portfolio-integration/.github/workflows/dagger-security.yml`

**Step 1: Add the two-step CI workflow**

Use only pinned checkout and pinned Dagger action. Pass exact `${{ github.sha }}` and the masked
repository Actions secret `DAGGER_GIT_HTTP_AUTH_HEADER` as a typed Dagger secret. Its value is the
value-only `Basic <base64(x-access-token:TOKEN)>` Git header; do not add the `Authorization:` name
or derive credentials in the workflow. Name the required job/check exactly `Dagger`.

**Step 2: Add the two-step scheduled/manual security workflow**

Use the same ingress and call only `security`. Keep it outside required branch protection.

**Step 3: Prove workflow contracts**

Run:

```bash
uv run pytest tests/test_repository_contract.py .dagger/tests/test_public_contracts.py -q
actionlint .github/workflows/*.yml
zizmor .github/workflows
git diff --check
```

**Step 4: Commit workflow migration**

Commit message:

```text
ci: route verification through Dagger
```

## Task 6: Prove the complete Agentic Saga candidate locally

**Files:**
- Verification only; change code only through a new failing regression test if a defect appears.

**Step 1: Run the existing authoritative product commands independently**

Run:

```bash
uv sync --frozen --all-groups --all-extras
uv run poe gate
(cd web/flight-recorder && corepack enable && pnpm install --frozen-lockfile && pnpm gate)
uv run poe release-candidate
```

**Step 2: Run the Dagger functions against the exact candidate**

Run `dagger call ci` and `dagger call security` with the exact commit SHA and a typed secret sourced from the authenticated GitHub CLI. Do not print or persist the secret.

**Step 3: Apply the Python quality contract**

Run the repository's Ruff, format, mypy, Xenon, pytest, coverage, and audit commands over both product and Dagger Python. Confirm every edited function remains at most 15 lines and complexity Grade A.

**Step 4: Commit any test-driven fixes separately**

Each defect discovered during verification begins with a failing regression test and receives its own narrow commit.

## Task 7: Open, verify, merge, and protect Agentic Saga

**Files:**
- GitHub metadata only after the branch is green.

**Step 1: Push and open the Agentic Saga PR**

Use the design and plan as the PR rationale. Verify the PR head SHA and every required check.

**Step 2: Merge only the exact green head**

Do not publish a package, create a release, or change repository visibility. Verify the merge commit and the post-merge `Dagger` check on `main`.

**Step 3: Configure branch protection**

Require the exact GitHub Actions `Dagger` check, strict up-to-date branches, conversation resolution, admin enforcement, no force pushes, and no deletions. Preserve the intentionally chosen approval and linear-history settings. Verify the API response rather than assuming the write succeeded.

**Step 4: Delete merged branches and remove the worktree**

Delete the remote feature branch, remove the local Dagger worktree, prune worktree metadata, and delete only the merged local branch created for this effort.

## Task 8: Add Agentic Saga to the central fleet

**Files:**
- Modify: `/Users/harish/dev/oss/ci/.dagger/src/ci/fleet.py`
- Modify: `/Users/harish/dev/oss/ci/.dagger/tests/test_fleet.py`
- Modify: `/Users/harish/dev/oss/ci/README.md`
- Modify: `/Users/harish/dev/oss/ci/CHANGELOG.md`
- Modify only if current count text requires it: `/Users/harish/dev/oss/ci/.dagger/src/ci/fleet_policy.py`
- Modify only if current architecture inventory requires it: `/Users/harish/dev/oss/ci/docs/superpowers/specs/2026-08-27-dagger-lego-architecture-design.md`

**Step 1: Create a new fleet worktree from merged central `main`**

Use branch `feat/register-agentic-saga`. Do not reuse the merged Foundation branch.

**Step 2: Add failing fleet tests**

Require `agentic-saga` in the exact expected repository set with the standard `Dagger` context, shared Foundation, conversation resolution, and no grandfathering.

Run the focused test and witness failure because the expectation is absent.

**Step 3: Add the single fleet expectation and truthful counts**

Add `expectation("agentic-saga")`. Update only current counts and documentation that actually drifted. Do not add Agentic Saga to generic module fixtures.

**Step 4: Run central local and authoritative fleet gates**

Run focused fleet tests, full central gate/audit, module fixtures, workflow validation, and:

```bash
GITHUB_TOKEN="$(gh auth token)" DAGGER_NO_NAG=1 \
  dagger call fleet --github-token=env:GITHUB_TOKEN --include-central
```

Expected: zero findings for the exact protected Agentic Saga `main` commit and all other fleet members.

**Step 5: Commit, push, PR, merge, and clean**

Commit message:

```text
feat: register Agentic Saga in Dagger fleet
```

Merge only after exact CI and post-merge fleet proof. Delete the remote/local branch and remove its worktree.

## Task 9: Register Agentic Saga and Dagger in the canonical portfolio

**Files:**
- Create: `/Users/harish/dev/project-ideas/oss/agentic-saga.md`
- Modify: `/Users/harish/dev/project-ideas/portfolio.json`
- Regenerate: `/Users/harish/dev/project-ideas/PORTFOLIO-STATUS.md`
- Regenerate: `/Users/harish/dev/project-ideas/oss/README.md`
- Regenerate: `/Users/harish/dev/project-ideas/ALL-PROJECT-IDEAS.md`
- Modify portfolio tests only when the new truthful schema entry requires an assertion.

**Step 1: Reconcile and preserve the existing local-ahead commit**

Inspect `082c89a` and origin state. Never rewrite or drop it. Create the portfolio branch from the intended current local `main` identity, and keep the PR diff explicit.

**Step 2: Add failing manifest tests/checks**

Require:

- A private unranked Agentic Saga entry with correct repository, status, evidence, and owned spec.
- The existing shared CI entry to describe the current Dagger control plane rather than retired reusable workflows.
- Agentic Saga's spec in `corpus_sources`.

Witness the manifest/render check fail before editing `portfolio.json`.

**Step 3: Write the OSS status document**

Lead with a TL;DR, explain deterministic Saga safety in plain language, provide a copy-paste quickstart and realistic e-commerce compensation demo, distinguish shipped behavior from future work, and link exact verification evidence. Do not claim publication or public availability.

**Step 4: Update the canonical manifest and regenerate views**

Edit `portfolio.json`, then use the repository renderer to regenerate all three owned views. Never hand-edit generated status files.

**Step 5: Run the complete portfolio gate**

Run:

```bash
uv run python scripts/render_portfolio.py check
uv run poe gate
git diff --check
```

Also run link, stale-language, and corpus-source validation exposed by the repository.

**Step 6: Commit, push, PR, merge, and clean**

Commit message:

```text
docs: add Agentic Saga to OSS portfolio
```

Merge only after exact checks pass. Verify generated views on `main`, then remove only the branch/worktree created for this task.

## Task 10: Final Northstar, production-state, and cleanup proof

**Files:**
- Verification only; produce evidence, not new architecture.

**Step 1: Run Northstar across the delivered repositories**

Re-run correctness, security, privacy, operability, documentation, and craft checks for Agentic Saga, central CI, and the portfolio integration. Resolve every material finding through TDD before proceeding.

**Step 2: Verify exact remote state**

Confirm:

- All three repositories' intended `main` commits are present remotely.
- Agentic Saga's exact merged `main` has a successful `Dagger` check.
- Central fleet reports zero findings.
- Branch protection requires the exact `Dagger` context.
- No PR from this effort remains open.
- No package/release was published and Agentic Saga remains private.

**Step 3: Remove stale task artifacts**

For branches and worktrees created by this plan only:

```bash
git worktree prune
git branch --merged main
git ls-remote --heads origin
```

Delete merged task branches locally and remotely, remove task worktrees, and retain only `main` unless an unrelated branch is proven active or user-owned.

**Step 4: Report evidence**

Report exact commit SHAs, PR numbers, check conclusions, test counts, coverage, fleet result, portfolio render result, visibility, publication status, branch/worktree cleanup, and any honest limitation.
