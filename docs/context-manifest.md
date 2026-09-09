# Saga Context Manifest

## TL;DR

`saga.yaml` tells an agent what outcome to pursue and supplies validated limits and check references
for explicit application assembly. It does not define a workflow or configure the kernel by itself.
The agent explores the registered tool catalog and chooses one next action from current evidence.

## Load a manifest

Register typed tools and deterministic checks in application code, then validate the manifest's
references against their name inventories:

```python
from pathlib import Path

from agentic_saga import SagaManifest, load_saga_context

tool_names = ("check_inventory", "reserve_inventory", "release_inventory")
digest = SagaManifest.tool_catalog_sha256(tool_registry, tool_names)

context = load_saga_context(
    Path("saga.yaml"),
    registry=tool_registry,
    policy_checks=("customer_authorized",),
    invariant_checks=("order_fulfilled", "inventory_released", "no_external_effects"),
)
```

`digest` is the value to pin as `tools.catalog_sha256` whenever the selected registered descriptor
schemas change. `context.agent_context` is deterministic structured JSON for an agent driver;
`context.tool_descriptors` contains the authoritative schemas; and `context.budget` is the existing
kernel `ExecutionBudget`.

The four manifest budget fields are positive deterministic limits: agent turns,
business-tool calls, output-token allocation, and agent-call milliseconds. Token and elapsed
limits must each be at least the turn limit, so every configured turn has one whole unit. The
runtime reserves the fixed floors `token_limit // turn_limit` and
`elapsed_ms_limit // turn_limit` on every turn. Programmatic `ExecutionBudget` values may use zero
to represent no configured or remaining capacity. These values do not measure input tokens, actual
provider usage, end-to-end Saga time, money, or provider spend.

The optional [Deep Agents/OpenRouter adapter](agent-adapter.md) consumes this resolved context as
its system context and renders only the currently eligible descriptor subset on each turn.

This authoring facade does not install check callables, construct a `SagaDefinition`, or enforce an
allowlist inside `SagaRuntime`. The application layer must explicitly assemble those returned inputs
with its registered policy/invariant implementations. The executable ecommerce assembly is shipped
as the reference application; run its four-scenario offline proof with:

```bash
uv run --no-dev python -m examples.ecommerce.run
```

For an executable proof using real registries and both included manifests, run:

```bash
uv run pytest tests/integration/manifest/test_examples.py -q
```

## Authoring shape

Use [the ecommerce manifest](../examples/ecommerce/saga.yaml) as the full reference and
[the ticket-booking manifest](../examples/ticket-booking/saga.yaml) as the portability example.
Both use this domain-neutral shape:

```yaml
schema_version: "1.0"
name: ticket_booking
version: "1.0"
objective: Book the requested itinerary or restore a verified safe state.
instructions:
  - Inspect authoritative availability before proposing an effect.
success_criteria:
  - Exactly one acceptable ticket is issued and payment is confirmed.
autonomy:
  mode: guarded
  instructions:
    - Escalate rather than guessing when evidence is ambiguous.
budgets:
  turn_limit: 18
  tool_call_limit: 14
  elapsed_ms_limit: 120000
  token_limit: 10000
tools:
  catalog_sha256: "<digest from the registered descriptors>"
  allowed:
    - search_itineraries
checks:
  policy:
    - traveler_authorized
  success:
    - ticket_issued
  compensation:
    - hold_released
  clean_abort:
    - no_active_booking
escalation:
  conditions:
    - A provider outcome remains unknown after safe reconciliation.
  instructions:
    - Present durable redacted evidence and the unresolved decision.
example_paths:
  - name: ticket issue fails after payment
    kind: compensation_path
    narrative:
      - Refund confirmed payment, release the hold, and prove no active booking remains.
```

Instructions, success criteria, escalation guidance, and example paths give the agent useful
context. Tool/check names, the catalog digest, and budgets are machine-checked. Example paths are
stories, not executable branches; order and recovery choices remain the agent's responsibility.

## Fail-closed boundary

Loading fails before agent execution when YAML contains duplicate keys, unsafe or unknown tags,
container aliases/cycles, excessive size or depth, multiple documents, or invalid structure; when
strict fields are missing or unknown; when text matches known high-confidence credential patterns;
when a referenced tool or check is not registered; or when the descriptor digest has drifted.
Registered descriptor payloads are checked for private material before digesting or rendering, so
credential-shaped schema keys, defaults, or descriptions also fail closed.

The YAML source is capped at 64 KiB, depth 16, and 4,096 nodes. After validation, every generic JSON
value entering the core is capped at depth 16, 4,096 nodes, 256 items per container, 16 KiB per
UTF-8 string, and 64 KiB encoded.

`saga.yaml` is deliberately public. Its built-in scan rejects credential-shaped and selected
payment-secret material, but it is not a general PII detector. Never put names, email addresses,
phone numbers, postal addresses, account identifiers, receipts, or provider data in the manifest.
The application must construct an exact `SagaDefinition` whose immutable `RedactionPolicy`
explicitly lists every ordinary personal-data key allowed into runtime values. That exact policy is
then used throughout runtime evidence and trace export; unlisted ordinary fields are public by
contract.

`SagaDefinition` also pins the executable safety contract used by durable resumes: typed tool
schemas and model source, adapter identities, policy behavior, all four terminal requirements,
budgets, and redaction. Policy rules that call transitive helpers or depend on other non-JSON
globals must use `VersionedPolicyRule`; bump its public behavior version whenever that dependency
changes. Composition rejects a `TerminalGate` whose requirements differ from the definition.
Policy-context and invariant-evidence providers remain trusted host collaborators, so applications
must bump the Saga or invariant version whenever their behavior changes.

Tool schemas and adapters remain in the registered catalog. Live Saga state, operation identity,
idempotency keys, receipts, recovery evidence, and secrets remain in durable storage or application
configuration—never in `saga.yaml`. Pattern checks are defense in depth, not a complete secret
scanner; repository secret scanning remains required.

## Deliberate non-features

The manifest has no condition language, expressions, transitions, fixed step order, embedded code,
provider configuration, or tool schemas. Add capabilities by registering another typed tool or
named check—the Lego boundary—not by extending a workflow DSL.
