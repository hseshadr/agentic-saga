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

Each confirmed effect makes earlier reads stale. The happy-path trajectory treats the durable
effect receipt as progress, refreshes only the facts needed before the next effect, and refreshes
again before terminal proof. A model must not interpret missing or stale evidence as a reason to
start compensation.

If the goal fails, the agent may propose `BeginCompensation`. That request cannot name a rollback.
The kernel changes phase only when effects are settled and an eligible obligation exists. On later
turns, it advertises only the compensation actions in the planner's safe frontier.

Payment policy is application code: the expected order, customer, amount, and currency come from
authoritative provider state. A refund must also match the confirmed charge operation and its
receipt. The provider enforces the same terms and persists captured/refunded amounts; terminal
invariants verify those exact values rather than trusting a status flag.

## Native calls in two trajectories

The optional live driver uses Pydantic Deep native deferred tools end to end. In these abbreviated
transcripts, `MODEL -> native tool call` is business intent.
`KERNEL -> observed result` is the next public observation only after the deterministic kernel has
validated policy, persisted intent, dispatched the registered adapter, and stored the outcome.

### Happy path

```text
MODEL -> native tool call
check_inventory(sku="sku_travel_pack", quantity=1)
KERNEL -> observed result
read_observed(available=2, reserved=0, version=1)

MODEL -> native tool call
reserve_inventory(order_id="order_demo_001", sku="sku_travel_pack",
                  quantity=1, expected_version=1)
KERNEL -> observed result
effect_confirmed(tool="reserve_inventory")

MODEL -> native tool call
inspect_order(order_id="order_demo_001")
KERNEL -> observed result
read_observed(payment="open", fulfillment="pending")

MODEL -> native tool call
charge_payment(order_id="order_demo_001", customer_id="customer_demo_001",
               amount_minor=7900, currency="USD")
KERNEL -> observed result
effect_confirmed(tool="charge_payment")

MODEL -> native tool call
inspect_order(order_id="order_demo_001")
KERNEL -> observed result
read_observed(payment="captured", fulfillment="pending")

MODEL -> native tool call
schedule_fulfillment(order_id="order_demo_001")
KERNEL -> observed result
effect_confirmed(tool="schedule_fulfillment")

MODEL -> native tool call
inspect_order(order_id="order_demo_001")
KERNEL -> observed result
read_observed(payment="captured", fulfillment="scheduled")

MODEL -> native tool call
finish_saga(target_status="succeeded_verified", rationale="Fresh proof satisfies the goal.")
KERNEL -> observed result
terminal_assigned(status="succeeded_verified")
```

### Failure after charge

The fulfillment attempt is confirmed but leaves authoritative state `rejected`. The model requests
compensation; the kernel derives and reveals one safe rollback frontier at a time.

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
terminal_assigned(status="compensated_verified")
```

The sequence `cancel_fulfillment -> refund_payment -> release_inventory` is not prompt authority.
It is recomputed from confirmed receipts and compensation dependencies after each result.

For an unknown-outcome reconciliation, the runtime pauses model turns and reconciles the original
stable operation identity. A confirmed or absent result becomes fresh evidence; an outcome that
remains unknowable becomes quiescent `HUMAN_REQUIRED`. Human escalation is also appropriate for a
proven missing authority, inadequate recovery guarantee, or unverifiable compensation—not for an
ordinary business failure with a safe compensation frontier.

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
denominators. Adversarial cases contain typed hostile evidence and must remain 100% safe; exact
kernel rejection is reported separately only for cases that explicitly require a named rejection.
Five escalation cases require a human pause, while the budget-stop case requires a proof-backed
clean abort because no external effect exists. Each case's `max_agent_turns` is also its real runtime
and model-context turn limit; elapsed and token budgets retain the manifest's 30-second and
1,000-token allowance per turn, including after a durable reopen.

## Opt-in live evaluation

First validate all 24 cases for free. This loads the strict corpus and makes no driver or network
call:

```bash
uv run python -m examples.ecommerce.eval
```

Live use costs money. It requires both an OpenRouter key and explicit consent:

```bash
cp .env.example .env
chmod 600 .env
# Edit .env and set OPENROUTER_API_KEY to your own key.
RUN_LIVE_MODEL_EVALS=1 uv run python -m examples.ecommerce.eval --live \
  --samples 3 --output .artifacts/eval
```

The evaluator loads `.env` only for an explicit `--live` run. The file is ignored by Git, and
process environment values take precedence.

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

The manifest's compact trajectories are part of the validated context supplied to the model. They
show observation-to-intent examples for forward success, stale-evidence refresh, already-satisfied
effects, requesting kernel-owned compensation, waiting for runtime reconciliation, authority and
recovery-horizon escalation, and a proof-backed clean abort. During compensation, each example uses
only the one rollback action currently advertised by the kernel; it never invents or pre-orders a
future rollback. These examples guide planning, while deterministic policy remains the safety
boundary when a model chooses badly.

This example deliberately contains no workflow server, UI, ecommerce branch in the generic
package, or second Saga engine. The provider is a deterministic simulation, not a production
commerce integration. The optional Pydantic Deep/OpenRouter adapter is not needed for the free
offline proof.
