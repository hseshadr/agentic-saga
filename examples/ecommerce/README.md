# Ecommerce Agentic Saga

## TL;DR

A Saga completes a business transaction across several systems without pretending they share one
database transaction. Each successful external change records how to reverse it. If a later step
fails, the Saga compensates the completed changes in reverse order.

This example checks out one order:

```text
reserve stock → charge payment → schedule delivery → verify the order
      ↓                ↓                 ↓
 release stock ← refund payment ← cancel delivery
```

The agent chooses the next forward tool from the goal and current public evidence. Temporal owns
the durable loop. The Workflow—not the prompt—enforces prerequisites, call budgets, fresh terminal
proof, stable operation identities, retries, reconciliation, and compensation order.

## Run it

Start Temporal's lightweight local development server in one terminal:

```bash
temporal server start-dev
```

Run a scenario in another terminal:

```bash
uv run python -m examples.ecommerce.run happy-path
uv run python -m examples.ecommerce.run business-failure
uv run python -m examples.ecommerce.run lost-response
uv run python -m examples.ecommerce.run compensation-failure
```

Run all four and open their recorded results together in the Flight Recorder:

```bash
uv run python -m examples.ecommerce.run all --open
```

The commands execute real Temporal Workflows using a deterministic `CheckoutAgent` and simulated
providers. They do not use JEV or call a model. With `--open`, execution finishes before the browser
opens; only the read-only recorder server remains active until Ctrl+C.

The example CLI is intentionally local-only and connects to `localhost:7233` by default. An
alternate loopback port is supported:

```bash
uv run python -m examples.ecommerce.run happy-path \
  --temporal-address 127.0.0.1:7333
```

For Temporal Cloud or another remote service, use the library's typed `TemporalCloudConfig` and
`connect_cloud_client` helpers shown in the root quickstart; the local connector refuses plaintext
remote targets.

The automated acceptance tests need no separately installed server. Temporal's time-skipping test
environment starts a lightweight test service for the duration of each test:

```bash
uv run pytest tests/bdd/steps/test_ecommerce_saga.py -q
```

## What the agent controls

[`saga.yaml`](./saga.yaml) gives the agent only:

- the goal and public order context;
- the four forward tools it may choose;
- examples of useful forward decisions;
- fixed turn and tool-call budgets.

The agent never sees compensation tools as forward choices. It also cannot request compensation or
human escalation. When forward work fails, the Workflow derives obligations from confirmed receipts
and executes `cancel_fulfillment → refund_payment → release_inventory`.

This separation is the point of Agentic Saga: planning is flexible; transaction safety is not.

## The four executable stories

1. **Healthy checkout** — each effect happens once and `verify_order` supplies fresh authoritative
   proof before success.
2. **Delivery rejected** — scheduling is a confirmed external change, but authoritative proof says
   the order was rejected. The Workflow compensates all three prior effects in reverse order.
3. **Payment reply lost** — the charge commits, both Activity attempts lose their reply, and one
   reconciliation checks the same stable operation ID. The customer is charged once.
4. **Refund reply lost** — the refund commits but cannot be proven from the Activity response. The
   Workflow pauses before releasing stock. A stale decision and an unauthorized decision are
   rejected; a demo verifier accepts one opaque authorization reference and compensation resumes.

Human involvement is therefore narrow: it resolves an uncertain compensation outcome. Ordinary
business rejection is handled automatically.

## Files to read

| File | Responsibility |
| --- | --- |
| `saga.yaml` | Agent goal, public context, forward tools, budgets, and examples. |
| `domain.py` | Strict ecommerce commands and provider state. |
| `provider.py` | In-memory idempotent provider simulation and tool registry. |
| `demo.py` | Driver, registry-derived Workflow declarations, worker, and four scenarios. |
| `run.py` | Real local-Temporal command-line runner. |
| `export_flight_recorder.py` | Projects Temporal state into checked-in Flight Recorder traces. |
| `tests/bdd/features/ecommerce_saga.feature` | The four plain-language acceptance stories. |

## Flight Recorder

Every run is projected through `agentic_saga.temporal.project_run_trace`. Regenerate all four traces:

```bash
uv run python -m examples.ecommerce.export_flight_recorder
```

The JSON files in `flight-recorder/traces/` drive the existing replay UI. They contain public,
redacted workflow evidence: effects, reconciliation, proof, compensation, human resolution, and
terminal state.

For a ready-made catalog without a running Temporal server, use
`uv run agentic-saga demo --open`. All four recordings are available in one page. The bundled
compensation-failure recording deliberately ends at human review; the example runner demonstrates
the authorized resolution too, so its fresh recording ends with verified compensation.

## Model-backed evaluation

The deterministic `CheckoutAgent` is the transaction proof: it uses the exact `AgentDriver`
contract a model uses, without cost or network variability. The optional evaluation modules and
corpus remain the place to measure model planning quality. A model may choose a poor proposal; the
same Workflow eligibility, budget, proof, and compensation rules still apply.

The provider is intentionally an in-memory simulation. Production adapters should call their real
systems with the supplied stable operation ID, retain idempotency records for the recovery horizon,
and implement authoritative reconciliation.
