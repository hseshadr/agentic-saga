# Lean Dagger CI Design

## TL;DR

Keep the complete Agentic Saga release proof on both pull requests and the exact merged `main`
commit. Reduce wall time by fixing graph composition: verify the source once, install dependencies
from lockfile-only inputs, build the first-party release artifacts once, validate those same artifacts
on Python 3.12 and 3.13 concurrently, run the frontend proof once, and remove duplicate quality
execution from release measurement. GitHub continues to expose one protected `Dagger` check.

## Why

The current Dagger graph is correct but expensive. The exact merged-main run at commit
`18c004695d6bb069feeeb7ab9a9576378bafe5db` took about 44.5 minutes. The graph serializes the
Python versions, rebuilds equivalent environments, mounts the entire source before dependency
installation, constructs browser dependencies repeatedly, and asks release measurement to rerun
quality checks that the graph already completed.

The solution is not a second workflow system or a generic command DSL. Dagger remains the
composition engine, and the shared `hseshadr/ci` modules remain coarse, typed Legos.

## Goals

- Preserve full release proof on pull requests and on the exact merged `main` SHA.
- Preserve the single required GitHub check named `Dagger`.
- Preserve Python 3.12 and 3.13 quality, offline-install, package, coverage, performance, and
  packaged-browser verification.
- Preserve the separate scheduled/manual dependency security audit.
- Resolve and guard the authenticated exact source once per CI invocation.
- Build the Agentic Saga wheel and sdist once and validate those exact bytes on both Python
  runtimes.
- Build runtime-specific dependency wheelhouses where Python ABI compatibility requires them.
- Make Python and frontend dependency layers depend on pinned images and lockfiles, not arbitrary
  application-source changes.
- Run independent branches with explicit bounded concurrency.
- Replace the implementation-mirroring fake Dagger runtime with small behavioral contracts plus a
  real Dagger smoke test.
- Measure cold and warm hosted runtime before making performance claims.

## Non-goals

- Do not remove Dagger or move product commands back into GitHub Actions YAML.
- Do not introduce a generic pipeline DSL, plugin registry, scheduler, or dynamic command catalog.
- Do not weaken, skip, or sample existing quality and release checks.
- Do not share mutable cache volumes between product containers.
- Do not publish packages or change registry state.
- Do not require a central `hseshadr/ci` API change unless implementation proves an existing typed
  primitive is insufficient.
- Do not add OS-level multiprocessing. Concurrency is expressed by Dagger graph branches.

## Ownership

Central `hseshadr/ci` continues to own:

- authenticated source/history binding through `portfolio-foundation`;
- artifact envelopes and tamper verification;
- locked Python dependency audit mechanics;
- fleet policy, branch protection, and exact-main validation.

Agentic Saga owns:

- its supported Python versions and pinned runtime images;
- the release artifact layout and offline-install scenario;
- the React/Playwright Flight Recorder proof;
- product-specific performance and release budgets;
- the thin composition adapter under `.dagger/`.

Extraction into central `ci` happens only after a capability has a second real consumer and a
stable typed boundary.

## Target graph

```text
authenticated exact source
        |
        +-- first-party wheel + sdist (built once)
        |
        +-- frontend toolchain (lock inputs only)
        |       `-- frontend quality + packaged browser proof (once)
        |
        +-- Python 3.12 toolchain (lock inputs only)
        |       `-- quality -> runtime wheelhouse -> exact artifact install -> measurement
        |
        `-- Python 3.13 toolchain (lock inputs only)
                `-- quality -> runtime wheelhouse -> exact artifact install -> measurement

all branches complete -> one successful `Dagger` result
```

The first-party artifacts and frontend proof are immutable inputs to both Python lanes. Each Python
lane remains ordered internally. At most two expensive runtime lanes execute simultaneously; the
frontend proof may execute concurrently when runner capacity permits without spawning processes.

## Content-addressed dependency layers

The Python dependency layer consumes only:

- the exact pinned Python image;
- the exact pinned `uv` binary;
- `pyproject.toml` and `uv.lock`;
- dependency-install command arguments.

It installs dependencies without installing the project. The full verified source is mounted only
after that layer exists; the project is then installed offline without dependency resolution.

The frontend dependency layer consumes only:

- the exact pinned Node image;
- `web/flight-recorder/package.json`;
- `web/flight-recorder/pnpm-lock.yaml`;
- the exact Corepack/pnpm and Playwright install commands.

The full frontend source is overlaid after dependency and Chromium installation. No mutable cache
volume is attached.

## Build-once artifact contract

`scripts/build_release_artifacts.sh OUTPUT_DIR` produces repository-owned artifacts only:

- one wheel;
- one sdist;
- `runtime-requirements.txt`;
- `SOURCE_COMMIT`;
- `SHA256SUMS` for the wheel, sdist, and `runtime-requirements.txt`.

Runtime dependency wheels move to
`OUTPUT_DIR/wheelhouses/<python-major>.<python-minor>/`. The verification script creates or accepts
that runtime-specific wheelhouse and installs the already-built first-party wheel. It never rebuilds
the wheel or sdist when valid artifacts are supplied.

Both runtime lanes must report the same wheel and sdist digests. A missing, extra, modified, or
wrong-commit artifact fails closed. After both lanes succeed, the public Dagger result emits that
validated three-entry manifest as the hosted evidence channel.

## Identity-bound quality proof

Release measurement must not rerun a quality gate that the same graph already completed. It may
reuse results only through a JSON proof generated after the real gates succeed.

The proof schema is versioned and contains:

- schema version `1`;
- full 40-character source commit;
- Python major/minor version;
- SHA-256 digests of `pyproject.toml`, `uv.lock`, frontend `package.json`, and
  `pnpm-lock.yaml`;
- SHA-256 digests of Python, release-script, and frontend coverage JSON;
- the literal completed check names `python-gate` and `frontend-gate`;
- evaluated coverage results used by the release report.

The producer writes canonical JSON atomically. The consumer recomputes every identity and evidence
digest before accepting it. Unknown schema versions, missing/extra keys, duplicate checks,
non-finite values, wrong runtimes, dirty/wrong source identities, or modified evidence fail closed.

There is no `--skip-quality` option. Without an explicitly supplied valid proof, local
`measure_release.py` retains its current behavior and runs the complete Python and frontend gates.

## Tests

Use strict red-green-refactor cycles.

- Shell release tests prove one first-party build can feed both runtime-specific wheelhouses and
  that tampering or a wrong source marker fails.
- Release-runner tests prove valid proof reuse and refusal for every identity/evidence mismatch.
- Adapter tests assert observable orchestration: exact-source guard first, one artifact build, one
  frontend proof, both runtime lanes, bounded fan-out, and failure propagation.
- Workflow contracts preserve the two thin pinned ingress files and the single `Dagger` check.
- One real Dagger smoke invocation validates the generated graph and container/artifact handoffs.
- The complete existing test, type, lint, complexity, security, and release gates remain required.

The large fake container implementation is deleted after the replacement behavioral tests and real
smoke test demonstrate equivalent protection. Tests must not mirror Dagger internals.

## Performance acceptance

The correctness gate is unchanged. Performance is accepted only after one hosted cold run and one
hosted warm run on an identical commit.

- Target cold `Dagger` job: at most 20 minutes.
- Target warm `Dagger` job: at most 12 minutes.
- If either target is missed, retain the correctness changes and record the measured bottleneck;
  do not weaken tests to hit the target.
- The old 44.5-minute exact-main run remains the baseline.

## Hosted-evidence status

The 20-minute cold and 12-minute warm figures above are acceptance targets, not measured
performance claims. Local graph and contract checks establish only local behavior. Hosted evidence
must record the pull-request run URLs, the full immutable commit SHA, wall and stage timings,
Python versions, artifact digests, test counts, and coverage. A separate full Dagger run and
security audit on the exact merged `main` SHA are required; pull-request evidence never substitutes
for merged-main evidence.

## Rollout and recovery

Land the work through a non-Dependabot pull request. Require the complete PR proof, independent code
review, and an exact merged-main proof. If hosted behavior reveals a cache or concurrency defect,
revert the composition commit without changing branch protection or workflow ingress. Do not
publish packages during this work.
