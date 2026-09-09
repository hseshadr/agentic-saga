# Lean Architecture Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise the private v0.1 from Northstar B/85 to A by removing duplicated safety contracts, making package dependencies one-way, centralizing identity framing, and providing one small supported runtime composition path.

**Architecture:** Keep `contracts` as the dependency leaf, `kernel` as deterministic policy/state plus its ports, `storage` as a kernel-port adapter, `execution` as the orchestration shell, and `evidence`/`agents`/`manifest` as consumers. Preserve every persisted identifier byte-for-byte. Add one named factory that assembles the existing Lego pieces; do not add a DI container, builder framework, ORM, repository layer, or second runtime.

**Tech Stack:** Python 3.12+, Pydantic 2, stdlib SQLite/hashlib, pytest, mypy, Ruff, Xenon

**Spec:** `docs/superpowers/specs/2026-09-06-agentic-saga-design.md`

## Global Constraints

- Mandatory runtime dependencies remain exactly Pydantic and ruamel.yaml.
- Ecommerce remains under `examples/`; no commerce vocabulary enters `src/agentic_saga`.
- Agent output remains untrusted proposals; deterministic code owns policy, durable intent, dispatch, reconciliation, compensation, terminal proof, and human pause.
- Preserve all existing persisted ID and digest values with golden compatibility tests.
- No live model calls, network calls, package publication, ORM, DI framework, or SQLite partitioning in this plan.
- Use red -> green -> refactor for every behavior or boundary change; run the Python quality contract after every slice.

---

### Task 1: One lease contract and an acyclic package graph

**Files:**
- Create: `src/agentic_saga/contracts/clock.py`
- Create: `src/agentic_saga/contracts/redaction.py`
- Create: `src/agentic_saga/kernel/ports.py`
- Modify: `src/agentic_saga/contracts/runtime.py`
- Modify: `src/agentic_saga/contracts/trace.py`
- Modify: `src/agentic_saga/contracts/outcomes.py`
- Modify: `src/agentic_saga/kernel/state.py`
- Modify: `src/agentic_saga/kernel/policy.py`
- Modify: `src/agentic_saga/kernel/runtime.py`
- Modify: `src/agentic_saga/storage/base.py`
- Modify: `src/agentic_saga/storage/sqlite.py`
- Modify: `src/agentic_saga/execution/clock.py`
- Modify: `src/agentic_saga/execution/leases.py`
- Modify: internal imports that currently target `storage.base`, `execution.clock`, `execution.leases`, or `evidence.redaction`
- Test: `tests/unit/contracts/test_layering.py`
- Test: existing lease, kernel, storage, execution, trace, and manifest suites

**Interfaces:**
- Produces: one frozen Pydantic `Lease` model used by storage and execution without model-dump conversion.
- Produces: `Clock` in the contracts leaf; `execution.clock` may re-export implementations for compatibility.
- Produces: `kernel.ports` as the home of `KernelStore` and its command/result/error contracts; `storage.base` is at most a thin compatibility facade with no duplicated definitions.
- Produces: an import graph with no bidirectional edges among `contracts`, `kernel`, `storage`, `execution`, and `evidence`.

- [ ] **Step 1: Write the failing graph and identity tests**

```python
def test_core_package_dependencies_are_one_way() -> None:
    graph = production_package_import_graph()
    assert bidirectional_edges(graph, CORE_PACKAGES) == set()


def test_execution_and_storage_share_one_lease_type() -> None:
    assert execution.Lease is kernel_ports.Lease
```

- [ ] **Step 2: Run the focused tests and witness the current bidirectional edges and duplicate lease failure**

Run: `uv run --python 3.12 pytest tests/unit/contracts/test_layering.py -q`

Expected: FAIL naming the existing package pairs and distinct `Lease`/`LeaseState` classes.

- [ ] **Step 3: Move leaf values and ports without changing behavior**

```python
class Lease(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    saga_id: SagaId
    owner: Owner
    fence_token: FenceToken
    expires_at: AwareDatetime
```

Use this same model in the storage protocol and `LeaseService`; delete serialization conversion helpers. Move `Clock` and redaction primitives below their consumers. Move storage boundary models/protocols into `kernel.ports`, retaining only a compatibility re-export if a tested public import requires it. Move `ExecutionBudget` and `SagaStatus` to leaf contracts or re-export them from their former modules so `contracts` imports neither `kernel` nor `evidence`.

- [ ] **Step 4: Make internal imports follow the one-way graph**

Internal production modules import the new authoritative definitions directly. A compatibility facade must not become a second definition and must not be used by production internals.

- [ ] **Step 5: Run focused and full regression tests**

Run: `uv run --python 3.12 pytest tests/unit/contracts/test_layering.py tests/unit/execution tests/unit/storage tests/unit/kernel tests/unit/evidence -q`

Expected: PASS with no persisted behavior change.

- [ ] **Step 6: Run quality gates and commit only this slice**

Run: `uv run --python 3.12 --frozen poe gate`

Run: `uv run --python 3.12 ruff check src tests && uv run --python 3.12 mypy src tests && uv run --python 3.12 xenon --max-absolute A --max-modules A --max-average A src`

Commit: `refactor: make core package boundaries one way`

---

### Task 2: One domain-separated identity framing primitive

**Files:**
- Modify: `src/agentic_saga/kernel/identity.py`
- Modify: `src/agentic_saga/kernel/runtime.py`
- Modify: `src/agentic_saga/execution/runtime.py`
- Modify: `src/agentic_saga/execution/dispatcher.py`
- Modify: `src/agentic_saga/execution/reconciliation.py`
- Modify: `src/agentic_saga/storage/sqlite.py`
- Modify: the manifest/catalog digest module if and only if it uses the same framed-SHA layout
- Test: `tests/unit/kernel/test_identity_compatibility.py`
- Test: existing runtime, dispatcher, reconciliation, SQLite, and manifest golden tests

**Interfaces:**
- Produces: one private `framed_sha256(namespace: bytes, *components: bytes) -> str` implementation in `kernel.identity`.
- Preserves: all named ID factories and every current output byte.

- [ ] **Step 1: Freeze current outputs before refactoring**

```python
@pytest.mark.parametrize("factory_case", GOLDEN_FACTORY_CASES)
def test_stable_identifiers_remain_byte_compatible(factory_case: FactoryCase) -> None:
    assert factory_case.build() == factory_case.expected_v01_value
```

Include operation, transition, event, claim, reconciliation, and any catalog identifier that uses the same eight-byte length framing.

- [ ] **Step 2: Run the golden tests against the current implementation**

Run: `uv run --python 3.12 pytest tests/unit/kernel/test_identity_compatibility.py -q`

Expected: PASS; these tests are the migration fence.

- [ ] **Step 3: Add the single primitive and replace only identical algorithms**

```python
def framed_sha256(namespace: bytes, *components: bytes) -> str:
    values = (namespace, *components)
    return sha256(b"".join(_length_delimit(value) for value in values)).hexdigest()
```

Keep named domain factories and their namespaces. Do not force canonical-JSON IDs through this helper when their historical byte layout differs.

- [ ] **Step 4: Delete repeated delimiter/hash helpers and rerun golden tests**

Run: `uv run --python 3.12 pytest tests/unit/kernel/test_identity_compatibility.py tests/unit/kernel tests/unit/execution tests/unit/storage -q`

Expected: PASS with identical IDs.

- [ ] **Step 5: Run mutation and quality gates, then commit**

Run: `uv run --python 3.12 --frozen poe gate`

Run the repository identity mutation target and require every mutant killed.

Commit: `refactor: centralize stable identity framing`

---

### Task 3: One supported runtime composition path

**Files:**
- Create: `src/agentic_saga/execution/composition.py`
- Modify: `src/agentic_saga/execution/__init__.py`
- Modify: `src/agentic_saga/__init__.py`
- Modify: `examples/ecommerce/demo.py`
- Modify: `README.md`
- Modify: `QUICKSTART.md`
- Test: `tests/unit/execution/test_composition.py`
- Test: `tests/test_public_api.py`
- Test: ecommerce integration/BDD suites

**Interfaces:**
- Produces: `compose_runtime(...) -> SagaRuntime`, exported from both `agentic_saga.execution` and the root package.
- Produces: root exports for `SagaRuntime`, `SagaDefinition`, and `SagaGoal`; low-level collaborators remain available from advanced modules.
- Consumes: one `KernelStore`, `SagaDefinition`, `PolicyContextProvider`, `TerminalGate`, `InvariantEvidenceProvider`, `Clock`, worker ID, and stable ID namespace.

- [ ] **Step 1: Write a failing public composition test**

```python
def test_public_factory_runs_a_minimal_durable_saga(tmp_path: Path) -> None:
    runtime = compose_runtime(
        store=store,
        definition=definition,
        policy_context_provider=contexts,
        terminal_gate=terminal_gate,
        invariant_evidence_provider=evidence,
        clock=clock,
        worker_id="test-worker",
        id_namespace=b"test/v1",
    )
    assert isinstance(runtime, SagaRuntime)
```

The integration assertion must start or resume through the real runtime; do not merely inspect private attributes.

- [ ] **Step 2: Witness the missing import/factory failure**

Run: `uv run --python 3.12 pytest tests/unit/execution/test_composition.py tests/test_public_api.py -q`

Expected: FAIL because the supported factory and root exports do not exist.

- [ ] **Step 3: Implement the smallest factory over existing components**

```python
def compose_runtime(*, store: KernelStore, definition: SagaDefinition,
                    policy_context_provider: PolicyContextProvider,
                    terminal_gate: TerminalGate,
                    invariant_evidence_provider: InvariantEvidenceProvider,
                    clock: Clock, worker_id: str,
                    id_namespace: bytes) -> SagaRuntime:
    leases = LeaseService(store)
    # Construct the existing kernel, dispatcher, reconciler, unwinder, and catalog once.
```

Do not add a container, builder object, plugin registry, global singleton, or hidden I/O.

- [ ] **Step 4: Replace only the ecommerce service wiring with the supported factory**

Retain domain-specific registry, policy, context, invariant evidence, and provider assembly. Remove the duplicated six-service constructor block and its now-unused deep imports.

- [ ] **Step 5: Document one copy-paste composition path and run behavior tests**

Run: `uv run --python 3.12 pytest tests/unit/execution/test_composition.py tests/test_public_api.py tests/integration tests/bdd -q`

Expected: PASS; all four ecommerce trace outcomes remain byte-stable unless a separately reviewed evidence-version change is required.

- [ ] **Step 6: Run quality gates and commit**

Run: `uv run --python 3.12 --frozen poe gate`

Commit: `feat: add lean runtime composition facade`

---

### Task 4: Make the architecture page an executable source map

**Files:**
- Modify: `docs/architecture.html`
- Modify: `README.md`
- Create: `tests/test_architecture_contract.py`

**Interfaces:**
- Produces: a no-network standalone architecture page that presents direct typed construction and optional `saga.yaml` as two inputs to the same validated runtime.
- Produces: a complete source map containing `contracts`, `kernel`, `storage`, `execution`, `evidence`, `agents`, `manifest`, `demo`, and `cli` responsibilities.

- [ ] **Step 1: Write failing source-map and optional-authoring contract tests**

```python
def test_architecture_marks_manifest_as_optional() -> None:
    page = ARCHITECTURE.read_text()
    assert "Optional authoring" in page
    assert "Direct typed construction" in page


def test_architecture_source_paths_exist() -> None:
    assert architecture_source_paths() <= repository_paths()
```

Also assert README links the page and the HTML names every public package responsibility.

- [ ] **Step 2: Witness the current manifest/source-map failures**

Run: `uv run --python 3.12 pytest tests/test_architecture_contract.py -q`

Expected: FAIL on mandatory-looking manifest station and omitted packages.

- [ ] **Step 3: Update the semantic flow and source map without visual expansion**

Keep the same five-section page and seven lifecycle stages. Redraw only the authoring entry so direct typed inputs and optional YAML converge before deterministic validation. Add missing package responsibilities and a concise supported-vs-advanced API distinction.

- [ ] **Step 4: Rerun contract, accessibility, responsive, and no-network gates**

Run: `uv run --python 3.12 pytest tests/test_architecture_contract.py -q`

Run the existing browser matrix at 1440, 900, 640/200%, and 320 pixels; require no overflow, keyboard access, axe zero critical/serious violations, no console errors, no external requests, and full no-JS disclosure.

- [ ] **Step 5: Commit the architecture slice**

Commit: `docs: align architecture with optional authoring flow`

---

## Final integration

- [ ] Merge the four reviewed slices with Task 12 release automation and documentation truth.
- [ ] Run Python 3.12 and 3.13 full gates, mutation, frontend/Playwright, packaged-wheel browser proof, exact sdist/wheel contents, secret/dependency audits, and `uv run python scripts/measure_release.py`.
- [ ] Run a cold exact-commit Northstar audit; fix every Critical and Important finding.
- [ ] Record the exact commit, artifact digests, environment, measured budgets, and remaining honest limitations before any push or merge.
