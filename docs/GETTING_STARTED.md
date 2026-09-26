# Getting started for developers

TL;DR: install Python 3.13 and uv, run `uv sync --group dev`, then `uv run poe gate`. If that is
green you have the same Python checks CI runs. Every command below was run from a fresh clone on
macOS (Apple silicon) on 2026-09-25; the times are what it took there, on a machine that was
busy with other work, so expect yours to be faster.

## 1. Prerequisites

| Tool | Version | How to get it |
| --- | --- | --- |
| Python | 3.12 or 3.13 (CI uses 3.12.12 and 3.13.14) | uv installs it for you if it is missing |
| [uv](https://docs.astral.sh/uv/) | 0.8 or newer (tested with 0.8.5) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Temporal CLI](https://docs.temporal.io/cli) | any current | `brew install temporal`. Only for running examples against a local server; the tests do not need it |
| Node.js | 24 or newer (tested with 26.5) | Only if you change the browser replay app in `web/flight-recorder/` |

Linux or macOS only. Windows is not supported.

Local traps we hit:

- The first test run downloads Temporal's test-server program. The first `uv run poe gate` on a
  new machine is slower than later ones.
- Example runs print a `WARN temporalio_sdk_core::worker ... Temporal Server 1.16.0 or newer`
  line. It is harmless.
- The `lost-response` and `compensation-failure` scenarios print Python tracebacks ending in
  `BusinessToolAdapterFailure`. Those are the simulated failures, not a bug.
- Tests run with sockets blocked (`--disable-socket`). A test that needs the network must be marked
  and run with `uv run poe test-network`; it is never part of the default checks.

## 2. Clone, install, run

```bash
git clone https://github.com/hseshadr/agentic-saga.git   # 2 s
cd agentic-saga
uv sync --group dev                                        # 7 s with an empty uv cache
```

Success looks like uv listing the installed packages and ending with no error.

Run one test file to check your setup:

```bash
uv run pytest -q tests/unit/temporal/test_journal.py       # 6 s
```

```text
...........                                                              [100%]
11 passed in 3.47s
```

Run the four checkout scenarios end to end. This starts a throwaway Temporal test server, so you
need nothing else running:

```bash
uv run pytest -q -m temporal --force-enable-socket tests/bdd/steps/test_ecommerce_saga.py   # 13 s
```

```text
4 passed, 1 deselected, 1 warning in 9.73s
```

To run the example against a real local Temporal server, start one in another terminal with
`temporal server start-dev`, then:

```bash
uv run python -m examples.ecommerce.run business-failure   # 1 s once the server is up
```

It ends with `Outcome: compensated_verified · provider effects: 6`.

## 3. The full check

```bash
uv run poe gate
```

This is the Python part of what CI runs: ruff lint and format, `mypy --strict`, Xenon complexity
(grade A everywhere), the offline tests, the Temporal tests, and branch coverage of at least 90% on
the core and on the release scripts. It took 3 minutes 50 seconds from a fresh clone.

If you touch `web/flight-recorder/`, also run its check:

```bash
cd web/flight-recorder
npx --yes pnpm@11.5.0 install --frozen-lockfile   # 9 s
npx --yes pnpm@11.5.0 gate                        # 35-50 s
```

This runs lint, type checks, unit tests with coverage, the build, and browser tests. On our
laptop it failed twice, both times on one slow test (`story-view.test.tsx` "bounds a large story",
which hit its 5-second timeout while the machine was very busy); the other 199 tests passed. The
browser tests have also failed once in CI on `main` (`app.test.tsx`, a focus check). If you hit
one of these, rerun before assuming your change broke it.

CI runs all of this again inside [Dagger](https://docs.dagger.io) (see `.dagger/`), on Python 3.12
and 3.13, then builds the wheel and source archive and installs and tests them.

## 4. Map of the code

| Path | What it is |
| --- | --- |
| `src/agentic_saga/temporal/workflow.py` | The Workflow: checks each proposal, enforces limits, runs undo in reverse order, pauses for a person |
| `src/agentic_saga/temporal/activities.py` | The Activities that call the agent and your tools, and reconcile lost replies |
| `src/agentic_saga/temporal/client.py`, `worker.py` | Helpers to connect to Temporal, start a saga, and build a Worker |
| `src/agentic_saga/contracts/` | Typed, size-limited values that cross every boundary (tool calls, outcomes, traces) |
| `src/agentic_saga/agents/` | Decision adapters: Pydantic AI through OpenRouter, Jev |
| `src/agentic_saga/manifest.py` | Loads and checks `saga.yaml` |
| `src/agentic_saga/cli/`, `src/agentic_saga/demo/` | The `agentic-saga demo` command and its local replay server |
| `examples/ecommerce/` | The checkout example: simulated providers, scripted agent, runner |
| `tests/unit/`, `tests/integration/`, `tests/bdd/` | Fast unit tests, cross-module tests, and the four checkout scenarios in plain language |
| `web/flight-recorder/` | The browser replay app (TypeScript) |

More on how the pieces fit: [Architecture](ARCHITECTURE.md).

## 5. Make your first change

A typical small change: tighten a safety limit on the values the agent can send. The limits live
at the top of `src/agentic_saga/contracts/common.py` (for example `_MAX_JSON_DEPTH`), and their
tests live in `tests/unit/contracts/test_common_bounds.py`.

1. Branch: `git switch -c fix/json-depth-limit`.
2. Write the failing test first. Copy an existing test in `test_common_bounds.py`, such as
   `test_should_reject_extreme_receipt_depth_without_recursion_error`, and change it to assert
   the new limit.
3. Run just that test and watch it fail:

   ```bash
   uv run pytest -q tests/unit/contracts/test_common_bounds.py -k receipt_depth   # 6 s
   ```

4. Change the constant in `common.py`, run the same command, and watch it pass.
5. Run `uv run poe gate` before pushing.

Changing Workflow behavior works the same way, with the test in `tests/unit/temporal/` or a new
scenario in `tests/bdd/features/ecommerce_saga.feature`. If you change what the README says, update
`tests/test_readme_contract.py` too: it pins the README's section order, its real example output,
and a list of words to avoid.

## 6. Open a pull request

- Branch names use a type prefix: `fix/...`, `feat/...`, `docs/...`, `ci/...`, `chore/...`.
  Commit messages follow the same style, for example `fix(ci): ...` or `docs: ...`.
- Push and open a PR against `main`. The `Dagger` workflow runs on every PR: the Python checks on
  3.12 and 3.13, the replay app's checks and browser tests, and a build, install, and test of the
  wheel and source archive. A separate weekly workflow audits dependencies.
- Reviewers look for a test that fails without your change, green CI, no new complexity above
  grade A, and docs updated in the same PR when behavior changes. Never commit keys, `.env`,
  generated traces, or `dist/`.

See [CONTRIBUTING.md](../CONTRIBUTING.md) and the [security policy](../SECURITY.md).
