# Contributing

## Development setup

New here? Start with [Getting started for developers](docs/GETTING_STARTED.md).

Agentic Saga supports Python 3.12 and 3.13 on POSIX systems. Install the locked Python development
toolchain:

`uv sync --group dev`

## Test-driven changes
Write a failing behavior test, run it to observe the intended failure, make the smallest implementation pass, then refactor.

## Local quality gate

`uv run poe gate`

If you change the Flight Recorder, also run its pinned frontend gate:

```bash
cd web/flight-recorder
npx --yes pnpm@11.5.0 install --frozen-lockfile
npx --yes pnpm@11.5.0 gate
```

## Live-model tests
Live OpenRouter evaluations are optional, separately marked, and must never be required for transactional correctness.

Socket-using tests are also optional and must stay out of the default gate. Use `uv run poe test-network` to opt in to the `network` marker with sockets enabled.

## Pull requests

Keep changes focused, include tests and user-facing documentation together, and do not commit credentials, local databases, generated traces, or model transcripts containing sensitive data.

Follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report vulnerabilities through the private route
in [SECURITY.md](SECURITY.md), never through a public issue.
