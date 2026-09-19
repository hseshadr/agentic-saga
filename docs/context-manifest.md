# Saga context manifest

## TL;DR

`saga.yaml` is a public context template for the agent. It describes the goal, registered
capability names, safety guidance, decision budgets, named checks, and short example paths.

It is deliberately **not** a workflow language. It has no transitions, conditions, embedded code,
provider credentials, or executable tool definitions. Temporal owns durable execution; the
application registers typed tools; the agent chooses one currently eligible forward action.

## Why keep context in a file?

Agents make better decisions when the objective, vocabulary, constraints, and examples are
explicit. Keeping that public context in a small YAML file lets domain experts review it without
turning prose into transaction authority.

The split is intentional:

| Concern | Source of truth |
| --- | --- |
| Objective, guidance, examples | `saga.yaml` |
| Tool schemas and provider adapters | application code |
| Eligibility, prerequisites, budgets, proof, compensation | deterministic Temporal Workflow |
| Durable history, retries, recovery, timers | Temporal Service |
| One next forward choice | agent adapter |

## Full shape

```yaml
schema_version: "1.0"
name: ticket_booking
version: "1.0"
objective: Book the requested itinerary or restore a verified safe state.
instructions:
  - Inspect authoritative availability before proposing an effect.
  - Choose exactly one currently eligible forward tool.
success_criteria:
  - Exactly one acceptable ticket is issued and its payment is confirmed.
autonomy:
  mode: guarded
  instructions:
    - Never guess when provider evidence is ambiguous.
budgets:
  turn_limit: 18
  tool_call_limit: 14
  elapsed_ms_limit: 120000
  token_limit: 10000
tools:
  catalog_sha256: "<digest of the selected registered descriptors>"
  allowed:
    - search_itineraries
    - hold_itinerary
    - charge_payment
    - issue_ticket
checks:
  policy:
    - fare_within_limit
    - traveler_authorized
  success:
    - payment_captured
    - ticket_issued
  compensation:
    - hold_released
    - payment_refunded
  clean_abort:
    - no_active_booking
escalation:
  conditions:
    - Compensation remains unresolved after safe retries and reconciliation.
  instructions:
    - Present redacted evidence to an authorized operator.
example_paths:
  - name: ticket issue fails after payment
    kind: compensation_path
    narrative:
      - The agent proposes forward actions from fresh evidence.
      - Failed final proof makes the Workflow compensate confirmed effects in reverse order.
```

Compensation tools may be included in the catalog digest because they are registered capabilities,
but the Workflow never advertises them as forward agent choices. The Workflow invokes the paired
compensation when recovery is required.

## Load and validate it

Register tools and named checks in application code, then load the manifest against those exact
inventories:

```python
from pathlib import Path

from agentic_saga import SagaManifest, load_saga_context

selected_tools = (
    "charge_payment",
    "hold_itinerary",
    "issue_ticket",
    "search_itineraries",
)
digest = SagaManifest.tool_catalog_sha256(tool_registry, selected_tools)

context = load_saga_context(
    Path("saga.yaml"),
    registry=tool_registry,
    policy_checks=("fare_within_limit", "traveler_authorized"),
    invariant_checks=(
        "hold_released",
        "no_active_booking",
        "payment_captured",
        "payment_refunded",
        "ticket_issued",
    ),
)
```

Pin `digest` as `tools.catalog_sha256`. Loading fails if a selected descriptor changes without a
manifest update. `context.agent_context` is deterministic public JSON for the decision adapter,
`context.tool_descriptors` contains the validated schemas, and `context.budget` is the bounded
planning budget used by the Temporal Activities.

The executable ecommerce manifest is at
[`examples/ecommerce/saga.yaml`](../examples/ecommerce/saga.yaml). The independent
[`examples/ticket-booking/saga.yaml`](../examples/ticket-booking/saga.yaml) shows that the format is
not commerce-specific.

## How examples guide the agent

Example paths teach a decision pattern; they do not create branches. A useful compensation example
says:

```text
1. Use fresh provider evidence for the next eligible forward choice.
2. If final proof fails, stop proposing forward mutations.
3. Let the Workflow compensate the confirmed effects.
4. Require a verified human only if automatic recovery remains unresolved.
```

Do not teach the model to call rollback tools, choose compensation order, declare an unresolved
outcome safe, or self-authorize a human resolution. Those are deterministic responsibilities.

## Budgets

All four manifest budgets are positive and bounded:

- `turn_limit`: maximum agent decisions;
- `tool_call_limit`: maximum business-tool calls across the Saga;
- `elapsed_ms_limit`: planning-time allocation passed to adapters; and
- `token_limit`: output-token allocation passed to adapters.

The Workflow independently enforces `max_agent_turns`, the global tool-call ceiling, per-tool call
limits, and prerequisites. Provider account spend limits and end-to-end Workflow timeouts remain
application and infrastructure responsibilities.

## Fail-closed loading

The loader rejects duplicate keys, unknown YAML tags, aliases or cycles, multiple documents,
missing or extra fields, unknown tool/check names, descriptor-digest drift, credential-shaped
content, and oversized or deeply nested input.

Limits are 64 KiB of YAML, depth 16, and 4,096 nodes. Generic JSON boundaries also cap depth,
nodes, container size, string length, and encoded size.

The manifest is public by contract. Never put credentials, names, email addresses, phone numbers,
postal addresses, account identifiers, receipts, or provider data in it. Pattern checks are defense
in depth, not a general personal-data detector.

## Deliberate non-features

There is no condition language, transition graph, embedded Python, provider configuration, secret
store, fixed step order, or tool schema DSL. Add a capability by registering another typed Lego;
add context by editing the public template; keep transaction authority in deterministic code.
