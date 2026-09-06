# Saga Flight Recorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and package a static, accessible Saga Flight Recorder that turns a typed `RunTrace` into an offline scenario workbench and causal Story/Ledger/Proof inspection experience, launched by `uv run agentic-saga demo`.

**Architecture:** A Vite + React + TypeScript application validates every trace with one strict Zod boundary, then derives all visible state through pure cursor-based projections. The Python CLI runs the reference scenarios, materializes their redacted traces beside the bundled static assets in a temporary directory, and serves that directory on loopback with the standard library; the browser never mutates Saga state and no general web API is introduced.

**Tech Stack:** Python 3.12+, `argparse`, `importlib.resources`, `http.server`, Pydantic trace exports supplied by the Saga evidence layer, React, TypeScript strict mode, Vite, Zod, Vitest, Testing Library, axe-core, Playwright, pnpm 11.5.0, Node 24, uv, hatchling.

**Spec:** `docs/superpowers/specs/2026-09-06-agentic-saga-design.md`

## Global Constraints

- The default quickstart is deterministic, offline, and free: `uv sync` followed by `uv run agentic-saga demo --scenario inventory-exhausted --open`.
- The optional live command is `uv run agentic-saga demo --live --scenario inventory-exhausted --open`; every trace and screen labels `scripted` and `live` modes distinctly.
- The console consumes one stored `RunTrace` contract and remains a read-only incident inspector, not a workflow editor, monitoring platform, hosted operations platform, or operations control plane.
- Agent observations and proposals use dashed signal amber; policy uses a gate; executed effects use solid cobalt; compensation uses a reverse connector; verified invariants use proof teal; failures use fault red plus text and an icon.
- Use porcelain `#F5F6F1`, ink `#17212B`, cobalt `#2457A6`, signal amber `#D68A00`, fault red `#B43A35`, and proof teal `#087D71`.
- Use Atkinson Hyperlegible for interface text and IBM Plex Mono only for IDs, hashes, operation keys, and payloads; ship the font files with the static bundle and make no font-network request.
- Never request, store, display, or imply private chain-of-thought. Display only redacted commands/results, structured rationale, policy evidence, effect receipts, and invariant evidence already present in the exported trace.
- The console is keyboard operable, WCAG AA, responsive, reduced-motion aware, and supplies semantic list/table representations for every visualization.
- Ordinary tests and the default demo require no network, no model credentials, and no live LLM.
- `web/flight-recorder/package.json` declares `"packageManager": "pnpm@11.5.0"`; installs use `pnpm install --frozen-lockfile` against `web/flight-recorder/pnpm-lock.yaml`.
- Do not publish a package or make the repository public as part of this plan.

## Locked Integration Contract

The Flight Recorder does not serialize the kernel's internal `RunTrace` directly. The ecommerce plan must provide a `RunTraceExport` wrapper in `examples/ecommerce/demo.py`; that wrapper maps authoritative kernel evidence to the exact camelCase browser fields defined in Task 1, performs redaction before serialization, and rejects extra fields. It must provide this boundary before Task 10 begins:

```python
import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


class DemoScenarioId(StrEnum):
    HAPPY_PATH = "happy-path"
    INVENTORY_EXHAUSTED = "inventory-exhausted"
    ALTERNATE_INVENTORY = "alternate-inventory"
    PROCESS_CRASH = "process-crash"
    REFUND_RETRY = "refund-retry"
    ILLEGAL_PROPOSAL = "illegal-proposal"
    RECOVERY_EXHAUSTED = "recovery-exhausted"


class RunTraceExport(Protocol):
    schema_version: Literal["1.0"]
    run_id: str
    scenario_id: DemoScenarioId
    mode: Literal["scripted", "live"]

    def model_dump_json(
        self,
        *,
        by_alias: Literal[True] = True,
        indent: int | None = None,
    ) -> str: ...


class DemoTraceProvider(Protocol):
    async def __call__(
        self,
        scenario_id: DemoScenarioId,
        *,
        mode: Literal["scripted", "live"],
    ) -> RunTraceExport: ...


async def produce_run_trace(
    scenario_id: DemoScenarioId,
    *,
    mode: Literal["scripted", "live"],
) -> RunTraceExport: ...


def available_scenarios() -> Sequence[DemoScenarioId]: ...
```

The wrapper uses Pydantic aliases and emits exactly `schemaVersion`, `runId`, `scenarioId`, `scenarioName`, `mode`, `startedAt`, `finishedAt`, `outcome`, `faultConfig`, `initialState`, `requiredInvariantIds`, `proofs`, and `events`. Each top-level proof emits `eventId`, `ruleId`, `description`, `inputs`, `result`, `reason`, and `evaluatedSagaSequence`. Each event emits the exact camelCase fields enumerated in Task 1 (`eventId`, `sequence`, `sagaSequence`, `recordedAt`, `sagaState`, `stepId`, `operationId`, `compensatesOperationId`, `durationMs`, `idempotencyKey`, `inputHash`, and `outputHash`); snake_case is never accepted by the browser parser. Scripted mode must use the real kernel, SQLite store, scripted agent, and durable fake services; it must not synthesize browser-only events. `model_dump_json(by_alias=True)` emits schema version `1.0`, redacted payloads, monotonically increasing event sequences, and one of the seven scenario IDs above.

Sequential CLI ownership is also fixed:

```python
# Created by the foundation plan in src/agentic_saga/cli/main.py
def build_parser() -> argparse.ArgumentParser: ...
def main(argv: Sequence[str] | None = None) -> int: ...

# Created by ecommerce Task 8 in src/agentic_saga/cli/demo.py
@dataclass(frozen=True)
class DemoArguments:
    scenario: DemoScenarioId
    live: bool

async def generate_demo_traces(
    provider: DemoTraceProvider,
    *,
    mode: Literal["scripted", "live"],
    selected: DemoScenarioId,
) -> Mapping[DemoScenarioId, RunTraceExport]: ...

def configure_demo_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> argparse.ArgumentParser: ...
```

Task 11 modifies these existing functions. It preserves ecommerce scenario generation, live credential checks, and error mapping; it adds only the `--open`/`--port` options plus materialize/serve/open lifecycle.

## File Map

- `web/flight-recorder/package.json`, `pnpm-lock.yaml`, `tsconfig*.json`, `vite.config.ts`, `vitest.config.ts`, `playwright.config.ts`: isolated pnpm 11.5.0/Node 24 web toolchain and repeatable commands.
- `web/flight-recorder/src/trace/schema.ts`: sole runtime/compile-time `RunTrace` contract.
- `web/flight-recorder/src/trace/parseRunTrace.ts`: strict parse result with actionable errors.
- `web/flight-recorder/src/scenarios/catalog.ts`, `repository.ts`: seven-scenario metadata and static trace loading.
- `web/flight-recorder/src/replay/projectTrace.ts`, `useReplay.ts`: pure causal projection and user-controlled playback.
- `web/flight-recorder/src/components/`: focused workbench, header, path, Story, Ledger, Proof, details, and controls.
- `web/flight-recorder/src/styles/`: tokens, layout, component states, print/reduced-motion/responsive rules.
- `web/flight-recorder/src/test/`: trace builder and browser-test setup.
- `web/flight-recorder/e2e/flight-recorder.spec.ts`: real-browser accessibility, responsive, deep-link, and replay coverage.
- `src/agentic_saga/demo/assets.py`, `server.py`: package-resource materialization and loopback-only static serving.
- `src/agentic_saga/cli/demo.py`, `main.py`: existing foundation/ecommerce command extended with local launch and cleanup.
- `tests/unit/demo/`, `tests/integration/cli/`: Python asset/server/command tests.
- `pyproject.toml`: console script and packaged static assets.
- `.github/workflows/ci.yml`, `.github/workflows/security-audit.yml`: SHA-pinned shared frontend gate and locked pnpm dependency audit.
- `README.md`, `docs/flight-recorder.md`: five-minute path, visual grammar, trust boundary, and inspection guide.

---

### Task 1: Web Toolchain and Strict RunTrace Ingestion

**Files:**
- Create: `web/flight-recorder/package.json`
- Create: `web/flight-recorder/pnpm-lock.yaml`
- Create: `web/flight-recorder/index.html`
- Create: `web/flight-recorder/tsconfig.json`
- Create: `web/flight-recorder/tsconfig.node.json`
- Create: `web/flight-recorder/vite.config.ts`
- Create: `web/flight-recorder/vitest.config.ts`
- Create: `web/flight-recorder/eslint.config.js`
- Create: `web/flight-recorder/src/test/setup.ts`
- Create: `web/flight-recorder/src/test/makeRunTrace.ts`
- Create: `web/flight-recorder/src/trace/schema.ts`
- Create: `web/flight-recorder/src/trace/parseRunTrace.ts`
- Test: `web/flight-recorder/src/trace/parseRunTrace.test.ts`

**Interfaces:**
- Consumes: JSON produced by `RunTraceExport.model_dump_json()` under schema version `1.0`.
- Produces: `RunTrace`, `TraceEvent`, `SagaState`, `ScenarioId`, `JsonValue`, and `parseRunTrace(input: unknown): ParseResult`.

- [ ] **Step 1: Create the isolated Vite test harness**

Run:

```bash
cd web/flight-recorder
corepack enable
corepack prepare pnpm@11.5.0 --activate
pnpm init
pnpm add react react-dom zod @fontsource/atkinson-hyperlegible @fontsource/ibm-plex-mono
pnpm add --save-dev vite typescript @vitejs/plugin-react vitest jsdom @testing-library/react @testing-library/user-event @testing-library/jest-dom @types/react @types/react-dom eslint typescript-eslint eslint-plugin-react-hooks eslint-plugin-react-refresh
pnpm install --frozen-lockfile
```

Set package scripts to:

```json
{
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "test": "vitest run",
    "test:watch": "vitest",
    "typecheck": "tsc -b --pretty false",
    "lint": "eslint . --max-warnings 0",
    "gate": "pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm test:e2e"
  },
  "packageManager": "pnpm@11.5.0"
}
```

Configure `vite.config.ts` with `base: "./"`, React, and `build.outDir: "../../src/agentic_saga/demo/static"`. Configure Vitest for `jsdom`, globals, and `src/test/setup.ts`. The setup file imports `@testing-library/jest-dom/vitest`. Set `"type": "module"` in `package.json`. Configure `eslint.config.js` with the recommended TypeScript, React Hooks, and React Refresh flat configurations over `src/**/*.{ts,tsx}`.

- [ ] **Step 2: Write failing parser tests**

```ts
import { describe, expect, it } from "vitest";
import { makeRunTrace } from "../test/makeRunTrace";
import { parseRunTrace } from "./parseRunTrace";

describe("parseRunTrace", () => {
  it("accepts a versioned, ordered, redacted trace", () => {
    const result = parseRunTrace(makeRunTrace());
    expect(result).toEqual(expect.objectContaining({ ok: true }));
  });

  it("rejects an unsupported schema without throwing", () => {
    const result = parseRunTrace({ ...makeRunTrace(), schemaVersion: "2.0" });
    expect(result).toEqual({
      ok: false,
      message: "This recorder supports RunTrace 1.0; received 2.0.",
    });
  });

  it("rejects duplicate or descending event sequences", () => {
    const trace = makeRunTrace();
    trace.events[1].sequence = trace.events[0].sequence;
    expect(parseRunTrace(trace)).toEqual({
      ok: false,
      message: "RunTrace events must have strictly increasing sequence numbers.",
    });
  });

  it("requires authoritative top-level invariant proofs", () => {
    const { proofs: _proofs, ...withoutProofs } = makeRunTrace();
    const result = parseRunTrace(withoutProofs);
    expect(result).toEqual(expect.objectContaining({
      ok: false,
      message: expect.stringContaining("proofs"),
    }));
  });

  it("rejects snake_case aliases and extra browser fields", () => {
    const trace = makeRunTrace() as RunTrace & { run_id?: string };
    trace.run_id = trace.runId;
    expect(parseRunTrace(trace)).toEqual(expect.objectContaining({ ok: false }));
    const event = { ...makeRunTrace().events[0], saga_sequence: 1 };
    expect(parseRunTrace({ ...makeRunTrace(), events: [event] }))
      .toEqual(expect.objectContaining({ ok: false }));
  });
});
```

- [ ] **Step 3: Run the parser test to prove red**

Run: `cd web/flight-recorder && pnpm test -- src/trace/parseRunTrace.test.ts`

Expected: FAIL because `parseRunTrace`, the schema, and trace builder do not exist.

- [ ] **Step 4: Implement the schema and parser**

Define these exact discriminants in `schema.ts`:

```ts
export const scenarioIds = [
  "happy-path",
  "inventory-exhausted",
  "alternate-inventory",
  "process-crash",
  "refund-retry",
  "illegal-proposal",
  "recovery-exhausted",
] as const;

export const sagaStates = [
  "CREATED", "RUNNING", "RECOVERY_PLAN_REQUIRED", "RETRY_WAIT",
  "RECONCILING_UNKNOWN", "COMPENSATING", "HUMAN_REQUIRED",
  "SUCCEEDED_VERIFIED", "COMPENSATED_VERIFIED", "ABORTED_CLEAN",
  "RESOLVED_WITH_EXCEPTION",
] as const;

export const eventKinds = [
  "agent_observation", "agent_proposal", "policy_decision",
  "effect_intent", "effect_result", "compensation_intent",
  "compensation_result", "invariant_result", "process_boundary",
  "human_required", "state_transition",
] as const;

export const traceLanes = ["agent", "policy", "saga", "proof", "system"] as const;
export const traceStatuses = [
  "planned", "approved", "rejected", "durable", "dispatched", "confirmed",
  "no_effect", "partial", "unknown", "failed", "valid", "invalid", "stale",
  "entered", "completed",
] as const;
export const traceDirections = ["read", "forward", "compensation"] as const;
```

`TraceEvent` contains `eventId`, ledger `sequence`, material `sagaSequence`, `recordedAt`, `lane`, `kind`, `status`, `summary`, and `sagaState`; optional evidence fields are `stepId`, `operationId`, `compensatesOperationId`, `direction`, `attempt`, `durationMs`, `idempotencyKey`, `inputHash`, `outputHash`, `input`, `output`, `proposal`, `policy`, `effect`, and `proof`. `proposal` contains `tool`, redacted `arguments`, and `reason`; `policy` contains `ruleId`, `decision`, and `reason`; `effect` contains `tool`, optional `receipt`, and redacted `before`/`after`; an event-level `proof` contains `ruleId`, `result`, and `evaluatedSagaSequence` only for causal replay. All Zod objects use `.strict()`.

Define authoritative `InvariantProof` as `eventId`, `ruleId`, `description`, redacted `inputs`, `result: "valid" | "invalid" | "stale"`, `reason`, and `evaluatedSagaSequence`. `RunTrace` contains `schemaVersion: "1.0"`, `runId`, `scenarioId`, `scenarioName`, `mode: "scripted" | "live"`, `startedAt`, optional `finishedAt`, `outcome`, `faultConfig: Record<string, boolean>`, `initialState`, `requiredInvariantIds: string[]`, `proofs: InvariantProof[]`, and at least one event. Infer and export `InvariantProof`, `TraceLane`, `TraceStatus`, and all other TypeScript types from the schemas. `parseRunTrace` returns:

```ts
export type ParseResult =
  | { ok: true; trace: RunTrace }
  | { ok: false; message: string };
```

Check `schemaVersion` before Zod parsing, flatten validation failures into `RunTrace is invalid: <path> <message>.`, then check strictly increasing sequence numbers.

```ts
export function parseRunTrace(input: unknown): ParseResult {
  if (typeof input === "object" && input !== null && "schemaVersion" in input
      && input.schemaVersion !== "1.0") {
    return { ok: false, message: `This recorder supports RunTrace 1.0; received ${String(input.schemaVersion)}.` };
  }
  const parsed = runTraceSchema.safeParse(input);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    return { ok: false, message: `RunTrace is invalid: ${issue.path.join(".")} ${issue.message}.` };
  }
  const unordered = parsed.data.events.some(
    (event, index, events) => index > 0 && event.sequence <= events[index - 1].sequence,
  );
  if (unordered) {
    return { ok: false, message: "RunTrace events must have strictly increasing sequence numbers." };
  }
  return { ok: true, trace: parsed.data };
}
```

- [ ] **Step 5: Run contract checks**

Run: `cd web/flight-recorder && pnpm test -- src/trace/parseRunTrace.test.ts && pnpm typecheck`

Expected: parser tests PASS and TypeScript reports zero errors.

- [ ] **Step 6: Commit the trace boundary**

```bash
git add web/flight-recorder
git commit -m "feat(console): validate RunTrace input"
```

### Task 2: Scenario Catalog, Static Repository, and Workbench

**Files:**
- Create: `web/flight-recorder/src/scenarios/catalog.ts`
- Create: `web/flight-recorder/src/scenarios/repository.ts`
- Create: `web/flight-recorder/src/components/ScenarioWorkbench.tsx`
- Test: `web/flight-recorder/src/scenarios/repository.test.ts`
- Test: `web/flight-recorder/src/components/ScenarioWorkbench.test.tsx`

**Interfaces:**
- Consumes: `ScenarioId`, `RunTrace`, and `parseRunTrace(input)` from Task 1; static `/traces/index.json` and `/traces/<scenario-id>.json` created by Task 10.
- Produces: `SCENARIOS`, `TraceRepository.load(scenarioId, signal): Promise<RunTrace>`, and `ScenarioWorkbenchProps { selectedId; mode; availableIds; onSelect; onRun }`.

- [ ] **Step 1: Write failing repository and workbench tests**

```ts
it("loads and validates a selected trace", async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(makeRunTrace())));
  const repository = new TraceRepository(fetcher);
  await expect(repository.load("inventory-exhausted")).resolves.toMatchObject({
    scenarioId: "inventory-exhausted",
  });
  expect(fetcher).toHaveBeenCalledWith("./traces/inventory-exhausted.json", { signal: undefined });
});

it("offers all seven scenarios as one named choice group", () => {
  render(
    <ScenarioWorkbench
      selectedId="inventory-exhausted"
      mode="scripted"
      availableIds={SCENARIOS.map(({ id }) => id)}
      onSelect={vi.fn()}
      onRun={vi.fn()}
    />,
  );
  expect(screen.getByRole("radiogroup", { name: "Demo scenario" })).toBeVisible();
  expect(screen.getAllByRole("radio")).toHaveLength(7);
  expect(screen.getByText("Offline scripted evidence")).toBeVisible();
});
```

- [ ] **Step 2: Run tests to prove red**

Run: `cd web/flight-recorder && pnpm test -- src/scenarios/repository.test.ts src/components/ScenarioWorkbench.test.tsx`

Expected: FAIL because the repository, catalog, and workbench do not exist.

- [ ] **Step 3: Implement catalog and repository**

Each `ScenarioDefinition` has `id`, `name`, `summary`, `expectedOutcome`, and `faults`. Use these names in order: Happy path, Inventory exhausted, Alternate inventory, Process crash, Refund retry, Illegal proposal, Recovery exhausted. The repository accepts an injectable `typeof fetch`, requests `./traces/${scenarioId}.json`, reports HTTP failures as `Could not load <name> evidence (HTTP <status>).`, parses JSON, and converts parser failures into `Invalid <name> evidence: <message>`.

```ts
export class TraceRepository {
  constructor(private readonly fetcher: typeof fetch = fetch) {}

  async load(scenarioId: ScenarioId, signal?: AbortSignal): Promise<RunTrace> {
    const response = await this.fetcher(`./traces/${scenarioId}.json`, { signal });
    const scenario = SCENARIOS.find(({ id }) => id === scenarioId)!;
    if (!response.ok) {
      throw new Error(`Could not load ${scenario.name} evidence (HTTP ${response.status}).`);
    }
    const result = parseRunTrace(await response.json());
    if (!result.ok) throw new Error(`Invalid ${scenario.name} evidence: ${result.message}`);
    return result.trace;
  }
}
```

- [ ] **Step 4: Implement the workbench**

Render a `<fieldset>` with a visible `Demo scenario` legend and seven radio inputs. Show the selected scenario's expected outcome and exact active faults. Mode copy is `Offline scripted evidence` or `Live OpenRouter evidence`; do not use color as the only mode indicator. `Run scenario` calls `onRun(selectedId)` and selection calls `onSelect(id)`. In live mode, keep all seven scenarios visible but disable IDs absent from `availableIds`, with `Run this scenario in a separate live command.` as adjacent explanation.

```tsx
<fieldset>
  <legend>Demo scenario</legend>
  {SCENARIOS.map((scenario) => (
    <label key={scenario.id}>
      <input
        type="radio"
        name="scenario"
        value={scenario.id}
        checked={scenario.id === selectedId}
        disabled={!availableIds.includes(scenario.id)}
        onChange={() => onSelect(scenario.id)}
      />
      <span>{scenario.name}</span>
      <small>{scenario.summary}</small>
    </label>
  ))}
</fieldset>
```

- [ ] **Step 5: Verify and commit**

Run: `cd web/flight-recorder && pnpm test -- src/scenarios src/components/ScenarioWorkbench.test.tsx && pnpm typecheck`

Expected: both suites PASS and TypeScript reports zero errors.

```bash
git add web/flight-recorder/src/scenarios web/flight-recorder/src/components/ScenarioWorkbench.tsx web/flight-recorder/src/components/ScenarioWorkbench.test.tsx
git commit -m "feat(console): add scenario workbench"
```

### Task 3: Pure Causal Projection and Flight Path

**Files:**
- Create: `web/flight-recorder/src/replay/projectTrace.ts`
- Create: `web/flight-recorder/src/components/FlightPath.tsx`
- Create: `web/flight-recorder/src/components/OutcomeHeader.tsx`
- Test: `web/flight-recorder/src/replay/projectTrace.test.ts`
- Test: `web/flight-recorder/src/components/FlightPath.test.tsx`

**Interfaces:**
- Consumes: a validated `RunTrace`, its authoritative top-level `proofs`, and a zero-based cursor.
- Produces: `RunProjection`, `projectTrace(trace, cursor)`, `FlightPathProps { projection; onSelectEvent }`, and `OutcomeHeaderProps { trace; projection }`.

```ts
export interface OperationProjection {
  operationId: string;
  forwardStatus?: TraceStatus;
  compensationOperationId?: string;
  compensationStatus?: TraceStatus;
}

export interface RunProjection {
  cursor: number;
  visibleEvents: TraceEvent[];
  currentEvent: TraceEvent;
  currentState: SagaState;
  operations: Map<string, OperationProjection>;
  proofs: InvariantProof[];
  requiredInvariantIds: string[];
  validProofCount: number;
  requiredProofCount: number;
  terminalVerified: boolean;
}
```

- [ ] **Step 1: Write failing projection tests for effects and compensation**

```ts
it("pairs a compensation with the confirmed forward operation", () => {
  const trace = makeRunTrace({ includeCompensation: true });
  const projection = projectTrace(trace, trace.events.length - 1);
  expect(projection.operations.get("op-charge")).toMatchObject({
    forwardStatus: "confirmed",
    compensationStatus: "confirmed",
    compensationOperationId: "op-refund",
  });
});

it("uses only evidence at or before the replay cursor", () => {
  const trace = makeRunTrace({ includeCompensation: true });
  const failureIndex = trace.events.findIndex((event) => event.status === "failed");
  const projection = projectTrace(trace, failureIndex);
  expect(projection.visibleEvents).toHaveLength(failureIndex + 1);
  expect(projection.proofs).toHaveLength(0);
  expect(projection.currentState).toBe(trace.events[failureIndex].sagaState);
});
```

- [ ] **Step 2: Run projection tests to prove red**

Run: `cd web/flight-recorder && pnpm test -- src/replay/projectTrace.test.ts`

Expected: FAIL because `projectTrace` is missing.

- [ ] **Step 3: Implement `projectTrace` as a pure reducer**

Clamp the cursor to `0..events.length - 1`; slice visible events once; index effects by `operationId`; connect compensation through `compensatesOperationId`; expose `currentEvent`, `currentState`, `operations`, `proofs`, `requiredInvariantIds`, `validProofCount`, `requiredProofCount`, and `terminalVerified`. `terminalVerified` is true only when the current Saga state is terminal and the latest evidence for every ID in `trace.requiredInvariantIds` has `result: "valid"`; it must be false for a terminal string with missing, stale, or failing proof evidence.

```ts
export function projectTrace(trace: RunTrace, requestedCursor: number): RunProjection {
  const cursor = Math.max(0, Math.min(requestedCursor, trace.events.length - 1));
  const visibleEvents = trace.events.slice(0, cursor + 1);
  const operations = indexOperations(visibleEvents);
  const currentSagaSequence = visibleEvents.at(-1)!.sagaSequence;
  const proofs = latestProofsByRule(
    trace.proofs.filter((proof) => proof.evaluatedSagaSequence <= currentSagaSequence),
  );
  const terminal = terminalStates.has(visibleEvents.at(-1)!.sagaState);
const terminalVerified = terminal && trace.requiredInvariantIds.every(
    (ruleId) => {
      const proof = proofs.get(ruleId);
      return proof?.result === "valid" && proof.evaluatedSagaSequence === currentSagaSequence;
    },
  );
  return {
    cursor,
    visibleEvents,
    currentEvent: visibleEvents.at(-1)!,
    currentState: visibleEvents.at(-1)!.sagaState,
    operations,
    proofs: [...proofs.values()],
    requiredInvariantIds: trace.requiredInvariantIds,
    validProofCount: [...proofs.values()].filter(
      (proof) => proof.result === "valid" && proof.evaluatedSagaSequence === currentSagaSequence,
    ).length,
    requiredProofCount: trace.requiredInvariantIds.length,
    terminalVerified,
  };
}
```

Define private helpers in this file with the exact signatures `indexOperations(events: readonly TraceEvent[]): Map<string, OperationProjection>` and `latestProofsByRule(proofs: readonly InvariantProof[]): Map<string, InvariantProof>`. Define `terminalStates` as `SUCCEEDED_VERIFIED`, `COMPENSATED_VERIFIED`, `ABORTED_CLEAN`, and `RESOLVED_WITH_EXCEPTION`; `HUMAN_REQUIRED` is quiescent but not terminal. `indexOperations` creates entries only from effect/compensation events with operation IDs and applies later statuses by sequence; `latestProofsByRule` overwrites by `ruleId` in `evaluatedSagaSequence` order. Event-level proof data drives the causal Story only; it never replaces the top-level authoritative proof.

- [ ] **Step 4: Write failing semantic flight-path tests**

```tsx
it("distinguishes proposal, policy, effect, compensation, and proof in text", () => {
  const projection = projectTrace(makeRunTrace({ includeCompensation: true }), 99);
  render(<FlightPath projection={projection} onSelectEvent={vi.fn()} />);
  expect(screen.getByRole("list", { name: "Saga causal path" })).toBeVisible();
  expect(screen.getByText("Agent proposal")).toBeVisible();
  expect(screen.getByText("Policy approved")).toBeVisible();
  expect(screen.getByText("Payment captured")).toBeVisible();
  expect(screen.getByText("Payment refunded")).toBeVisible();
  expect(screen.getByText("Invariant valid")).toBeVisible();
});
```

- [ ] **Step 5: Implement path and outcome header**

Use a semantic ordered list in execution order. Every item includes a visible lane noun and state text; CSS classes may add signal-line layout but must not change reading order. Add `data-lane` and `data-direction` hooks for styling. The outcome header shows run ID, scenario, scripted/live source, current Saga state, and `n/m invariants valid`; render `Verified outcome` only when `terminalVerified` is true.

```tsx
<ol aria-label="Saga causal path">
  {projection.visibleEvents.map((event) => (
    <li key={event.eventId} data-lane={event.lane} data-direction={event.direction}>
      <button type="button" onClick={() => onSelectEvent(event.eventId)}>
        <span>{laneLabel(event)}</span>
        <strong>{event.summary}</strong>
        <span>{statusLabel(event.status)}</span>
      </button>
    </li>
  ))}
</ol>
```

- [ ] **Step 6: Verify and commit**

Run: `cd web/flight-recorder && pnpm test -- src/replay/projectTrace.test.ts src/components/FlightPath.test.tsx && pnpm typecheck`

Expected: projection and component tests PASS.

```bash
git add web/flight-recorder/src/replay/projectTrace.ts web/flight-recorder/src/replay/projectTrace.test.ts web/flight-recorder/src/components/FlightPath.tsx web/flight-recorder/src/components/FlightPath.test.tsx web/flight-recorder/src/components/OutcomeHeader.tsx
git commit -m "feat(console): project causal Saga evidence"
```

### Task 4: User-Controlled Replay and Reduced Motion

**Files:**
- Create: `web/flight-recorder/src/replay/useReplay.ts`
- Create: `web/flight-recorder/src/replay/useReducedMotion.ts`
- Create: `web/flight-recorder/src/components/ReplayControls.tsx`
- Test: `web/flight-recorder/src/replay/useReplay.test.tsx`
- Test: `web/flight-recorder/src/components/ReplayControls.test.tsx`

**Interfaces:**
- Consumes: event count and browser `prefers-reduced-motion`.
- Produces: `ReplayState { cursor; isPlaying; atStart; atEnd }`, `ReplayActions { play; pause; next; previous; restart; seek }`, and `useReplay({ eventCount, reducedMotion, pauseAfter, initialCursor = 0, intervalMs = 700 })`.

- [ ] **Step 1: Write failing timer and reduced-motion tests**

```ts
it("advances one event per interval and pauses at the end", () => {
  vi.useFakeTimers();
  const pauseAfter = new Set<number>();
  const { result } = renderHook(() => useReplay({ eventCount: 3, reducedMotion: false, pauseAfter }));
  act(() => result.current.actions.play());
  act(() => vi.advanceTimersByTime(1400));
  expect(result.current.state).toMatchObject({ cursor: 2, isPlaying: false, atEnd: true });
});

it("does not autoplay when reduced motion is requested", () => {
  const pauseAfter = new Set<number>();
  const { result } = renderHook(() => useReplay({ eventCount: 3, reducedMotion: true, pauseAfter }));
  act(() => result.current.actions.play());
  expect(result.current.state).toMatchObject({ cursor: 0, isPlaying: false });
});
```

- [ ] **Step 2: Run replay tests to prove red**

Run: `cd web/flight-recorder && pnpm test -- src/replay/useReplay.test.tsx`

Expected: FAIL because the replay hooks are missing.

- [ ] **Step 3: Implement replay hooks and controls**

Use one managed timeout, clear it on pause/unmount/event-count change, clamp all seeks, and pause at the final event or after an index in `pauseAfter`. App passes failure and policy-decision indices in `pauseAfter` and uses `initialCursor: "end"` so first load shows the proved outcome; Restart moves to zero before playing. `useReducedMotion` subscribes to `matchMedia("(prefers-reduced-motion: reduce)")`. Render native buttons named Previous event, Play replay/Pause replay, Next event, and Restart replay plus `Event <current> of <total>`.

```ts
useEffect(() => {
  if (!isPlaying || reducedMotion || cursor >= eventCount - 1) return;
  const timer = window.setTimeout(() => {
    setCursor((value) => {
      const next = Math.min(value + 1, eventCount - 1);
      if (pauseAfter.has(next)) setIsPlaying(false);
      return next;
    });
  }, intervalMs);
  return () => window.clearTimeout(timer);
}, [cursor, eventCount, intervalMs, isPlaying, pauseAfter, reducedMotion]);

useEffect(() => {
  if (cursor >= eventCount - 1 || reducedMotion) setIsPlaying(false);
}, [cursor, eventCount, reducedMotion]);
```

Use native `disabled` semantics at the first/last event and put keyboard shortcuts in each button's accessible description.

- [ ] **Step 4: Add keyboard behavior tests**

```tsx
it("supports left, right, home, and space without stealing form input keys", async () => {
  const onPrevious = vi.fn();
  const onNext = vi.fn();
  const onRestart = vi.fn();
  const onToggle = vi.fn();
  render(<ReplayControls state={state} actions={{ onPrevious, onNext, onRestart, onToggle }} />);
  await userEvent.keyboard("{ArrowRight}{ArrowLeft}{Home} ");
  expect(onNext).toHaveBeenCalledOnce();
  expect(onPrevious).toHaveBeenCalledOnce();
  expect(onRestart).toHaveBeenCalledOnce();
  expect(onToggle).toHaveBeenCalledOnce();
});
```

Attach shortcuts only while focus is outside inputs, selects, textareas, and contenteditable regions.

- [ ] **Step 5: Verify and commit**

Run: `cd web/flight-recorder && pnpm test -- src/replay src/components/ReplayControls.test.tsx`

Expected: replay and control tests PASS with no leaked timers.

```bash
git add web/flight-recorder/src/replay/useReplay.ts web/flight-recorder/src/replay/useReplay.test.tsx web/flight-recorder/src/replay/useReducedMotion.ts web/flight-recorder/src/components/ReplayControls.tsx web/flight-recorder/src/components/ReplayControls.test.tsx
git commit -m "feat(console): add causal replay controls"
```

### Task 5: Story View and Evidence Details

**Files:**
- Create: `web/flight-recorder/src/components/StoryView.tsx`
- Create: `web/flight-recorder/src/components/EventDetails.tsx`
- Create: `web/flight-recorder/src/components/JsonEvidence.tsx`
- Test: `web/flight-recorder/src/components/StoryView.test.tsx`
- Test: `web/flight-recorder/src/components/EventDetails.test.tsx`

**Interfaces:**
- Consumes: visible `TraceEvent[]` and selected event ID.
- Produces: `StoryViewProps { events; selectedEventId; onSelectEvent }` and `EventDetailsProps { event; onClose }`.

- [ ] **Step 1: Write failing Story and privacy tests**

```tsx
it("states where probabilistic reasoning ends and deterministic authority begins", () => {
  const trace = makeRunTrace({ includeRejectedProposal: true });
  render(<StoryView events={trace.events} selectedEventId={null} onSelectEvent={vi.fn()} />);
  expect(screen.getByText(/agent proposed/i)).toBeVisible();
  expect(screen.getByText(/policy rejected/i)).toBeVisible();
  expect(screen.queryByText(/chain.of.thought/i)).not.toBeInTheDocument();
});

it("shows structured rationale and redacted evidence without inventing explanation", () => {
  const event = makeRunTrace().events.find((item) => item.kind === "agent_proposal")!;
  render(<EventDetails event={event} onClose={vi.fn()} />);
  expect(screen.getByText(event.proposal!.reason)).toBeVisible();
  expect(screen.getByText("Recorded rationale")).toBeVisible();
  expect(screen.queryByText("AI reasoning")).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run component tests to prove red**

Run: `cd web/flight-recorder && pnpm test -- src/components/StoryView.test.tsx src/components/EventDetails.test.tsx`

Expected: FAIL because the components are missing.

- [ ] **Step 3: Implement semantic story and details**

Story is an `<ol aria-label="Causal story">`; each event is a button with sequence, visible lane, summary, and status. `EventDetails` is an `<aside>` with a heading focused on open and an Escape handler that closes and returns focus to the originating event. Show only fields present on the event: recorded input/output, `Recorded rationale`, policy rule/decision, before/after state, receipt, attempt, duration, operation ID, compensation target, idempotency key, and hashes. `JsonEvidence` renders escaped `JSON.stringify(value, null, 2)` text inside `<pre>` and never uses `dangerouslySetInnerHTML`.

```tsx
export function JsonEvidence({ label, value }: { label: string; value: JsonValue }) {
  return (
    <section>
      <h4>{label}</h4>
      <pre tabIndex={0}>{JSON.stringify(value, null, 2)}</pre>
    </section>
  );
}

useEffect(() => {
  if (!event) return;
  headingRef.current?.focus();
  const closeOnEscape = (keyboardEvent: KeyboardEvent) => {
    if (keyboardEvent.key === "Escape") onClose();
  };
  window.addEventListener("keydown", closeOnEscape);
  return () => window.removeEventListener("keydown", closeOnEscape);
}, [event, onClose]);
```

- [ ] **Step 4: Verify and commit**

Run: `cd web/flight-recorder && pnpm test -- src/components/StoryView.test.tsx src/components/EventDetails.test.tsx && pnpm typecheck`

Expected: Story and details tests PASS.

```bash
git add web/flight-recorder/src/components/StoryView.tsx web/flight-recorder/src/components/StoryView.test.tsx web/flight-recorder/src/components/EventDetails.tsx web/flight-recorder/src/components/EventDetails.test.tsx web/flight-recorder/src/components/JsonEvidence.tsx
git commit -m "feat(console): explain recorded Saga decisions"
```

### Task 6: Filterable Ledger View

**Files:**
- Create: `web/flight-recorder/src/components/LedgerView.tsx`
- Create: `web/flight-recorder/src/components/LedgerFilters.tsx`
- Create: `web/flight-recorder/src/ledger/filterEvents.ts`
- Test: `web/flight-recorder/src/ledger/filterEvents.test.ts`
- Test: `web/flight-recorder/src/components/LedgerView.test.tsx`

**Interfaces:**
- Consumes: visible `TraceEvent[]`.
- Produces: `LedgerFilter { lane: TraceLane | "all"; status: TraceStatus | "all"; query: string }`, `filterEvents(events, filter)`, and `LedgerViewProps`.

- [ ] **Step 1: Write failing filtering and table tests**

```ts
it("filters by lane, status, and case-insensitive evidence text", () => {
  const events = makeRunTrace({ includeRejectedProposal: true }).events;
  expect(filterEvents(events, { lane: "policy", status: "rejected", query: "second charge" }))
    .toHaveLength(1);
});

it("renders chronological evidence as a semantic table", () => {
  render(<LedgerView events={makeRunTrace().events} onSelectEvent={vi.fn()} />);
  expect(screen.getByRole("table", { name: "Execution ledger" })).toBeVisible();
  expect(screen.getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
    "Sequence", "Recorded", "Lane", "Event", "Step", "Status", "Evidence",
  ]);
});
```

- [ ] **Step 2: Run ledger tests to prove red**

Run: `cd web/flight-recorder && pnpm test -- src/ledger/filterEvents.test.ts src/components/LedgerView.test.tsx`

Expected: FAIL because ledger filtering and components are missing.

- [ ] **Step 3: Implement ledger filtering and table**

Filter the concatenation of summary, step ID, operation ID, policy rule, tool, receipt, and hashes without serializing hidden fields. Keep source order; show an explicit `No ledger events match these filters.` row spanning seven columns. The Evidence cell is a button named `Inspect event <sequence>` and opens Task 5 details. On narrow screens, retain table semantics while CSS turns each row into a labeled block using `data-label` attributes.

```ts
export function filterEvents(events: TraceEvent[], filter: LedgerFilter): TraceEvent[] {
  const query = filter.query.trim().toLocaleLowerCase();
  return events.filter((event) => {
    if (filter.lane !== "all" && event.lane !== filter.lane) return false;
    if (filter.status !== "all" && event.status !== filter.status) return false;
    if (!query) return true;
    const evidence = [
      event.summary, event.stepId, event.operationId, event.policy?.ruleId,
      event.proposal?.tool, event.effect?.tool, event.effect?.receipt,
      event.inputHash, event.outputHash,
    ].filter(Boolean).join(" ").toLocaleLowerCase();
    return evidence.includes(query);
  });
}
```

- [ ] **Step 4: Verify and commit**

Run: `cd web/flight-recorder && pnpm test -- src/ledger src/components/LedgerView.test.tsx && pnpm typecheck`

Expected: ledger tests PASS.

```bash
git add web/flight-recorder/src/ledger web/flight-recorder/src/components/LedgerView.tsx web/flight-recorder/src/components/LedgerView.test.tsx web/flight-recorder/src/components/LedgerFilters.tsx
git commit -m "feat(console): add filterable evidence ledger"
```

### Task 7: Invariant Proof View and Terminal-State Guard

**Files:**
- Create: `web/flight-recorder/src/components/ProofView.tsx`
- Create: `web/flight-recorder/src/proof/summarizeProof.ts`
- Test: `web/flight-recorder/src/proof/summarizeProof.test.ts`
- Test: `web/flight-recorder/src/components/ProofView.test.tsx`

**Interfaces:**
- Consumes: authoritative `RunProjection.proofs`, current Saga sequence, current state, and outcome; causal proof events remain available through Story/Ledger.
- Produces: `ProofSummary { valid; invalid; stale; missing; terminalVerified }`, `summarizeProof(projection)`, and `ProofViewProps { projection; onSelectEvent }`.

- [ ] **Step 1: Write failing proof-safety tests**

```ts
it("refuses a verified badge when a terminal trace lacks fresh proof", () => {
  const trace = makeRunTrace();
  trace.proofs = [];
  const summary = summarizeProof(projectTrace(trace, 99));
  expect(summary.terminalVerified).toBe(false);
  expect(summary.missing).toBeGreaterThan(0);
});

it("shows concrete inputs and reason for every invariant", () => {
  const projection = projectTrace(makeRunTrace(), 99);
  render(<ProofView projection={projection} onSelectEvent={vi.fn()} />);
  expect(screen.getByRole("list", { name: "Invariant proofs" })).toBeVisible();
  expect(screen.getByText("Payment is refunded when order is cancelled")).toBeVisible();
  expect(screen.getByText(/payment_status.*REFUNDED/s)).toBeVisible();
});
```

- [ ] **Step 2: Run proof tests to prove red**

Run: `cd web/flight-recorder && pnpm test -- src/proof/summarizeProof.test.ts src/components/ProofView.test.tsx`

Expected: FAIL because proof summary and view are missing.

- [ ] **Step 3: Implement proof summary and view**

Count the latest evidence for every declared `ruleId`, treating an earlier sequence or explicit `stale` result as stale. Render each rule's description, result text/icon, recorded inputs, reason, evidence sequence, and Inspect button. The top copy is exactly one of `Verified from n current invariants` or `Outcome is not verified: <n> missing, <n> stale, <n> invalid.` Never infer proof from the outcome label.

```ts
export function summarizeProof(projection: RunProjection): ProofSummary {
  const byRule = new Map(projection.proofs.map((proof) => [proof.ruleId, proof]));
  const currentSagaSequence = projection.currentEvent.sagaSequence;
  const results = projection.requiredInvariantIds.map((ruleId) => {
    const proof = byRule.get(ruleId);
    if (!proof) return "missing";
    if (proof.evaluatedSagaSequence !== currentSagaSequence) return "stale";
    return proof.result;
  });
  const count = (result: "valid" | "invalid" | "stale" | "missing") =>
    results.filter((value) => value === result).length;
  const summary = {
    valid: count("valid"),
    invalid: count("invalid"),
    stale: count("stale"),
    missing: count("missing"),
    terminalVerified: projection.terminalVerified,
  };
  return summary;
}
```

- [ ] **Step 4: Verify and commit**

Run: `cd web/flight-recorder && pnpm test -- src/proof src/components/ProofView.test.tsx && pnpm typecheck`

Expected: proof tests PASS.

```bash
git add web/flight-recorder/src/proof web/flight-recorder/src/components/ProofView.tsx web/flight-recorder/src/components/ProofView.test.tsx
git commit -m "feat(console): render invariant proof evidence"
```

### Task 8: Compose the Inspector and Apply the Signal-Box Design

**Files:**
- Create: `web/flight-recorder/src/App.tsx`
- Create: `web/flight-recorder/src/main.tsx`
- Create: `web/flight-recorder/src/components/RunInspector.tsx`
- Create: `web/flight-recorder/src/components/ViewTabs.tsx`
- Create: `web/flight-recorder/src/styles/tokens.css`
- Create: `web/flight-recorder/src/styles/layout.css`
- Create: `web/flight-recorder/src/styles/components.css`
- Test: `web/flight-recorder/src/App.test.tsx`

**Interfaces:**
- Consumes: scenario repository, workbench, replay, projection, Story/Ledger/Proof, and event details from Tasks 2–7.
- Produces: complete static application with `?scenario=<ScenarioId>&event=<eventId>&view=story|ledger|proof` deep links.

- [ ] **Step 1: Write the failing end-to-end component test**

```tsx
it("loads the deep-linked scenario and keeps all views on one replay projection", async () => {
  const repository = { load: vi.fn().mockResolvedValue(makeRunTrace({ includeCompensation: true })) };
  window.history.replaceState(null, "", "?scenario=inventory-exhausted&view=proof");
  render(<App repository={repository} />);
  expect(await screen.findByRole("heading", { name: "Inventory exhausted" })).toBeVisible();
  expect(screen.getByRole("tab", { name: "Proof" })).toHaveAttribute("aria-selected", "true");
  await userEvent.click(screen.getByRole("button", { name: "Previous event" }));
  expect(screen.getByText(/Outcome is not verified/)).toBeVisible();
});
```

- [ ] **Step 2: Run app test to prove red**

Run: `cd web/flight-recorder && pnpm test -- src/App.test.tsx`

Expected: FAIL because the application shell does not exist.

- [ ] **Step 3: Compose the application**

Use one selected trace, cursor, selected event, and active view in `App`. Abort the previous fetch when scenario changes. Put outcome first, then a desktop three-column region (`scenario 15rem / path minmax(0,1fr) / proof 20rem`), then the authority sequence `Agent proposes → Policy decides → Saga acts → Invariants prove`, then replay controls and tabs. Keep Story/Ledger/Proof mounted only when active; preserve the shared cursor and selected event across tab changes. Update the query string with `history.replaceState` and validate all incoming query values before use.

```tsx
<main id="main-content">
  <OutcomeHeader trace={trace} projection={projection} />
  <p className="authority-chain">Agent proposes → Policy decides → Saga acts → Invariants prove</p>
  <div className="recorder-grid">
    <ScenarioWorkbench {...workbenchProps} />
    <FlightPath projection={projection} onSelectEvent={setSelectedEventId} />
    <ProofView projection={projection} onSelectEvent={setSelectedEventId} />
  </div>
  <ReplayControls state={replay.state} actions={controlActions} />
  <ViewTabs active={activeView} onChange={setActiveView} />
  {activeView === "story" && <StoryView events={projection.visibleEvents} {...selectionProps} />}
  {activeView === "ledger" && <LedgerView events={projection.visibleEvents} {...selectionProps} />}
  {activeView === "proof" && <ProofView projection={projection} {...selectionProps} />}
  {selectedEvent && <EventDetails event={selectedEvent} onClose={closeDetails} />}
</main>
```

- [ ] **Step 4: Implement tokens and state-bearing styles**

Define the six required colors as CSS custom properties and use them with border style, icon, and visible lane/status labels. Agent items have an amber dashed border; policy is an ink gate with approved/rejected text; effects are solid cobalt; compensation has a leftward arrow and `Compensation for <operation>`; proof uses teal only when valid; fault states use red and a visible `Failed`/`Rejected` word. Import local fontsource CSS from `main.tsx`. No non-user-triggered animation is allowed except the replay cursor moving after Play.

```css
:root {
  --porcelain: #f5f6f1;
  --ink: #17212b;
  --cobalt: #2457a6;
  --signal-amber: #d68a00;
  --fault-red: #b43a35;
  --proof-teal: #087d71;
}

[data-lane="agent"] { border: 2px dashed var(--signal-amber); }
[data-lane="saga"] { border: 2px solid var(--cobalt); }
[data-lane="proof"][data-status="valid"] { border-color: var(--proof-teal); }
[data-status="failed"], [data-status="rejected"] { border-color: var(--fault-red); }
```

- [ ] **Step 5: Verify and commit**

Run: `cd web/flight-recorder && pnpm test && pnpm typecheck && pnpm lint && pnpm build`

Expected: all Vitest suites PASS, lint/typecheck are clean, and Vite creates `src/agentic_saga/demo/static/index.html` plus local JS, CSS, and font assets.

```bash
git add web/flight-recorder src/agentic_saga/demo/static
git commit -m "feat(console): compose Saga Flight Recorder"
```

### Task 9: Browser Accessibility, Responsive Layout, and Reduced-Motion Gate

**Files:**
- Modify: `web/flight-recorder/package.json`
- Modify: `web/flight-recorder/pnpm-lock.yaml`
- Create: `web/flight-recorder/playwright.config.ts`
- Create: `web/flight-recorder/e2e/flight-recorder.spec.ts`
- Create: `web/flight-recorder/e2e/installTraceRoutes.ts`
- Create: `web/flight-recorder/src/accessibility.test.tsx`
- Modify: `web/flight-recorder/src/styles/layout.css`
- Modify: `web/flight-recorder/src/styles/components.css`

**Interfaces:**
- Consumes: the complete application from Task 8 and static deterministic trace files materialized by the test fixture.
- Produces: `pnpm test:e2e` as the browser-level accessibility/responsive contract and a `pnpm gate` entry point for shared CI.

- [ ] **Step 1: Install and configure the browser gate**

Run:

```bash
cd web/flight-recorder
pnpm add --save-dev @playwright/test axe-core @axe-core/playwright
pnpm exec playwright install chromium
pnpm install --frozen-lockfile
```

Add `"test:e2e": "playwright test"` to scripts. Configure Playwright `webServer.command` as `pnpm dev -- --host 127.0.0.1`, reuse disabled in CI, and projects named `desktop` at 1280×800 and `mobile` at 390×844. Confirm `pnpm gate` runs lint, typecheck, Vitest, production build, and this Playwright suite in that order.

- [ ] **Step 2: Write failing accessibility and viewport tests**

```ts
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { installTraceRoutes } from "./installTraceRoutes";

test("keyboard and screen-reader semantics expose the complete safety story", async ({ page }) => {
  await installTraceRoutes(page);
  await page.goto("/?scenario=inventory-exhausted&view=story");
  await expect(page.getByRole("radiogroup", { name: "Demo scenario" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Saga causal path" })).toBeVisible();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByText(/Event 2 of/)).toBeVisible();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test("mobile uses one causal stream without page-level horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installTraceRoutes(page);
  await page.goto("/?scenario=inventory-exhausted&view=ledger");
  const sizes = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(sizes.scroll).toBe(sizes.client);
  await expect(page.getByRole("table", { name: "Execution ledger" })).toBeVisible();
});
```

`installTraceRoutes(page)` imports `makeRunTrace`, intercepts `**/traces/<scenario-id>.json`, derives the requested ID from the URL, and fulfills a matching valid trace with `contentType: "application/json"`. This makes Task 9 a deterministic browser test without committing synthetic production traces; Task 12 replaces route interception with the real Python-generated traces.

- [ ] **Step 3: Run E2E to prove red**

Run: `cd web/flight-recorder && pnpm test:e2e`

Expected: FAIL on missing fixture/static trace wiring, layout overflow, or accessibility violations.

- [ ] **Step 4: Complete responsive and accessible behavior**

At 700–1099 px stack proof below the path. Below 700 px render the flight path as one ordered stream; transform ledger rows into labeled blocks without removing table roles; constrain `<pre>` to its own scroll container; never create page-level horizontal scroll. Add a skip link, visible `:focus-visible`, 44×44 px coarse-pointer controls, `aria-live="polite"` only for failure/policy/terminal transitions, and `@media (prefers-reduced-motion: reduce)` that removes transitions and leaves manual stepping functional. Add a Vitest axe smoke using `axe.run(container)` for the loading, loaded, invalid-trace, and no-filter-results states.

```css
@media (max-width: 1099px) {
  .recorder-grid { grid-template-columns: 15rem minmax(0, 1fr); }
  .proof-panel { grid-column: 1 / -1; }
}

@media (max-width: 699px) {
  .recorder-grid { display: block; }
  .flight-path { display: block; }
  pre { max-width: 100%; overflow-x: auto; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; }
}
```

- [ ] **Step 5: Verify and commit**

Run: `cd web/flight-recorder && pnpm test && pnpm test:e2e && pnpm typecheck && pnpm lint`

Expected: unit/component tests and both Playwright viewports PASS with zero axe violations and no overflow.

```bash
git add web/flight-recorder
git commit -m "test(console): enforce accessible responsive replay"
```

### Task 10: Package Assets and Serve Them on Loopback

**Files:**
- Modify: `src/agentic_saga/demo/__init__.py`
- Create: `src/agentic_saga/demo/assets.py`
- Create: `src/agentic_saga/demo/server.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/demo/test_assets.py`
- Test: `tests/unit/demo/test_server.py`

**Interfaces:**
- Consumes: Vite output under `src/agentic_saga/demo/static` and `Mapping[DemoScenarioId, RunTraceExport]`.
- Produces: `materialize_recorder_site(destination: Path, traces: Mapping[DemoScenarioId, RunTraceExport]) -> None` and `serve_recorder(directory: Path, *, port: int = 0, verbose: bool = False) -> RecorderServer`.

- [ ] **Step 1: Write failing asset materialization test**

```python
def test_materialize_recorder_site_writes_static_assets_and_redacted_traces(tmp_path: Path) -> None:
    trace = FakeTrace('{"schemaVersion":"1.0","scenarioId":"inventory-exhausted"}')
    materialize_recorder_site(tmp_path, {DemoScenarioId.INVENTORY_EXHAUSTED: trace})
    assert (tmp_path / "index.html").is_file()
    assert (tmp_path / "traces" / "inventory-exhausted.json").read_text() == trace.payload
    assert json.loads((tmp_path / "traces" / "index.json").read_text()) == {
        "schemaVersion": "1.0",
        "scenarios": ["inventory-exhausted"],
    }
```

- [ ] **Step 2: Run asset test to prove red**

Run: `uv run pytest tests/unit/demo/test_assets.py -v`

Expected: FAIL because the demo asset module is missing.

- [ ] **Step 3: Implement safe package-resource materialization**

Use `importlib.resources.files("agentic_saga.demo").joinpath("static")`, `resources.as_file`, and `shutil.copytree(..., dirs_exist_ok=True)`. Reject an empty trace mapping, call `model_dump_json(by_alias=True, indent=2)`, validate each mapping key equals the camelCase JSON `scenarioId`, write UTF-8 with mode `0o600`, and sort scenario IDs in `index.json`. Do not log payloads. Preserve every existing export in `src/agentic_saga/demo/__init__.py`; add only `RecorderServer`, `materialize_recorder_site`, and `serve_recorder`.

```python
def materialize_recorder_site(
    destination: Path,
    traces: Mapping[DemoScenarioId, RunTraceExport],
) -> None:
    if not traces:
        raise ValueError("At least one RunTrace is required.")
    with resources.as_file(resources.files("agentic_saga.demo").joinpath("static")) as static:
        shutil.copytree(static, destination, dirs_exist_ok=True)
    trace_dir = destination / "traces"
    trace_dir.mkdir(mode=0o700)
    for scenario_id, trace in sorted(traces.items()):
        payload = trace.model_dump_json(by_alias=True, indent=2)
        if json.loads(payload)["scenarioId"] != scenario_id.value:
            raise ValueError(f"Trace scenarioId does not match {scenario_id.value}.")
        target = trace_dir / f"{scenario_id.value}.json"
        target.write_text(payload, encoding="utf-8")
        target.chmod(0o600)
    write_trace_index(trace_dir, tuple(sorted(item.value for item in traces)))
```

- [ ] **Step 4: Write failing loopback server test**

```python
def test_server_binds_loopback_and_sets_static_security_headers(site: Path) -> None:
    with serve_recorder(site, port=0) as server:
        assert server.host == "127.0.0.1"
        response = urlopen(f"{server.url}/index.html")
        assert response.status == 200
        assert response.headers["Content-Security-Policy"] == (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; font-src 'self'; script-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        assert response.headers["X-Content-Type-Options"] == "nosniff"
```

- [ ] **Step 5: Implement bounded static serving**

Wrap `ThreadingHTTPServer(("127.0.0.1", port), handler)` in `RecorderServer`, with `url`, `host`, `port`, `wait()`, `shutdown()`, and context-manager methods. `serve_recorder` starts exactly one daemon serving thread before returning; `wait()` joins it until shutdown. Subclass `SimpleHTTPRequestHandler` with a fixed `directory`, add the exact CSP above, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, and suppress request logging unless `verbose=True`. Return 404 for files outside the materialized directory; add a test for `/../pyproject.toml`.

```python
def serve_recorder(directory: Path, *, port: int = 0, verbose: bool = False) -> RecorderServer:
    handler = partial(RecorderRequestHandler, directory=str(directory), verbose=verbose)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = Thread(target=httpd.serve_forever, name="saga-flight-recorder", daemon=True)
    thread.start()
    return RecorderServer(httpd=httpd, thread=thread)
```

- [ ] **Step 6: Register static package data and verify**

In hatchling configuration include `src/agentic_saga/demo/static/**/*`; keep `demo = []` as the zero-dependency optional extra because serving uses the standard library. Rebuild the frontend before the wheel.

Run:

```bash
cd web/flight-recorder && pnpm install --frozen-lockfile && pnpm build
cd ../.. && uv run pytest tests/unit/demo -v
uv build
unzip -l dist/*.whl | grep 'agentic_saga/demo/static/index.html'
```

Expected: Python tests PASS; the wheel contains `index.html`, JS, CSS, and local font assets.

- [ ] **Step 7: Commit packaged serving**

```bash
git add pyproject.toml src/agentic_saga/demo tests/unit/demo web/flight-recorder
git commit -m "feat(console): package and serve recorder assets"
```

### Task 11: Python Demo Command and Scenario Generation

**Files:**
- Modify: `src/agentic_saga/cli/__init__.py`
- Modify: `src/agentic_saga/cli/main.py`
- Modify: `src/agentic_saga/cli/demo.py`
- Modify: `pyproject.toml`
- Test: `tests/integration/cli/test_demo_command.py`

**Interfaces:**
- Consumes: `examples.ecommerce.demo.produce_run_trace`, `available_scenarios`, Task 10 materialization/server, and CLI options `--scenario`, `--live`, `--open`, and `--port`.
- Produces: extended `DemoArguments { scenario; live; open_browser; port }`, preserved `generate_demo_traces(provider, mode, selected)`, and `run_demo(args, *, provider=produce_run_trace, browser_open=webbrowser.open, wait_for_shutdown=RecorderServer.wait)`.

- [ ] **Step 1: Write failing offline command test**

```python
def test_demo_generates_all_scripted_traces_and_opens_selected_deep_link(tmp_path: Path) -> None:
    provider = AsyncMock(side_effect=trace_for_scenario)
    browser_open = Mock(return_value=True)
    with recorder_test_runtime(tmp_path):
        exit_code = run_demo(
            DemoArguments(
                scenario=DemoScenarioId.INVENTORY_EXHAUSTED,
                live=False,
                open_browser=True,
                port=0,
            ),
            provider=provider,
            browser_open=browser_open,
            wait_for_shutdown=lambda _server: None,
        )
    assert exit_code == 0
    assert {call.args[0] for call in provider.await_args_list} == set(DemoScenarioId)
    assert all(call.kwargs == {"mode": "scripted"} for call in provider.await_args_list)
    assert "scenario=inventory-exhausted" in browser_open.call_args.args[0]
    assert "view=story" in browser_open.call_args.args[0]
```

- [ ] **Step 2: Write failing live-mode isolation test**

```python
def test_live_demo_generates_only_selected_paid_trace() -> None:
    provider = AsyncMock(return_value=trace_for_scenario(DemoScenarioId.INVENTORY_EXHAUSTED))
    traces = asyncio.run(generate_demo_traces(provider, mode="live", selected=DemoScenarioId.INVENTORY_EXHAUSTED))
    assert list(traces) == [DemoScenarioId.INVENTORY_EXHAUSTED]
    provider.assert_awaited_once_with(DemoScenarioId.INVENTORY_EXHAUSTED, mode="live")
```

- [ ] **Step 3: Run command tests to prove red**

Run: `uv run pytest tests/integration/cli/test_demo_command.py -v`

Expected: FAIL because the existing ecommerce command has no `open_browser`/`port` arguments and does not materialize or serve the recorder.

- [ ] **Step 4: Implement CLI generation and lifecycle**

Preserve foundation `build_parser()`/`main()` dispatch and ecommerce `generate_demo_traces()` behavior. Extend the parser returned by `configure_demo_parser()` with `port=0` and `--open` using `action="store_true"`; no browser opens unless `--open` is supplied. Scripted generation still produces all seven traces concurrently; live generation still produces only the selected trace so one command cannot silently spend seven model runs. Materialize into `TemporaryDirectory(prefix="agentic-saga-demo-")`, print the loopback URL plus `Source: offline scripted evidence` or `Source: live OpenRouter evidence`, open `<url>/?scenario=<id>&view=story`, and serve until Ctrl-C. Convert missing credentials, trace-generation failures, and bind errors into one-line stderr messages with exit code 2; never print prompt, command, result, token, or payment contents.

```python
def run_demo(
    args: DemoArguments,
    *,
    provider: DemoTraceProvider = produce_run_trace,
    browser_open: Callable[[str], bool] = webbrowser.open,
    wait_for_shutdown: Callable[[RecorderServer], None] = RecorderServer.wait,
) -> int:
    traces = asyncio.run(generate_demo_traces(provider, mode="live" if args.live else "scripted", selected=args.scenario))
    with TemporaryDirectory(prefix="agentic-saga-demo-") as temp_dir:
        site = Path(temp_dir)
        materialize_recorder_site(site, traces)
        with serve_recorder(site, port=args.port) as server:
            url = f"{server.url}/?scenario={quote(args.scenario.value)}&view=story"
            print_demo_location(url, live=args.live)
            if args.open_browser and not browser_open(url):
                print("Browser did not open; use the URL above.", file=sys.stderr)
            try:
                wait_for_shutdown(server)
            except KeyboardInterrupt:
                return 0
    return 0
```

- [ ] **Step 5: Add CLI parser and failure tests**

Cover invalid scenario choices, missing `OPENROUTER_API_KEY` before live provider invocation, provider exception cleanup, browser-open failure continuing with a printed URL, and Ctrl-C returning zero. Verify `HUMAN_REQUIRED` traces are served without autonomous resume.

- [ ] **Step 6: Register and exercise the console script**

Verify the foundation's existing `[project.scripts]` entry remains `agentic-saga = "agentic_saga.cli.main:main"`; do not replace or duplicate it.

Run:

```bash
uv run pytest tests/integration/cli/test_demo_command.py -v
uv run agentic-saga demo --help
uv run agentic-saga demo --scenario inventory-exhausted --port 0
```

Expected: tests PASS; help lists exactly seven scenario choices; the smoke command prints a `127.0.0.1` URL and waits without making a network request. Stop it with Ctrl-C and expect exit code 0.

- [ ] **Step 7: Commit the CLI integration**

```bash
git add pyproject.toml src/agentic_saga/cli tests/integration/cli
git commit -m "feat(cli): launch Saga Flight Recorder"
```

### Task 12: Documentation, Contract Fixtures, and Full Release Gate

**Files:**
- Modify: `README.md`
- Create: `docs/flight-recorder.md`
- Modify: `examples/ecommerce/demo.py`
- Modify: `src/agentic_saga/evidence/run_trace.py`
- Create: `tests/contract/test_run_trace_web_contract.py`
- Modify: `web/flight-recorder/package.json`
- Modify: `web/flight-recorder/pnpm-lock.yaml`
- Modify: `web/flight-recorder/playwright.config.ts`
- Modify: `web/flight-recorder/e2e/flight-recorder.spec.ts`
- Create: `web/flight-recorder/playwright.packaged.config.ts`
- Create: `web/flight-recorder/e2e-packaged/flight-recorder.spec.ts`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/security-audit.yml`
- Modify: `tests/test_repository_contract.py`

**Interfaces:**
- Consumes: the real seven scripted traces, packaged application, CLI, and `RunTrace` version `1.0`.
- Produces: copy-pasteable five-minute onboarding, a cross-language contract, SHA-pinned shared frontend CI with Playwright, and locked Python/pnpm dependency audits.

- [ ] **Step 1: Write failing shared-CI policy tests**

Append to `tests/test_repository_contract.py`:

```python
def test_flight_recorder_uses_pinned_shared_frontend_gate() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert (
        "uses: hseshadr/ci/.github/workflows/frontend-gate.yml@"
        "8166345c9355dde54c12fa95d0457c4ea97d3e64 # ci-v3.3.0"
    ) in workflow
    assert "working-directory: web/flight-recorder" in workflow
    assert "package-json-file: web/flight-recorder/package.json" in workflow
    assert "cache-dependency-path: web/flight-recorder/pnpm-lock.yaml" in workflow
    assert 'node-version: "24"' in workflow
    assert 'install-args: "--frozen-lockfile"' in workflow
    assert "install-playwright: true" in workflow
    assert "playwright-working-directory: web/flight-recorder" in workflow
    assert 'gate-command: "pnpm gate"' in workflow


def test_security_audit_includes_locked_frontend_dependencies() -> None:
    workflow = (ROOT / ".github/workflows/security-audit.yml").read_text()
    assert "run-python-audit: true" in workflow
    assert "run-pnpm-audit: true" in workflow
    assert "frontend-working-directory: web/flight-recorder" in workflow
    assert 'node-version: "24"' in workflow


def test_flight_recorder_pins_pnpm_11_5_0() -> None:
    package = json.loads((ROOT / "web/flight-recorder/package.json").read_text())
    assert package["packageManager"] == "pnpm@11.5.0"
    assert (ROOT / "web/flight-recorder/pnpm-lock.yaml").is_file()
```

- [ ] **Step 2: Run the workflow-policy tests to prove red**

Run: `uv run pytest tests/test_repository_contract.py -q`

Expected: FAIL because the existing workflows do not yet call the frontend gate or pnpm audit.

- [ ] **Step 3: Extend the pinned shared workflows**

Append this job to `.github/workflows/ci.yml` without changing the foundation Python or secret-scan jobs:

```yaml
  frontend:
    name: Frontend gate
    uses: hseshadr/ci/.github/workflows/frontend-gate.yml@8166345c9355dde54c12fa95d0457c4ea97d3e64 # ci-v3.3.0
    with:
      working-directory: web/flight-recorder
      package-json-file: web/flight-recorder/package.json
      cache-dependency-path: web/flight-recorder/pnpm-lock.yaml
      node-version: "24"
      install-args: "--frozen-lockfile"
      gate-command: "pnpm gate"
      install-playwright: true
      playwright-browsers: chromium
      playwright-working-directory: web/flight-recorder
```

Extend the existing `dependencies` job's `with:` mapping in `.github/workflows/security-audit.yml`; preserve the SHA-pinned reusable workflow and Python audit:

```yaml
      run-python-audit: true
      run-pnpm-audit: true
      frontend-working-directory: web/flight-recorder
      node-version: "24"
```

Run: `uv run pytest tests/test_repository_contract.py -q`

Expected: PASS with the exact shared CI SHA, `# ci-v3.3.0` annotation, frontend directory, Node 24, frozen lockfile, Playwright installation, and both dependency audits.

- [ ] **Step 4: Write the failing Python-to-browser contract test**

```python
@pytest.mark.parametrize("scenario_id", list(DemoScenarioId))
def test_real_scripted_trace_matches_browser_contract(scenario_id: DemoScenarioId) -> None:
    trace = asyncio.run(produce_run_trace(scenario_id, mode="scripted"))
    payload = json.loads(trace.model_dump_json(by_alias=True))
    assert set(payload) == {
        "schemaVersion", "runId", "scenarioId", "scenarioName", "mode",
        "startedAt", "finishedAt", "outcome", "faultConfig", "initialState",
        "requiredInvariantIds", "proofs", "events",
    }
    assert payload["schemaVersion"] == "1.0"
    assert payload["scenarioId"] == scenario_id.value
    assert all("sagaSequence" in event and "saga_sequence" not in event for event in payload["events"])
    assert all(
        {"eventId", "ruleId", "description", "inputs", "result", "reason", "evaluatedSagaSequence"}
        == set(proof)
        for proof in payload["proofs"]
    )
    assert all("evaluated_saga_sequence" not in proof for proof in payload["proofs"])
```

Task 1's Zod tests independently require the identical exact key set and reject snake_case or extra fields. Together, the pure-Python alias test and strict TypeScript parser test keep each side runnable in its native shared CI job; neither job assumes the other toolchain is installed.

- [ ] **Step 5: Run the contract test to prove red**

Run: `uv run pytest tests/contract/test_run_trace_web_contract.py -v`

Expected: FAIL until every Python export uses the exact browser aliases, required top-level proofs, and Saga-sequence fields.

- [ ] **Step 6: Reconcile the boundary without weakening it**

In `src/agentic_saga/evidence/run_trace.py`, keep the kernel trace authoritative. In `examples/ecommerce/demo.py`, map it into `RunTraceExport` with Pydantic `ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")`; serialize only with `by_alias=True`. The wrapper fields and nested event fields must exactly match the camelCase list in the Locked Integration Contract. Do not add `.passthrough()`, `unknown`, unchecked casts, optional required evidence, or browser-generated policy/effect/proof fields. Re-run until all seven real traces pass strict parsing.

- [ ] **Step 7: Write the five-minute README path**

Put this sequence before architecture detail:

```bash
uv sync
uv run agentic-saga demo --scenario inventory-exhausted --open
uv run pytest tests/bdd -q
```

Walk through the recorded order creation, payment capture, inventory failure, alternate/restock observations, structured compensation proposal, deterministic policy approval, refund/cancellation receipts, and final invariant evidence. Then direct the reader to switch to Illegal proposal and Process crash. Explicitly label scripted versus live evidence, state that the UI shows structured rationale rather than private chain-of-thought, and state the SQLite/single-host and at-least-once limitations.

- [ ] **Step 8: Document the inspector and trust grammar**

In `docs/flight-recorder.md`, document Story, Ledger, Proof, replay keys, deep-link parameters, the six colors plus their textual/icon equivalents, compensation pairing, terminal proof rule, redaction ownership, offline trace generation, optional live cost boundary, local-only server, and how to inspect a trace JSON file. Include contributor setup with `corepack prepare pnpm@11.5.0 --activate`, `pnpm install --frozen-lockfile`, and `pnpm gate`. Include the exact one-line authority chain: `Agent proposes → Policy decides → Saga acts → Invariants prove`.

- [ ] **Step 9: Extend fixture and packaged real-browser assertions**

Keep `playwright.config.ts` on the Vite server with `installTraceRoutes` so the shared pnpm-only frontend gate has no undeclared Python/uv dependency. Add `"test:e2e:packaged": "playwright test --config playwright.packaged.config.ts"` to `package.json`. Configure `playwright.packaged.config.ts` with `testDir: "./e2e-packaged"`, `webServer.command: "cd ../.. && uv run agentic-saga demo --scenario inventory-exhausted --port 4173"`, `webServer.url: "http://127.0.0.1:4173"`, and the desktop/mobile projects from the standard config. The packaged tests make no route interceptions and prove:

- Inventory exhausted shows forward charge, reverse refund/cancel, and `COMPENSATED_VERIFIED` only after current proofs.
- Illegal proposal shows `Policy rejected`, has no second-charge effect row, and retains rejection evidence.
- Process crash shows a process boundary, the reused payment idempotency key, and one payment receipt.
- Refund retry shows the failed compensation attempt and one confirmed refund effect.
- Recovery exhausted ends in quiescent `HUMAN_REQUIRED` with no later mutating event.
- Alternate inventory ends `SUCCEEDED_VERIFIED` without compensation.

- [ ] **Step 10: Run the complete release gate**

Run:

```bash
uv run ruff check .
uv run mypy src tests
uv run pytest
cd web/flight-recorder
pnpm install --frozen-lockfile
pnpm test
pnpm typecheck
pnpm lint
pnpm build
pnpm test:e2e
pnpm test:e2e:packaged
cd ../..
uv build
unzip -l dist/*.whl | grep 'agentic_saga/demo/static/index.html'
```

Expected: all Python tests, web unit/component tests, strict types, lint, production build, both Playwright viewports, and wheel asset inspection PASS; ordinary gates perform no external network or model call after dependencies are installed.

- [ ] **Step 11: Commit documentation and release proof**

```bash
git add README.md docs/flight-recorder.md tests/contract tests/test_repository_contract.py web/flight-recorder .github/workflows/ci.yml .github/workflows/security-audit.yml
git commit -m "docs: add Flight Recorder quickstart and proof"
```

## Final Manual Review

- [ ] Start `uv run agentic-saga demo --scenario inventory-exhausted --open`, complete the five-minute story from a clean checkout, and record elapsed time.
- [ ] Verify the Network panel shows only `127.0.0.1` assets/traces in scripted mode and no external font, telemetry, or model requests.
- [ ] Inspect desktop, tablet at 900 px, and mobile at 390 px; verify no content clipping, page-level horizontal scrolling, or color-only meaning.
- [ ] Navigate every control by keyboard, close event details with Escape, and confirm focus returns to the selected event.
- [ ] Enable reduced motion and confirm Play does not auto-advance while Previous/Next remain usable.
- [ ] Confirm the visible source label, structured-rationale label, policy boundary, effect receipts, compensation links, invariant freshness, and terminal badge all agree with the ledger.
- [ ] Confirm `git status --short` contains only intentional implementation artifacts and no trace containing credentials or sensitive payment data.
