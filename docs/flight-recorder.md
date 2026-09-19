# Saga Flight Recorder

TL;DR: the console command turns one real, redacted `RunTrace` 1.0 into a read-only causal signal
board on `127.0.0.1`. It never calls a model, executes a tool, resumes a Saga, or changes Saga
state.

## Run it

```bash
uv run --no-dev agentic-saga demo --scenario business-failure --open
```

The first dependency installation may access a package registry. The demo itself needs no
credential, model, or external runtime service. It materializes only the selected
distribution-bound trace, prints and flushes the actual loopback URL, waits for Ctrl-C, closes the
server, and removes its temporary site. Choose `happy-path`, `lost-response`, or
`compensation-failure` for another captured outcome. Omit `--open` when you want only the URL.

Frontend contributors can run the source workbench and its complete browser proof:

```bash
cd web/flight-recorder
npx --yes pnpm@11.5.0 install --frozen-lockfile
npx --yes pnpm@11.5.0 gate
```

## What the first screen proves

The top strip names the scenario, plain-language outcome, and verified safety-check count. Built-in
ecommerce traces also show the business flow: check stock, reserve the item, charge payment,
arrange delivery, and verify the order. If a later step fails, a separate lane shows cancel,
refund, and release progress in the safe reverse order. This business view is enabled by explicit
catalog metadata; generic trace catalogs continue to receive the generic recorder.

The trajectory lists stored runs. The center board keeps every durable event in history order across
Agent, Workflow guard, Effect + repair, and Proof lanes. The right inspector shows only recorded,
redacted evidence.

Select a signal with a pointer or keyboard. The board isolates all recorded events connected by
the same forward or compensation operation IDs. Every signal stays readable; visible `chain` and
`other` tags distinguish related from unrelated evidence without relying on color or dimming.
Amber dashed signals mark agent-originated activity, including the proposal lifecycle; blue is
deterministic authority/effects, the reverse arrow is compensation, teal is proof, and red plus text
marks a stop or human escalation.

`Agent proposes → Policy decides → Saga acts → Invariants prove`

## Replay and inspect

The first render shows the recorded outcome and never starts playback. Choose **Watch from start**
to return to the first event and begin immediately. One new event appears every 700 milliseconds so
a person can follow the forward work and reverse-order recovery. Use Previous, Next, Restart, or
Play for direct control; Left/Right, Home, and Space provide the same controls when focus is outside
an interactive element. Replay pauses at reconciliation, rejection, compensation, proof, and
human-review landmarks. With reduced motion enabled, automatic playback is disabled and manual
stepping remains available. Changing evidence views pauses playback; changing runs starts a fresh
local replay at that run’s recorded outcome.

This is a dynamic replay of captured evidence, not a claim that the backend is executing live. The
projection reads only events at or before the visible cursor, so future success, failure, recovery,
and proof do not leak into an earlier frame.

- **Story** translates recorded event types into fixed plain-language descriptions.
- **Ledger** filters only safe identifiers and receipt references, and renders at most 25 rows per
  page. It never searches payloads or structured rationale.
- **Proof** binds each rule to its exact invariant event, version, sequence, and target state.
  `HUMAN_REQUIRED` is a quiescent stop, not terminal proof.

The selected event, replay cursor, and view are local browser state. Changing runs resets them
predictably; the recorder does not create stale URL deep links. Flight Path and Story cap their DOM
at the latest 200 visible events, while the source trace remains available through bounded replay.

## Trust boundary

The browser accepts the current snake_case `RunTrace` contract projected from Temporal Workflow
state. The locked
`lossless-json` parser has no transitive dependencies and preserves numeric meaning across Python
and JavaScript. Python-generated hash goldens cover floats, exponent boundaries, negative zero,
and large integers. Strict validation rejects extra or missing fields, unsupported versions,
non-UTC or noncontiguous evidence, broken
status/time/identity chains, impossible calendar values, proof-source mismatches, evidence-hash
mismatches, and oversized, deep, cyclic, or excessively numerous values. UTC ordering uses exact
fractional units rather than millisecond browser dates. It does not synthesize an accepted-proposal
event.

The generic index accepts at most 100 unique runs. Each trace reference is one lowercase JSON file
name: absolute paths, dot segments, encoded traversal, backslashes, URL schemes, queries, and
fragments cannot match. The repository bounds streamed index and trace bodies before parsing and
verifies the trace SHA-256 from the index first. This digest detects corruption; it is not
authenticity against an attacker who can replace both the index and trace.

The package-resource materializer anchors every destination component through POSIX directory
descriptors, claims a fresh destination exclusively, bounds static/trace/index bytes, validates the
completed site, and never overwrites a competitor. The server binds only loopback, accepts GET/HEAD,
bounds paths, headers, and files, rejects symlinked content and unknown MIME, and sends a restrictive
CSP plus no-store, no-referrer, nosniff, and frame-denial headers. This protects against untrusted
data and accidental path races inside the cooperative same-user boundary; it is not a hostile
same-user filesystem sandbox.

The exact server boundary is `127.0.0.1` plus `GET` and `HEAD` only. A request must carry exactly
`Host: 127.0.0.1:<actual-port>`. `Origin` may be absent; when present, it must equal that exact
loopback origin. `Sec-Fetch-Site` may also be absent; when present, it must be `same-origin` or
`none`. At most eight handlers run concurrently, and an accepted connection has one second to
finish its request headers. Paths are capped at 240 characters, requests at 40 headers and 16 KiB
of header name/value bytes, and served files at 8 MiB. These controls are not authentication; root
or another hostile process with the same UID is outside the boundary.

Materialization requires POSIX descriptor operations, writes into a newly claimed destination, and
allows at most 200 static files and 32 MiB of static content. It never overwrites an existing
destination. Local-filesystem behavior, directory access, ACLs, same-UID mutation, retention, and
cleanup remain operator responsibilities.

Only redacted inputs, outputs, receipts, structured rationale, authority, state, and causal IDs are
displayed. Private chain-of-thought is neither expected nor rendered. The default Temporal
converter accepts only public/redacted Workflow payloads; private production history requires an
application-configured encryption codec and namespace access controls. Built-in credential and
payment-secret rules are only a floor, so applications must explicitly classify every ordinary PII
field they admit. Browser validation is defense in depth, not a secret scrubber. Generic JSON
fields entering the Python contract are capped at depth 16, 4,096 nodes, 256 items per container,
16 KiB per UTF-8 string, and 64 KiB encoded.

## Rebuild the real fixtures

The four catalog traces come from the real Temporal ecommerce Workflow with deterministic example
inputs. Regenerate the traces and their index digests from the repository root:

```bash
uv run python -m examples.ecommerce.export_flight_recorder
uv run pytest tests/integration/examples/test_flight_recorder_fixtures.py -q
```

The checked-in paths cover verified success, a lost response reconciled under the same stable
operation identity, verified compensation, and an unverifiable compensation that stops at
`HUMAN_REQUIRED`.

## Current boundary

V0.1 ships the source workbench, bundled static assets, four generated redacted traces, a
strict package-resource loader, exclusive materialization, loopback serving, the `agentic-saga demo`
entry point, a guided ecommerce flow, user-controlled Story/Ledger/Proof replay, keyboard and
reduced-motion behavior, responsive 320px layouts, and source plus packaged-wheel browser gates.
Every release candidate requires a clean exact-commit measurement and matching hosted CI evidence.

It is not a live monitor, workflow editor, operations control plane, general web API, hosted
service, or authenticity system. It shows captured evidence only. See the
[operations and release contract](operations.md) for privacy, local exposure, retention, cleanup,
resource limits, and measurement evidence.
