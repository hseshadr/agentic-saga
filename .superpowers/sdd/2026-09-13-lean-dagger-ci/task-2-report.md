# Task 2 report: identity-bound quality proofs

## Outcome

Implemented a fail-closed, versioned quality-proof handoff. Release measurement now accepts a
quality result only through `--quality-proof PATH`; without it, it continues to run the complete
Python and frontend quality gates. There is no skip-quality path.

`scripts/quality_proof.py` uses frozen Pydantic models at the JSON boundary, canonical atomic
writes, and independent recomputation of the full commit, clean-tree state, Python major/minor,
four lock/input digests, three coverage-evidence digests, completed gate names, and evaluated
budget results. Any schema, identity, evidence, check, or result mismatch raises a concrete
`ValueError`.

## TDD evidence

The initial focused test run was red as intended because `scripts.quality_proof` did not exist:

```text
ModuleNotFoundError: No module named 'scripts.quality_proof'
```

The green proof-contract tests cover valid reuse without rerunning gates; source/Python/lockfile
identity tampering; unknown, missing, and extra schema keys; duplicate/missing/reordered completed
checks; non-finite results; modified coverage evidence; abbreviated commits; dirty trees; atomic
replacement cleanup; measurement reuse; and the command-line boundary.

## Scope expansion

The approved ownership expansion added `tests/test_repository_contract.py`. Its exact
`typecheck-release` command contract now requires `scripts/quality_proof.py`, while preserving the
existing Python 3.12 toolchain-floor assertions. The complexity task is also extended in
`pyproject.toml`.

## Verification

```text
uv run pytest tests/test_measure_release.py tests/test_release_measurement_contract.py -q
130 passed

uv run poe release-script-test && uv run poe release-script-coverage
130 passed; total coverage 93.85%; release-script branch coverage 93.077%

uv run pytest tests/test_repository_contract.py tests/test_measure_release.py tests/test_release_measurement_contract.py -q
189 passed

uv run poe typecheck-release
Success: no issues found in 4 source files

uv run poe complexity
passed

uv run poe lint-check && uv run poe format-check
passed
```

## Concern

An unrelated concurrent edit remains in
`docs/superpowers/plans/2026-09-08-lean-architecture-hardening.md`; it is deliberately excluded
from the Task 2 commit.

## Fix round 1: strict proof schema and coverage enforcement

Review follow-up tightened the release surface and proof boundary:

- `release-script-test` now covers `scripts.quality_proof`, and the repository contract requires
  that exact coverage target.
- The frozen Pydantic proof models use strict scalar types. Every result field is required,
  including nullable `p50`, `maximum`, and `lower`; JSON arrays are structurally converted only
  after exact-shape validation.
- Exact schema validation now covers every result object before Pydantic parsing. Numeric strings,
  missing fields, and unknown keys fail closed.
- Result names are checked before any budget-table lookup, so an unknown name is a `ValueError`
  and the measurement CLI reports exit status 2 instead of leaking a `KeyError`.
- All Task 2 test helpers and tests touched in this round are limited to 15 lines or fewer and use
  Given/When/Then sections.

### RED evidence

Before the fix, the new regressions produced seven failures: omitted `p50`, `maximum`,
`comparison`, or `lower` fields were accepted; string numeric `actual` was coerced; an unknown
result name reached the budget lookup as `KeyError`; and the CLI propagated that error. After
adding quality-proof coverage, the existing branch guard reported 89.157%, proving the expanded
surface was not yet protected at the required floor.

### GREEN evidence

```text
uv run pytest tests/test_measure_release.py tests/test_release_measurement_contract.py tests/test_repository_contract.py -q
204 passed

uv run poe release-script-test && uv run poe release-script-coverage
145 passed; total coverage 94.80%; release-script branch coverage 93.373%

uv run poe typecheck-release
Success: no issues found in 4 source files

uv run poe complexity
passed

uv run poe lint-check && uv run poe format-check
passed
```
