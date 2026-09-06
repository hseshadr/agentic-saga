# Agentic Saga Project Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish an installable, strictly gated Python 3.12+ OSS repository that later runtime, agent, and console plans can extend without changing project conventions.

**Architecture:** Use a `src/` Python package built by hatchling, standard-library `argparse` for the CLI boundary, Pydantic for strict contracts, and `uv` plus Poe for one local/CI quality gate. Reuse the portfolio's SHA-pinned CI workflows and keep release work limited to reproducible local artifacts; this plan does not publish a package.

**Tech Stack:** Python 3.12+, Pydantic 2, standard-library argparse, hatchling, uv, pytest, pytest-bdd, Hypothesis, Ruff, mypy, Xenon, Poe, GitHub Actions

**Spec:** `docs/superpowers/specs/2026-09-06-agentic-saga-design.md`

## Global Constraints

- The public core must not require LangGraph, Deep Agents, or an OpenRouter client.
- Python support starts at 3.12; local and CI quality analysis targets Python 3.13.
- Apache-2.0 is the repository license.
- Ruff complexity is at most 5, Xenon must report A/A/A, and core branch coverage must remain at least 90%.
- CI must be deterministic, offline, and credential-free by default.
- GitHub Actions and shared `hseshadr/ci` references use immutable commit SHAs.
- The repository remains private, and no package is published without fresh explicit authorization.
- Documentation distinguishes shipped behavior, planned behavior, evidence, and limitations.

---

## File Map

- `pyproject.toml` — package metadata, optional extras, dependency groups, and canonical quality commands.
- `src/agentic_saga/__init__.py` — deliberately small public package surface.
- `src/agentic_saga/_version.py` — single package version source.
- `src/agentic_saga/cli/__init__.py` — CLI package marker.
- `src/agentic_saga/cli/main.py` — argparse root command; later plans attach demo commands here.
- `tests/test_package.py` — install/version contract.
- `tests/test_cli.py` — command-line boundary contract.
- `tests/test_repository_contract.py` — machine-checkable metadata, workflow, and documentation expectations.
- `README.md` — truthful private-development landing page until the working demo replaces it.
- `QUICKSTART.md` — exact current commands, with future commands explicitly labeled planned.
- `LICENSE` — Apache License 2.0 text.
- `CODE_OF_CONDUCT.md` — Contributor Covenant 2.1.
- `CONTRIBUTING.md` — environment, TDD loop, gate, and contribution workflow.
- `SECURITY.md` — private vulnerability-reporting and support policy.
- `PROVENANCE.md` — source/artifact boundary and current non-publication status.
- `CHANGELOG.md` — Keep a Changelog format with an unreleased section.
- `CITATION.cff` — project citation metadata.
- `.gitignore` — Python, uv, coverage, build, SQLite, trace, and frontend outputs.
- `.github/dependabot.yml` — weekly dependency updates without auto-merge.
- `.github/workflows/ci.yml` — shared Python gate and event-range secret scan.
- `.github/workflows/security-audit.yml` — scheduled full-history and locked-dependency audits.
- `scripts/build_release_artifacts.sh` — build exact-source wheel and sdist into an explicit directory.
- `scripts/verify_release_candidate.sh` — clean-install and smoke-test locally built artifacts.

### Task 1: Installable Package and CLI Boundary

**Files:**
- Create: `pyproject.toml`
- Create: `src/agentic_saga/__init__.py`
- Create: `src/agentic_saga/_version.py`
- Create: `src/agentic_saga/cli/__init__.py`
- Create: `src/agentic_saga/cli/main.py`
- Create: `tests/test_package.py`
- Create: `tests/test_cli.py`
- Create: `.gitignore`
- Create: `README.md`

**Interfaces:**
- Consumes: none.
- Produces: `agentic_saga.__version__: str`, `build_parser() -> argparse.ArgumentParser`, `main(argv: Sequence[str] | None = None) -> int`, and `entrypoint() -> NoReturn`; later plans extend the parser and dispatcher without replacing `--version`.

- [ ] **Step 1: Write failing package and CLI tests**

```python
# tests/test_package.py
import agentic_saga


def test_package_exposes_initial_version() -> None:
    assert agentic_saga.__version__ == "0.1.0"
```

```python
# tests/test_cli.py
from agentic_saga.cli.main import main


def test_cli_reports_version(capsys) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "agentic-saga 0.1.0"
```

- [ ] **Step 2: Run the focused tests and verify the import fails**

Run: `uv run --with pytest --with pydantic pytest tests/test_package.py tests/test_cli.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'agentic_saga'`.

- [ ] **Step 3: Create package metadata and quality configuration**

Create `pyproject.toml` with these exact core decisions:

```toml
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "agentic-saga"
dynamic = ["version"]
description = "Smart agent orchestration with deterministic Saga safety."
readme = "README.md"
requires-python = ">=3.12"
license = "Apache-2.0"
authors = [{ name = "Harish Seshadri" }]
dependencies = ["pydantic>=2.10,<3"]

[project.optional-dependencies]
deepagents = []
openrouter = []
demo = []

[project.scripts]
agentic-saga = "agentic_saga.cli.main:entrypoint"

[dependency-groups]
dev = [
  "hypothesis>=6.130,<7",
  "mypy>=1.17,<2",
  "poethepoet>=0.37,<1",
  "pytest>=9,<10",
  "pytest-asyncio>=1.2,<2",
  "pytest-bdd>=8,<9",
  "pytest-cov>=6,<8",
  "pytest-xdist>=3.8,<4",
  "ruff>=0.12,<1",
  "xenon>=0.9.3,<1",
]

[tool.hatch.version]
path = "src/agentic_saga/_version.py"

[tool.hatch.build.targets.wheel]
packages = ["src/agentic_saga"]

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "W", "C901", "B", "UP", "SIM", "N", "RUF", "ASYNC", "S", "PL"]
ignore = ["N818", "RUF036"]

[tool.ruff.lint.mccabe]
max-complexity = 5

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["PLR2004", "S101", "S603", "S607"]

[tool.mypy]
python_version = "3.13"
strict = true

[tool.pytest.ini_options]
addopts = "--strict-config --strict-markers"
asyncio_mode = "strict"
markers = ["live_model: requires network access and an OpenRouter API key"]
testpaths = ["tests"]

[tool.coverage.run]
branch = true
source = ["agentic_saga"]

[tool.poe.tasks]
lint-check = "ruff check src tests scripts"
format-check = "ruff format --check src tests scripts"
typecheck = "mypy --strict src tests"
complexity = "xenon --max-absolute A --max-modules A --max-average A src"
test = "pytest -m 'not live_model' --cov=src/agentic_saga --cov-branch --cov-fail-under=90"
gate = ["lint-check", "format-check", "typecheck", "complexity", "test"]
```

- [ ] **Step 4: Implement the minimum package and root command**

```python
# src/agentic_saga/_version.py
__version__ = "0.1.0"
```

```python
# src/agentic_saga/__init__.py
from agentic_saga._version import __version__

__all__ = ["__version__"]
```

```python
# src/agentic_saga/cli/main.py
import argparse
from collections.abc import Sequence
from typing import NoReturn

from agentic_saga import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentic-saga")
    parser.add_argument("--version", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.version:
        print(f"agentic-saga {__version__}")
    else:
        build_parser().print_help()
    return 0


def entrypoint() -> NoReturn:
    raise SystemExit(main())
```

Keep `src/agentic_saga/cli/__init__.py` empty. Create `.gitignore` entries for `.venv/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.coverage*`, `coverage.xml`, `htmlcov/`, `dist/`, `build/`, `*.egg-info/`, `*.sqlite*`, `.agentic-saga/`, `web/flight-recorder/node_modules/`, `web/flight-recorder/dist/`, and `test-results/`.

Create `README.md` with a TL;DR stating that the repository is under private development, the smart-agent/deterministic-kernel thesis, the current `uv sync --group dev && uv run agentic-saga --version` command, a link to the approved design, and an explicit “Not shipped yet” list for runtime, live-agent adapter, and console.

- [ ] **Step 5: Lock dependencies and run the focused tests**

Run: `uv lock && uv sync --group dev && uv run pytest tests/test_package.py tests/test_cli.py -q`

Expected: `2 passed`.

- [ ] **Step 6: Run formatting and type checks**

Run: `uv run ruff format src tests && uv run ruff check src tests && uv run mypy --strict src tests`

Expected: all commands exit 0.

- [ ] **Step 7: Commit the installable foundation**

```bash
git add pyproject.toml uv.lock .gitignore README.md src tests/test_package.py tests/test_cli.py
git commit -m "build: establish Python package foundation"
```

### Task 2: Governance, Security, and Truthful Project Documentation

**Files:**
- Create: `LICENSE`
- Create: `CODE_OF_CONDUCT.md`
- Create: `CONTRIBUTING.md`
- Create: `SECURITY.md`
- Create: `PROVENANCE.md`
- Create: `CHANGELOG.md`
- Create: `CITATION.cff`
- Create: `QUICKSTART.md`
- Create: `tests/test_repository_contract.py`

**Interfaces:**
- Consumes: the package and commands from Task 1.
- Produces: machine-checked repository policy and contributor entry points used by CI and final documentation.

- [ ] **Step 1: Write the failing repository contract tests**

```python
# tests/test_repository_contract.py
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_required_oss_files_exist() -> None:
    required = {
        "LICENSE",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "PROVENANCE.md",
        "CHANGELOG.md",
        "CITATION.cff",
        "QUICKSTART.md",
    }
    assert required <= {path.name for path in ROOT.iterdir()}


def test_docs_do_not_claim_unshipped_runtime() -> None:
    readme = (ROOT / "README.md").read_text()
    quickstart = (ROOT / "QUICKSTART.md").read_text()
    assert "Under private development" in readme
    assert "Planned command" in quickstart
```

- [ ] **Step 2: Run the contract tests and verify missing-file failures**

Run: `uv run pytest tests/test_repository_contract.py -q`

Expected: FAIL because the required files do not exist.

- [ ] **Step 3: Add the governance and security documents**

Copy the complete Apache License 2.0 text from <https://www.apache.org/licenses/LICENSE-2.0.txt> into `LICENSE`, preserving the canonical wording and appending no custom restrictions.

Copy Contributor Covenant 2.1 from `/Users/harish/dev/oss/edge-proc/CODE_OF_CONDUCT.md` to `CODE_OF_CONDUCT.md`; replace only the enforcement contact with `harish.seshadri@gmail.com` if the source differs.

Create `CONTRIBUTING.md` with these exact sections and commands:

```markdown
# Contributing

## Development setup
`uv sync --group dev`

## Test-driven changes
Write a failing behavior test, run it to observe the intended failure, make the smallest implementation pass, then refactor.

## Local quality gate
`uv run poe gate`

## Live-model tests
Live OpenRouter evaluations are optional, separately marked, and must never be required for transactional correctness.

## Pull requests
Keep changes focused, include tests and user-facing documentation together, and do not commit credentials, local databases, generated traces, or model transcripts containing sensitive data.
```

Create `SECURITY.md` with supported version `0.1.x` while private, private reporting to `harish.seshadri@gmail.com`, a request not to open public vulnerability issues, a 72-hour acknowledgement target, and explicit exclusions: v0.1 is a single-host reference backend and does not claim universal exactly-once delivery or arbitrary-tool safety.

Create `PROVENANCE.md` stating that source commits are authoritative, `uv.lock` freezes Python inputs, frontend lockfiles will freeze console inputs, CI builds from the checked-out commit, local release candidates are verified in clean environments, no registry artifacts exist yet, and publication requires a separate authorization plus trusted publishing.

Create `CHANGELOG.md` using Keep a Changelog headings with only `[Unreleased]`, containing `Added: Initial private design specification and project foundation`.

Create `CITATION.cff` with `cff-version: 1.2.0`, title `Agentic Saga`, type `software`, author `Harish Seshadri`, repository URL `https://github.com/hseshadr/agentic-saga`, license `Apache-2.0`, and version `0.1.0`.

- [ ] **Step 4: Add the truthful quickstart**

Create `QUICKSTART.md` with clone, `uv sync --group dev`, `uv run agentic-saga --version`, and `uv run poe gate` as current commands. Add a separately labeled section:

```markdown
## Planned command—not shipped yet

The approved v0.1 design will add:

`uv run agentic-saga demo --scenario inventory-exhausted --open`

Until that command has an executable integration test, it is roadmap behavior rather than a working quickstart.
```

- [ ] **Step 5: Run repository contracts and the full local gate**

Run: `uv run pytest tests/test_repository_contract.py -q && uv run poe gate`

Expected: repository contracts pass and the gate exits 0 with at least 90% branch coverage.

- [ ] **Step 6: Commit governance and documentation**

```bash
git add LICENSE CODE_OF_CONDUCT.md CONTRIBUTING.md SECURITY.md PROVENANCE.md CHANGELOG.md CITATION.cff QUICKSTART.md tests/test_repository_contract.py
git commit -m "docs: add OSS governance and trust boundaries"
```

### Task 3: Shared CI and Scheduled Security Controls

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/security-audit.yml`
- Create: `.github/dependabot.yml`
- Modify: `tests/test_repository_contract.py`

**Interfaces:**
- Consumes: `uv.lock`, the `gate` Poe task, and repository documents from Tasks 1–2.
- Produces: PR/push quality contexts and weekly full-history/dependency evidence. The Flight Recorder plan later adds the frontend gate and pnpm audit to these callers.

- [ ] **Step 1: Add failing workflow-policy tests**

Append:

```python
def test_workflows_use_sha_pinned_shared_ci() -> None:
    workflows = "\n".join(
        path.read_text() for path in (ROOT / ".github" / "workflows").glob("*.yml")
    )
    shared_ref = "@8166345c9355dde54c12fa95d0457c4ea97d3e64"
    assert workflows.count(shared_ref) >= 4
    assert "@main" not in workflows
    assert "@ci-v" not in workflows.replace("# ci-v3.3.0", "")


def test_security_schedule_requests_full_history() -> None:
    workflow = (ROOT / ".github/workflows/security-audit.yml").read_text()
    assert 'cron: "17 8 * * 1"' in workflow
    assert "full-history: true" in workflow
    assert "run-python-audit: true" in workflow
```

- [ ] **Step 2: Run the focused tests and verify missing-workflow failure**

Run: `uv run pytest tests/test_repository_contract.py -q`

Expected: FAIL reading `.github/workflows/security-audit.yml`.

- [ ] **Step 3: Add the PR and push workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: read

jobs:
  python:
    name: Python gate
    uses: hseshadr/ci/.github/workflows/python-gate.yml@8166345c9355dde54c12fa95d0457c4ea97d3e64 # ci-v3.3.0
    with:
      python-version: "3.13"
      sync-args: "--frozen --group dev"

  secrets:
    name: Secret scan
    uses: hseshadr/ci/.github/workflows/secret-scan.yml@8166345c9355dde54c12fa95d0457c4ea97d3e64 # ci-v3.3.0
```

- [ ] **Step 4: Add the weekly security workflow**

Create `.github/workflows/security-audit.yml`:

```yaml
name: Security audit

on:
  schedule:
    - cron: "17 8 * * 1"
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: read

jobs:
  history:
    name: Full-history secret scan
    uses: hseshadr/ci/.github/workflows/secret-scan.yml@8166345c9355dde54c12fa95d0457c4ea97d3e64 # ci-v3.3.0
    with:
      full-history: true

  dependencies:
    name: Locked dependency audit
    uses: hseshadr/ci/.github/workflows/security-audit.yml@8166345c9355dde54c12fa95d0457c4ea97d3e64 # ci-v3.3.0
    with:
      run-python-audit: true
```

- [ ] **Step 5: Add weekly Dependabot configuration**

Create `.github/dependabot.yml` with weekly Monday updates for `pip` and `github-actions`, a limit of five open pull requests per ecosystem, and labels `dependencies` plus the ecosystem name. Do not add auto-merge configuration; portfolio policy forbids automatic Dependabot merges.

- [ ] **Step 6: Verify workflows locally**

Run: `uv run pytest tests/test_repository_contract.py -q && actionlint .github/workflows/*.yml && uvx zizmor==1.29.0 --persona=auditor --min-severity=low --offline --strict-collection .github`

Expected: all commands exit 0. If `actionlint` is unavailable, install it with the platform package manager and rerun; do not omit the check.

- [ ] **Step 7: Commit shared CI callers**

```bash
git add .github tests/test_repository_contract.py
git commit -m "ci: add shared quality and security gates"
```

### Task 4: Reproducible Local Release Candidate

**Files:**
- Create: `scripts/build_release_artifacts.sh`
- Create: `scripts/verify_release_candidate.sh`
- Create: `tests/test_release_contract.py`
- Modify: `pyproject.toml`
- Modify: `PROVENANCE.md`

**Interfaces:**
- Consumes: installable package, lockfile, and quality gate from prior tasks.
- Produces: `scripts/build_release_artifacts.sh OUTPUT_DIR` and `scripts/verify_release_candidate.sh OUTPUT_DIR`, later used by final release verification without publishing.

- [ ] **Step 1: Write failing release-contract tests**

```python
# tests/test_release_contract.py
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_release_scripts_are_strict_and_non_publishing() -> None:
    scripts = [
        ROOT / "scripts" / "build_release_artifacts.sh",
        ROOT / "scripts" / "verify_release_candidate.sh",
    ]
    for script in scripts:
        content = script.read_text()
        assert "set -euo pipefail" in content
        assert "twine upload" not in content
        assert "uv publish" not in content


def test_build_configuration_includes_trust_files() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert '"/LICENSE"' in pyproject
    assert '"/SECURITY.md"' in pyproject
    assert '"/PROVENANCE.md"' in pyproject
```

- [ ] **Step 2: Run tests and verify the scripts are missing**

Run: `uv run pytest tests/test_release_contract.py -q`

Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Add exact build inclusion rules**

Append to `pyproject.toml`:

```toml
[tool.hatch.build.targets.sdist]
include = [
  "/LICENSE",
  "/README.md",
  "/QUICKSTART.md",
  "/SECURITY.md",
  "/PROVENANCE.md",
  "/pyproject.toml",
  "/src/agentic_saga",
]
```

Add Poe tasks:

```toml
artifacts = "bash scripts/build_release_artifacts.sh dist/release"
release-candidate = "bash scripts/verify_release_candidate.sh dist/release"
```

- [ ] **Step 4: Implement the artifact builder**

Create executable `scripts/build_release_artifacts.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:?usage: build_release_artifacts.sh OUTPUT_DIR}"
rm -rf "$output_dir"
mkdir -p "$output_dir"
uv build --out-dir "$output_dir"
test "$(find "$output_dir" -name '*.whl' | wc -l | tr -d ' ')" = "1"
test "$(find "$output_dir" -name '*.tar.gz' | wc -l | tr -d ' ')" = "1"
```

- [ ] **Step 5: Implement clean-install verification**

Create executable `scripts/verify_release_candidate.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:?usage: verify_release_candidate.sh OUTPUT_DIR}"
bash scripts/build_release_artifacts.sh "$output_dir"
wheel="$(find "$output_dir" -name '*.whl' -print -quit)"
venv="$(mktemp -d)/venv"
uv venv --python 3.13 "$venv"
uv pip install --python "$venv/bin/python" "$wheel"
actual="$($venv/bin/agentic-saga --version)"
test "$actual" = "agentic-saga 0.1.0"
```

Mark both files executable. Extend `PROVENANCE.md` with `uv run poe release-candidate` as the exact local proof and repeat that the command builds but does not publish.

- [ ] **Step 6: Run the release proof and full quality gate**

Run: `uv run pytest tests/test_release_contract.py -q && uv run poe release-candidate && uv run poe gate`

Expected: one wheel and one sdist are produced, the clean-installed CLI reports `agentic-saga 0.1.0`, and the full gate exits 0.

- [ ] **Step 7: Commit release-candidate tooling**

```bash
git add pyproject.toml uv.lock scripts tests/test_release_contract.py PROVENANCE.md
git commit -m "build: add reproducible release candidate proof"
```

## Plan Completion Gate

After all four tasks:

1. Run `uv run poe gate`.
2. Run `uv run poe release-candidate`.
3. Run `actionlint .github/workflows/*.yml`.
4. Run the Python quality skill's complete checks.
5. Confirm `git status --short` is empty.
6. Push the implementation branch and require CI before merge.
7. Do not publish to PyPI and do not make the repository public.
