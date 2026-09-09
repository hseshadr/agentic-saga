# Security policy

TL;DR: private v0.1 protects typed Saga authority and local evidence under a cooperative-host,
single-host contract. Treat model/provider/persisted input as untrusted data, isolate hostile code
outside the process, and report suspected vulnerabilities privately.

## Supported version

While the project is private, security support covers the `0.1.x` line.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to
[harish.seshadri@gmail.com](mailto:harish.seshadri@gmail.com). Do not open a public vulnerability issue;
private reporting gives us an opportunity to investigate and coordinate a fix before details are
disclosed.

We will aim to acknowledge a report within 72 hours. Include enough detail to reproduce the issue,
the affected version or commit, and relevant logs with secrets and personal data removed. Do not
send live credentials, private prompts, raw receipts, or unredacted provider data.

## Scope and limitations

V0.1 is a single-host reference backend and loopback-only read-only viewer. It does not claim
universal exactly-once delivery, atomic cross-service commit, high availability, multi-host writer
safety, arbitrary-tool safety, or containment of hostile installed Python/native code. The
documented guarantees apply only to implemented typed interfaces and their stated storage,
filesystem, adapter, provider-idempotency, and cooperative-code assumptions.

All generic JSON accepted by core contracts is strict and capped at depth 16, 4,096 nodes, 256
items in any container, 16 KiB per UTF-8 string, and 64 KiB encoded. These are ingress bounds, not a
database quota; applications should impose smaller domain/provider limits and monitor durable
storage.

### Data classification and redaction

Each exact `SagaDefinition` owns one immutable `RedactionPolicy`. Composition carries that same
policy through policy constraints, observations, read/effect/reconciliation outcomes, durable
public evidence, and historical trace export. The replacement marker is always `[REDACTED]`.

The built-in policy detects credential-shaped keys and selected payment secrets. It is a minimum
floor, not a general personal-data classifier. Before accepting real data, the application must add
every ordinary PII key it allows—such as names, email addresses, postal addresses, phone numbers,
and account identifiers—to `sensitive_keys`. An unlisted ordinary field is treated as public. The
optional `saga.yaml` is public authoring context and uses the built-in credential checks before a
`SagaDefinition` exists; never put PII, credentials, receipts, or private provider data in it.

### SQLite and local files

`SQLiteKernelStore` is POSIX-only. Its immediate parent must exist, be owned by the effective user,
and have no group/world write bits. Database, `-wal`/`-shm`, temporary backup, final backup, and
restored leaves are regular current-user files hardened to mode `0600`; no-follow and identity
checks reject supported symlink/substitution races. Backup and restore accept fresh destinations
only and never overwrite an existing path.

This is a cooperative-host boundary. It assumes a truthful local filesystem with SQLite locking,
atomic publication, flush, and sync semantics. ACLs, root, or a hostile process with the same UID
can exceed mode-bit protection; network filesystems and shared multi-host volumes are unsupported.
The operator owns directory permissions, encryption at rest, quotas, backup retention, and secure
deletion of the database plus every sidecar and copy.

### Loopback Flight Recorder

The recorder is not an authenticated multi-user service. It binds only `127.0.0.1` and serves only
`GET` and `HEAD`. Every request must use the exact `Host: 127.0.0.1:<actual-port>`. `Origin` may be
absent; when present, it must equal that exact loopback origin. `Sec-Fetch-Site` may also be absent;
when present, it must be `same-origin` or `none`. The server allows at most eight active handlers,
gives accepted clients one second to finish request headers, and rejects paths over 240 characters,
more than 40 headers, more than 16 KiB of header name/value bytes, and files over 8 MiB. It rejects
symlinked content and unknown MIME types.

These controls limit accidental exposure and resource abuse inside a cooperative local account;
same-UID/root access remains outside the boundary. Trace digests detect corruption, not an attacker
who can replace both trace and digest. The exporter and its exact Saga redaction policy are the
privacy boundary; the browser is not a secret scrubber.

### Model-provider spend

The kernel records deterministic turn, tool-call, output-token, and agent-call-time allocations.
It does not meter money, input tokens, actual provider usage, or end-to-end elapsed time and cannot
enforce a provider spend cap. The maintained OpenRouter adapter uses the per-turn token allocation
as a maximum-output setting and the elapsed allocation as the call deadline. Operators must set
provider-account spend and rate limits before an explicitly authorized live evaluation. Ordinary
tests, demos, and release measurement make no live model call.

Read the [operations and release contract](docs/operations.md) for the complete threat model,
privacy and egress inventory, accepted residual risks, resource bounds, recovery procedure, and
operator responsibilities. Read the [kernel safety contract](docs/kernel-safety-contract.md) for
the exact claim-to-test evidence.
