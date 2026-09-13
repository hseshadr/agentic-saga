# Lean Dagger CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve complete pull-request and exact-main release proof while cutting redundant Dagger work through build-once artifacts, content-addressed dependency layers, identity-bound quality evidence, and bounded graph concurrency.

**Architecture:** The authenticated exact source is resolved once. A single first-party artifact build and a single frontend proof feed two ordered Python-runtime lanes that execute concurrently and converge into the existing protected `Dagger` result. Release measurement accepts prior quality results only through a fail-closed, identity-bound proof; direct local measurement still executes the complete gates.

**Tech Stack:** Dagger 0.21.8 Python SDK, Python 3.12/3.13, uv, pytest, Poe the Poet, Bash, pnpm, Playwright, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-13-lean-dagger-ci-design.md`

## Global Constraints

- Keep the complete release proof on pull requests and the exact merged `main` SHA.
- Keep exactly one protected GitHub check named `Dagger` and retain the separate `Dagger security audit` workflow.
- Preserve all Python 3.12/3.13, coverage, offline-install, artifact, browser, performance, security, and exact-source checks.
- Do not add a generic pipeline DSL, dynamic command catalog, mutable cache volume, or OS multiprocessing.
- Keep runtime images, Dagger dependencies, checkout, and Dagger GitHub Action pinned to immutable digests/SHAs.
- Build one first-party wheel and sdist; create runtime-specific dependency wheelhouses under `dist/release/wheelhouses/<major>.<minor>/`.
- A quality proof is accepted only after schema, source, runtime, lockfile, evidence-file, and coverage-result validation.
- `scripts/measure_release.py` with no arguments must continue to execute the complete Python and frontend quality gates.
- Use red-green-refactor for every behavior change and commit tests with implementation.
- Maintain strict mypy, Ruff, Xenon Grade A, and at least 90% branch coverage for core and release logic.
- Do not publish a package or merge a Dependabot pull request.
- Preserve unrelated working-tree changes and the held `feat/dagger-portfolio-integration` worktree.

---

## File map

- `scripts/build_release_artifacts.sh`: build repository-owned wheel/sdist and immutable manifests once.
- `scripts/build_runtime_wheelhouse.sh`: build one Python-major/minor dependency wheelhouse from the exported locked requirements.
- `scripts/verify_release_candidate.sh`: verify immutable artifacts, select/build the current runtime wheelhouse, and perform the offline install proof.
- `scripts/quality_proof.py`: own the versioned proof schema, canonical serialization, identity validation, and conversion to coverage budget results.
- `scripts/release_runner.py`: run real gates by default or consume validated quality results when an explicit proof path is provided.
- `scripts/measure_release.py`: expose only `--quality-proof PATH` in addition to the complete default measurement.
- `.dagger/src/agentic_saga_ci/main.py`: compose verified source, stable dependency layers, build-once artifacts, frontend proof, and two concurrent runtime lanes.
- `.dagger/tests/test_public_contracts.py`: retain compact API, workflow, pin, and VCS contracts; delete the fake container runtime.
- `.dagger/tests/test_orchestration.py`: test the small pure concurrency/failure orchestration boundary with real awaitables, not a fake Dagger SDK.
- `tests/test_release_contract.py`: prove build-once artifacts and per-runtime wheelhouse behavior.
- `tests/test_measure_release.py`: prove proof creation/consumption and CLI behavior.
- `tests/test_release_measurement_contract.py`: pressure-test proof identity, evidence tampering, schema, and coverage failures.
- `pyproject.toml`: include `scripts/quality_proof.py` in strict release typing and complexity gates.
- `README.md` and `docs/architecture.html`: explain the lean graph and exact PR/main assurance without claiming unmeasured speed.

---

### Task 1: Split first-party artifacts from runtime wheelhouses

**Files:**
- Create: `scripts/build_runtime_wheelhouse.sh`
- Modify: `scripts/build_release_artifacts.sh`
- Modify: `scripts/verify_release_candidate.sh`
- Modify: `tests/test_release_contract.py`

**Interfaces:**
- Consumes: existing `dist/release` artifact contract and locked `runtime-requirements.txt`.
- Produces: `build_runtime_wheelhouse.sh RELEASE_DIR [PYTHON_VERSION]` and `dist/release/wheelhouses/<major>.<minor>/`; `verify_release_candidate.sh OUTPUT_DIR [PYTHON_VERSION]` reuses valid first-party artifacts.

- [ ] **Step 1: Add failing release-contract tests**

Add tests that invoke the real shell scripts in a temporary repository and assert literal outcomes:

```python
def test_first_party_artifacts_are_not_rebuilt_for_a_second_runtime(tmp_path: Path) -> None:
    release = build_release(tmp_path)
    before = artifact_digests(release)
    build_runtime_wheelhouse(release, "3.12")
    build_runtime_wheelhouse(release, "3.13")
    assert artifact_digests(release) == before
    assert sorted(path.name for path in (release / "wheelhouses").iterdir()) == ["3.12", "3.13"]


def test_candidate_verification_rejects_modified_build_once_artifact(tmp_path: Path) -> None:
    release = build_release(tmp_path)
    next(release.glob("*.whl")).write_bytes(b"tampered")
    result = verify_candidate(release, "3.12")
    assert result.returncode == 1
    assert "artifact digest mismatch" in result.stderr
```

Use the existing real-script fixture style in `tests/test_release_contract.py`; do not mock subprocess execution.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_release_contract.py -q
```

Expected: the build-once and versioned-wheelhouse tests fail because the new script/layout do not exist and the current artifact builder writes a single unversioned `wheelhouse/`.

- [ ] **Step 3: Implement the runtime wheelhouse script**

Implement `build_runtime_wheelhouse.sh` with this command contract:

```text
scripts/build_runtime_wheelhouse.sh RELEASE_DIR [PYTHON_VERSION]
```

Resolve `PYTHON_VERSION` to the executing interpreter's `<major>.<minor>` when omitted; accept only `3.12` or `3.13`; validate `RELEASE_DIR` under the repository `dist/`; require the locked requirements and immutable artifact manifests; create a temporary directory; run hash-required binary-only `pip download`; then atomically replace
`RELEASE_DIR/wheelhouses/<version>/`. Reuse the safety checks and digest helper style already present in the release scripts.

- [ ] **Step 4: Make artifact build and verification composable**

Remove dependency-wheel downloading from `build_release_artifacts.sh`. Update `verify_release_candidate.sh` so it:

1. builds first-party artifacts only when the manifest set is absent;
2. verifies `SOURCE_COMMIT`, `SHA256SUMS`, the wheel, and the sdist before any install;
3. invokes `build_runtime_wheelhouse.sh` only when the selected runtime directory is absent;
4. installs locked dependencies from `wheelhouses/<version>/` and the exact verified first-party wheel;
5. never invokes `uv build` when valid build-once artifacts were supplied.

- [ ] **Step 5: Verify GREEN and the mutation cases**

Run:

```bash
uv run pytest tests/test_release_contract.py -q
uv run pytest tests/test_measure_release.py tests/test_release_measurement_contract.py -q
```

Expected: all tests pass. Manually change the recorded source commit in the temporary fixture and confirm the existing wrong-commit regression test fails closed.

- [ ] **Step 6: Commit**

```bash
git add scripts/build_release_artifacts.sh scripts/build_runtime_wheelhouse.sh scripts/verify_release_candidate.sh tests/test_release_contract.py
git commit -m "refactor: build release artifacts once"
```

---

### Task 2: Add identity-bound quality proofs

**Files:**
- Create: `scripts/quality_proof.py`
- Modify: `scripts/release_runner.py`
- Modify: `scripts/measure_release.py`
- Modify: `tests/test_measure_release.py`
- Modify: `tests/test_release_measurement_contract.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: completed `.coverage.json`, `.coverage-release-scripts.json`, `web/flight-recorder/coverage/coverage-summary.json`, repository identity, and lockfiles.
- Produces: `write_quality_proof(path: Path) -> None`, `quality_results_from_proof(path: Path) -> tuple[BudgetResult, ...]`, and `measure_release(quality_proof: Path | None = None) -> ReleaseReport`.

- [ ] **Step 1: Write proof-contract tests**

Add hand-authored fixtures and behavioral tests:

```python
def test_valid_quality_proof_reuses_results_without_running_gates(tmp_path: Path) -> None:
    proof = valid_quality_proof(tmp_path)
    results = quality_results_from_proof(proof)
    assert tuple(result.name for result in results) == (
        "core_branch_coverage_percent",
        "release_scripts_branch_coverage_percent",
        "frontend_branch_coverage_percent",
        "python_complexity_grade_a",
        "browser_behavior_checks",
    )


@pytest.mark.parametrize("field", ["source_commit", "python_version", "uv_lock_sha256"])
def test_quality_proof_rejects_identity_mismatch(tmp_path: Path, field: str) -> None:
    proof = tampered_quality_proof(tmp_path, field)
    with pytest.raises(ValueError, match="quality proof identity mismatch"):
        quality_results_from_proof(proof)
```

Also cover unknown, missing, or extra schema fields; duplicate or missing checks; non-finite coverage; modified coverage files; abbreviated commits; dirty checkout; and atomic-write cleanup.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_measure_release.py tests/test_release_measurement_contract.py -q
```

Expected: import/test failures identify the absent `scripts.quality_proof` interface.

- [ ] **Step 3: Implement the proof module**

Use immutable typed boundaries:

```python
@dataclass(frozen=True)
class QualityProof:
    schema_version: int
    source_commit: str
    python_version: str
    input_digests: Mapping[str, str]
    evidence_digests: Mapping[str, str]
    completed_checks: tuple[str, ...]
    results: tuple[BudgetResult, ...]


def write_quality_proof(path: Path) -> None:
    payload = quality_proof_payload()
    atomic_write_json(path, payload)


def quality_results_from_proof(path: Path) -> tuple[BudgetResult, ...]:
    proof = parse_quality_proof(read_json_object(path))
    validate_quality_proof(proof)
    return proof.results
```

Keep functions at 15 lines or fewer by separating canonical JSON, SHA-256, exact-key validation, identity collection, evidence validation, result parsing, and atomic replacement. Use `Mapping[str, object]`; do not introduce `Any`, `TypedDict`, or mutable global state.

- [ ] **Step 4: Integrate proof consumption without a skip flag**

Change the measurement boundary to:

```python
def measure_release(quality_proof: Path | None = None) -> ReleaseReport:
    identity = collect_environment()
    quality = quality_results_from_proof(quality_proof) if quality_proof else _quality_results()
    with tempfile.TemporaryDirectory(prefix="agentic-saga-release-") as directory:
        results = _release_results(_wheel_cli(Path(directory)), quality)
    return ReleaseReport(identity, results)
```

Update internal result composition to receive `quality` explicitly. Accept only
`measure_release.py --quality-proof PATH`; no argument retains complete gate execution, and any other argument returns usage error 2.

- [ ] **Step 5: Extend static quality enforcement**

Add `scripts/quality_proof.py` to `typecheck-release` and `complexity` in `pyproject.toml`. Ensure Ruff, strict mypy, and Xenon Grade A cover the new module.

- [ ] **Step 6: Verify GREEN and mutation resistance**

Run:

```bash
uv run pytest tests/test_measure_release.py tests/test_release_measurement_contract.py -q
uv run poe typecheck-release
uv run poe complexity
uv run ruff check scripts/quality_proof.py scripts/release_runner.py scripts/measure_release.py tests/test_measure_release.py tests/test_release_measurement_contract.py
```

Expected: all pass. Mutating a lockfile digest, coverage digest, or completed-check name makes at least one focused test fail.

- [ ] **Step 7: Commit**

```bash
git add scripts/quality_proof.py scripts/release_runner.py scripts/measure_release.py tests/test_measure_release.py tests/test_release_measurement_contract.py pyproject.toml
git commit -m "feat: bind release measurement to quality proof"
```

---

### Task 3: Compose the lean bounded Dagger graph

**Files:**
- Modify: `.dagger/src/agentic_saga_ci/main.py`
- Modify: `.dagger/tests/test_public_contracts.py`
- Create: `.dagger/tests/test_orchestration.py`

**Interfaces:**
- Consumes: build-once artifact directory, per-runtime wheelhouse contract, `write_quality_proof`, and `measure_release.py --quality-proof PATH` from Tasks 1-2.
- Produces: `_bounded_gather(*operations: Awaitable[object], limit: int = 2) -> None`, one frontend proof directory, one release artifact directory, and two ordered runtime lanes.

- [ ] **Step 1: Add failing orchestration tests**

Use real asyncio awaitables and observable events rather than a fake Dagger SDK:

```python
@pytest.mark.asyncio
async def test_bounded_gather_never_runs_more_than_two_operations() -> None:
    probe = ConcurrencyProbe()
    await _bounded_gather(*(probe.operation(index) for index in range(5)), limit=2)
    assert probe.maximum_active == 2


@pytest.mark.asyncio
async def test_bounded_gather_propagates_a_lane_failure() -> None:
    async def fail() -> None:
        raise RuntimeError("python 3.13 failed")

    with pytest.raises(RuntimeError, match="python 3.13 failed"):
        await _bounded_gather(fail(), limit=2)
```

Add compact public-contract assertions for exactly one source resolution, one artifact build command, one frontend gate/proof, both versioned wheelhouse paths, and a measurement command carrying `--quality-proof`.

- [ ] **Step 2: Run focused Dagger tests and verify RED**

Run:

```bash
uv run --directory .dagger pytest tests/test_orchestration.py tests/test_public_contracts.py -q
```

Expected: failures identify missing bounded fan-out, repeated full-source dependency installs, unversioned wheelhouse use, and absent proof handoff.

- [ ] **Step 3: Make Python and frontend dependency inputs stable**

Refactor the adapter so Python dependencies are installed from a directory containing only
`pyproject.toml` and `uv.lock`, with `uv sync --no-install-project`; mount the verified source afterward and install the project offline without dependency resolution. Build frontend dependencies from only `package.json` and `pnpm-lock.yaml`, install Chromium there, and overlay the complete frontend source afterward. Do not attach a Dagger cache volume.

- [ ] **Step 4: Build shared immutable outputs once**

Create one artifact directory by running `uv run poe artifacts` in the pinned builder runtime. Create one frontend-proof directory by running the complete `pnpm gate` once and retaining its coverage output. Feed those immutable directories to both runtime lanes.

- [ ] **Step 5: Add ordered runtime lanes and bounded fan-out**

Each runtime lane must perform this exact order:

```text
Python gate
-> mount build-once first-party artifacts
-> verify/build wheelhouses/<version>
-> mount verified frontend coverage evidence
-> write identity-bound quality proof
-> measure release with --quality-proof
```

Run Python 3.12 and 3.13 through `_bounded_gather(..., limit=2)`. The top-level `ci()` first resolves the source, then builds artifacts and frontend proof concurrently, then executes the two runtime lanes concurrently, and finally returns the unchanged success message.

- [ ] **Step 6: Remove duplicate security guarding**

Let `python-package.dependency_audit()` own the exact Foundation guard already performed internally. Await it before `pnpm audit`; remove the extra explicit `_guard()` call from `security()`. Preserve fail-closed ordering in a focused contract test.

- [ ] **Step 7: Verify GREEN and static quality**

Run:

```bash
DAGGER_NO_NAG=1 dagger develop
uv run --directory .dagger pytest tests/test_orchestration.py tests/test_public_contracts.py -q
uv run --directory .dagger poe gate
uv run --directory .dagger poe audit
```

Expected: all pass, with the public `ci` and `security` signatures unchanged.

- [ ] **Step 8: Commit**

```bash
git add .dagger/src/agentic_saga_ci/main.py .dagger/tests/test_orchestration.py .dagger/tests/test_public_contracts.py
git commit -m "perf: compose bounded content-addressed Dagger gates"
```

---

### Task 4: Remove the fake Dagger runtime and prove the real graph

**Files:**
- Modify: `.dagger/tests/test_public_contracts.py`
- Modify: `.dagger/tests/test_orchestration.py`

**Interfaces:**
- Consumes: the final Task 3 graph and unchanged public `ci`/`security` interface.
- Produces: compact static boundary tests plus a recorded successful real Dagger invocation.

- [ ] **Step 1: Establish replacement behavioral coverage**

Before deleting fake-runtime helpers, ensure the compact tests catch these mutations:

```text
remove exact source resolution
build first-party artifacts once per runtime
replace versioned wheelhouse with a shared mutable directory
drop Python 3.13
omit frontend quality proof
accept a runtime lane failure
add an unapproved public Dagger function
unpin an image, shared module, checkout action, or Dagger action
track generated .dagger/sdk or .dagger/.venv content
```

Use AST only for public signatures and immutable literals. Exercise concurrency and failure propagation through real awaitables. Do not recreate container snapshots, mounts, files, directories, or Dagger method traces.

- [ ] **Step 2: Run replacement tests against isolated mutations**

For each listed production mutation, modify only a temporary copied source string or fixture and assert the relevant test fails for its intended reason. Restore the fixture after each case.

Run:

```bash
uv run --directory .dagger pytest tests/test_orchestration.py tests/test_public_contracts.py -q
```

Expected: all baseline tests pass and each mutation is rejected by a named test.

- [ ] **Step 3: Delete implementation-mirroring fakes**

Delete `_Container`, `_Dag`, `_Snapshot`, mount/artifact fakes, exact method-operation traces, and lineage mutation helpers from `test_public_contracts.py`. Retain the Git-index boundary tests, public API contract, pinned-input contract, workflow ingress contract, and the smaller orchestration tests.

- [ ] **Step 4: Run the real local Dagger proof**

Run with the existing read-only authentication secret without printing it:

```bash
DAGGER_NO_NAG=1 dagger call ci --source=. --commit-sha="$(git rev-parse HEAD)" --git-auth-header=env:DAGGER_GIT_HTTP_AUTH_HEADER
DAGGER_NO_NAG=1 dagger call security --source=. --commit-sha="$(git rev-parse HEAD)" --git-auth-header=env:DAGGER_GIT_HTTP_AUTH_HEADER
```

Expected: both return their unchanged success messages. Record elapsed stage timings and artifact digests in the task report.

- [ ] **Step 5: Commit**

```bash
git add .dagger/tests/test_public_contracts.py .dagger/tests/test_orchestration.py
git commit -m "test: replace fake Dagger runtime with contracts"
```

---

### Task 5: Document, review, benchmark, and ship exact PR/main proof

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.html`
- Modify: `docs/superpowers/specs/2026-09-13-lean-dagger-ci-design.md`
- Modify: `docs/superpowers/plans/2026-09-13-lean-dagger-ci.md`

**Interfaces:**
- Consumes: the verified graph, artifact digests, local test evidence, and hosted run timings.
- Produces: honest OSS documentation, reviewed pull request, exact merged-main evidence, and clean branch/worktree state.

- [ ] **Step 1: Update human documentation**

Add a TL;DR architecture section showing source-once, build-once, frontend-once, and dual-runtime validation. State that both pull requests and merged `main` receive complete proof. Explain that cold/warm targets are targets until hosted evidence exists; do not call the pipeline faster based only on local expectations.

- [ ] **Step 2: Run the complete local quality gate**

Run:

```bash
uv run poe gate
uv run poe release-candidate
uv run --directory .dagger poe gate
uv run --directory .dagger poe audit
actionlint .github/workflows/*.yml
uvx zizmor==1.29.0 --persona=auditor --min-severity=low --offline --strict-collection .github
git diff --check
```

Expected: every command exits 0. The complete Python gate retains at least 90% branch coverage.

- [ ] **Step 3: Commit documentation and verification record**

```bash
git add README.md docs/architecture.html docs/superpowers/specs/2026-09-13-lean-dagger-ci-design.md docs/superpowers/plans/2026-09-13-lean-dagger-ci.md
git commit -m "docs: explain lean Dagger release proof"
```

- [ ] **Step 4: Run independent whole-branch review**

Review the complete branch against the design, including correctness, secret handling, source identity, artifact reuse, concurrency cancellation/failure behavior, proof tamper resistance, test honesty, and documentation claims. Resolve every load-bearing finding and rerun the affected complete gate.

- [ ] **Step 5: Open the pull request and collect cold/warm proof**

Push `codex/lean-dagger-ci`, open a non-Dependabot pull request, and let its first `Dagger` run establish the cold measurement. Rerun the exact same commit to establish warm measurement. Record both run URLs, exact SHA, wall time, stage timings, Python versions, artifact digests, test counts, and coverage results.

- [ ] **Step 6: Merge and verify exact main**

After required checks and review pass, merge the pull request. Wait for the full `Dagger` workflow on the exact merge SHA and the separate security audit. Do not treat the PR proof as merged-main proof.

- [ ] **Step 7: Reconcile performance targets honestly**

If cold is at most 20 minutes and warm is at most 12 minutes, record those measured results. Otherwise record actual timings and the remaining named bottleneck without weakening the gates.

- [ ] **Step 8: Clean implementation artifacts**

After exact-main proof succeeds, fast-forward the normal checkout, delete the merged remote/local feature branch, remove only the `lean-dagger-ci` worktree, and confirm the remote repository retains only intended branches. Preserve the unrelated held worktree and its edit.
