# Quickstart

TL;DR: clone the private repository, then run the candidate console command against one
deterministic compensation trace in the local Flight Recorder. No model key or external runtime
service is required.

```bash
git clone https://github.com/hseshadr/agentic-saga.git
cd agentic-saga
uv run --no-dev agentic-saga demo --scenario business-failure --open
```

The first `uv run --no-dev` may install the seven locked runtime packages. The source-checkout demo then generates no
external request: it serves one distribution-bound redacted trace on `127.0.0.1`, prints the
actual URL, and waits. Press Ctrl-C to close the server and remove its temporary site. Fresh-wheel
and hosted-CI proof are required before this command is release evidence.

## Explore all four Saga outcomes

```bash
uv run --no-dev agentic-saga demo --scenario happy-path --open
uv run --no-dev agentic-saga demo --scenario lost-response --open
uv run --no-dev agentic-saga demo --scenario compensation-failure --open
```

The default is `business-failure`. The four captured traces prove verified success, restart
reconciliation without a duplicate business effect, reverse-order verified compensation, and an
unverifiable repair parked in quiescent `HUMAN_REQUIRED`. They came from the real ecommerce
reference runtime, but the viewer is generic, read-only, and imports no ecommerce runtime or
provider.

Omit `--open` to print and serve the URL without launching a browser. Use `--port 0` for an
OS-selected loopback port or choose a valid explicit port.

## Run the executable reference

Run the same realistic ecommerce application directly and print its ordered evidence:

```bash
uv run --no-dev python -m examples.ecommerce.run
```

Pass `happy-path`, `lost-response`, or `compensation-failure` to select another path. Every
scenario uses temporary SQLite databases, a separate deterministic provider store, and the
production kernel APIs. It needs no credential or network.

Run one realistic, generic durable effect through intent, execution, verified compensation,
backup, restore, and projection replay:

```bash
uv run pytest tests/integration/test_kernel_end_to_end.py -q
```

## Compose your application

`compose_runtime` is the supported assembly path. Build your domain-specific registry, policy,
context provider, invariant evidence, and terminal gate, then supply the exact eight keyword
arguments explicitly:

```python
from agentic_saga import SagaGoal, compose_runtime

runtime = compose_runtime(
    store=store,
    definition=definition,
    policy_context_provider=policy_context_provider,
    terminal_gate=terminal_gate,
    invariant_evidence_provider=invariant_evidence_provider,
    clock=clock,
    worker_id="orders-worker",
    id_namespace=b"acme-orders-v1",
)

goal = SagaGoal(goal_id="order-123", text="Complete the order safely.", context={})
result = await runtime.start(definition=definition, goal=goal, agent=agent)
```

`SagaGoal` is passed to `runtime.start(...)` separately because it is transaction input, not runtime
configuration. The executable ecommerce assembly in
[`examples/ecommerce/demo.py`](examples/ecommerce/demo.py) shows each collaborator in context.

## Author a domain-neutral Saga context

The ecommerce and ticket-booking manifests use the same three-name API: registered tool name,
registered policy-check name, and registered invariant-check name. Validate both manifests against
their real typed registries:

```bash
uv run pytest tests/integration/manifest/test_examples.py -q
```

Read the [Saga Context Manifest guide](docs/context-manifest.md) before choosing the optional
`saga.yaml` authoring format. The manifest supplies public objective and instructions, four
deterministic planning/execution limits, tool names, and proof references; it does not embed a
workflow or install executable tools. Applications may instead build the same typed runtime inputs
directly.

Treat `saga.yaml` as public. Put no personal data, credential, receipt, or provider secret in it.
When constructing the exact `SagaDefinition`, configure its redaction policy with every ordinary
personal-data key your application admits; the built-in credential checks are only a safety floor.

## Prove the checkout

```bash
uv sync --group dev
uv run poe gate
uv run python scripts/measure_release.py
```

The first command installs development and optional-agent dependencies. The quality gate and
implemented measurement harness are offline and credential-free. The measurement command runs both
quality gates, builds and installs a wheel from locked inputs, exercises the packaged recorder, and
enforces the published budgets. It reports `overall: FAIL` for an invalid release environment; a
dirty-tree result is diagnostic only. A clean-current-commit report and matching hosted CI run still
must be recorded before release. The
[operations and release contract](docs/operations.md) lists every threshold and required artifact.

## Optional planning adapter

Install and construct the Deep Agents/OpenRouter integration without making a model call:

```bash
uv sync --extra agent --group dev
uv run pytest tests/unit/agents -q
```

The adapter receives public resolved context and returns one strict proposal. It receives no
business-tool callable, receipt, credential, or kernel authority. See the
[agent adapter guide](docs/agent-adapter.md).

Validate the fixed 24-case evaluation corpus without a model:

```bash
uv run python -m examples.ecommerce.eval
```

Live evaluation costs money and requires a key plus explicit consent. It is separate from the demo,
ordinary tests, and release measurement:

```bash
export OPENROUTER_API_KEY=your_key
RUN_LIVE_MODEL_EVALS=1 uv run python -m examples.ecommerce.eval --live \
  --samples 3 --output .artifacts/eval
```

The library does not meter money or cap provider spend. Its token limit caps output allocation for
the maintained adapter, and its elapsed limit caps agent-call time; neither is actual provider
usage or an end-to-end Saga budget. Configure account-level provider spend controls before opting
in.

Before integrating a real provider, read the
[kernel safety contract](docs/kernel-safety-contract.md). It defines at-least-once attempts,
provider idempotency obligations, reconciliation, compensation proof, SQLite assumptions, and the
conditions that require a human.
