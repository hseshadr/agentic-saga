# Provenance and release status

TL;DR: source commits are authoritative. A release candidate is one immutable wheel and source
archive built from a clean exact commit, verified offline against hash-pinned inputs, measured
against the [operations contract](docs/operations.md), and matched to hosted CI. The project is
not published to a package registry.

The committed `uv.lock` freezes Python inputs. `web/flight-recorder/pnpm-lock.yaml` freezes
frontend inputs and `package.json` pins pnpm 11.5.0. The locked `lossless-json` parser preserves
numeric lexemes required to verify Python canonical evidence hashes without JavaScript precision
loss.

## Current evidence

The release machinery is implemented. These are the local entry points:

```bash
uv run poe gate
uv run poe release-candidate
uv run python scripts/measure_release.py
```

The local `uv run poe gate` enforces lint, formatting, strict types, Xenon grade A, offline tests,
and separate true branch floors for the kernel and release scripts. The release-candidate command
builds exactly one wheel and one source archive from an isolated `git archive` of committed `HEAD`,
records and checks their SHA-256 digests, installs the wheel and hash-pinned runtime dependencies
offline in a clean environment matching the active supported Python (3.12 or 3.13), verifies the
CLI version, and proves optional-provider import isolation. The build stage downloads a
platform-specific wheelhouse with pip's hash checking; verification then disables indexes,
network access, and Python downloads, so it does not depend on an ambient package cache. Package tests
also compare the wheel's static/reference bytes exactly and require the source archive's public
documents and safe relative links.

`scripts/measure_release.py` is the fail-closed executable form of the published operations budget.
It records the full commit, dirty/clean state, OS, CPU, Python, Node, and pnpm; runs the Python and
frontend gates; builds and installs a wheel offline from hash-pinned inputs; exercises all four
reference scenarios and the packaged Chromium recorder; and reports sample counts, p50/p95/max
latencies, bundle/catalog sizes, peak RSS, boundary limits, and a result for every budget. It makes
no paid model call.

Audited baseline commit `3fcf10ea6a6dbd2799f242758cecbbd6321ff639` passed hosted
[Dagger run 34807057405](https://github.com/hseshadr/agentic-saga/actions/runs/34807057405)
and [security run 34859242334](https://github.com/hseshadr/agentic-saga/actions/runs/34859242334).
The Dagger run covered the complete Python 3.12/3.13 and packaged-browser release matrix and prints
its validated SHA-256 manifest in the run log. It uploaded no GitHub artifact, so the log is digest
evidence rather than hosted wheel/sdist distribution. Only green checks bound to the release
candidate's own exact head count as final evidence. A dirty-tree measurement remains diagnostic
only. No registry artifacts exist, and none of these commands publishes one.

## Repository controls

`.github/workflows/dagger.yml` and `.github/workflows/dagger-security.yml` are compact, read-only
ingress into the version-pinned Dagger graph. The graph composes shared Foundation and Python-package
controls from exact `hseshadr/ci` commit `5cf3b7550442bb06d1cce1f146e48c064dcf511c` with the
product-specific Python, frontend, packaged-browser, and release-measurement gates. Checkout
credentials remain disabled.

Branch protection requires the exact GitHub Actions `Dagger` check, enforces administrator rules
and conversation resolution, and disallows force pushes and branch deletion. The run links above
bind execution evidence to the exact main commit. Hosted deployment availability is N/A because
v0.1 is a non-hosted library with a local loopback viewer.

## Publication boundary

Publication requires separate, fresh authorization and trusted publishing from the reviewed
immutable artifact. The source release workflow does not publish to PyPI or another package
registry. Opening the optional OpenRouter evaluation route also requires separate explicit consent;
its output is model-quality evidence, not release correctness evidence.
