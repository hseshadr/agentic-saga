# Provenance and release status

TL;DR: source commits are authoritative. A release candidate is one immutable wheel and source
archive built from a clean exact commit, verified offline against hash-pinned inputs, measured
against the [operations contract](docs/operations.md), and matched to hosted CI. The project is
private and no package has been published.

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

The final clean-current-commit measurement and matching hosted workflow URL have not yet been
recorded. A dirty-tree measurement is diagnostic only, even when every individual budget passes.
No registry artifacts exist yet, and none of these commands publishes one.

## Repository controls

`.github/workflows/ci.yml` defines Python gates on 3.12 and 3.13, the frozen Node 24/pnpm 11.5.0
frontend gate, a packaged-recorder matrix on both Python versions, hash-verified offline wheel
installation, packaged Chromium proof, release measurement, and secret scanning for pushes and pull requests.
`.github/workflows/security-audit.yml` schedules full-history secret and locked Python/pnpm
dependency audits. Shared workflows and setup actions are pinned to the full
`hseshadr/ci` commit `8166345c9355dde54c12fa95d0457c4ea97d3e64`, documented upstream as
`ci-v3.3.0`; Playwright itself installs Chromium directly because that release's Playwright
composite does not validate on the current runner. Checkout credentials remain disabled and jobs
use read-only permissions.

Configuration is not execution evidence. No hosted run is claimed here. Before calling a release
reviewed and ready, record the hosted workflow URL, exact commit, artifact digests, Python 3.12/3.13
wheel results, packaged-demo browser result, dependency and secret scans, and measurement report
together. Hosted deployment availability is N/A because v0.1 is a non-hosted library and local
loopback viewer.

## Publication boundary

Publication requires separate, fresh authorization and trusted publishing from the reviewed
immutable artifact. This private development workflow does not publish to PyPI or another package
registry. Opening the optional OpenRouter evaluation route also requires separate explicit consent;
its output is model-quality evidence, not release correctness evidence.
