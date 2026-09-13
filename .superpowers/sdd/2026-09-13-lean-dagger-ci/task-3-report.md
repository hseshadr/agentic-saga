# Task 3 report — bounded content-addressed Dagger gates

## Outcome

The Dagger adapter now resolves and guards the canonical source once, then executes two bounded
phases. The first phase builds one immutable first-party release directory and one frontend
coverage-proof directory concurrently. The second phase runs the ordered Python 3.12 and 3.13
release lanes concurrently with a hard limit of two active awaitables.

Python dependency layers receive only `pyproject.toml` and `uv.lock`, install locked dependencies
with `--no-install-project`, and receive the complete source only before the offline project sync.
Frontend dependency layers likewise receive only `package.json` and `pnpm-lock.yaml`, install
Chromium before the complete source overlay, and use no mutable Dagger cache volume.

Each runtime lane now performs the required order: Python gate, immutable artifact mount,
runtime-specific wheelhouse verification/build, frontend coverage mount, identity-bound quality
proof creation, and release measurement with `--quality-proof`. Measurement receives
`wheelhouses/3.12` or `wheelhouses/3.13` explicitly. The security entry point delegates its single
Foundation guard to `python-package.dependency_audit()` and still awaits that audit before the
frontend dependency audit. Public signatures and both success messages are unchanged.

## TDD evidence

The first collection run failed because `_bounded_gather` did not exist. The test import was then
adjusted so the complete RED surface could execute rather than stopping at collection.

RED:

```text
uv run --directory .dagger pytest tests/test_orchestration.py tests/test_public_contracts.py -q
7 failed, 36 passed
```

The seven intended failures identified the absent bounded fan-out, missing build-once artifacts,
full-source dependency inputs, shared/unversioned wheelhouse use, absent quality-proof handoff,
and duplicate explicit security guard. The bounded tests use real asyncio awaitables and prove
both the two-operation ceiling and concrete lane-failure propagation.

GREEN after implementation and contract reconciliation:

```text
uv run --directory .dagger pytest tests/test_orchestration.py tests/test_public_contracts.py -q
40 passed in 1.13s
```

## Verification

```text
DAGGER_NO_NAG=1 dagger develop
passed (Dagger v0.21.8 module generation)

uv run --directory .dagger poe gate
ruff/format passed; mypy strict passed; Xenon A/A/A passed;
40 tests passed; 100% line and branch coverage for the adapter

uv run --directory .dagger poe audit
No known vulnerabilities found
```

The audit correctly skips the two local, unpublished distributions (`agentic-saga-ci` and the
generated local `dagger-io` SDK). `git diff --check` is clean. Every production function is at most
11 source lines and the full adapter remains Radon/Xenon Grade A.

## Files changed

- `.dagger/src/agentic_saga_ci/main.py`
- `.dagger/tests/test_orchestration.py`
- `.dagger/tests/test_public_contracts.py`

## Concern

Task 3 validates generated Dagger interfaces and the complete local adapter gate, but intentionally
does not run the expensive authenticated real `dagger call ci/security` smoke. That proof and the
removal of the legacy fake runtime are explicitly assigned to Task 4.
