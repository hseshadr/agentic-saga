# Operations and release contract

TL;DR: Agentic Saga is a non-hosted Python library with a loopback-only, read-only Flight
Recorder. The deterministic kernel makes local intent and evidence durable before trusted
application adapters contact external systems. It cannot make a remote provider transactional,
contain hostile Python code, or resolve an outcome that remains unknown after safe reconciliation.

This document is the required-before-release operational contract for v0.1. The limits and
evidence commands below are acceptance criteria, not proof that the current source checkout passes
them. The
[kernel safety contract](kernel-safety-contract.md) maps kernel claims to executable tests; this
page defines the surrounding threat, privacy, recovery, resource, and release boundaries.

## Release workload

The final exact-commit release proof must cover:

- four real deterministic ecommerce example runs: verified success, a lost response reconciled
  after restart, verified compensation, and failed compensation parked in `HUMAN_REQUIRED`;
- the 24-case deterministic offline evaluation corpus plus
  offline adapter construction and consent guards;
- the packaged Flight Recorder at 1440, 1100, 900, 390, and 320 CSS pixels, 200% text zoom, and
  reduced motion; and
- one wheel installed on Python 3.12 and 3.13 plus a frozen Node 24/pnpm 11.5.0 frontend build.

This release proof excludes live paid model calls unless separately authorized outside the release
workflow. A live evaluation measures model quality; it is not release-correctness evidence.

Ecommerce is executable example code outside the domain-neutral package. The wheel contains only
the four generated, redacted reference traces; it does not contain or import the ecommerce runtime
or provider.

## Threat model

### Assets and actors

The protected assets are external business effects, durable Saga evidence, provider identities and
receipts, human-resolution authority, credentials, application data, and bounded host resources.

- **Untrusted data:** model proposals, provider responses, persisted inputs, imported trace JSON,
  manifest YAML, CLI arguments, and loopback HTTP requests.
- **Cooperative application code:** installed `AgentDriver` and `EffectAdapter` implementations.
  They run with host-process authority and must honor cancellation, stable identity, fencing, and
  their declared provider capabilities.
- **Outside the boundary:** hostile Python or native plugins and a hostile same-user process. Isolate
  them in another process, container, account, or service; the library is not a sandbox.

### Entry points and fail-closed controls

| Entry point | Representative abuse | Control |
| --- | --- | --- |
| `saga.yaml` and tool descriptors | Unsafe YAML, secret material, catalog drift, or an unregistered capability | Bounded safe loading, strict fields, secret-pattern defense in depth, digest pinning, and registered-name resolution before agent execution |
| Agent proposal | Invented tool, stale state, exhausted allocation, or an attempt to execute directly | One strict typed proposal; policy, sequence, limits, identity, and eligibility are recomputed by the kernel |
| Provider outcome | Forged success, lost response, or duplicate effect | Durable intent first, stable operation identity, capability proof, provider deduplication/fencing where declared, and deterministic reconciliation |
| Human decision | Forged, stale, replayed, or unrelated approval | Application-supplied authentication plus exact Saga ID, sequence, public decision fields and canonical digest, and single-use decision ID binding |
| SQLite store or backup | Stale projection, tampering, partial write, or unsafe replacement | POSIX path/identity checks, private files, transactions, append-only ledger, replay verification, fresh-only watermarked backup/restore, and typed corruption failures |
| Trace/index import | Traversal, digest drift, oversized/deep JSON, or false proof | Strict names, streamed byte limits, SHA-256 corruption check, strict `RunTrace` validation, graph limits, and proof-chain checks |
| Loopback recorder | Remote exposure, DNS rebinding, unsafe method/path/MIME, slow headers, excessive concurrency, oversized input, or browser egress | Bind only `127.0.0.1`; require the exact Host and validate optional Origin and Sec-Fetch-Site headers when present; serve GET/HEAD only; cap handlers, header time, paths, headers, and files; reject symlinks; fixed MIME/security headers; restrictive CSP; browser network assertions |

### Accepted residual risks

- At-least-once attempts can contact a provider more than once after ambiguity. Business-effect
  deduplication depends on the provider honoring the stable operation identity for its declared
  recovery horizon.
- SHA-256 trace and artifact digests detect corruption; they are not authenticity if an attacker can
  replace both content and digest.
- SQLite assumes one host and truthful local locking, atomic rename, flush, and sync semantics. A
  network filesystem, shared-volume multi-host writer, or storage layer that acknowledges lost
  writes is unsupported.
- The SQLite store requires POSIX file controls and an immediate parent owned by the effective user
  with no group/world write bits. It hardens leaves to mode `0600`, but ACLs, root, and a hostile
  same-UID process remain outside the boundary.
- The recorder exposes redacted evidence to processes and browsers owned by the same local user
  while it is running. It is not an authenticated multi-user service.
- Every recorder request requires exactly `Host: 127.0.0.1:<actual-port>`. `Origin` may be absent;
  when present, it must be that exact loopback origin. `Sec-Fetch-Site` may also be absent; when
  present, it must be `same-origin` or `none`. Only `GET` and `HEAD` are served; accepted
  connections have a one-second header deadline and share eight handler slots.
- Generic JSON is bounded by the core, but provider/domain objects may need smaller limits.
  Adapters must reject provider-specific excess before persistence. The store has no global
  database quota; the operator must monitor and bound durable storage.
- POSIX descriptor anchoring prevents the materializer from overwriting or deleting a competing
  destination. A hostile same-user rename race may leave an empty renamed directory; it cannot make
  the materializer delete foreign contents. The demo's temporary parent is removed during normal
  shutdown, but applications should remove any empty orphan they own after an interrupted run.

## Privacy, egress, retention, and deletion

Each exact `SagaDefinition` owns the immutable `RedactionPolicy` applied across its runtime and
historical trace export. Built-in credential and payment-secret detection is a non-removable floor,
not a general data-classification policy. The host application must explicitly list every ordinary
PII key that it allows into Saga values. Unlisted fields are public by contract. The host also owns
what may reach a provider and configures lawful retention and deletion.

| Data | Destination and egress | Retention and deletion |
| --- | --- | --- |
| `saga.yaml` | Public local authoring input. The built-in loader rejects credential-shaped and selected payment-secret material, but does not classify ordinary PII. | Source-control/file lifetime. Never put PII, credentials, receipts, or private provider data here. |
| Objective, resolved public context, current observation, eligible schemas | Local by default. Sent to OpenRouter only by the explicit optional adapter/evaluator path. The exact definition-owned policy redacts classified keys before runtime observations or evidence are rendered; manifest context must already be public. | Host-owned memory and durable evidence. The host chooses lawful retention and deletes its stores/backups. |
| `OPENROUTER_API_KEY` | Read from the process environment or injected settings; masked and excluded from prompts, metadata, and safe exceptions. | Process/configuration lifetime. Rotate and revoke through OpenRouter; never store it in `saga.yaml`. |
| Model/provider response | Untrusted response data enters strict validation. Ordinary gates make no model call. | Only validated, redacted public evidence may be persisted. Raw provider errors, headers, bodies, URLs, and causes are not retained by the adapter error contract. |
| SQLite kernel evidence | POSIX local filesystem only; no library telemetry or automatic upload. Database, `-wal`/`-shm`, temporary, final-backup, and restored leaves are hardened to mode `0600`. | Persists until the owner deletes the database, `-wal`, `-shm`, temporary files, and every backup. Stop readers and writers before deleting the complete set. |
| Logs and errors | The candidate console demo is quiet by default and returns bounded safe errors. Verbose loopback access logging is opt-in. | Controlled by the invoking process or log collector. Applications must not log raw prompts, tokens, receipts, private rationale, or unredacted payloads. |
| Browser replay state | Loopback document, static assets, index, and selected redacted trace only. No telemetry, font, CDN, WebSocket, or external runtime request. | In-memory page state; closing or reloading the page discards selection and replay position. |
| Temporary recorder site | Selected redacted trace plus distribution-bound static assets in an owner-controlled temporary directory. | Removed after clean shutdown or Ctrl-C. After abnormal termination, the OS/application owner removes its temporary directory and any empty owned orphan. |
| Exported traces | Written only when the owner requests an export; the recorder renders their redacted fields. | Persist until the owner deletes every copy. Redaction is the exporter's responsibility; the browser is not a secret scrubber. |
| Live-evaluation artifacts | Opt-in OpenRouter egress; verified redacted samples and reports under the chosen output directory. | Persist until the owner deletes the output directory. Resume verifies self-digests but does not provide attacker-resistant authenticity. |

## Reliability contract

### Guarantees and limits

- Durable intent precedes every external effect dispatch.
- Attempts are at least once. Stable identity, provider deduplication, fencing, and reconciliation
  make retries safe only where the registered provider contract says they are safe.
- Unknown effects are never guessed or blindly repeated. Reconciliation establishes success,
  failure, safe same-ID retry, a bounded wait, or durable `HUMAN_REQUIRED`.
- Compensation is a new verified business effect in reverse dependency order, not erased history.
- Fresh invariant evidence is required before a terminal success, compensated, or clean-abort state.
- Turn, tool-call, output-token, and agent-call-time allocations are durable and deterministic.
  Token/time limits are not input-token, actual-usage, end-to-end-time, or monetary meters.
- The SQLite implementation is a single-host reference backend. It does not provide high
  availability, serializable cross-Saga isolation, or atomic cross-service commit.
- Hosted availability is **N/A**: v0.1 is a library and local loopback viewer, not a hosted service.

### Shared mutable state

| State | Owner and serialization rule |
| --- | --- |
| SQLite database and `-wal`/`-shm` sidecars, ledger, projections, outbox, reconciliation jobs, leases, receipts | One local host; transactions plus lease/fence checks cover the complete check-to-commit boundary. |
| External provider state | Provider-owned; adapters must use the kernel operation identity and truthfully declare deduplication/fence support. |
| Backups and restore destination | Operator-owned, fresh paths only; the store verifies a private temporary backup and its watermarks before no-overwrite publication, and verifies a restored store before use. |
| Deep Agents profile registry | Process-wide cooperative integration state; isolate graphs in separate processes when the same exact model needs a different profile. |
| Recorder materialization directory | One materializer owns a fresh destination through anchored POSIX descriptors; it never overwrites a competitor. |
| Loopback port and server thread | One CLI process; bind is loopback-only and shutdown closes the server before temporary cleanup. |
| Browser selection and replay cursor | One page instance; changing a run resets local state and creates no durable Saga mutation. |

### Time, retry, and recovery semantics

Default effect and reconciliation calls time out after 30 seconds; command and reconciliation claims
last one minute; Saga and reconciliation leases last five minutes. The default reconciliation
horizon reserves up to five minutes of retry delay, a one-hour operator-response window, and 30
seconds of clock-skew allowance. Applications may provide positive alternatives and must align the
provider's idempotency retention with the resulting total horizon.

Under the documented SQLite/filesystem assumptions, a transaction that returns successfully has a
local committed-ledger RPO of zero. A crash before commit may require retry; an ambiguous provider
effect requires reconciliation. Backup RPO is the operator's last verified backup. There is no
universal wall-clock RTO because provider availability and human review are outside the library.
The packaged-demo release objective is a ready URL within the measured budget below;
`HUMAN_REQUIRED` intentionally has no automatic RTO.

## Operator runbook

1. **Prepare.** Use POSIX local storage and a current-user-owned immediate parent with no
   group/world write bits. Reserve space for the database, `-wal`, `-shm`, temporary backup, and
   final backup, and use one active application host. Register typed adapters, an exact
   definition-owned redaction policy including ordinary PII keys, policy/invariant checks, provider
   capability proofs, and an authenticated human-resolution verifier.
2. **Start.** Open an existing store only through its verified open path, or initialize a fresh v0.1
   store. Treat schema/corruption failures as a stop; never delete or recreate evidence automatically.
3. **Run and observe.** Alert on typed store corruption, repeated lease contention, exhausted budgets,
   reconciliation waits, and `HUMAN_REQUIRED`. Preserve the durable public packet and correlation
   identifiers, not raw secrets or provider exception text.
4. **Restart after a crash.** Reopen and verify the store, then resume due outbox and reconciliation
   work with a fresh lease. Keep the same operation identity. Never infer an external outcome from a
   missing local response.
5. **Resolve `HUMAN_REQUIRED`.** Stop automatic work for that Saga. An authorized operator examines
   redacted provider evidence, chooses the application-approved action, and submits an authenticated
   decision. The application-supplied verifier may require a cryptographic signature. The decision
   binds the Saga ID, current sequence, public decision fields and their canonical digest, and a
   single-use decision ID. Never edit the ledger or database directly.
6. **Back up.** Use `SQLiteKernelStore.backup_to` to a different, fresh path in an
   owner-controlled secure parent. Existing destinations and sidecars are rejected; no overwrite
   mode exists. Preserve the source until the private temporary backup is verified and published.
   Keep backups under the source's access, encryption, retention, and deletion policy.
7. **Restore local evidence.** Stop writers, restore into a fresh destination with
   `SQLiteKernelStore.restore_from`, let open/replay verification complete, then point the
   application at the verified destination. Restoring local SQLite evidence cannot undo, reverse,
   or otherwise roll back a provider effect.
8. **Handle corruption or disk exhaustion.** Stop dispatch, preserve the database, `-wal`, and `-shm` for
   diagnosis, free space without deleting evidence, and restore the latest verified backup to a new
   path. Escalate if provider state may have advanced beyond local evidence.
9. **Handle lease contention.** Do not bypass or lengthen a live lease ad hoc. Confirm there is one
   intended host, allow expiry or release, and acquire a fresh fence before resuming.
10. **Stop the demo.** Press Ctrl-C. The server closes before its temporary directory is removed. If
    startup or cleanup fails, remove only the temporary paths owned by that invocation; never delete
    a destination that another process substituted.

## Numeric resource limits and release evidence

| Area | v0.1 budget |
| --- | --- |
| Core safety coverage | At least 90% branch coverage; changed Python/TypeScript safety logic at least 90% focused branch coverage |
| Python complexity | Xenon grade A; functions no longer than 15 lines unless independently justified |
| Core JSON | Depth 16, 4,096 nodes, 256 items per container, 16 KiB per UTF-8 string, and 64 KiB encoded |
| Manifest | At most 64 KiB, 16 levels, and 4,096 nodes; `turn_limit`, `tool_call_limit`, `token_limit`, and `elapsed_ms_limit` are positive; token and elapsed limits are each at least the turn limit. Programmatic budgets may use zero for no configured or remaining capacity |
| Optional model turn | One model call, eight graph steps, zero SDK retries; fixed `token_limit // turn_limit` maximum-output allocation and `elapsed_ms_limit // turn_limit` agent-call deadline, further capped by adapter settings |
| Provider spend | Not metered or capped by this library; operator-owned provider account controls are required for live calls |
| Recorder bundle | JavaScript at most 110 KiB gzip; CSS at most 5 KiB gzip |
| Reference catalog | Exactly four traces; at most 1 MiB total |
| Import boundary | At most 100 runs, 8 MiB per trace, 512 KiB index, 16 JSON levels, and 100,000 JSON nodes |
| Materializer/server | POSIX descriptor materialization; at most 200 static files and 32 MiB static bytes; exact required `Host`, independently optional validated `Origin` and `Sec-Fetch-Site`, GET/HEAD only, eight active handlers, one-second accepted-header deadline, 240-character request path, 40 headers/16 KiB header name/value bytes, and 8 MiB served file |
| SQLite files | Secure current-user, non-shared-writable immediate parent; regular mode-`0600` database, sidecar, temporary, backup, and restore leaves; fresh destinations only |
| SQLite contention | Five-second busy timeout; lease durations must be positive and at most one day |
| Offline Saga runtime | Each of the four reference scenarios p95 at most 2 seconds over 20 in-process, fresh temporary-store runs |
| Packaged demo start | Ready loopback URL p95 at most 3 seconds over 10 cold temporary runs after dependencies are installed |
| Browser first usable | Recorded outcome visible p95 at most 2 seconds over 10 packaged local navigations |
| Loopback server | `GET /` p95 at most 100 ms over 100 warm requests |
| Materialization | Four-trace materialization p95 at most 500 ms over 25 runs |
| Representative Python memory | Peak RSS at most 256 MiB during the measured four-scenario/materialization workload |
| Maximum-input rejection | An over-limit trace is rejected within 1 second and 256 MiB peak RSS |
| Browser behavior | No document overflow at required widths, no initial autoplay or external request, and no critical/serious axe violation |

The required final release-measurement interface is:

```bash
uv run python scripts/measure_release.py
```

The implemented command records the full source commit and dirty/clean state, builds and installs a
wheel from locked inputs with network access and Python downloads disabled, runs the Python and
frontend quality gates, exercises the four reference scenarios and packaged Chromium recorder, and
evaluates every budget above. It fails the release environment when the tree is dirty, the commit is
not a full SHA, or the toolchain is outside Python 3.12/3.13, Node 24, and pnpm 11.5.0.

The report must record the exact commit, dirty/clean state, OS, CPU, Python, Node, and pnpm versions;
sample counts; cold and warm measurements; p50/p95 and relevant maxima; bundle and catalog bytes;
peak memory; and pass/fail against every budget. Measurements from a dirty tree are diagnostic only.

Release evidence additionally requires exact-commit lint, format, strict types, tests and branch
coverage, Xenon, the bounded mutation gate, wheel/sdist build, clean Python 3.12/3.13 wheel installs,
packaged-demo browser proof, dependency audit, and secret scan. CI contains the dual-Python package
and browser matrix, but configuration is not proof: the final clean-current-commit report and
matching hosted run still must be recorded. By contract, ordinary gates and the measurement command
make no paid model call. Live evaluation can cost money and requires separate consent plus
provider-account spend controls. Package publication requires separate, fresh authorization.

The source distribution must include `CHANGELOG.md`, `docs/flight-recorder.md`,
`docs/architecture.html`, and this operations contract. The package contract test verifies those
files, all resolvable relative documentation links, safe archive paths and member types, and exact
wheel static/reference bytes and third-party notices. The final exact-commit archive result is
required release evidence.
