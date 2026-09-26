# Agentic Saga

A Python library that lets an AI agent run multi-step jobs like a checkout, and undoes finished steps if one fails.

**Try it:** `git clone https://github.com/hseshadr/agentic-saga && cd agentic-saga && uv sync --group dev`, then follow [Try it](#try-it).

[![CI](https://github.com/hseshadr/agentic-saga/actions/workflows/dagger.yml/badge.svg)](https://github.com/hseshadr/agentic-saga/actions/workflows/dagger.yml)
[![License](https://img.shields.io/github/license/hseshadr/agentic-saga)](LICENSE)

A checkout touches several outside services: reserve the stock, charge the card, book the
delivery. If the delivery company says no after the card was charged, someone has to refund the
card and release the stock, in that order, exactly once. Doing that by hand in code is fiddly.
Letting an AI agent do it is worse: you do not want a language model deciding when to refund or
whether a lost response means "paid" or "not paid".

Agentic Saga splits the job. The agent only picks the next step from a short list of steps that
are allowed right now. Ordinary Python rules, run by [Temporal](https://temporal.io) (an
open-source service that records every step, so a crash does not lose work), check that choice,
run the step, and undo the finished steps newest-first if the job cannot be proven done. If even
the undo cannot be proven, it stops and waits for a person.

**Technical docs:** [Architecture](docs/ARCHITECTURE.md) · [Getting started for developers](docs/GETTING_STARTED.md) · [Safety rules](docs/temporal-safety-contract.md) · [Operations](docs/operations.md)

## Try it

This runs a pretend checkout on your machine. It needs Python 3.12 or 3.13 and
[uv](https://docs.astral.sh/uv/). No account or API key. The "agent" here is a scripted stand-in,
so no AI model is called.

1. Get the code and install it:

   ```bash
   git clone https://github.com/hseshadr/agentic-saga.git
   cd agentic-saga
   uv sync --group dev
   ```

2. Save this as `try_saga.py` in the `agentic-saga` folder. In this scenario the delivery company
   rejects the order after accepting it.

   ```python
   import asyncio

   from examples.ecommerce.demo import run_scenario

   run = asyncio.run(run_scenario("business-failure"))

   print("Agent chose:", " -> ".join(run.proposals))
   print("Undo order: ", " -> ".join(run.compensation_order))
   print("Outcome:    ", run.state.status.value)
   ```

3. Run it with `uv run python try_saga.py`. The output:

   ```text
   WARN temporalio_sdk_core::worker: Temporal Server 1.16.0 or newer is required ...
   Agent chose: reserve_inventory -> charge_payment -> schedule_fulfillment -> verify_order
   Undo order:  cancel_fulfillment -> refund_payment -> release_inventory
   Outcome:     compensated_verified
   ```

   The agent walked forward through the checkout. The final check failed, so the three finished
   steps were undone newest-first and the result was checked again. The `WARN` line comes from
   Temporal's local test server and is harmless. The first run downloads that test server once,
   so it can take a minute.

4. Now watch a lost response. Install the [Temporal CLI](https://docs.temporal.io/cli), start a
   local server with `temporal server start-dev`, and in a second terminal run:

   ```bash
   uv run python -m examples.ecommerce.run lost-response
   ```

   ```text
   Agent proposals: reserve_inventory → charge_payment → schedule_fulfillment → verify_order → finish
   01  saga_started
   03  effect_outcome_recorded  reserve_inventory
   05  effect_outcome_recorded  charge_payment
   06  reconciliation_recorded  charge_payment
   08  effect_outcome_recorded  schedule_fulfillment
   10  invariant_evaluated  verify_order
   12  terminal_assigned
   Outcome: succeeded_verified · provider effects: 3
   Order succeeded. Verified complete.
   ```

   The payment went through but its reply was lost. Instead of charging again, the job asked the
   payment provider what happened (`reconciliation_recorded`) and carried on. Three steps, three
   effects: nobody was charged twice. Above this output you will also see two Python tracebacks
   ending in `BusinessToolAdapterFailure`. That is the simulated lost reply, and it is expected.

5. To replay recorded runs in your browser, run
   `uv run --no-dev agentic-saga demo --scenario business-failure --open` and click
   **Watch from start**. Press Ctrl+C to stop it.

## How it works

Your code registers each step as a typed Python function, and pairs every step that changes
something with the step that undoes it (charge with refund, reserve with release). On each turn the
agent sees the goal, the current facts, and only the steps allowed right now, and proposes one.
A Temporal Workflow checks the proposal against those rules and limits, runs the step, and records
the result. If a reply is lost, it asks the provider what happened before doing anything else. The
job only counts as done after a fresh check proves it; otherwise every finished step is undone in
reverse order, and a person is asked only when the undo itself cannot be proven.

The agent can be the scripted driver used above, a model reached through
[Pydantic AI](https://ai.pydantic.dev) and OpenRouter, or Jev (a decision engine that ranks a
fixed list of choices your app builds). See the
[agent adapter guide](docs/agent-adapter.md).

## What it does not do

- **It is not released.** Version 0.1.0 is not on PyPI and has no tagged release. Install it from
  source.
- **It cannot make an outside service safe to retry.** Your payment or stock service must accept a
  stable request ID so a repeated call is recognized. The example services are simulations.
- **It needs a Temporal server.** The examples start a throwaway one for you. Real use needs your
  own (`temporal server start-dev` locally) or Temporal Cloud.
- **It does not judge the AI model.** Tests never call a paid model. How well a live model picks
  steps is not tested here.
- **Anything you put in a step's inputs is stored in Temporal.** Use Temporal's payload encryption
  for private data. With the optional AI agent on, the goal, the current facts, and the allowed
  steps are sent to OpenRouter with your key.
- **Recorded runs are hash-checked, not signed.** Someone who can edit a trace file can rewrite it.
- Python 3.12 or 3.13 on Linux or macOS only.

## When to use something else

| If you need | Use |
| --- | --- |
| Every change lives in one database | A database transaction |
| A fixed sequence of steps that rarely changes | Temporal on its own |
| An agent that only reads or drafts, and nothing it does is hard to undo | An agent framework calling tools directly |
| A few routes you can write out by hand | Plain `if/else` code |
| An agent picking steps that change real things, with undo and proof built in | Agentic Saga |

## Install

Not on PyPI yet. Install from source with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/hseshadr/agentic-saga.git
cd agentic-saga
uv sync --group dev                         # library, tests, and the example
uv sync --extra agent --group dev           # + Pydantic AI through OpenRouter
uv sync --extra jev --group dev             # + Jev (TypeSafe API)
uv sync --extra jev-openrouter --group dev  # + Jev through OpenRouter Decisions
```

For the optional AI agent, `cp .env.example .env` and add your `OPENROUTER_API_KEY`. The library
never reads settings on its own; your application does. See
[Architecture: configuration](docs/ARCHITECTURE.md#configuration) and the
[Quickstart](QUICKSTART.md) for the full walkthrough, and
[Architecture: minimal integration shape](docs/ARCHITECTURE.md#minimal-integration-shape) for how
to wire it into your own app.

## Develop

```bash
uv run poe gate
```

This runs the Python checks CI runs: formatting, lint, strict typing, complexity, offline tests,
Temporal tests, and at least 90% branch coverage on the core. It took about 4 minutes on a laptop.
The browser replay app in `web/flight-recorder/` has its own check. Start with
[Getting started for developers](docs/GETTING_STARTED.md), then [CONTRIBUTING.md](CONTRIBUTING.md).

## More detail

- [Getting started for developers](docs/GETTING_STARTED.md): from a fresh clone to a green local
  build and your first change.
- [Architecture](docs/ARCHITECTURE.md): the flow, the building blocks, the source map, security,
  and what the tests prove.
- [Explore the interactive architecture map](docs/architecture/index.html).
- [Temporal safety contract](docs/temporal-safety-contract.md): the exact rules the Workflow
  enforces.
- [Operations](docs/operations.md): running Workers, retries, human pauses, and known limits.
- [Agent adapters](docs/agent-adapter.md): the scripted driver, Pydantic AI, and Jev.
- [Context manifest](docs/context-manifest.md): describing a job to the agent in `saga.yaml`.
- [Flight Recorder](docs/flight-recorder.md): the browser replay of recorded runs.
- [ADR 0001](docs/adr/0001-temporal-runtime.md): why Temporal is the only durability engine.
- [Quality review, 2026-09-19](docs/NORTHSTAR-REVIEW.md): an internal review and its findings.
- [Ecommerce example](examples/ecommerce/README.md) and the [Quickstart](QUICKSTART.md).
- [PROVENANCE.md](PROVENANCE.md): how a release candidate is built and checked.
- [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md), and
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT — see [LICENSE](LICENSE). To cite this project, use [CITATION.cff](CITATION.cff).
