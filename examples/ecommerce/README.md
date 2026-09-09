# Ecommerce reference Saga

TL;DR: this is a small, offline example of the Agentic Saga boundary. A proposal-only agent chooses
the next business action. The kernel owns authorization, durable intent, dispatch, reconciliation,
the compensation frontier, human escalation, and terminal proof.

Run the default business-failure and compensation path from the repository root:

```bash
uv run --no-dev python -m examples.ecommerce.run
```

Pass `happy-path`, `lost-response`, or `compensation-failure` to run another path. Every scenario
uses temporary SQLite databases, no credential, no network, and the same production kernel APIs.

## The boundary in one minute

`ScriptedProposalDriver` reads the latest public `SagaObservation` and the currently advertised tool
descriptors, then returns exactly one typed proposal. It never calls a provider. The runtime sends
that proposal through deterministic policy and only then records and dispatches an effect.

If the goal fails, the agent may propose `BeginCompensation`. That request cannot name a rollback.
The kernel changes phase only when effects are settled and an eligible obligation exists. On later
turns, it advertises only the compensation actions in the planner's safe frontier.

Payment policy is application code: the expected order, customer, amount, and currency come from
authoritative provider state. A refund must also match the confirmed charge operation and its
receipt. The provider enforces the same terms and persists captured/refunded amounts; terminal
invariants verify those exact values rather than trusting a status flag.

## Ten-minute source map

| File | One responsibility |
| --- | --- |
| `saga.yaml` | Human-readable goal, guardrails, budgets, tool allowlist, and named checks. |
| `domain.py` | Strict ecommerce commands, provider state, and compact demo result types. |
| `provider.py` | Separate durable fake provider, idempotency, fencing, fault injection, and counters. |
| `demo.py` | Registry, application policy/invariants, proposal driver, runtime assembly, and restart. |
| `run.py` | CLI argument parsing and concise timeline rendering only. |
| `eval-corpus-v1.json` | Fixed 24-case model-evaluation inputs and deterministic safety oracles. |
| `evaluation.py` | Strict corpus loading, evidence scoring, provider separation, and release thresholds. |
| `live_eval.py` | Opt-in execution, redacted atomic artifacts, resume checks, and aggregation. |
| `eval.py` | Source-checkout CLI for free validation or explicitly authorized live evaluation. |
| `tests/bdd/features/ecommerce_saga.feature` | Four executable product stories and exact effects. |

Read `demo.py` in this order:

1. `EcommerceContexts` and `EcommerceEvidence` bind policy and proof to provider state.
2. `ScriptedProposalDriver` demonstrates the agent boundary with no model or network.
3. `build_registry`, `_policy`, and `_terminal_gate` assemble application-owned capabilities.
4. `_assembly` wires the public storage, kernel, dispatcher, reconciler, and runtime Lego pieces.
5. `run_scenario` and `_resume_lost_response` execute or reopen a Saga from durable state.
6. `_demo_run` exports the real `RunTrace` used by tests and the checked-in Flight Recorder.

## What each scenario proves

- `happy-path`: dynamic read/effect proposals end only after fresh exact-state invariants pass.
- `business-failure`: verified effects compensate once in `cancel → refund → release` order.
- `lost-response`: the provider commits a charge, loses the response, and restart reconciliation
  confirms it with one execute and one business effect.
- `compensation-failure`: an ambiguous refund cannot unblock later compensation; the Saga becomes
  quiescent `HUMAN_REQUIRED` with an operator packet.

## Offline evaluation contract

Run the corpus and scoring contract without a model, credential, or network:

```bash
uv run pytest tests/live_model/test_corpus_contract.py tests/live_model/test_scoring.py -q
```

The versioned corpus has six straightforward, recoverable, adversarial, and escalation cases.
These 24 case IDs name evaluation fixtures, not extra demo CLI scenarios. The scorer revalidates a
real `SagaResult` and `RunTrace`, then checks fresh proof targets, backed compensation, typed
forbidden effects, human-pause quiescence, durable agent-turn budgets, and trusted pre-redaction
evidence. Provider exhaustion has a separate machine-readable status and never lowers model-quality
denominators.

## Opt-in live evaluation

First validate all 24 cases for free. This loads the strict corpus and makes no driver or network
call:

```bash
uv run python -m examples.ecommerce.eval
```

Live use costs money. It requires both an OpenRouter key and explicit consent:

```bash
export OPENROUTER_API_KEY=your_key
RUN_LIVE_MODEL_EVALS=1 uv run python -m examples.ecommerce.eval --live \
  --samples 3 --output .artifacts/eval
```

Rerun the same command and output directory to resume verified samples. Each sample is committed
before aggregation and carries a self-digest verified before reuse. Inspect
`.artifacts/eval/traces`, `.artifacts/eval/samples`, and `.artifacts/eval/report.json`. This is
tamper-evident integrity and corruption detection, not authenticity against an attacker who can
rewrite both an artifact and its digest. The alternate warehouse case starts with the preferred
warehouse unavailable and asks the agent to discover and reserve an allowed alternate without a
second charge.

The scripted demo is transactional proof; this optional run is model-quality evidence.
A provider failure is reported separately and cannot improve the model score.
We make no universal exactly-once claim: this example proves deduplication only for its provider
contract. The configured model route and measured latency are recorded. Returned model/provider
identity, token usage, and cost remain `null` because the current public adapter does not expose
trustworthy telemetry.

This example deliberately contains no workflow server, UI, ecommerce branch in the generic
package, or second Saga engine. The provider is a deterministic simulation, not a production
commerce integration. The optional Deep Agents/OpenRouter adapter is not needed for the free
offline proof.
