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
