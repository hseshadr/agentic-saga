## Northstar Report — Agentic Saga

**Grade: B (provisional)** — Identified defects are corrected and local gates pass; security scanning and clean-commit release evidence remain unverified.

Review date: 2026-09-19. Scope: current working tree, including this session's fixes; no clean-commit release certification.

### Gate status

- Python quality: ✅ lint, formatting (108 files), strict typing (102 source files), complexity, 822 ordinary tests, 35 Temporal tests, and 90.385% core branch coverage on the final source.
- Frontend quality: ✅ 190 unit tests, 91.18% branch coverage, 27 desktop/mobile browser tests, and six isolated installed-wheel browser tests (navigation p95 182 ms). Catalog refresh, reconnect, automatic build reload, use-case checkmarks, and labeled event results verified.
- Security: ❌ verification incomplete; Aikido unavailable, vulnerability count unknown. This is not a finding of a known vulnerability.
- Requested backend/python-quality skill was unavailable at the checked active Codex skill locations. Repository commands were used; no integrations were installed or restored.

### Code review (critical/major only)

- No unresolved critical/major finding identified in the inspected paths after the fixes below; this is not an exhaustive assurance.
- `src/agentic_saga/temporal/workflow.py:317` — Corrected false verified recovery after a rejected refund; real Temporal regression requires human resolution before remaining compensation and proof.
- `src/agentic_saga/temporal/trace.py:78` — Corrected evidence classification: confirmed failure, unknown outcome, and ordinary reads now remain distinct.
- `src/agentic_saga/temporal/contracts.py:611` — Replaced exponential dependency traversal with standard-library `graphlib.TopologicalSorter`; dense 100-tool and disconnected-cycle regressions added.

### Craft findings

- Dead code: removed 119 lines comprising four unused adapter helpers, the obsolete ecommerce result/helper hierarchy, and its unused BDD fixture. Caller/export checks and 177 focused tests support the cleanup.
- API scope: `contracts/outcomes.py:132,137,258,263` contains four functions without repository callers; other contracts and journal helpers have test-only consumers. Retained pending an explicit API decision; lack of callers alone does not prove public APIs removable.
- Existing libraries: Temporal owns durability; Pydantic and ruamel.yaml own contracts/YAML; React, Vite, and Zod support the recorder. The project already uses established infrastructure.
- Low-risk simplification: `demo/assets.py:433` manually validates index dictionaries. Two strict Pydantic models could centralize shape validation while retaining path, digest, and uniqueness checks; no new dependency needed. [Pydantic models](https://docs.pydantic.dev/latest/concepts/models/)
- Duplication: HTTP status classification is repeated in `agents/pydanticai.py`, `agents/jev.py:242`, and `agents/openrouter_decisions.py:304`; share classification while retaining provider-specific exception handling.
- Resolved: the adapter now constructs a bare Pydantic AI `Agent` directly; the Pydantic Deep wrapper dependency is gone. Tool selection, deferred execution, and the two-request limit are covered by `tests/unit/agents/test_pydanticai.py`. [Deferred tools](https://ai.pydantic.dev/deferred-tools/)
- Runtime policy: `temporal/workflow.py:42` fixes all Activity timeouts at 30 seconds and retries at one/two attempts. Typed per-activity policy would support integrations with different latency requirements.
- Hardcoding: no checkout tool-name coupling found in reusable runtime modules. Fixture values, protocol identifiers, and server safety limits have deliberate roles.
- Infrastructure: the recorder's server and descriptor-based file writer enforce tested browser/filesystem boundaries. A framework swap needs equivalent behavior tests; adding packages solely to reduce line count is not justified.
- Onboarding: README, quickstart, changelog, license, operational guidance, and runnable examples are present; optional adapters are intentional product surfaces.

### Punch list to reach A

1. Obtain dependency/security scan evidence and resolve any high/critical findings before publication.
2. Verify documentation against the final API and run the repository's clean-commit release proof before claiming publish readiness.
3. Optional maintenance: decide which unused public-looking contracts remain supported, then simplify duplicated validation/error handling. Adapter/server migrations are separate design choices, not grade blockers.

### What's already great

- Deterministic transaction safety remains separate from model decisions, with stable operation IDs and authenticated recovery.
- Extensive behavior tests, strict typing, coverage gates, and installed-artifact checks make changes reviewable.
- The recorder now exposes all four scenarios and separates recorded outcome, replay position, and serving status.
