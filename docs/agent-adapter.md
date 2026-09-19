# Pydantic Deep planning adapter

## TL;DR

Pydantic Deep owns the model/tool protocol; the deterministic Saga kernel owns execution,
state, reconciliation, compensation order, and terminal proof. The model receives only the tools
eligible for the current turn and must make exactly one native tool call. That call is a proposal,
not direct authority over a payment, ticket, inventory system, or other external service.

This is the whole control loop:

```text
public observation + currently eligible schemas
                    |
                    v
        Pydantic Deep native tool call
                    |
                    v
          typed, sequence-bound proposal
                    |
                    v
 deterministic kernel -> durable intent -> business adapter
                    |
                    v
        next public observation or terminal proof
```

No Markov decision process is involved. State and legal actions are explicit. Unknown external
outcomes are reconciled by the runtime instead of estimated by the model.

## Install and prove it offline

The agent integration is optional and exactly pins the two libraries that define its model/tool
boundary:

```text
pydantic-deep==0.3.43
pydantic-ai-slim[openrouter]==2.45.0
```

Install the extra and run its offline tests:

```bash
uv sync --extra agent --group dev
uv run pytest tests/unit/agents -q
```

The tests construct the Pydantic Deep and OpenRouter path without sending a network request. Core
Agentic Saga imports do not require the `agent` extra.

## Supported API

```python
from agentic_saga.agents import (
    DeepAgentsDriver,
    OpenRouterSettings,
    build_openrouter_driver,
)

settings = OpenRouterSettings.from_environment()
driver = build_openrouter_driver(context, settings)
proposal = await driver.next_action(observation, eligible_descriptors)
```

`context` is the validated `SagaContext` returned by `load_saga_context`. The runtime validates the
returned `ToolCall`, `Finish`, `BeginCompensation`, or `Escalate`; applications must not execute the
proposal directly.

For deterministic offline tests, an injected proposal function remains available:

```python
async def scripted(system_context: str, turn_context: str) -> object:
    return {
        "proposal": {
            "kind": "escalate",
            "reason_code": "ambiguous_evidence",
            "rationale": "Conflicting public evidence needs an operator.",
        }
    }


driver = DeepAgentsDriver(context, scripted)
```

This injected seam is not the provider protocol. The maintained provider-backed path uses native
Pydantic AI tool calls.

## What “native tool” means here

For each turn, the adapter builds a strict Pydantic AI `ExternalToolset` from only the descriptors
the kernel currently permits. It also provides three typed control tools when legal:

- `finish_saga(target_status, rationale)`
- `begin_compensation(reason_code, rationale)`
- `escalate_to_human(reason_code, rationale)`

Pydantic Deep owns the request/response loop and native schema transport. Its output is
`DeferredToolRequests`, and the adapter accepts exactly one deferred call with no approval request.
The call is then converted to a typed Saga proposal. The adapter supplies the proposal identity and
current Saga sequence; the model cannot select either protocol field.

Deferral is the safety seam. Pydantic Deep never receives a registered Python business callable,
provider credential, receipt, operation identity, idempotency key, or kernel lease. A model call to
`charge_payment(...)` therefore cannot charge anything by itself. Only the kernel can validate the
proposal, durably record intent, derive stable operation identity, and dispatch the registered
adapter.

Every proposal schema is strict. A Pydantic AI capability supplies `tool_choice="required"`, so the
provider must return a native tool call instead of free-form text. If a provider returns text or an
invalid native result, Pydantic may make one corrective model request with validation feedback.
Only an exact `DeferredToolRequests` result can leave the adapter; zero or multiple deferred calls are rejected before execution. The model remains useful—it chooses the next business action—but it
is not the transaction coordinator or source of truth.

## Native trajectories

These are abbreviated from the ecommerce example. `MODEL -> native tool call` is the Pydantic Deep
protocol. `KERNEL -> observed result` is the next redacted, durable observation after policy,
persistence, dispatch, and reconciliation. Opaque receipt values are intentionally omitted.

### Happy path

```text
MODEL -> native tool call
check_inventory(sku="sku_travel_pack", quantity=1)
KERNEL -> observed result
read_observed(available=2, reserved=0, version=1, warehouse_id="primary")

MODEL -> native tool call
reserve_inventory(order_id="order_demo_001", sku="sku_travel_pack",
                  quantity=1, expected_version=1)
KERNEL -> observed result
effect_confirmed(tool="reserve_inventory", durable_receipt=true)

MODEL -> native tool call
inspect_order(order_id="order_demo_001")
KERNEL -> observed result
read_observed(payment="open", fulfillment="pending")

MODEL -> native tool call
charge_payment(order_id="order_demo_001", customer_id="customer_demo_001",
               amount_minor=7900, currency="USD")
KERNEL -> observed result
effect_confirmed(tool="charge_payment", durable_receipt=true)

MODEL -> native tool call
inspect_order(order_id="order_demo_001")
KERNEL -> observed result
read_observed(payment="captured", fulfillment="pending")

MODEL -> native tool call
schedule_fulfillment(order_id="order_demo_001")
KERNEL -> observed result
effect_confirmed(tool="schedule_fulfillment", durable_receipt=true)

MODEL -> native tool call
inspect_order(order_id="order_demo_001")
KERNEL -> observed result
read_observed(payment="captured", fulfillment="scheduled")

MODEL -> native tool call
finish_saga(target_status="succeeded_verified", rationale="Fresh proof satisfies the goal.")
KERNEL -> observed result
terminal_assigned(status="succeeded_verified", invariant_proof="passed")
```

Every effect invalidates affected reads. That is why the trajectory refreshes order evidence rather
than relying on a stale prompt snapshot.

### Failure after charge

The provider has confirmed the reserve, charge, and fulfillment attempt, but authoritative order
evidence reports the fulfillment as rejected. The model requests compensation; it does not invent
the rollback sequence.

```text
MODEL -> native tool call
begin_compensation(reason_code="forward_goal_unreachable",
                   rationale="Authoritative order evidence reports fulfillment rejected.")
KERNEL -> observed result
compensation_started(eligible_tools=["cancel_fulfillment"])

MODEL -> native tool call
cancel_fulfillment(order_id="order_demo_001")
KERNEL -> observed result
effect_confirmed(tool="cancel_fulfillment", eligible_tools=["refund_payment"])

MODEL -> native tool call
refund_payment(order_id="order_demo_001", customer_id="customer_demo_001",
               amount_minor=7900, currency="USD")
KERNEL -> observed result
effect_confirmed(tool="refund_payment", eligible_tools=["release_inventory"])

MODEL -> native tool call
release_inventory(order_id="order_demo_001", sku="sku_travel_pack", quantity=1)
KERNEL -> observed result
effect_confirmed(tool="release_inventory", eligible_tools=[])

MODEL -> native tool call
finish_saga(target_status="compensated_verified",
            rationale="Fresh compensation proof satisfies every obligation.")
KERNEL -> observed result
terminal_assigned(status="compensated_verified", invariant_proof="passed")
```

The order `cancel_fulfillment -> refund_payment -> release_inventory` comes from durable receipts
and dependency metadata. On each turn, the model sees only the current kernel-computed frontier.

## Unknown-outcome reconciliation and human escalation

An interrupted API call may have committed remotely even when the caller received no response. The
runtime records the outcome as unknown and pauses model planning. It reconciles the same stable
operation identity through the registered adapter:

```text
business call loses its response
        -> kernel records OutcomeUnknown
        -> no model turn and no blind retry
        -> runtime reconciles the original operation identity
             confirmed: record receipt and resume
             absent: prove absence and resume according to adapter policy
             unresolved: enter quiescent HUMAN_REQUIRED
```

Human escalation is therefore a narrow safety outcome, not the normal compensation mechanism. It
is used when an external fact remains unknowable, required business authority is unavailable, a
recovery guarantee is insufficient, or a compensation cannot be verified. The operator receives a
durable, redacted evidence packet. No further autonomous effect is dispatched while the Saga is in
`HUMAN_REQUIRED`.

## Deliberately stripped Pydantic Deep capabilities

Pydantic Deep is a broad agent harness. This adapter uses its typed model/tool protocol but grants
none of its workstation or delegation authority. All general-purpose capabilities are disabled:

- built-in and user-supplied executable tools and toolsets;
- filesystem, shell execution, web search/fetch, and document parsing;
- TODO/planning, memory, skills, context discovery/files, and history processing;
- subagents, built-in subagents, teams, monitoring, and self-improvement;
- checkpoints, archives, forking, tool search, call patching, and loop reminders;
- extended thinking, context compression, and framework cost tracking.

These features are useful in coding and research agents. They are unnecessary authority for one
bounded transactional decision. Agentic Saga instead supplies a fresh, public observation and one
ephemeral eligible toolset per turn.

## Exact authority and data boundary

The adapter receives `SagaContext.agent_context`, the current public observation, bounded durable
read evidence, and the current eligible subset of the context's pinned descriptors. Reads carry the
Saga sequence at which they were observed plus a `fresh` or `stale` label. Descriptor drift,
duplicates, private material caught by the public-input checks, unavailable tools, malformed
outputs, and provider failures fail before a proposal reaches the kernel.

Generic JSON at this boundary is capped at depth 16, 4,096 nodes, 256 items per container, 16 KiB
per UTF-8 string, and 64 KiB encoded. The exact `SagaDefinition` owns the immutable redaction policy.
Built-in credential and payment-secret checks are only a floor; applications must add ordinary PII
keys they admit. Unlisted fields are public by contract, and `saga.yaml` must contain public
authoring context only.

Adapter failures use secret-free `AgentPlanningError` categories. Raw provider messages, URLs,
headers, bodies, causes, prompts, credentials, and response material are not retained in those
errors.

## OpenRouter route and limits

The maintained default is the pinned `openai/gpt-oss-120b` open-weight model through Pydantic AI's
OpenRouter provider. It is the inexpensive route that produced the stronger observed completion on
this repository's fixed release corpus. That result is not a universal reliability claim. The code
allows an application to inject another concrete pinned model ID, but any alternative must earn its
place with fresh evidence on the same corpus.

The design deliberately keeps the model's job small enough for cheaper tool-calling models: choose
one exact eligible action from current evidence. Safety does not become weaker with a smaller model;
poor choices are rejected by policy or fail the evaluation corpus. Moving the maintained default to
a smaller model still requires fresh evidence on that fixed corpus.

SDK retries are zero. Pydantic result correction is exactly one, so one durable agent turn can make
at most two model requests. The correction handles structural output failure; it never executes a
business effect because every exposed proposal tool is external and deferred. Temperature is zero,
reasoning effort is low, and provider support is required for every parameter actually sent.
`parallel_tool_calls` and `seed` are not sent because support varies across OpenRouter endpoints.
Safety does not depend on either provider hint: the adapter requires one deferred proposal call and
rejects zero or multiple calls before any proposal reaches the kernel. OpenRouter may fail over
among providers serving the same model, but it cannot silently switch to a different model. Moving
aliases and OpenRouter variant suffixes are rejected.

The kernel durably reserves whole-unit planning allocations before each turn. The adapter divides
that turn allocation across the two possible model requests: each request receives at most
`(token_limit // turn_limit) // 2` output tokens and
`(elapsed_ms_limit // turn_limit) // 2` milliseconds, further capped by adapter settings. A zero
per-request allocation is rejected before a model call. These are not input-token accounting,
actual provider usage, end-to-end Saga time, or a monetary meter. Configure provider-side spending
limits before any live evaluation.

Set `OPENROUTER_API_KEY` only in the process environment or inject `OpenRouterSettings`. The masked
secret is not added to prompts, metadata, driver representations, or safe exceptions. Live
evaluation records the configured route and measured latency; returned provider identity, usage,
and cost remain `null` when trustworthy telemetry is unavailable.

## Current limits

The optional adapter is a bounded, proposal-only integration for the v0.1 release candidate. Its
ordinary tests are deterministic and offline. Paid live evaluation is opt-in and is not part of the
normal quality gate. The reference provider is a simulation, not a production commerce service,
and no claim is made that this adapter creates universal exactly-once effects.

Useful upstream references:

- [Pydantic Deep framework](https://github.com/vstorm-co/pydantic-deepagents)
- [Pydantic AI deferred tools](https://ai.pydantic.dev/deferred-tools/)
- [Pydantic AI toolsets](https://ai.pydantic.dev/toolsets/)
- [OpenRouter tool calling](https://openrouter.ai/docs/guides/features/tool-calling)
