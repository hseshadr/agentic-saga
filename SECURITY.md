# Security policy

TL;DR: Agentic Saga keeps orchestration deterministic, but Temporal records Workflow and Activity
payloads durably. The default contracts are for public-safe data. Never put credentials or private
customer data into them. A production system that must carry private payloads needs client-side
payload encryption backed by its KMS, tightly scoped Temporal Namespace access, and application
authentication for every human action.

## Supported version

Security support covers the `0.1.x` line.

## Report a vulnerability

Email [harish.seshadri@gmail.com](mailto:harish.seshadri@gmail.com). Do not open a public issue for
a suspected vulnerability. We aim to acknowledge a report within 72 hours.

Include the affected version or commit, reproduction steps, and sanitized logs. Never send an API
key, access token, raw prompt, private receipt, or unredacted customer payload.

## Security boundary

Agentic Saga provides typed orchestration rules on top of Temporal. It does not sandbox installed
Python code, make an external API transactional, or turn at-least-once Activity execution into
universal exactly-once delivery.

Treat agent decisions, provider responses, Workflow history, human-resolution requests, manifest
YAML, trace JSON, CLI arguments, and loopback HTTP requests as untrusted. Application Activities,
tool adapters, and agent drivers run with the Worker's process authority. Isolate hostile or
third-party code in a separate process, container, account, or service.

## Public-safe payload contract

Temporal Event History can contain Workflow inputs, Activity inputs and results, failure details,
and Update arguments. Search Attributes and Memos have their own visibility paths. Assume all of
them are durable records.

The built-in `connect_client` helper and Pydantic data converter are for public-safe payloads:

- use opaque business identifiers instead of names, emails, addresses, or account numbers;
- store references to secrets and documents, not the secret or document itself;
- resolve provider credentials inside Activities from a secret manager;
- reduce provider receipts to the minimum fields needed for reconciliation or compensation;
- turn raw exceptions into bounded, typed, non-sensitive failures before they leave an Activity;
- never put secrets or sensitive data in Search Attributes; and
- apply the same minimization to logs, traces, Queries, and the Flight Recorder.

Redaction is defense in depth for projections. It does not erase values already written to
Temporal history.

## Private production payloads

If private data must cross the Temporal boundary, the application must construct its Client and
Workers with the same client-side encrypted Data Converter and Payload Codec. Manage encryption
keys in an external KMS, rotate them through an explicit policy, and keep old key versions
available for every retained history that still needs replay or decryption. Temporal's default
converter does not encrypt application payloads. See the official
[Python data-handling guide](https://docs.temporal.io/develop/python/data-handling).

Encryption is not an access-control substitute. Production also requires:

- a dedicated Temporal Namespace with least-privilege roles for Workers, Clients, operators, and
  visibility users;
- namespace-scoped API keys or mTLS identities, stored outside Workflow history;
- retention matched to deletion and legal obligations;
- audit-log monitoring and credential rotation; and
- a separately secured Codec Server only if authorized operators need decrypted payloads in the
  Temporal Web UI.

Temporal documents Namespace isolation, scoped authentication, roles, and client-side encryption
in its [Cloud security model](https://docs.temporal.io/evaluate/cloud/security). Self-hosted
operators must provide equivalent authentication, authorization, encryption, audit, backup, and
network controls.

## Model and OpenRouter boundary

Ordinary tests and the recorded demo make no model call. A live model path is opt-in.

```bash
cp .env.example .env
chmod 600 .env
# Edit .env and set OPENROUTER_API_KEY to your own key.
```

`.env` is ignored by Git; `.env.example` contains no secret. Never put `OPENROUTER_API_KEY` in a
manifest, Workflow input, Activity argument, trace, log, or test fixture. The library does not
enforce provider spend. Configure provider-side budget and rate limits before a live run.

## Activity delivery and external effects

Temporal may retry an Activity after the provider completed an effect but its response was lost.
Every side-effecting tool needs a stable idempotency key, a typed receipt, reconciliation for an
unknown outcome, bounded timeouts/retries, and retryable versus non-retryable failure classes.

Do not blindly retry or compensate an unknown effect. Reconcile first. If the provider cannot prove
what happened, pause and require an authenticated human decision.

## Human actions

A Workflow Query is read-only. A human decision enters through a typed, validated Workflow Update.
The surrounding application must authenticate the person, authorize access to that Saga, bind the
decision to the current event sequence and operation, and reject stale or replayed decisions.
Temporal messaging is not the application's identity provider.

## Workflow safety

Workflow code must remain deterministic. Network calls, filesystem access, model calls, secret
lookups, and provider I/O belong in Activities. Replay representative production histories before
deploying changed Workflow code, and use a compatible Worker/versioning strategy for retained
executions.

## Local Flight Recorder

The Flight Recorder is a loopback-only, read-only viewer, not an authenticated multi-user service.
It binds to `127.0.0.1` and serves packaged, redacted evidence. Same-user or root access is outside
its boundary. Exported traces persist until the operator deletes every copy.

Read the [operations contract](docs/operations.md) and
[Temporal safety contract](docs/temporal-safety-contract.md) before production use.
