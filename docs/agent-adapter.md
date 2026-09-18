# Agent planning adapter

## TL;DR

The optional adapter gives a smart agent the resolved Saga context and the tools eligible now, then
accepts exactly one typed proposal. It cannot execute a business tool. Only the deterministic Saga
kernel can durably authorize and dispatch side effects.

The proposal is one strict `ToolCall`, `Finish`, `BeginCompensation`, or `Escalate`. A
`BeginCompensation` proposal requests a phase change only; the kernel verifies eligibility and
retains control of the compensation frontier and every dispatched effect.

## Install and prove it offline

```bash
uv sync --extra agent --group dev
uv run pytest tests/unit/agents -q
```

This proof constructs the maintained Deep Agents and OpenRouter integrations but makes no network
request and needs no credential. Core package imports remain usable without the `agent` extra.

## Three-symbol API

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

`context` is the validated `SagaContext` returned by `load_saga_context`. The application passes
the proposal to its kernel runtime; it must never execute the proposal directly.

For deterministic offline CI, inject an asynchronous proposal function instead of a model:

```python
async def scripted(system_context: str, turn_context: str) -> object:
    return {
        "proposal": {
            "kind": "escalate",
            "proposal_id": "proposal_offline1",
            "based_on_saga_seq": 3,
            "reason_code": "ambiguous_evidence",
            "rationale": "Conflicting public evidence needs an operator.",
        }
    }


driver = DeepAgentsDriver(context, scripted)
```

## Exact authority boundary

The adapter renders `SagaContext.agent_context`, the current public observation, and the current
eligible subset of the context's pinned descriptors. It rejects descriptor drift, duplicate
descriptors, material caught by the built-in public-input checks, stale sequence numbers, unknown
tools, malformed output, and provider failures before a proposal reaches the kernel.
Generic JSON at this boundary is capped at depth 16, 4,096 nodes, 256 items per container, 16 KiB
per UTF-8 string, and 64 KiB encoded.

The exact `SagaDefinition` owns the immutable redaction policy used by the runtime to produce public
observations and evidence before they reach this adapter. Built-in credential and payment-secret
rules are only a floor; applications must add every ordinary PII key they permit, and unlisted
fields are public by contract. The optional `saga.yaml` is also public and must never contain PII,
credentials, receipts, or private provider data.

Adapter failures use `AgentPlanningError`, imported from its defining
`agentic_saga.agents.deepagents` module. Its closed `AgentFailureCategory` distinguishes trusted
rate-limit, server, and transport exhaustion from invalid responses and internal failures. The
error retains no provider message, URL, headers, body, cause, or context. It is intentionally not
added to the small package facade.

No registered Python tool callable, effect adapter, credential, receipt, or kernel authority is
passed to Deep Agents. Its general-purpose subagent and `task` tool are disabled through Deep
Agents' public harness profile; maintained planning tools may use only ephemeral in-memory state.
The disabling profile is registered for the exact configured `provider:model`. Deep Agents keeps
that registry process-wide, so isolate graphs in separate processes only if the same exact model
must retain its general-purpose subagent elsewhere. Arbitrary prebuilt-model construction stays
package-internal; the public live-model path is the validated OpenRouter builder. One model call and
eight graph steps are allowed per proposal.

The kernel durably reserves fixed whole-unit planning allocations before each call. For a positive
turn limit, each turn receives `token_limit // turn_limit` output tokens and
`elapsed_ms_limit // turn_limit` milliseconds; validation requires both quotients to be at least
one. The maintained OpenRouter builder caps `max_tokens` and its call timeout with those values.
These are output allocation and agent-call deadline controls—not input-token accounting, actual
provider usage, end-to-end Saga elapsed time, or a monetary meter. Arbitrary `AgentDriver`
implementations are trusted application code and must enforce the provided limits themselves.

## OpenRouter route

The deterministic default route is:

1. `openai/gpt-oss-20b`
2. `qwen/qwen3-30b-a3b-instruct-2507`

OpenRouter receives that ordered fallback in one request. SDK retries are disabled, temperature
and seed are zero, parallel tool calls are disabled, reasoning effort is low, and parameter support
is required. A provider/model transport failure may select the fallback. A schema-invalid or unsafe
response fails closed locally; it is never retried as a different answer.
Moving aliases and OpenRouter variant suffixes are rejected because the safety profile is pinned to
one exact model identity.

Set `OPENROUTER_API_KEY` only in the process environment or inject `OpenRouterSettings`. The key is
a masked secret and is never added to prompts, metadata, or exception messages. Safe adapter
metadata records the configured provider and ordered route. The evaluation harness records those
configured values and measured latency. Actual responding model/provider identity, token usage,
and cost remain `null` because the adapter does not expose trustworthy response telemetry.

The maintained adapter disables ambient LangSmith tracing around every model invocation. Setting
LangChain/LangSmith tracing variables in the surrounding process therefore does not create a
second telemetry destination for Saga context. Applications that build a different adapter own
and must document its observability egress.

The library does not estimate or enforce provider spend. Configure OpenRouter account budgets and
rate limits independently before an explicitly authorized live evaluation.

## Current versus planned

V0.1 ships generic context rendering, all four strict proposal types, the maintained Deep
Agents graph, bounded OpenRouter construction, safe typed failures, deterministic offline tests,
the executable ecommerce reference, its opt-in resumable live-model evaluator, and the read-only
Flight Recorder with distribution-bound resources and Story, Ledger, and Proof views. The
implemented release harness exercises a wheel-installed recorder; each release candidate must
produce its own clean exact-commit report and matching hosted CI evidence.

Trustworthy model-provider usage receipts remain planned: the current adapter deliberately reports
unknown returned identity, token use, and cost as `null` instead of guessing. No live paid call
runs in ordinary CI, the demo, or the release measurement. The
[operations and release contract](operations.md) states exactly what reaches OpenRouter, what stays
local, and who owns retention and deletion.

## Maintained integration references

- [Deep Agents customization](https://docs.langchain.com/oss/python/deepagents/customization)
- [Deep Agents profiles](https://docs.langchain.com/oss/python/deepagents/profiles)
- [LangChain structured output](https://docs.langchain.com/oss/python/langchain/structured-output)
- [LangChain OpenRouter integration](https://docs.langchain.com/oss/python/integrations/chat/openrouter)
- [OpenRouter model fallbacks](https://openrouter.ai/docs/guides/routing/model-fallbacks)
