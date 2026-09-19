# Quickstart

TL;DR: Agentic Saga lets an agent choose the next approved business tool while a deterministic
Temporal Workflow owns retries, compensation order, durable state, and human pauses.

## Run the complete ecommerce Saga

From a cloned checkout, this one command runs the canonical Temporal example: success, reverse
compensation, lost-response reconciliation without a second charge, and authenticated human
resolution.

```bash
uv run pytest -q -m temporal --force-enable-socket tests/bdd/steps/test_ecommerce_saga.py
```

The command uses `WorkflowEnvironment.start_time_skipping()`. It starts an isolated Temporal test
server for the test process, so it needs no separately running service, model key, or provider
account. It is test infrastructure—not an embedded production mode. The first run may download
locked dependencies and Temporal's test-server binary.

The readable behavior lives in `tests/bdd/features/ecommerce_saga.feature`. The implementation is
in `examples/ecommerce/demo.py` and `examples/ecommerce/provider.py`.

## What the example proves

The happy path is:

```text
reserve inventory -> charge payment -> schedule fulfillment -> verify order
```

If fulfillment fails after accepting the request, compensation runs automatically in reverse
order:

```text
cancel fulfillment -> refund payment -> release inventory
```

If a payment succeeds but its response is lost, the Activity reconciles the same operation before
the Saga continues. It does not create a second charge. If a refund remains uncertain, the
Workflow reaches `human_required`; stale and unauthorized Updates are rejected, and an authorized
Update resumes compensation.

## Why the agent is useful

The agent is the planner, not the transaction engine. It receives the current public observation
and only the tools eligible at that moment. A deterministic or model-backed agent can choose among
those tools. The Temporal Workflow validates the decision and controls execution.

This keeps the useful flexibility without letting a model decide durability, retry safety,
compensation order, or final proof.

## See the flow

The packaged Flight Recorder replays redacted evidence at a human-readable pace:

```bash
uv run --no-dev agentic-saga demo --scenario business-failure --open
```

This is a read-only replay, not a live Workflow or provider call. Other captured outcomes are
`happy-path`, `lost-response`, and `compensation-failure`.

## Run Temporal locally

For manual application development, install the Temporal CLI and run:

```bash
temporal server start-dev
```

That starts a disposable local Temporal Service on `localhost:7233` and its Web UI on
`localhost:8233`. It is for development, not production. Temporal recommends Temporal Cloud or a
production self-hosted service for production; see the official
[deployment guide](https://docs.temporal.io/production-deployment).

| Use | Temporal mode |
| --- | --- |
| Automated tests | `WorkflowEnvironment.start_time_skipping()`; isolated test server and virtual time |
| Local manual development | `temporal server start-dev`; disposable service and Web UI |
| Production | Temporal Cloud or an operated, production-ready self-hosted Temporal Service |

## Integrate the library

The supported Temporal Legos include `TemporalActivities`, `build_worker`, local and Cloud client
connectors, `start_saga`, `query_saga_state`, `resolve_human_compensation`, and
`project_run_trace`.

```python
from agentic_saga.temporal import (
    TemporalActivities,
    build_worker,
    connect_local_client,
    start_saga,
)

client = await connect_local_client("localhost:7233", namespace="default")
activities = TemporalActivities(agent, tool_registry, execution_budget)

async with build_worker(
    client,
    task_queue="checkout",
    activities=activities,
    human_resolution_activity=verify_human_resolution,
):
    handle = await start_saga(client, saga_input, task_queue="checkout")
    final_state = await handle.result()
```

Use the same task queue for the Worker and client call. `verify_human_resolution` is an application
Activity that validates the authorization reference and returns a typed result.

`connect_client` remains a compatibility alias for `connect_local_client`; both reject non-loopback
targets because they deliberately disable TLS. For Temporal Cloud, use `TemporalCloudConfig` with
`connect_cloud_client`. Its API key is a masked `SecretStr`, TLS is always configured through the
official SDK, and the Pydantic converter remains consistent with Workers. If private production
payloads enter history, configure the Client and Workers with the same encrypted Data
Converter/Payload Codec backed by your KMS. Also enforce least-privilege Namespace access. See
[Security](SECURITY.md).

```python
import os

from pydantic import SecretStr

from agentic_saga.temporal import TemporalCloudConfig, connect_cloud_client

cloud = TemporalCloudConfig(
    target_host="your-namespace.tmprl.cloud:7233",
    namespace="your-namespace.your-account",
    api_key=SecretStr(os.environ["TEMPORAL_API_KEY"]),
)
client = await connect_cloud_client(cloud)
```

## Add your domain

Define typed `WorkflowTool` values for reads and effects. Each reversible effect names its
compensation tool; prerequisites describe dependencies. Register implementations in a
`ToolRegistry`. Ecommerce is only a sample—ticketing, onboarding, provisioning, and other
long-running transactions use the same contracts.

Provider Activities must accept at-least-once delivery. Give each logical effect a stable
idempotency key, retain the provider reference needed for compensation, and implement
reconciliation for a lost or ambiguous response.

The optional `saga.yaml` supplies public context, objectives, limits, and registered names. It is
not executable Workflow code. Never put credentials, private receipts, or personal data in it.

## Optional OpenRouter agent

The deterministic example is the release baseline. To use the optional model-backed planner:

```bash
cp .env.example .env
chmod 600 .env
# Edit .env and set OPENROUTER_API_KEY to your own key.
uv sync --extra agent --group dev
```

`.env` stays local and `.env.example` is secret-free. The library does not read project files
implicitly; your application decides whether to load `.env`. Never send the key through Temporal
payloads. Configure OpenRouter spend limits before live use.

Pydantic Deep owns the model/tool-calling loop. Agentic Saga exposes only currently eligible
proposal tools and bounds the decision Activity. The model never receives a callable business
adapter, Temporal client, provider credential, or human authorization token.

## Prove the checkout

```bash
uv sync --group dev
uv run poe gate
cd web/flight-recorder
npx --yes pnpm@11.5.0 install --frozen-lockfile
npx --yes pnpm@11.5.0 gate
```

The Python gate covers ordinary tests, Temporal integration tests, strict types, formatting,
complexity, branch coverage, and release-contract unit tests. The pinned frontend gate covers the
recorder's tests, accessibility, build, packaged-asset parity, and browser behavior. Neither command
makes a paid model call. A final release claim still requires `uv run poe release-candidate` from a
clean exact commit and its matching hosted Dagger run.

Next: [Temporal safety contract](docs/temporal-safety-contract.md),
[operations](docs/operations.md), and [architecture](docs/architecture/index.html).
