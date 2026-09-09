# Saga Context Manifest Vertical Slice

## TL;DR

Ship one small, domain-neutral authoring facade: a validated `saga.yaml` becomes deterministic
agent context plus machine-checked references to an existing tool registry and named application
checks. The manifest describes intent and guardrails; it is not a workflow language.

## Public contract

The YAML document contains:

- schema/name/version identity;
- objective, instructions, success criteria, autonomy guidance, escalation guidance, and
  non-binding example stories;
- typed execution budgets;
- a sorted allowlist of tool names and a SHA-256 digest of their authoritative descriptors;
- sorted references to registered policy, success, compensation, and clean-abort checks.

Tool schemas, adapters, callable checks, secrets, live state, operation IDs, receipts, and recovery
evidence do not belong in YAML. Tool schemas continue to come only from `ToolRegistry` and
`ToolDescriptor`.

The public Python surface is intentionally three names:

- `SagaManifest`: the immutable Pydantic boundary model;
- `SagaContext`: the immutable resolved result containing the manifest, canonical agent context,
  authoritative descriptors, and existing `ExecutionBudget`;
- `load_saga_context(path, *, registry, policy_checks, invariant_checks)`: bounded safe load,
  strict reference validation, and canonical rendering.

The facade does not retain check callables, construct `SagaDefinition`, or install a runtime
allowlist. The ecommerce application will explicitly assemble the returned inputs with its check
implementations in the next vertical slice.

## Safety and determinism

Use maintained `ruamel.yaml` in safe YAML 1.2 mode because it rejects duplicate mapping keys by
default and provides a native parser depth limit. Limit source bytes before parsing, reject unsafe
or unknown tags, reject multiple documents and cyclic/oversized structures, and let strict
Pydantic models reject unknown fields and coercion. Existing redaction plus high-confidence
credential-pattern checks provide defense in depth; repository secret scanning remains required.

Rendering is canonical JSON with sorted object keys. Tool and check references are normalized into
sorted unique tuples, and descriptors are resolved and sorted from the registry. A catalog digest
mismatch, missing tool, missing check, duplicate reference, unsafe input, or schema error fails
closed before an agent sees context.

## Portability proof

Add one polished ecommerce manifest and one tiny ticket-booking manifest. Both use the same schema;
only their registered tools, named checks, and narrative differ. Do not add a ticket UI, provider,
workflow graph, expression evaluator, or general DSL.

## Tests and release boundary

Drive implementation with focused unit and integration tests, including duplicate keys, unsafe
tags, depth/size bounds, secret-like content, missing bindings, digest drift, immutability, and
stable rendering. Do not add `pytest-bdd` merely to parse the manifest; reintroduce it only when
executable end-to-end feature files land with the ecommerce demo. README examples cover only this
shipped authoring slice. Deep Agents, OpenRouter, ecommerce execution, and the Flight Recorder
remain explicitly planned.
