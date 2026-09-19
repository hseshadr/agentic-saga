# Agent decision adapters

## TL;DR

The agent chooses one currently eligible business capability. The deterministic Temporal workflow
executes that choice and owns retries, reconciliation, reverse compensation, terminal proof, and
verified human handling.

The model never receives compensation or human-escalation controls. It sees `finish_saga` only
after the workflow has fresh proof for `succeeded_verified`.

```text
bounded public observation + eligible business schemas
                         |
                         v
             Pydantic Deep / OpenRouter
                         |
                         v
             typed, sequence-bound proposal
                         |
                         v
      deterministic Temporal workflow -> Activity -> provider
                         |
                         v
       reconciled evidence, compensation, or verified finish
```

This split is deliberate. A model-quality evaluation measures whether the model selected the right
advertised tool with the right public arguments. Temporal integration tests separately prove
transaction correctness. A successful tool call is not evidence that a payment or rollback was
correctly executed.

## Install and prove it offline

```bash
uv sync --extra agent --group dev
uv run pytest tests/unit/agents tests/unit/temporal tests/integration/temporal -q
uv run python -m examples.ecommerce.eval
```

The last command strictly validates all 24 transaction fixtures and makes no model or network call.
The Pydantic Deep/OpenRouter dependencies are optional; importing core Agentic Saga does not load
them.

## Pydantic Deep with OpenRouter

```python
from agentic_saga.agents import OpenRouterSettings, build_openrouter_driver

settings = OpenRouterSettings.from_environment()
driver = build_openrouter_driver(context, settings)
proposal = await driver.next_action(observation, eligible_descriptors)
```

`context` is the validated, public `SagaContext`. `observation` is the bounded view produced by the
Temporal decision Activity. `eligible_descriptors` contains only capabilities whose prerequisites
and call budgets currently pass.

The native provider-visible surface is:

- each currently eligible business tool;
- `finish_saga(target_status="succeeded_verified", rationale=...)` only when fresh terminal proof
  exists.

There is no native `begin_compensation` or `escalate_to_human` tool. An ordinary failed proof
triggers workflow-owned compensation. An unknown effect is reconciled before any later choice.
Unresolved reconciliation or compensation moves the workflow to `HUMAN_REQUIRED`; a validated
Workflow Update resumes it only after a verification Activity accepts public authorization.

Pydantic Deep receives deferred schemas, never registered Python business callables. A native
`charge_payment(...)` response therefore cannot charge anything. The adapter accepts exactly one
deferred call, supplies host-owned proposal identity and current sequence, and returns a typed
proposal to Temporal for deterministic revalidation.

The adapter disables Pydantic Deep's filesystem, execution, web, subagent, planning, memory, and
other general-purpose capabilities. Temperature is zero. One validation correction is allowed,
with at most two provider requests per durable agent turn. Provider SDK retries are zero because
Temporal owns retry policy.

The default model is pinned to `openai/gpt-oss-120b`. A moving OpenRouter route such as
`openrouter/auto`, `openrouter/free`, or a `latest` alias is rejected.

## Bounded Jev choices

Jev is useful when the application can materialize complete candidate proposals. It selects one
opaque candidate ID and returns confidence plus probabilities; it does not invent tool arguments.

```python
from agentic_saga.agents import (
    JevSettings,
    ProposalCandidate,
    ToolCallIntent,
    build_jev_driver,
)


async def candidates(observation, eligible_descriptors):
    order_id = observation.goal.context["order_id"]
    eligible = {descriptor.name for descriptor in eligible_descriptors}
    if "verify_order" not in eligible:
        return ()
    return (
        ProposalCandidate(
            candidate_id="choice_00000001",
            criteria="Verify the authoritative final order state.",
            minimum_confidence=0.9,
            proposal=ToolCallIntent(
                tool_name="verify_order",
                arguments={"order_id": order_id},
                rationale="Fresh success proof is required.",
            ),
        ),
    )


driver = build_jev_driver(context, candidates, JevSettings.from_environment())
```

The direct route pins `jev-1.13.0`. The OpenRouter Decisions route pins
`typesafe/jev-1.13` and calls only `https://openrouter.ai/api/alpha/decisions`; it never falls back
to chat completions. Both reject unknown or duplicate choices, malformed probabilities, non-finite
confidence, low-confidence selections, private payloads, and stale proposals. A single legal
candidate is selected locally without a paid request.

Install only the chosen transport:

```bash
uv sync --extra jev --group dev
# or
uv sync --extra jev-openrouter --group dev

uv run pytest tests/unit/agents/test_choice.py tests/unit/agents/test_jev.py \
  tests/unit/agents/test_openrouter_decisions.py -q
```

## Opt-in live model evaluation

The live evaluator calls the real Pydantic Deep/OpenRouter native-tool path with the same bounded
observation shape used by the Temporal decision Activity. It does not run business Activities and
does not claim transaction correctness.

```bash
cp .env.example .env
chmod 600 .env
# Add your OPENROUTER_API_KEY to the ignored .env file.

RUN_LIVE_MODEL_EVALS=1 uv run python -m examples.ecommerce.eval \
  --live --suite smoke --samples 1 --output .artifacts/eval
```

The smoke suite makes one bounded decision request. `release` runs the three canonical corpus
checkpoints where a model has a legal decision. `extended` runs all 18 model-relevant checkpoints;
the six workflow-owned escalation fixtures are intentionally excluded.

The report records the configured model/provider, selected native tool, correctness, latency, and
token/cost data when the public adapter exposes it. The current adapter does not expose provider
usage, so those fields remain `null` and the CLI says `usage=unavailable`. Provider failures are
reported separately and never improve or reduce model-quality denominators. Artifacts contain no
credentials or expected arguments.

Live evaluation requires both `RUN_LIVE_MODEL_EVALS=1` and `OPENROUTER_API_KEY`. Ordinary tests do
not load `.env` and do not make network calls.

## Safety boundary

The agent may:

- select one advertised business tool and provide schema-valid public arguments;
- propose `succeeded_verified` only when the workflow advertises verified finish.

The Temporal workflow alone may:

- accept or reject stale and ineligible proposals;
- assign stable provider idempotency identity;
- execute and retry Activities;
- reconcile an unknown effect before continuing;
- record compensation obligations and unwind them in reverse order;
- enter `HUMAN_REQUIRED` and verify an authorization-bound resolution;
- assign final workflow status.

## Honest limits

- Model evaluation demonstrates decision behavior on a fixed public corpus, not universal model
  reliability.
- Temporal provides durable execution, not external exactly-once semantics. Providers still need
  durable idempotency and authoritative reconciliation.
- Compensation is a business action, not database rollback. It can fail and require a verified
  operator decision.
- Model/provider identity is configured identity unless the provider response exposes a separately
  authenticated returned identity.
